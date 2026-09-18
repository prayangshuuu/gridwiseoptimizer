"""Orchestrates LLM interpretation. OpenRouter is the one and only provider."""
from __future__ import annotations

import collections
import copy
import json
import logging
import os
import threading
from dataclasses import dataclass

from .llm_errors import LLMError
from .llm_providers import LLMProvider, build_openrouter_provider

logger = logging.getLogger(__name__)

_CACHE: "collections.OrderedDict[str, list]" = collections.OrderedDict()
_CACHE_LOCK = threading.Lock()
_LAST_PROVIDER_USED: str | None = None
_LAST_PROVIDER_LOCK = threading.Lock()


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else val


def _timeout_seconds() -> float | None:
    """Per-request timeout in seconds, or None for "never expire".

    ``LLM_TIMEOUT``/``LLM_TIMEOUT_SECONDS`` set to 0, blank, ``none`` or ``off``
    disables the client-side clock so a slow free-tier model can run to
    completion instead of raising APITimeoutError and dropping to the
    deterministic fallback."""
    for name in ("LLM_TIMEOUT_SECONDS", "LLM_TIMEOUT"):
        raw = _env(name)
        if raw is None:
            continue
        raw = raw.lower()
        if raw in ("", "0", "none", "off", "inf"):
            return None
        try:
            return max(0.1, float(raw))
        except ValueError:
            pass
    return None


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
    """Interpret operator notes via OpenRouter — the one and only provider.

    A ``fallback`` slot remains only so tests can inject a stub; the production
    path built by :meth:`from_env` never wires a second provider."""

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
        # OpenRouter is the sole provider (project requirement). Provider-
        # selection env vars are intentionally ignored so nothing else is used.
        primary = build_openrouter_provider()
        try:
            cache_size = int(float(_env("LLM_CACHE_SIZE", "256") or 256))
        except (TypeError, ValueError):
            cache_size = 256
        return cls(primary=primary, fallback=None, cache_size=cache_size)

    def interpret(
        self,
        operator_notes: list[str],
        battery: object | None = None,
    ) -> InterpretationResult:
        notes = list(operator_notes)
        cap = None
        if battery is not None:
            from .llm_contract import _battery_capacity_kwh

            cap = _battery_capacity_kwh(battery)
        cache_key = json.dumps(
            {"notes": notes, "capacity_kwh": cap},
            ensure_ascii=False,
            sort_keys=True,
        )
        cached = _cache_get(cache_key)
        if cached is not None:
            provider = get_last_provider_used() or "cache"
            return InterpretationResult(cached, provider)

        timeout = _timeout_seconds()
        last_error: LLMError | None = None

        if self._primary is not None:
            try:
                results = self._primary.interpret(notes, timeout, battery=battery)
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
                results = self._fallback.interpret(notes, timeout, battery=battery)
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
