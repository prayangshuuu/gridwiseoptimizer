"""Orchestrates primary (Gemini) and fallback (OpenRouter) LLM interpretation."""
from __future__ import annotations

import collections
import copy
import json
import logging
import os
import threading
from dataclasses import dataclass

from .llm_errors import LLMError
from .llm_providers import (
    LLMProvider,
    build_gemini_provider,
    build_openrouter_provider,
)

logger = logging.getLogger(__name__)

_CACHE: "collections.OrderedDict[str, list]" = collections.OrderedDict()
_CACHE_LOCK = threading.Lock()
_LAST_PROVIDER_USED: str | None = None
_LAST_PROVIDER_LOCK = threading.Lock()


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else val


def _timeout_seconds() -> float:
    for name in ("LLM_TIMEOUT_SECONDS", "LLM_TIMEOUT"):
        raw = _env(name)
        if raw:
            try:
                return max(0.1, float(raw))
            except ValueError:
                pass
    return 10.0


def _cache_get(key: str):
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return copy.deepcopy(_CACHE[key])
    return None


def _cache_put(key: str, value, maxsize: int) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = copy.deepcopy(value)
        _CACHE.move_to_end(key)
        while len(_CACHE) > maxsize:
            _CACHE.popitem(last=False)


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def get_last_provider_used() -> str | None:
    with _LAST_PROVIDER_LOCK:
        return _LAST_PROVIDER_USED


def _set_last_provider_used(name: str | None) -> None:
    global _LAST_PROVIDER_USED
    with _LAST_PROVIDER_LOCK:
        _LAST_PROVIDER_USED = name


@dataclass(frozen=True)
class InterpretationResult:
    results: list[dict]
    provider_used: str


class DirectiveInterpreter:
    """Try Gemini first; on provider failure, try OpenRouter once."""

    def __init__(
        self,
        primary: LLMProvider | None = None,
        fallback: LLMProvider | None = None,
        *,
        cache_size: int = 256,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._cache_size = max(1, cache_size)

    @classmethod
    def from_env(cls) -> DirectiveInterpreter:
        primary_name = (_env("LLM_PRIMARY_PROVIDER") or _env("LLM_PROVIDER") or "gemini").lower()
        fallback_name = (_env("LLM_FALLBACK_PROVIDER") or "openrouter").lower()

        primary = build_gemini_provider() if primary_name in ("gemini", "google") else None
        fallback = (
            build_openrouter_provider()
            if fallback_name in ("openrouter", "open_router")
            else None
        )
        try:
            cache_size = int(float(_env("LLM_CACHE_SIZE", "256") or 256))
        except (TypeError, ValueError):
            cache_size = 256
        return cls(primary=primary, fallback=fallback, cache_size=cache_size)

    def interpret(self, operator_notes: list[str]) -> InterpretationResult:
        notes = list(operator_notes)
        cache_key = json.dumps(notes, ensure_ascii=False, sort_keys=False)
        cached = _cache_get(cache_key)
        if cached is not None:
            provider = get_last_provider_used() or "cache"
            return InterpretationResult(cached, provider)

        timeout = _timeout_seconds()
        last_error: LLMError | None = None

        if self._primary is not None:
            try:
                results = self._primary.interpret(notes, timeout)
                _cache_put(cache_key, results, self._cache_size)
                _set_last_provider_used(self._primary.name)
                logger.info("directive_interpretation provider_used=%s", self._primary.name)
                return InterpretationResult(results, self._primary.name)
            except LLMError as exc:
                last_error = exc
                logger.warning(
                    "Primary LLM provider (%s) failed: %s",
                    self._primary.name,
                    type(exc).__name__,
                )

        if self._fallback is not None:
            try:
                results = self._fallback.interpret(notes, timeout)
                _cache_put(cache_key, results, self._cache_size)
                _set_last_provider_used(self._fallback.name)
                logger.info("directive_interpretation provider_used=%s", self._fallback.name)
                return InterpretationResult(results, self._fallback.name)
            except LLMError as exc:
                last_error = exc
                logger.warning(
                    "Fallback LLM provider (%s) failed: %s",
                    self._fallback.name,
                    type(exc).__name__,
                )

        raise last_error or LLMError("All LLM providers failed.")
