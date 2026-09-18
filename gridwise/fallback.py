"""Deterministic keyword/rule interpreter for unit tests and offline tooling.

Not wired into POST /optimize-energy (that path uses OpenRouter only via
``gridwise.llm.interpret_notes``). Emits the same raw shape as the LLM
interpreter; output still passes through guardrails when used in tests.

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
                 "decrease", "less", "down", "scale", "curtail", "de-rate",
                 "leave", "only", "usable", "treat", "treated", "of the forecast",
                 "of the predicted", "of the expected", "of the scheduled")
_RESERVE_WORDS = ("reserve", "minimum", "at least", "floor", "keep", "maintain",
                  "hold", "below", "no lower than", "don't let", "remain",
                  "stored", "requires")
_GRID_CAP_WORDS = ("cap", "limit", "ceiling", "no more than", "at most",
                   "restrict", "max", "maximum", "not exceed", "below",
                   "under", "at or below", "stay at or below", "stay below",
                   "stay under")

# Words that map to explicit hour windows when no numeric clock is given.
_NAMED_WINDOWS = {
    "morning": (6, 12),
    "afternoon": (12, 18),
    "evening": (18, 22),
    "midday": (11, 14),
    "overnight": (0, 6),
    "night": (22, 24),
    "all day": (0, 24),
    "all-day": (0, 24),
    "allday": (0, 24),
    "24 hours": (0, 24),
    "24-hour": (0, 24),
    "24h": (0, 24),
    "full day": (0, 24),
    "the day": (0, 24),
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
    """True when the number at span_start is a TARGET level ("scale to 70%",
    "treated as 25%") rather than an amount to remove ("cut by 30%"). Only
    "to/at/reach/of/=/as" in the few characters immediately before the number
    count — this avoids the "to" inside a time range ("noon to 4pm") flipping
    the meaning."""
    prefix = low[max(0, span_start - 16):span_start]
    return bool(re.search(
        r"\b(to|at|reach|of|=|as|down to|set to|leave|usable|treated|"
        r"of the forecast|of the predicted|of the expected)\b[^0-9]{0,10}$",
        prefix,
    ))


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
    if any(p in low for p in ("do not", "don't", "dont", "no ",
                              "avoid", "stop", "halt", "prevent", "without",
                              "off", "block", "ban", "forbid", "restrict",
                              "refuse", "must not", "isolated", "isolating",
                              "unavailable", "disabled", "disable",
                              "out of service", "offline", "not be", "cannot")):
        return True
    # "keep/prevent/stop the battery FROM (dis)charging"
    return bool(re.search(r"\b(keep|prevent|stop|hold|block|restrict|"
                          r"disabled?|isolated?|unavailable|"
                          r"forbidden?|not\s+be)\b[^.]*\b(from|to)\b[^.]*charg",
                          low))


def _extract_kwh_for_reserve(text: str, capacity_kwh: float | None) -> float | None:
    """Return kWh for a minimum_battery_reserve directive.

    Prefers explicit ``kWh`` units; falls back to ``<pct>% of the battery
    capacity`` when capacity is known. Returns None when neither can be
    resolved (caller decides no_op vs other directive)."""
    kwh = _extract_number(text, r"(?:kwh|kw|kilowatt(?:-?hours?)?)")
    if kwh is not None:
        return kwh
    if capacity_kwh and capacity_kwh > 0:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s*(?:of\s*(?:the\s*)?"
                      r"(?:battery|capacity|pack))?", text, re.IGNORECASE)
        if m:
            return round(capacity_kwh * float(m.group(1)) / 100.0, 6)
    return None


def _battery_capacity_kwh(battery: dict | None) -> float | None:
    if not isinstance(battery, dict):
        return None
    for key in ("capacity", "capacity_kwh"):
        val = battery.get(key)
        if val is not None:
            try:
                cap = float(val)
            except (TypeError, ValueError):
                continue
            if cap > 0:
                return cap
    return None


def _interpret_one(note: str, index: int, battery: dict | None = None) -> dict:
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
        return result(True, "no_discharge_window", {"hours": hours}) if hours else no_op()
    if "charg" in low and _negated_charge(low):
        return result(True, "no_charge_window", {"hours": hours}) if hours else no_op()

    if _has(low, _SOLAR_WORDS) and _has(low, _REDUCE_WORDS):
        if hours:
            return result(True, "solar_reduction",
                          {"hours": hours, "factor": _solar_factor(note)})
        return no_op()
    # A percentage attached to solar without a reduce verb is still a reduction.
    if _has(low, _SOLAR_WORDS) and re.search(r"\d+(?:\.\d+)?\s*(?:%|percent)", low):
        if hours:
            return result(True, "solar_reduction",
                          {"hours": hours, "factor": _solar_factor(note)})
        return no_op()

    kwh = _extract_number(low, r"(?:kwh|kw|kilowatt(?:-?hours?)?)")
    if "grid" in low and _has(low, _GRID_CAP_WORDS) and kwh is not None:
        return result(True, "max_grid_window",
                      {"hours": hours, "max_grid_kwh": kwh}) if hours else no_op()

    capacity = _battery_capacity_kwh(battery)
    reserve_kwh = _extract_kwh_for_reserve(note, capacity)
    if _has(low, _RESERVE_WORDS) and reserve_kwh is not None and (
        "batter" in low or "reserve" in low or "charge" in low or "soc" in low
        or "capacity" in low or "data center" in low or "emergency" in low
    ):
        return result(True, "minimum_battery_reserve",
                      {"hours": hours, "minimum_energy_kwh": reserve_kwh}) if hours else no_op()

    return no_op()


def interpret_notes_fallback(operator_notes, battery: dict | None = None) -> list[dict]:
    """Deterministic interpretation of notes. Never raises; returns one raw
    object per note in order, in the same shape as the LLM interpreter.

    ``battery`` is optional but, when provided, lets the interpreter resolve
    ``"<pct>% of the battery capacity"`` style directives into kWh.

    Not used on the live API path — kept for unit tests of keyword parsing.
    """
    return [_interpret_one(str(note), i, battery=battery)
            for i, note in enumerate(operator_notes)]
