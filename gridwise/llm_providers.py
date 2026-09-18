"""OpenAI-compatible LLM providers for operator-note interpretation."""
from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Callable, Sequence

from .llm_contract import (
    build_chat_messages,
    parse_provider_content,
    validate_interpretation_schema,
)
from .llm_errors import LLMError

logger = logging.getLogger(__name__)

ProviderClientFactory = Callable[[str, str | None, float], object]

_PROVIDER_BASE_URLS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "openrouter": "https://openrouter.ai/api/v1",
}

# Project requirement: OpenRouter + this model only (env overrides are ignored).
OPENROUTER_MODEL = "deepseek/deepseek-v4.1-flash"


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else val


def _llm_error(msg: str, *, transient: bool = False, auth_failure: bool = False) -> LLMError:
    err = LLMError(msg)
    err.transient = transient
    err.auth_failure = auth_failure
    return err


def _num_env(name: str, default: float) -> float:
    """Read a numeric env var, tolerating blanks and junk."""
    raw = _env(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _retry_config() -> tuple[int, float, float | None]:
    """(max_retries, base_backoff_seconds, deadline_seconds|None).

    ``LLM_DEADLINE`` of 0/blank/none means "no overall deadline" — combined
    with a disabled ``LLM_TIMEOUT`` this lets a slow free-tier model run to
    completion instead of expiring into the deterministic fallback."""
    max_retries = max(0, int(_num_env("LLM_MAX_RETRIES", 3)))
    backoff = max(0.0, _num_env("LLM_RETRY_BACKOFF", 0.5))
    raw_deadline = (_env("LLM_DEADLINE") or "").lower()
    if raw_deadline in ("", "0", "none", "off", "inf"):
        deadline: float | None = None
    else:
        try:
            deadline = float(raw_deadline)
            if deadline <= 0:
                deadline = None
        except ValueError:
            deadline = None
    return max_retries, backoff, deadline


def _resolve_timeout(timeout: float | None):
    """Turn a numeric timeout into a value the OpenAI SDK understands.

    ``None`` (or a non-positive number) means "never expire" on generation: we
    disable the read/write/pool clocks so a slow free-tier model can stream to
    completion instead of raising APITimeoutError. A finite ``connect`` clock is
    kept so an unreachable host still fails fast (and can retry / rotate keys)
    rather than hanging a worker forever."""
    if timeout is not None and timeout > 0:
        return timeout
    try:
        from openai import Timeout

        return Timeout(connect=_num_env("LLM_CONNECT_TIMEOUT", 30.0),
                       read=None, write=None, pool=None)
    except Exception:  # pragma: no cover - Timeout ships with openai
        return None


def _reasoning_body() -> dict | None:
    """OpenRouter ``reasoning`` control, the main speed lever for free models.

    Free endpoints route to reasoning models that emit slow chain-of-thought by
    default. ``LLM_REASONING``:
      off (default) -> {"enabled": false}   fastest; no thinking tokens
      low/medium/high -> {"effort": <level>}
      blank/none/default -> omit (use the model's own default)"""
    level = (_env("LLM_REASONING") or "off").lower()
    if level in ("off", "false", "0", "none-thinking", "disabled"):
        return {"reasoning": {"enabled": False}}
    if level in ("low", "medium", "high"):
        return {"reasoning": {"effort": level}}
    return None  # blank / "default" -> leave the model default in place


def _deadline_ok(started: float, deadline: float | None, next_delay: float) -> bool:
    """True when another retry (after ``next_delay``) still fits the deadline.

    A ``None`` deadline means "no budget" — retries are limited only by
    ``LLM_MAX_RETRIES``."""
    if deadline is None:
        return True
    return (time.monotonic() - started) + next_delay < deadline


def _is_transient(exc, openai_pkg) -> bool:
    if isinstance(exc, (openai_pkg.APITimeoutError, openai_pkg.APIConnectionError)):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status == 429 or status >= 500)


def _is_auth_failure(exc, openai_pkg) -> bool:
    if isinstance(exc, openai_pkg.AuthenticationError):
        return True
    status = getattr(exc, "status_code", None)
    if status == 403:
        return True
    if status == 400:
        body = str(getattr(exc, "body", "") or exc).lower()
        return "api key" in body or (
            "invalid_argument" in body and "key" in body
        )
    return False


def _should_rotate_api_key(exc, openai_pkg) -> bool:
    if _is_auth_failure(exc, openai_pkg):
        return True
    if isinstance(exc, openai_pkg.RateLimitError):
        return True
    status = getattr(exc, "status_code", None)
    return status == 429


def gemini_api_key_candidates() -> list[str]:
    seen: set[str] = set()
    keys: list[str] = []
    for name in ("LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        raw = _env(name)
        if not raw:
            continue
        for part in raw.split(","):
            part = part.strip()
            if part and part not in seen:
                seen.add(part)
                keys.append(part)
    studio = [k for k in keys if k.startswith("AIzaSy")]
    return studio if studio else keys


def openrouter_api_key_candidates() -> list[str]:
    seen: set[str] = set()
    keys: list[str] = []
    for name in ("OPENROUTER_API_KEY", "LLM_FALLBACK_API_KEY"):
        raw = _env(name)
        if not raw:
            continue
        for part in raw.split(","):
            part = part.strip()
            if part and part not in seen:
                seen.add(part)
                keys.append(part)
    return keys


class LLMProvider(ABC):
    """Provider adapter that returns schema-validated directive payloads."""

    name: str

    @abstractmethod
    def interpret(
        self,
        operator_notes: list[str],
        timeout: float | None,
        battery: object | None = None,
    ) -> list[dict]:
        """One controlled interpretation attempt (may rotate keys internally)."""


class OpenAICompatibleProvider(LLMProvider):
    """Gemini / OpenRouter via the OpenAI chat completions API."""

    def __init__(
        self,
        *,
        name: str,
        model: str,
        api_keys: Sequence[str],
        base_url: str | None,
        client_factory: ProviderClientFactory | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.api_keys = list(api_keys)
        self.base_url = base_url
        self._client_factory = client_factory

    def _make_client(self, api_key: str, timeout):
        if self._client_factory is not None:
            return self._client_factory(api_key, self.base_url, timeout)
        from openai import OpenAI

        kwargs: dict = {"api_key": api_key, "timeout": timeout, "max_retries": 0}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return OpenAI(**kwargs)

    def interpret(
        self,
        operator_notes: list[str],
        timeout: float | None,
        battery: object | None = None,
    ) -> list[dict]:
        if not self.api_keys:
            raise _llm_error(f"No API key configured for provider {self.name}.")
        if not self.model:
            raise _llm_error(f"No model configured for provider {self.name}.")

        try:
            import openai as openai_pkg
        except Exception as exc:  # pragma: no cover
            raise _llm_error(f"OpenAI SDK unavailable: {type(exc).__name__}") from exc

        messages = build_chat_messages(operator_notes, battery=battery)
        num_notes = len(operator_notes)
        last_error: LLMError | None = None

        max_retries, backoff, deadline = _retry_config()
        resolved_timeout = _resolve_timeout(timeout)
        # Cap output to keep generation fast. Explicit LLM_MAX_TOKENS wins;
        # otherwise scale with note count (~150 tokens/directive) so large
        # batches don't truncate into a parse-retry loop.
        override = _env("LLM_MAX_TOKENS")
        if override:
            max_tokens = max(64, int(_num_env("LLM_MAX_TOKENS", 700)))
        else:
            max_tokens = 256 + 150 * num_notes
        reasoning_body = _reasoning_body()
        started = time.monotonic()

        for key_idx, api_key in enumerate(self.api_keys):
            attempt = 0
            while True:
                client = self._make_client(api_key, resolved_timeout)
                try:
                    create_kwargs: dict = dict(
                        model=self.model,
                        temperature=0,
                        max_tokens=max_tokens,
                        response_format={"type": "json_object"},
                        messages=messages,
                        timeout=resolved_timeout,
                    )
                    if reasoning_body is not None:
                        create_kwargs["extra_body"] = reasoning_body
                    response = client.chat.completions.create(**create_kwargs)
                except Exception as exc:
                    transient = _is_transient(exc, openai_pkg)
                    auth_failure = _is_auth_failure(exc, openai_pkg)
                    status = getattr(exc, "status_code", None)
                    detail = f"{type(exc).__name__}" + (f" ({status})" if status else "")
                    last_error = _llm_error(
                        f"LLM call failed: {detail}",
                        transient=transient,
                        auth_failure=auth_failure,
                    )
                    # Auth / rate-limit: move to the next key rather than retry.
                    if _should_rotate_api_key(exc, openai_pkg):
                        if key_idx + 1 < len(self.api_keys):
                            logger.warning(
                                "Provider %s key rejected or rate-limited; trying next key.",
                                self.name,
                            )
                            break  # advance the outer for-loop to the next key
                        raise last_error from exc
                    # Timeouts / connection drops / 5xx: back off and retry the
                    # same key until we run out of attempts or the deadline.
                    delay = backoff * (2 ** attempt)
                    if (
                        transient
                        and attempt < max_retries
                        and _deadline_ok(started, deadline, delay)
                    ):
                        logger.warning(
                            "Provider %s transient error (%s); retry %d/%d in %.1fs.",
                            self.name, detail, attempt + 1, max_retries, delay,
                        )
                        if delay > 0:
                            time.sleep(delay)
                        attempt += 1
                        continue
                    raise last_error from exc

                # A 200 with malformed / truncated / wrong-shaped output is
                # common on free-tier models. Treat it like a transient error
                # and retry the same key rather than failing straight through.
                try:
                    content = response.choices[0].message.content
                    raw = parse_provider_content(content or "", num_notes)
                    return validate_interpretation_schema(num_notes, raw)
                except (AttributeError, IndexError, LLMError) as exc:
                    last_error = (
                        exc if isinstance(exc, LLMError)
                        else _llm_error("LLM response had no message content.")
                    )
                    delay = backoff * (2 ** attempt)
                    if attempt < max_retries and _deadline_ok(started, deadline, delay):
                        logger.warning(
                            "Provider %s bad response (%s); retry %d/%d in %.1fs.",
                            self.name, type(exc).__name__, attempt + 1, max_retries, delay,
                        )
                        if delay > 0:
                            time.sleep(delay)
                        attempt += 1
                        continue
                    raise last_error from exc

        raise last_error or _llm_error(f"Provider {self.name} failed with no error captured.")


class GeminiProvider(OpenAICompatibleProvider):
    """Primary Google Gemini provider."""


class OpenRouterProvider(OpenAICompatibleProvider):
    """The sole LLM provider: DeepSeek via OpenRouter."""


def build_gemini_provider(
    *,
    model: str | None = None,
    client_factory: ProviderClientFactory | None = None,
) -> GeminiProvider:
    provider_model = model or _env("LLM_PRIMARY_MODEL") or _env("LLM_MODEL")
    base = _env("LLM_BASE_URL") or _PROVIDER_BASE_URLS["gemini"]
    return GeminiProvider(
        name="gemini",
        model=provider_model or "",
        api_keys=gemini_api_key_candidates(),
        base_url=base,
        client_factory=client_factory,
    )


def build_openrouter_provider(
    *,
    model: str | None = None,
    client_factory: ProviderClientFactory | None = None,
) -> OpenRouterProvider:
    # Tests may inject ``model``; production always uses OPENROUTER_MODEL.
    provider_model = model or OPENROUTER_MODEL
    base = _env("LLM_FALLBACK_BASE_URL") or _PROVIDER_BASE_URLS["openrouter"]
    return OpenRouterProvider(
        name="openrouter",
        model=provider_model,
        api_keys=openrouter_api_key_candidates(),
        base_url=base,
        client_factory=client_factory,
    )
