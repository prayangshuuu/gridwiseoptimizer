"""Guardrails: turn UNTRUSTED LLM output into clean, optimizer-ready directives.

Untrusted-input contract
------------------------
The `raw` argument is whatever ``gridwise.llm.interpret_notes`` returned — parsed
JSON produced by a language model. It is NOT trusted: fields may be missing, the
wrong type, out of range, duplicated, out of order, or an entirely invented
directive type. This module is pure Python (no network, no DB, no randomness).

Safe-failure policy (coerce, never crash)
-----------------------------------------
`validate_directives` never raises on bad content and never lets a bad value
reach the optimizer. A malformed or unsupported entry is coerced to a safe
``no_op`` (applies=False, structured_adjustment=None) so a single bad note can
never sink the whole request. The result is always exactly one validated entry
per note, in ``note_index`` order 0..num_notes-1, ready to pass straight into
``gridwise.optimizer.optimize(hours, battery, directives)`` and, via
``to_interpretation``, into the response's ``directive_interpretation`` array.

Never modifies demand, tariff, or battery parameters, and never emits a
directive_type outside the allowed set.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

# The only directive types the optimizer understands. Anything else -> no_op.
WINDOW_TYPES = frozenset({
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
})
NO_OP = "no_op"
ALLOWED_TYPES = WINDOW_TYPES | {NO_OP}


@dataclass
class Directive:
    """A validated directive for exactly one operator note.

    Exposes exactly the fields the optimizer and the response schema read.
    ``structured_adjustment`` is a dict for active directives (holding the
    validated ``hours`` plus the type-specific numeric field) or ``None`` for a
    ``no_op``.
    """

    note_index: int
    applies: bool
    directive_type: str
    structured_adjustment: Optional[dict]
    explanation: str = ""


def to_interpretation(entry: Directive) -> dict:
    """Return the exact response-schema dict for a validated directive."""
    return {
        "note_index": entry.note_index,
        "applies": entry.applies,
        "directive_type": entry.directive_type,
        "structured_adjustment": entry.structured_adjustment,
        "explanation": entry.explanation,
    }


def _no_op(note_index: int, explanation: str = "") -> Directive:
    return Directive(
        note_index=note_index,
        applies=False,
        directive_type=NO_OP,
        structured_adjustment=None,
        explanation=explanation or "No actionable directive.",
    )


def _battery_capacity(battery: Any) -> float:
    """Read the battery capacity in kWh, tolerating an object with
    ``capacity_kwh`` or a dict with ``capacity_kwh`` / ``capacity``. Unknown
    capacity -> +inf (do not reject reserves on capacity grounds)."""
    for getter in (
        lambda b: getattr(b, "capacity_kwh"),
        lambda b: b["capacity_kwh"],
        lambda b: b["capacity"],
    ):
        try:
            value = getter(battery)
        except (AttributeError, KeyError, TypeError):
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return math.inf


def _is_finite_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _clean_hours(value: Any) -> Optional[list[int]]:
    """Unique ints in 0..23, deduped and sorted ascending. Non-ints and
    out-of-range values are dropped. Returns None if nothing valid remains."""
    if not isinstance(value, (list, tuple)):
        return None
    kept: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            continue  # drop non-ints (bool is an int subclass -> also dropped)
        if 0 <= item <= 23:
            kept.add(item)
    if not kept:
        return None
    return sorted(kept)


def _coerce_entry(entry: Any, note_index: int, capacity: float) -> Directive:
    """Validate one raw entry into a Directive, or a safe no_op on any problem."""
    if not isinstance(entry, dict):
        return _no_op(note_index)

    explanation = entry.get("explanation")
    if not isinstance(explanation, str):
        explanation = ""

    dtype = entry.get("directive_type")
    if dtype == NO_OP or dtype not in WINDOW_TYPES:
        return _no_op(note_index, explanation)

    adj = entry.get("structured_adjustment")
    if not isinstance(adj, dict):
        return _no_op(note_index, explanation)

    hours = _clean_hours(adj.get("hours"))
    if hours is None:
        return _no_op(note_index, explanation)

    if dtype == "solar_reduction":
        factor = adj.get("factor")
        if not _is_finite_number(factor):
            return _no_op(note_index, explanation)
        factor = min(1.0, max(0.0, float(factor)))  # clamp to [0, 1]
        return Directive(note_index, True, dtype,
                         {"hours": hours, "factor": factor}, explanation)

    if dtype == "minimum_battery_reserve":
        reserve = adj.get("minimum_energy_kwh")
        if not _is_finite_number(reserve) or reserve < 0 or reserve > capacity:
            return _no_op(note_index, explanation)  # negative or infeasible
        return Directive(note_index, True, dtype,
                         {"hours": hours, "minimum_energy_kwh": float(reserve)},
                         explanation)

    if dtype == "max_grid_window":
        cap = adj.get("max_grid_kwh")
        if not _is_finite_number(cap) or cap < 0:
            return _no_op(note_index, explanation)
        return Directive(note_index, True, dtype,
                         {"hours": hours, "max_grid_kwh": float(cap)}, explanation)

    # no_charge_window / no_discharge_window: hours only.
    return Directive(note_index, True, dtype, {"hours": hours}, explanation)


def validate_directives(raw: Any, num_notes: int, battery: Any) -> list[Directive]:
    """Validate raw LLM directive output into a clean, ordered directive list.

    Args:
        raw: untrusted value from interpret_notes (expected: a list of one
            object per note, but any shape is tolerated).
        num_notes: number of operator notes; the result has exactly this length.
        battery: the scenario battery (object with ``capacity_kwh`` or a dict
            with ``capacity_kwh`` / ``capacity``), used to bound reserves.

    Returns:
        list[Directive] of length ``num_notes``, ordered by note_index 0..N-1,
        each ready for optimize() and to_interpretation(). Never raises.
    """
    capacity = _battery_capacity(battery)

    # Rebuild strictly by index: first valid entry per index wins; entries with
    # a missing/duplicate/out-of-range note_index are ignored, and any note with
    # no valid entry becomes a no_op below.
    by_index: dict[int, dict] = {}
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            idx = entry.get("note_index")
            if (isinstance(idx, int) and not isinstance(idx, bool)
                    and 0 <= idx < num_notes and idx not in by_index):
                by_index[idx] = entry

    result: list[Directive] = []
    for i in range(num_notes):
        entry = by_index.get(i)
        result.append(_no_op(i) if entry is None else _coerce_entry(entry, i, capacity))
    return result
