"""Mandatory LLM interpretation layer.

`interpret_notes` turns free-text operator notes into structured directives by
calling an LLM in JSON mode. Provider, model, and API key come from environment
variables only. The call uses a short per-request timeout so it fits inside the
view's 30s request budget, and any transport/provider failure surfaces as a
typed :class:`LLMError`.

The parsed JSON is returned as-is. It is deliberately NOT trusted here: a later
layer (the optimizer) is responsible for validating and clamping every field
against the actual scenario. Do not act on this output directly.
"""
from __future__ import annotations

import collections
import copy
import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Directive contract shared with the prompt below.
#
# There are exactly six directive types the model may emit when a note applies.
# A note that carries no actionable instruction is a `no_op` (applies=False).
#
# Whole-hour rule: every hour reference is an integer in 0..23. A range
#   "A to B" / "A-B" / "between A and B" covers hour A up to but NOT including
#   hour B (endpoint-exclusive; B is the stop time). This same convention is
#   mirrored in gridwise/fallback.py and tests/paraphrase_cases.json.
# Factor rule (solar_reduction): `factor` is the FRACTION OF SOLAR REMAINING
#   after the cut (0.7 == "cut 30%", 0.5 == "by half", 0 == "off").
# ---------------------------------------------------------------------------
DIRECTIVE_TYPES = (
    "solar_reduction",          # scale solar_kwh in the given hours by `factor` (0..1)
    "minimum_battery_reserve",  # raise the battery energy floor to `reserve` kWh
    "no_charge",                # forbid battery charging in the given hours
    "no_discharge",             # forbid battery discharging in the given hours
    "max_grid",                 # cap grid import to `max_grid_kwh` in the given hours
)


class LLMError(Exception):
    """Raised when the LLM call fails (timeout, provider error, bad config,
    or unparseable response). The caller decides how to surface it.

    ``transient`` marks errors worth retrying (timeout, connection, 429, 5xx).
    """

    transient: bool = False


# --------------------------------------------------------------------------- #
# In-memory interpretation cache, keyed by the exact operator_notes list.      #
# Cutting repeat interpretations is the main p95 lever. Bounded LRU, guarded   #
# by a lock for thread safety under the ASGI server.                           #
# --------------------------------------------------------------------------- #
_CACHE: "collections.OrderedDict[str, list]" = collections.OrderedDict()
_CACHE_LOCK = threading.Lock()


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
    """Drop all cached interpretations (used by tests)."""
    with _CACHE_LOCK:
        _CACHE.clear()


# Default OpenAI-compatible base URLs per provider. Overridden by LLM_BASE_URL.
_PROVIDER_BASE_URLS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai/",
}


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else val


def _resolve_api_key(provider: str) -> str:
    """API key from env only. Prefer a generic LLM_API_KEY, then a
    provider-specific <PROVIDER>_API_KEY (e.g. OPENAI_API_KEY)."""
    key = _env("LLM_API_KEY") or _env(f"{provider.upper()}_API_KEY")
    if not key:
        raise LLMError(
            "No LLM API key configured (set LLM_API_KEY or "
            f"{provider.upper()}_API_KEY)."
        )
    return key


SYSTEM_PROMPT = f"""\
You are the interpretation layer of a grid energy optimizer. You are given a
list of short free-text operator notes. For EACH note, decide whether it
contains an actionable instruction that changes the optimization inputs.

Return STRICT JSON, an object of the form:
{{"results": [ <one object per input note, in the same order> ]}}

Each result object has exactly these fields:
  - "note_index": integer, the 0-based position of the note in the input list.
  - "applies": boolean, true only if the note is an actionable directive.
  - "directive_type": one of {list(DIRECTIVE_TYPES)} when applies is true,
    otherwise the string "no_op".
  - "structured_adjustment": object describing the change (see rules) when
    applies is true, or null when applies is false.
  - "explanation": a one-sentence justification grounded in the note text.

Rules:
  - Whole-hour rule: "hours" is an array of integer hour indices 0..23, unique
    and ascending. Each index h is the slot [h:00, h+1:00).
    INCLUSIVE/EXCLUSIVE: a range "A to B" / "A-B" / "between A and B" / "A
    until/through B" covers hour A up to but NOT including hour B (B is the end
    time). Examples: "noon to 4pm" -> [12,13,14,15]; "5pm to 9pm" ->
    [17,18,19,20]; "09:00 to 13:00" -> [9,10,11,12]. A single time such as "at
    3pm" or "the 15:00 hour" -> [15]. noon=12, midnight=0.
  - Structured_adjustment shape per directive_type:
      solar_reduction:         {{"hours": [...], "factor": <0..1>}}
        factor = FRACTION OF SOLAR REMAINING after the cut. "cut/reduce by 30%"
        or "by 0.3" -> 0.7; "by half" -> 0.5; "to 70%" -> 0.7; "no solar"/"off"
        -> 0. Never emit a factor above 1.
      minimum_battery_reserve: {{"hours": [...], "reserve": <kWh >= 0>}}
      no_charge:               {{"hours": [...]}}
      no_discharge:            {{"hours": [...]}}
      max_grid:                {{"hours": [...], "max_grid_kwh": <kWh >= 0>}}
  - no_op vs real: set no_op ONLY when the note carries no actionable energy
    instruction (greetings, thanks, status updates, FYIs, reminders). If the
    note names a concrete change to solar, battery charging/discharging, a
    reserve floor, or a grid-import cap, it is NOT a no_op.
  - Only use a directive_type from the allowed list. Output JSON only. Do not
    invent hours or numbers that the note does not imply.
"""

# Generic few-shot examples (NOT copied from any public/judge cases): one
# solar_reduction (factor=remaining), one window directive, one no_op. They also
# demonstrate the endpoint-exclusive hour rule ("noon to 4pm" -> [12,13,14,15];
# "6pm to 9pm" -> [18,19,20]).
FEWSHOT_USER = json.dumps({
    "operator_notes": [
        "Heavy cloud cover this afternoon, cut expected solar by 30% from noon to 4pm.",
        "Do not charge the battery during the evening peak, 6pm to 9pm.",
        "Thanks everyone for the great work covering the night shift!",
    ]
})

FEWSHOT_ASSISTANT = json.dumps({
    "results": [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [12, 13, 14, 15], "factor": 0.7},
            "explanation": "Cloud cover leaves 70% of solar over the noon-to-4pm window.",
        },
        {
            "note_index": 1,
            "applies": True,
            "directive_type": "no_charge",
            "structured_adjustment": {"hours": [18, 19, 20]},
            "explanation": "Battery charging is disallowed across the 6-to-9pm peak.",
        },
        {
            "note_index": 2,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": "The note is a thank-you message with no actionable directive.",
        },
    ]
})


def _is_transient(exc, openai_pkg) -> bool:
    """Whether a raised SDK exception is worth retrying."""
    if isinstance(exc, (openai_pkg.APITimeoutError, openai_pkg.APIConnectionError)):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status == 429 or status >= 500)


def _llm_error(msg: str, transient: bool = False) -> LLMError:
    """Build an LLMError tagged with whether it is worth retrying. Callers use
    `raise _llm_error(...) from exc` to preserve the underlying cause."""
    err = LLMError(msg)
    err.transient = transient
    return err


def interpret_notes(operator_notes):
    """Interpret operator notes into raw structured directives via an LLM.

    Reliability contract: results for an identical ``operator_notes`` list are
    served from an in-memory cache; each provider call has a hard per-attempt
    timeout; transient failures are retried up to LLM_MAX_RETRIES within an
    overall LLM_DEADLINE budget; on final failure a typed :class:`LLMError` is
    raised (never a crash) so the view can fall back deterministically.

    Args:
        operator_notes: list[str] of notes (already validated upstream).

    Returns:
        A list with one raw parsed object per note, in input order. The content
        is UNTRUSTED and must be validated by the guardrails before use.

    Raises:
        LLMError: on missing configuration, exhausted retries/deadline, provider
            error, or a response that is not parseable JSON.
    """
    notes = list(operator_notes)
    cache_key = json.dumps(notes, ensure_ascii=False, sort_keys=False)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    provider = _env("LLM_PROVIDER", "openai") or "openai"
    model = _env("LLM_MODEL")
    if not model:
        raise LLMError("No LLM model configured (set LLM_MODEL).")
    fallback_model = _env("LLM_MODEL_FALLBACK")

    api_key = _resolve_api_key(provider)
    base_url = _env("LLM_BASE_URL") or _PROVIDER_BASE_URLS.get(provider.lower())

    def _float_env(name, default):
        try:
            return float(_env(name, str(default)) or default)
        except (TypeError, ValueError):
            return float(default)

    def _int_env(name, default):
        try:
            return int(float(_env(name, str(default)) or default))
        except (TypeError, ValueError):
            return int(default)

    timeout = _float_env("LLM_TIMEOUT", 8)          # hard per-attempt timeout (s)
    max_retries = max(0, _int_env("LLM_MAX_RETRIES", 1))  # extra tries on transient
    deadline = _float_env("LLM_DEADLINE", 20)       # total budget (< 30s request)
    backoff = _float_env("LLM_RETRY_BACKOFF", 0.5)
    cache_size = max(1, _int_env("LLM_CACHE_SIZE", 256))

    # Import lazily so the app boots even if the SDK is absent at import time.
    try:
        from openai import OpenAI
        import openai as openai_pkg
    except Exception as exc:  # pragma: no cover - import guard
        raise LLMError(f"OpenAI SDK unavailable: {type(exc).__name__}") from exc

    client_kwargs = {"api_key": api_key, "timeout": timeout, "max_retries": 0}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = OpenAI(**client_kwargs)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": FEWSHOT_USER},
        {"role": "assistant", "content": FEWSHOT_ASSISTANT},
        {"role": "user", "content": json.dumps({"operator_notes": notes})},
    ]

    def _call(model_name, call_timeout):
        """One attempt. Raises LLMError (with .transient) on any failure."""
        try:
            response = client.chat.completions.create(
                model=model_name,
                temperature=0,
                response_format={"type": "json_object"},
                messages=messages,
                timeout=call_timeout,
            )
        except Exception as exc:
            transient = _is_transient(exc, openai_pkg)
            status = getattr(exc, "status_code", None)
            # Keep the message short and secret-free (type + status only).
            detail = f"{type(exc).__name__}" + (f" ({status})" if status else "")
            raise _llm_error(f"LLM call failed: {detail}", transient) from exc

        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError) as exc:
            raise _llm_error("LLM response had no message content.") from exc
        if not content:
            raise _llm_error("LLM returned an empty response.")
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError) as exc:
            raise _llm_error("LLM response was not valid JSON.") from exc

        # JSON mode returns an object; unwrap the results array if present. The
        # payload is returned raw and untrusted beyond this structural unwrap.
        if isinstance(parsed, dict) and "results" in parsed:
            return parsed["results"]
        return parsed

    # Try the primary model then the optional fallback; retry transient errors
    # up to `max_retries` per model, all bounded by the overall deadline.
    models = [model] + ([fallback_model] if fallback_model else [])
    start = time.monotonic()
    last_error: LLMError | None = None

    for model_name in models:
        for attempt in range(max_retries + 1):
            remaining = deadline - (time.monotonic() - start)
            if remaining <= 0.1:
                raise _llm_error("LLM deadline exceeded.", transient=True)
            try:
                result = _call(model_name, min(timeout, remaining))
                _cache_put(cache_key, result, cache_size)
                return result
            except LLMError as exc:
                last_error = exc
                if getattr(exc, "transient", False) and attempt < max_retries:
                    logger.warning(
                        "LLM transient error (model=%s attempt=%s); retrying: %s",
                        model_name, attempt + 1, exc,
                    )
                    time.sleep(min(backoff, max(0.0, remaining - 0.1)))
                    continue
                break  # non-transient or retries exhausted -> next model

    raise last_error or LLMError("LLM call failed with no error captured.")
