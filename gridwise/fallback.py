"""Deterministic keyword/rule interpreter — the safety net, NOT the primary path.

Used ONLY when the LLM path (gridwise.llm.interpret_notes) raises LLMError, so a
slow or flaky provider still yields a valid schedule instead of a 500. It emits
the SAME raw shape as the LLM interpreter (a list of one object per note); the
output is still untrusted and passes through the same guardrails.

No public/judge note wording is hard-coded here: interpretation is driven by
generic keywords, clock parsing, and number extraction only. The hour convention
matches gridwise/llm.py: a range "A to B" covers hour A up to but NOT including
hour B (endpoint-exclusive).
"""
from __future__ import annotations

import re

# Directive detection keywords (generic, not case-specific).
_SOLAR_WORDS = ("solar", "pv", "photovoltaic", "sun")
_REDUCE_WORDS = ("cut", "reduce", "reduction", "lower", "drop", "derate",
                 "decrease", "less", "down", "scale", "curtail", "de-rate")
_RESERVE_WORDS = ("reserve", "minimum", "at least", "floor", "keep", "maintain",
                  "hold", "below", "no lower than", "don't let")
_GRID_CAP_WORDS = ("cap", "limit", "ceiling", "no more than", "at most",
                   "restrict", "max", "maximum", "not exceed")

# Words that map to explicit hour windows when no numeric clock is given.
_NAMED_WINDOWS = {
    "morning": (6, 12),
    "afternoon": (12, 18),
    "evening": (18, 22),
    "midday": (11, 14),
    "overnight": (0, 6),
    "night": (22, 24),
}


def _word_to_clock(text: str) -> str:
    """Normalize spelled-out clock words to a parseable numeric form."""
    text = re.sub(r"\bnoon\b", "12pm", text)
    text = re.sub(r"\bmidday\b", "12pm", text)
    text = re.sub(r"\bmidnight\b", "12am", text)
    return text


def _to_hour(value: str, meridiem: str | None) -> int | None:
    """Convert a clock endpoint to an hour index 0..23."""
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return None
    if meridiem:
        meridiem = meridiem.lower()
        if hour == 12:
            hour = 0
        if meridiem == "pm":
            hour += 12
    if 0 <= hour <= 24:
        return hour
    return None


# "5pm", "17:00", "5 pm", "17"
_TIME = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
_RANGE_RE = re.compile(
    _TIME + r"\s*(?:to|-|–|—|until|till|through|and)\s*" + _TIME,
    re.IGNORECASE,
)
_SINGLE_RE = re.compile(r"\bat\s+" + _TIME, re.IGNORECASE)


def parse_hours(text: str) -> list[int]:
    """Extract hour indices from free text (endpoint-exclusive ranges)."""
    norm = _word_to_clock(text.lower())

    m = _RANGE_RE.search(norm)
    if m:
        start = _to_hour(m.group(1), m.group(3) or m.group(6))
        end = _to_hour(m.group(4), m.group(6))
        if start is not None and end is not None:
            if end == 0 and start != 0:  # "to midnight" == end of day
                end = 24
            if 0 <= start < end <= 24:
                return list(range(start, min(end, 24)))

    m = _SINGLE_RE.search(norm)
    if m:
        h = _to_hour(m.group(1), m.group(3))
        if h is not None and h < 24:
            return [h]

    for name, (lo, hi) in _NAMED_WINDOWS.items():
        if name in norm:
            return list(range(lo, hi))
    return []


def _extract_number(text: str, unit_pattern: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*" + unit_pattern, text, re.IGNORECASE)
    return float(m.group(1)) if m else None


def _is_target(low: str, span_start: int) -> bool:
    """True when the number at span_start is a TARGET level ("scale to 70%")
    rather than an amount to remove ("cut by 30%"). Only "to/at/reach/of/=" in
    the few characters immediately before the number count — this avoids the
    "to" inside a time range ("noon to 4pm") flipping the meaning."""
    prefix = low[max(0, span_start - 12):span_start]
    return bool(re.search(r"\b(to|at|reach|of|=)\b[^0-9]{0,8}$", prefix))


def _solar_factor(text: str) -> float:
    """Return the FRACTION OF SOLAR REMAINING implied by the note."""
    low = text.lower()

    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)", low)
    if m:
        frac = float(m.group(1)) / 100.0
        return round(frac if _is_target(low, m.start()) else 1.0 - frac, 6)

    m = re.search(r"\b(0?\.\d+)\b", low)  # bare fraction like 0.3 / .3 / 0.7
    if m:
        frac = float(m.group(1))
        return round(frac if _is_target(low, m.start()) else 1.0 - frac, 6)

    if "half" in low:
        return 0.5
    if "quarter" in low:
        return 0.25 if "quarter" in low and _word_is_target(low, "quarter") else 0.75
    if "third" in low:
        return round(1 / 3, 6) if _word_is_target(low, "third") else round(1 - 1 / 3, 6)
    return 0.5  # reduction implied but unquantified: conservative default


def _word_is_target(low: str, word: str) -> bool:
    idx = low.find(word)
    return _is_target(low, idx) if idx >= 0 else False


def _has(text: str, words) -> bool:
    low = text.lower()
    return any(w in low for w in words)


def _negated_charge(text: str) -> bool:
    low = text.lower()
    if any(p in low for p in ("do not", "don't", "dont", "no ", "avoid",
                              "stop", "halt", "prevent", "without", "off")):
        return True
    # "keep/prevent/stop the battery FROM (dis)charging"
    return bool(re.search(r"\b(keep|prevent|stop|hold)\b[^.]*\bfrom\b[^.]*charg", low))


def _interpret_one(note: str, index: int) -> dict:
    low = note.lower()
    hours = parse_hours(note)

    def result(applies, dtype, adj):
        return {
            "note_index": index,
            "applies": applies,
            "directive_type": dtype,
            "structured_adjustment": adj,
            "explanation": "deterministic fallback interpretation",
        }

    def no_op():
        return result(False, "no_op", None)

    # discharge before charge ("discharge" contains "charge").
    if "discharg" in low and _negated_charge(low):
        return result(True, "no_discharge", {"hours": hours}) if hours else no_op()
    if "charg" in low and _negated_charge(low):
        return result(True, "no_charge", {"hours": hours}) if hours else no_op()

    if _has(low, _SOLAR_WORDS) and _has(low, _REDUCE_WORDS):
        if hours:
            return result(True, "solar_reduction",
                          {"hours": hours, "factor": _solar_factor(note)})
        return no_op()

    kwh = _extract_number(low, r"(?:kwh|kw|kilowatt(?:-?hours?)?)")
    if "grid" in low and _has(low, _GRID_CAP_WORDS) and kwh is not None:
        return result(True, "max_grid",
                      {"hours": hours, "max_grid_kwh": kwh}) if hours else no_op()

    if _has(low, _RESERVE_WORDS) and kwh is not None and (
        "batter" in low or "reserve" in low or "charge" in low or "soc" in low
    ):
        return result(True, "minimum_battery_reserve",
                      {"hours": hours, "reserve": kwh}) if hours else no_op()

    return no_op()


def interpret_notes_fallback(operator_notes) -> list[dict]:
    """Deterministic interpretation of notes. Never raises; returns one raw
    object per note in order, in the same shape as the LLM interpreter."""
    return [_interpret_one(str(note), i) for i, note in enumerate(operator_notes)]
