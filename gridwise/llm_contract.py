"""Shared GridWise LLM interpretation contract: prompt, few-shot, schema validation."""
from __future__ import annotations

import json
import math
import re
from typing import Any

from .llm_errors import LLMError

DIRECTIVE_TYPES = (
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
)

NO_OP = "no_op"
WINDOW_TYPES = frozenset(DIRECTIVE_TYPES)
ALLOWED_TYPES = WINDOW_TYPES | {NO_OP}

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
      minimum_battery_reserve: {{"hours": [...], "minimum_energy_kwh": <kWh >= 0>}}
        Always emit absolute kWh, never a percentage. When the request includes
        battery.capacity_kwh, convert "N% of (the) battery capacity" to
        minimum_energy_kwh = capacity_kwh * N / 100.
      no_charge_window:        {{"hours": [...]}}
      no_discharge_window:     {{"hours": [...]}}
      max_grid_window:         {{"hours": [...], "max_grid_kwh": <kWh >= 0>}}
  - no_op vs real: set no_op ONLY when the note carries no actionable energy
    instruction (greetings, thanks, status updates, FYIs, reminders). If the
    note names a concrete change to solar, battery charging/discharging, a
    reserve floor, or a grid-import cap, it is NOT a no_op.
  - Only use a directive_type from the allowed list. Output JSON only. Do not
    invent hours or numbers that the note does not imply.
"""

FEWSHOT_USER = json.dumps({
    "operator_notes": [
        "Heavy cloud cover this afternoon, cut expected solar by 30% from noon to 4pm.",
        "Do not charge the battery during the evening peak, 6pm to 9pm.",
        "Keep at least 6 kWh in the battery overnight, from 00:00 to 05:00.",
        "Limit grid import to 5 kWh between 17:00 and 20:00.",
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
            "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [18, 19, 20]},
            "explanation": "Battery charging is disallowed across the 6-to-9pm peak.",
        },
        {
            "note_index": 2,
            "applies": True,
            "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {"hours": [0, 1, 2, 3, 4], "minimum_energy_kwh": 6},
            "explanation": "The battery must hold at least 6 kWh from midnight to 5am.",
        },
        {
            "note_index": 3,
            "applies": True,
            "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": [17, 18, 19], "max_grid_kwh": 5},
            "explanation": "Grid import is capped at 5 kWh across the 17:00-20:00 window.",
        },
        {
            "note_index": 4,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": "The note is a thank-you message with no actionable directive.",
        },
    ]
})


def _battery_capacity_kwh(battery: Any) -> float | None:
    """Capacity in kWh for prompt context (mirrors guardrails field aliases)."""
    if battery is None:
        return None
    getters = (
        lambda b: getattr(b, "capacity_kwh", None),
        lambda b: getattr(b, "capacity", None),
        lambda b: b.get("capacity_kwh") if isinstance(b, dict) else None,
        lambda b: b.get("capacity") if isinstance(b, dict) else None,
    )
    for getter in getters:
        try:
            val = getter(battery)
        except (AttributeError, KeyError, TypeError):
            continue
        if _is_finite_number(val) and float(val) > 0:
            return float(val)
    return None


def build_chat_messages(
    operator_notes: list[str],
    *,
    battery: Any = None,
) -> list[dict[str, str]]:
    """Messages shared by every LLM provider."""
    payload: dict[str, Any] = {"operator_notes": operator_notes}
    cap = _battery_capacity_kwh(battery)
    if cap is not None:
        payload["battery"] = {"capacity_kwh": cap}
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": FEWSHOT_USER},
        {"role": "assistant", "content": FEWSHOT_ASSISTANT},
        {
            "role": "user",
            "content": json.dumps(payload),
        },
    ]


def _is_finite_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _clean_hours(value: Any) -> list[int] | None:
    if not isinstance(value, (list, tuple)):
        return None
    kept: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            continue
        if 0 <= item <= 23:
            kept.add(item)
    if not kept:
        return None
    return sorted(kept)


def _validate_entry(entry: Any, note_index: int) -> dict:
    if not isinstance(entry, dict):
        raise LLMError("LLM schema validation failed: entry is not an object.")

    idx = entry.get("note_index")
    if not isinstance(idx, int) or isinstance(idx, bool) or idx != note_index:
        raise LLMError("LLM schema validation failed: note_index mismatch.")

    applies = entry.get("applies")
    if not isinstance(applies, bool):
        raise LLMError("LLM schema validation failed: applies must be boolean.")

    explanation = entry.get("explanation")
    if not isinstance(explanation, str):
        raise LLMError("LLM schema validation failed: explanation must be a string.")

    dtype = entry.get("directive_type")
    if dtype not in ALLOWED_TYPES:
        raise LLMError("LLM schema validation failed: unknown directive_type.")

    adj = entry.get("structured_adjustment")

    if not applies:
        if dtype != NO_OP or adj is not None:
            raise LLMError("LLM schema validation failed: inactive note must be no_op.")
        return {
            "note_index": note_index,
            "applies": False,
            "directive_type": NO_OP,
            "structured_adjustment": None,
            "explanation": explanation,
        }

    if dtype not in WINDOW_TYPES:
        raise LLMError("LLM schema validation failed: active note has invalid type.")
    if not isinstance(adj, dict):
        raise LLMError("LLM schema validation failed: structured_adjustment required.")

    hours = _clean_hours(adj.get("hours"))
    if hours is None:
        raise LLMError("LLM schema validation failed: invalid hours.")

    if dtype == "solar_reduction":
        factor = adj.get("factor")
        if not _is_finite_number(factor) or factor < 0 or factor > 1:
            raise LLMError("LLM schema validation failed: invalid solar factor.")
        return {
            "note_index": note_index,
            "applies": True,
            "directive_type": dtype,
            "structured_adjustment": {"hours": hours, "factor": float(factor)},
            "explanation": explanation,
        }

    if dtype == "minimum_battery_reserve":
        reserve = adj.get("minimum_energy_kwh")
        if not _is_finite_number(reserve) or reserve < 0:
            raise LLMError("LLM schema validation failed: invalid reserve.")
        return {
            "note_index": note_index,
            "applies": True,
            "directive_type": dtype,
            "structured_adjustment": {
                "hours": hours,
                "minimum_energy_kwh": float(reserve),
            },
            "explanation": explanation,
        }

    if dtype == "max_grid_window":
        cap = adj.get("max_grid_kwh")
        if not _is_finite_number(cap) or cap < 0:
            raise LLMError("LLM schema validation failed: invalid grid cap.")
        return {
            "note_index": note_index,
            "applies": True,
            "directive_type": dtype,
            "structured_adjustment": {"hours": hours, "max_grid_kwh": float(cap)},
            "explanation": explanation,
        }

    return {
        "note_index": note_index,
        "applies": True,
        "directive_type": dtype,
        "structured_adjustment": {"hours": hours},
        "explanation": explanation,
    }


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _loads_lenient(content: str):
    """json.loads, tolerating models that wrap JSON in prose or ```json fences.

    Some OpenRouter models ignore ``response_format=json_object`` and return the
    object inside a markdown fence or after a preamble. We try the raw string,
    then any fenced block, then the first balanced {...}/[...] span."""
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        pass

    for candidate in _FENCE_RE.findall(content):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    for opener, closer in (("{", "}"), ("[", "]")):
        start = content.find(opener)
        end = content.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(content[start:end + 1])
            except json.JSONDecodeError:
                continue

    raise LLMError("LLM response was not valid JSON.")


def parse_provider_content(content: str, num_notes: int) -> list[dict]:
    """Parse JSON content from a provider; raise LLMError on transport-level issues."""
    if not content:
        raise LLMError("LLM returned an empty response.")
    parsed = _loads_lenient(content)

    raw = parsed["results"] if isinstance(parsed, dict) and "results" in parsed else parsed
    if not isinstance(raw, list):
        raise LLMError("LLM response was not a JSON array of directives.")
    if len(raw) != num_notes:
        raise LLMError(
            f"LLM returned {len(raw)} directive(s) for {num_notes} note(s)."
        )
    return raw


def validate_interpretation_schema(num_notes: int, raw: list[dict]) -> list[dict]:
    """Strict canonical schema check before guardrails. Raises LLMError on failure."""
    if len(raw) != num_notes:
        raise LLMError(
            f"LLM returned {len(raw)} directive(s) for {num_notes} note(s)."
        )
    seen: set[int] = set()
    by_index: dict[int, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise LLMError("LLM schema validation failed: entry is not an object.")
        idx = entry.get("note_index")
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise LLMError("LLM schema validation failed: bad note_index.")
        if idx < 0 or idx >= num_notes or idx in seen:
            raise LLMError("LLM schema validation failed: duplicate or out-of-range index.")
        seen.add(idx)
        by_index[idx] = entry

    if len(seen) != num_notes:
        raise LLMError("LLM schema validation failed: missing note indices.")

    return [_validate_entry(by_index[i], i) for i in range(num_notes)]
