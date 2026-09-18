"""Deterministic guardrails over raw LLM output.

`validate_directives` takes the UNTRUSTED list returned by
``gridwise.llm.interpret_notes`` and turns it into a clean, typed list of
directives the optimizer can consume safely. It is pure Python: no network, no
DB, no randomness.

Two kinds of problem are handled differently:

* Structural integrity of the list as a whole (wrong length, missing/duplicate
  note indices) is a controlled failure -> :class:`GuardrailError`.
* A single malformed or unsupported entry is never trusted and never crashes:
  it is coerced to a safe ``no_op``. We never invent a directive type.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# The five actionable directive types plus the inert no_op. Must match the set
# the LLM is prompted with (gridwise/llm.py) and the optimizer consumes.
ACTIVE_TYPES = frozenset({
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge",
    "no_discharge",
    "max_grid",
})
NO_OP = "no_op"


class GuardrailError(Exception):
    """Raised for a structural problem with the directive list as a whole
    (not something we can safely coerce per entry)."""


@dataclass(frozen=True)
class Directive:
    """A validated directive for exactly one operator note.

    ``applies`` is False only for ``no_op`` entries, whose optional fields are
    all None/empty. For active entries, exactly the fields relevant to
    ``directive_type`` are populated:

      solar_reduction         -> hours, factor
      minimum_battery_reserve -> hours, reserve
      no_charge / no_discharge-> hours
      max_grid                -> hours, max_grid_kwh
    """

    note_index: int
    directive_type: str
    applies: bool
    hours: tuple[int, ...] = ()
    factor: float | None = None
    reserve: float | None = None
    max_grid_kwh: float | None = None

    def to_dict(self) -> dict:
        """JSON-serializable view used in the API's directive_interpretation."""
        out = {
            "note_index": self.note_index,
            "applies": self.applies,
            "directive_type": self.directive_type,
        }
        if not self.applies:
            out["structured_adjustment"] = None
            return out
        adj: dict = {"hours": list(self.hours)}
        if self.factor is not None:
            adj["factor"] = self.factor
        if self.reserve is not None:
            adj["reserve"] = self.reserve
        if self.max_grid_kwh is not None:
            adj["max_grid_kwh"] = self.max_grid_kwh
        out["structured_adjustment"] = adj
        return out


def _no_op(note_index: int) -> Directive:
    return Directive(note_index=note_index, directive_type=NO_OP, applies=False)


def _is_real_number(value) -> bool:
    # Reject bools (they are ints) and non-finite floats.
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _clean_hours(value) -> tuple[int, ...] | None:
    """Return a sorted tuple of unique hour indices in 0..23, or None if the
    value is not a usable, non-empty list of whole hours."""
    if not isinstance(value, (list, tuple)):
        return None
    hours: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            return None
        if item < 0 or item > 23:
            return None
        hours.add(item)
    if not hours:
        return None
    return tuple(sorted(hours))


def _coerce_entry(entry, note_index: int, capacity: float) -> Directive:
    """Validate one raw entry into a Directive, or a safe no_op on any problem."""
    if not isinstance(entry, dict):
        return _no_op(note_index)

    directive_type = entry.get("directive_type")
    applies = entry.get("applies")

    # Anything not clearly an applicable, supported active directive is no_op.
    if directive_type == NO_OP or applies is not True:
        return _no_op(note_index)
    if directive_type not in ACTIVE_TYPES:
        return _no_op(note_index)

    adj = entry.get("structured_adjustment")
    if not isinstance(adj, dict):
        return _no_op(note_index)

    hours = _clean_hours(adj.get("hours"))
    if hours is None:
        return _no_op(note_index)

    if directive_type == "solar_reduction":
        factor = adj.get("factor")
        if not _is_real_number(factor) or not (0.0 <= factor <= 1.0):
            return _no_op(note_index)
        return Directive(note_index, directive_type, True, hours=hours,
                         factor=float(factor))

    if directive_type == "minimum_battery_reserve":
        reserve = adj.get("reserve")
        if not _is_real_number(reserve) or reserve < 0 or reserve > capacity:
            return _no_op(note_index)
        return Directive(note_index, directive_type, True, hours=hours,
                         reserve=float(reserve))

    if directive_type == "max_grid":
        cap = adj.get("max_grid_kwh")
        if not _is_real_number(cap) or cap < 0:
            return _no_op(note_index)
        return Directive(note_index, directive_type, True, hours=hours,
                         max_grid_kwh=float(cap))

    # no_charge / no_discharge: hours only.
    return Directive(note_index, directive_type, True, hours=hours)


def validate_directives(raw, num_notes: int, capacity: float) -> list[Directive]:
    """Validate raw LLM directive output into a clean, ordered list.

    Args:
        raw: the untrusted value returned by interpret_notes (expected: a list
            of one object per note).
        num_notes: number of operator notes; the output has exactly this length.
        capacity: battery capacity in kWh, used to bound minimum_battery_reserve.

    Returns:
        list[Directive] of length ``num_notes``, ordered by note_index 0..N-1.

    Raises:
        GuardrailError: if the list structure is unusable (wrong length, or the
            note indices are not exactly {0..num_notes-1}).
    """
    if not isinstance(raw, list):
        raise GuardrailError("LLM output was not a list of directives.")
    if len(raw) != num_notes:
        raise GuardrailError(
            f"Expected {num_notes} directive(s), got {len(raw)}."
        )

    # Map each entry to its note_index; require exactly one per note, no dupes.
    by_index: dict[int, object] = {}
    for entry in raw:
        idx = entry.get("note_index") if isinstance(entry, dict) else None
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise GuardrailError("A directive is missing a valid note_index.")
        if idx < 0 or idx >= num_notes:
            raise GuardrailError(f"note_index {idx} is out of range.")
        if idx in by_index:
            raise GuardrailError(f"Duplicate note_index {idx}.")
        by_index[idx] = entry

    if set(by_index) != set(range(num_notes)):
        missing = sorted(set(range(num_notes)) - set(by_index))
        raise GuardrailError(f"Missing directive(s) for note index {missing}.")

    return [
        _coerce_entry(by_index[i], i, capacity) for i in range(num_notes)
    ]
