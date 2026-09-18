"""Single environment contract for OpenRouter LLM access.

All runtime code and scripts read API configuration from here — one API key
variable (``OPENROUTER_API_KEY``), one optional base URL (``LLM_BASE_URL``),
and one optional model override (``LLM_MODEL``).
"""
from __future__ import annotations

import os
from pathlib import Path

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOTENV_LOADED = False


def load_dotenv() -> None:
    """Load repo ``.env`` into ``os.environ`` (does not overwrite existing)."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    env_file = _REPO_ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    _DOTENV_LOADED = True


def getenv(name: str, default: str | None = None) -> str | None:
    load_dotenv()
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else val


def openrouter_api_keys() -> list[str]:
    """Comma-separated keys from ``OPENROUTER_API_KEY`` only."""
    raw = getenv("OPENROUTER_API_KEY")
    if not raw:
        return []
    seen: set[str] = set()
    keys: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if part and part not in seen:
            seen.add(part)
            keys.append(part)
    return keys


def openrouter_base_url() -> str:
    return getenv("LLM_BASE_URL") or OPENROUTER_BASE_URL


def openrouter_model() -> str:
    return getenv("LLM_MODEL") or DEFAULT_MODEL


def llm_timeout_seconds() -> float | None:
    """Per-request read timeout, or ``None`` when disabled (``LLM_TIMEOUT=0``)."""
    for name in ("LLM_TIMEOUT_SECONDS", "LLM_TIMEOUT"):
        raw = getenv(name)
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
