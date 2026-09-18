"""Orchestrates LLM interpretation via OpenRouter (single provider)."""
from __future__ import annotations

import collections
import copy
import json
import logging
import threading
from dataclasses import dataclass

from .llm_env import getenv, llm_timeout_seconds, load_dotenv
from .llm_errors import LLMError
from .llm_providers import LLMProvider, build_openrouter_provider

logger = logging.getLogger(__name__)

_CACHE: "collections.OrderedDict[str, list]" = collections.OrderedDict()
_CACHE_LOCK = threading.Lock()
_LAST_PROVIDER_USED: str | None = None
_LAST_PROVIDER_LOCK = threading.Lock()


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
    """Interpret operator notes via OpenRouter."""

    def __init__(
        self,
        provider: LLMProvider | None = None,
        *,
        cache_size: int = 256,
    ) -> None:
        self._provider = provider
        self._cache_size = max(1, cache_size)

    @classmethod
    def from_env(cls) -> DirectiveInterpreter:
        load_dotenv()
        try:
            cache_size = int(float(getenv("LLM_CACHE_SIZE", "256") or 256))
        except (TypeError, ValueError):
            cache_size = 256
        return cls(provider=build_openrouter_provider(), cache_size=cache_size)

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

        if self._provider is None:
            raise LLMError("No LLM provider configured.")

        timeout = llm_timeout_seconds()
        results = self._provider.interpret(notes, timeout, battery=battery)
        _cache_put(cache_key, results, self._cache_size)
        _set_last_provider_used(self._provider.name)
        logger.info("directive_interpretation provider_used=%s", self._provider.name)
        return InterpretationResult(results, self._provider.name)
