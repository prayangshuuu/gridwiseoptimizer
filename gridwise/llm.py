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

import json
import os

# ---------------------------------------------------------------------------
# Directive contract shared with the prompt below.
#
# There are exactly six directive types the model may emit when a note applies.
# A note that carries no actionable instruction is a `no_op` (applies=False).
#
# Whole-hour rule: every hour reference is an integer in 0..23 (no partial
#   hours, no ranges other than an explicit list of hour indices).
# Factor rule: proportional changes are expressed as a non-negative multiplier
#   in `factor` (0.7 == "reduce by 30%", 1.2 == "increase by 20%", 0 == "off").
#   Absolute set-points (e.g. a battery reserve floor) use `value` in kWh.
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
    or unparseable response). The caller decides how to surface it."""


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
  - Whole-hour rule: hour references are integers 0..23 in an "hours" array,
    unique and ascending. Convert clock times to hour indices
    (e.g. "noon to 4pm" -> [12,13,14,15,16]; "6pm to 9pm" -> [18,19,20,21]).
  - Structured_adjustment shape per directive_type:
      solar_reduction:         {{"hours": [...], "factor": <0..1>}}
        factor is the FRACTION REMAINING (0.7 == "cut 30%", 0 == "no solar").
      minimum_battery_reserve: {{"hours": [...], "reserve": <kWh >= 0>}}
      no_charge:               {{"hours": [...]}}
      no_discharge:            {{"hours": [...]}}
      max_grid:                {{"hours": [...], "max_grid_kwh": <kWh >= 0>}}
  - Only use a directive_type from the allowed list. If a note is small talk, a
    status update, or otherwise not actionable, set applies=false,
    directive_type="no_op", structured_adjustment=null.
  - Output JSON only. Do not invent hours or numbers not implied by the note.
"""

# 2-3 few-shot examples: a solar_reduction, a window directive, a no_op.
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
            "structured_adjustment": {"hours": [12, 13, 14, 15, 16], "factor": 0.7},
            "explanation": "Cloud cover reduces solar output by 30% over noon-4pm.",
        },
        {
            "note_index": 1,
            "applies": True,
            "directive_type": "no_charge",
            "structured_adjustment": {"hours": [18, 19, 20, 21]},
            "explanation": "Battery charging is disallowed during the 6-9pm peak.",
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


def interpret_notes(operator_notes):
    """Interpret operator notes into raw structured directives via an LLM.

    Args:
        operator_notes: list[str] of 1-3 notes (already validated upstream).

    Returns:
        A list with one raw parsed object per note, in input order. The content
        is UNTRUSTED and must be validated by the optimizer before use.

    Raises:
        LLMError: on missing configuration, timeout, provider error, or a
            response that is not parseable JSON in the expected shape.
    """
    provider = _env("LLM_PROVIDER", "openai") or "openai"
    model = _env("LLM_MODEL")
    if not model:
        raise LLMError("No LLM model configured (set LLM_MODEL).")
    fallback_model = _env("LLM_MODEL_FALLBACK")

    api_key = _resolve_api_key(provider)
    # base_url from env, else a per-provider default (e.g. Gemini's gateway).
    base_url = _env("LLM_BASE_URL") or _PROVIDER_BASE_URLS.get(provider.lower())
    # Short call timeout that stays well inside the 30s request budget.
    try:
        timeout = float(_env("LLM_TIMEOUT", "8") or "8")
    except ValueError:
        timeout = 8.0

    # Import lazily so the app boots even if the SDK is absent at import time.
    try:
        from openai import OpenAI
        import openai as openai_pkg
    except Exception as exc:  # pragma: no cover - import guard
        raise LLMError(f"OpenAI SDK unavailable: {exc}") from exc

    client_kwargs = {"api_key": api_key, "timeout": timeout, "max_retries": 0}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = OpenAI(**client_kwargs)

    user_payload = json.dumps({"operator_notes": list(operator_notes)})
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": FEWSHOT_USER},
        {"role": "assistant", "content": FEWSHOT_ASSISTANT},
        {"role": "user", "content": user_payload},
    ]

    def _call(model_name):
        """One attempt against a specific model. Raises LLMError on failure."""
        try:
            response = client.chat.completions.create(
                model=model_name,
                temperature=0,
                response_format={"type": "json_object"},
                messages=messages,
                timeout=timeout,
            )
        except openai_pkg.APITimeoutError as exc:
            raise LLMError(f"LLM call timed out after {timeout}s.") from exc
        except openai_pkg.APIError as exc:
            raise LLMError(f"LLM provider error: {exc}") from exc
        except Exception as exc:  # network/config/anything else
            raise LLMError(f"LLM call failed: {exc}") from exc

        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError) as exc:
            raise LLMError("LLM response had no message content.") from exc
        if not content:
            raise LLMError("LLM returned an empty response.")
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError) as exc:
            raise LLMError("LLM response was not valid JSON.") from exc

        # JSON mode returns an object; unwrap the results array if present. The
        # payload is returned raw and untrusted beyond this structural unwrap.
        if isinstance(parsed, dict) and "results" in parsed:
            return parsed["results"]
        return parsed

    # Try the primary model, then the optional fallback if it fails.
    models = [model] + ([fallback_model] if fallback_model else [])
    last_error = None
    for candidate in models:
        try:
            return _call(candidate)
        except LLMError as exc:
            last_error = exc
    raise last_error
