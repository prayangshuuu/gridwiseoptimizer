"""Shared base URL for verification scripts: local first, public Heroku fallback."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

LOCAL_BASE_URL = "http://localhost:8000"
PUBLIC_BASE_URL = "https://gridwiseoptimizer-5541fa80b3e4.herokuapp.com"


def health_ok(base_url: str, timeout: float = 2.0) -> bool:
    url = base_url.rstrip("/") + "/health"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read().decode())
            return isinstance(body, dict) and body.get("status") == "ok"
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
        return False


def resolve_base_url(explicit: str | None = None) -> str:
    """Use ``explicit`` when provided; else localhost if healthy, else Heroku."""
    if explicit:
        return explicit.rstrip("/")
    if health_ok(LOCAL_BASE_URL):
        return LOCAL_BASE_URL
    return PUBLIC_BASE_URL
