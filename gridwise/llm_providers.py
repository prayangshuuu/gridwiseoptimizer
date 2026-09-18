"""OpenAI-compatible LLM providers for operator-note interpretation."""
from __future__ import annotations

import logging
import os
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


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else val


def _llm_error(msg: str, *, transient: bool = False, auth_failure: bool = False) -> LLMError:
    err = LLMError(msg)
    err.transient = transient
    err.auth_failure = auth_failure
    return err


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
    def interpret(self, operator_notes: list[str], timeout: float) -> list[dict]:
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

    def _make_client(self, api_key: str, timeout: float):
        if self._client_factory is not None:
            return self._client_factory(api_key, self.base_url, timeout)
        from openai import OpenAI

        kwargs: dict = {"api_key": api_key, "timeout": timeout, "max_retries": 0}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return OpenAI(**kwargs)

    def interpret(self, operator_notes: list[str], timeout: float) -> list[dict]:
        if not self.api_keys:
            raise _llm_error(f"No API key configured for provider {self.name}.")
        if not self.model:
            raise _llm_error(f"No model configured for provider {self.name}.")

        try:
            import openai as openai_pkg
        except Exception as exc:  # pragma: no cover
            raise _llm_error(f"OpenAI SDK unavailable: {type(exc).__name__}") from exc

        messages = build_chat_messages(operator_notes)
        num_notes = len(operator_notes)
        last_error: LLMError | None = None

        for key_idx, api_key in enumerate(self.api_keys):
            client = self._make_client(api_key, timeout)
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    temperature=0,
                    response_format={"type": "json_object"},
                    messages=messages,
                    timeout=timeout,
                )
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
                if (
                    key_idx + 1 < len(self.api_keys)
                    and _should_rotate_api_key(exc, openai_pkg)
                ):
                    logger.warning(
                        "Provider %s key rejected or rate-limited; trying next key.",
                        self.name,
                    )
                    continue
                raise last_error from exc

            try:
                content = response.choices[0].message.content
            except (AttributeError, IndexError) as exc:
                raise _llm_error("LLM response had no message content.") from exc

            raw = parse_provider_content(content or "", num_notes)
            return validate_interpretation_schema(num_notes, raw)

        raise last_error or _llm_error(f"Provider {self.name} failed with no error captured.")


class GeminiProvider(OpenAICompatibleProvider):
    """Primary Google Gemini provider."""


class OpenRouterProvider(OpenAICompatibleProvider):
    """Backup OpenRouter provider (DeepSeek via OpenRouter)."""


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
    provider_model = (
        model
        or _env("LLM_FALLBACK_MODEL")
        or "~deepseek/deepseek-flash-latest"
    )
    base = _env("LLM_FALLBACK_BASE_URL") or _PROVIDER_BASE_URLS["openrouter"]
    return OpenRouterProvider(
        name="openrouter",
        model=provider_model,
        api_keys=openrouter_api_key_candidates(),
        base_url=base,
        client_factory=client_factory,
    )
