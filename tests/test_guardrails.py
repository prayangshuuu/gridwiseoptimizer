"""Tests for gridwise.guardrails.validate_directives.

Pure Python, no Django needed. Run with:

    uv run python tests/test_guardrails.py
    # or:  uv run python -m unittest -v tests.test_guardrails
"""
import pathlib
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from gridwise.guardrails import validate_directives, to_interpretation  # noqa: E402

BATTERY = {"capacity_kwh": 10.0}


class TestValidateDirectives(unittest.TestCase):

    def test_unsupported_type_becomes_no_op(self):
        raw = [{"note_index": 0, "applies": True,
                "directive_type": "charge_window",  # not in allowed set
                "structured_adjustment": {"hours": [1]}}]
        r = validate_directives(raw, 1, BATTERY)
        self.assertEqual(r[0].directive_type, "no_op")
        self.assertFalse(r[0].applies)
        self.assertIsNone(r[0].structured_adjustment)

    def test_factor_above_one_clamped_or_dropped(self):
        raw = [{"note_index": 0, "applies": True,
                "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": [10], "factor": 1.3}}]
        d = validate_directives(raw, 1, BATTERY)[0]
        if d.directive_type == "solar_reduction":       # clamped
            self.assertEqual(d.structured_adjustment["factor"], 1.0)
        else:                                            # or dropped
            self.assertEqual(d.directive_type, "no_op")

    def test_non_numeric_factor_becomes_no_op(self):
        raw = [{"note_index": 0, "applies": True,
                "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": [10], "factor": "lots"}}]
        self.assertEqual(validate_directives(raw, 1, BATTERY)[0].directive_type, "no_op")

    def test_reserve_above_capacity_becomes_no_op(self):
        raw = [{"note_index": 0, "applies": True,
                "directive_type": "minimum_battery_reserve",
                "structured_adjustment": {"hours": [0], "minimum_energy_kwh": 99}}]
        self.assertEqual(validate_directives(raw, 1, BATTERY)[0].directive_type, "no_op")

    def test_reserve_within_capacity_passes(self):
        raw = [{"note_index": 0, "applies": True,
                "directive_type": "minimum_battery_reserve",
                "structured_adjustment": {"hours": [0, 1], "minimum_energy_kwh": 5}}]
        d = validate_directives(raw, 1, BATTERY)[0]
        self.assertEqual(d.directive_type, "minimum_battery_reserve")
        self.assertEqual(d.structured_adjustment, {"hours": [0, 1], "minimum_energy_kwh": 5.0})

    def test_duplicate_and_out_of_order_note_index_repaired(self):
        raw = [
            {"note_index": 1, "applies": True, "directive_type": "no_charge_window",
             "structured_adjustment": {"hours": [5]}},
            {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
             "structured_adjustment": {"hours": [10], "factor": 0.5}},
            {"note_index": 0, "applies": False, "directive_type": "no_op",
             "structured_adjustment": None},  # duplicate index 0 -> ignored
        ]
        r = validate_directives(raw, 3, BATTERY)
        self.assertEqual([d.note_index for d in r], [0, 1, 2])
        self.assertEqual(r[0].directive_type, "solar_reduction")   # first idx-0 wins
        self.assertEqual(r[1].directive_type, "no_charge_window")
        self.assertEqual(r[2].directive_type, "no_op")             # missing note 2

    def test_hours_cleaned_dedup_sort_range(self):
        raw = [{"note_index": 0, "applies": True,
                "directive_type": "no_discharge_window",
                "structured_adjustment": {"hours": [15, 3, 3, 26]}}]
        d = validate_directives(raw, 1, BATTERY)[0]
        self.assertEqual(d.directive_type, "no_discharge_window")
        self.assertEqual(d.structured_adjustment["hours"], [3, 15])  # 26 dropped, 3 deduped

    def test_empty_hours_after_cleaning_becomes_no_op(self):
        raw = [{"note_index": 0, "applies": True,
                "directive_type": "no_charge_window",
                "structured_adjustment": {"hours": [24, 99, -1]}}]
        self.assertEqual(validate_directives(raw, 1, BATTERY)[0].directive_type, "no_op")

    def test_each_valid_type_passes_through_unchanged(self):
        raw = [
            {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
             "structured_adjustment": {"hours": [12, 13], "factor": 0.7}, "explanation": "e0"},
            {"note_index": 1, "applies": True, "directive_type": "minimum_battery_reserve",
             "structured_adjustment": {"hours": [0, 1], "minimum_energy_kwh": 4}, "explanation": "e1"},
            {"note_index": 2, "applies": True, "directive_type": "no_charge_window",
             "structured_adjustment": {"hours": [18, 19]}, "explanation": "e2"},
            {"note_index": 3, "applies": True, "directive_type": "no_discharge_window",
             "structured_adjustment": {"hours": [9, 10]}, "explanation": "e3"},
            {"note_index": 4, "applies": True, "directive_type": "max_grid_window",
             "structured_adjustment": {"hours": [20], "max_grid_kwh": 3}, "explanation": "e4"},
            {"note_index": 5, "applies": False, "directive_type": "no_op",
             "structured_adjustment": None, "explanation": "e5"},
        ]
        r = validate_directives(raw, 6, BATTERY)
        self.assertEqual(
            [d.directive_type for d in r],
            ["solar_reduction", "minimum_battery_reserve", "no_charge_window",
             "no_discharge_window", "max_grid_window", "no_op"],
        )
        self.assertTrue(all(r[i].applies for i in range(5)))
        self.assertFalse(r[5].applies)
        self.assertEqual(r[0].structured_adjustment, {"hours": [12, 13], "factor": 0.7})
        self.assertEqual(r[1].structured_adjustment, {"hours": [0, 1], "minimum_energy_kwh": 4.0})
        self.assertEqual(r[2].structured_adjustment, {"hours": [18, 19]})
        self.assertEqual(r[4].structured_adjustment, {"hours": [20], "max_grid_kwh": 3.0})
        # explanations preserved; to_interpretation yields the exact schema.
        self.assertEqual(r[0].explanation, "e0")
        self.assertEqual(
            set(to_interpretation(r[0])),
            {"note_index", "applies", "directive_type", "structured_adjustment", "explanation"},
        )

    def test_capacity_via_object_attribute(self):
        battery = SimpleNamespace(capacity_kwh=10.0)
        raw = [{"note_index": 0, "applies": True,
                "directive_type": "minimum_battery_reserve",
                "structured_adjustment": {"hours": [0], "minimum_energy_kwh": 8}}]
        self.assertEqual(
            validate_directives(raw, 1, battery)[0].directive_type,
            "minimum_battery_reserve",
        )

    def test_always_one_entry_per_note_in_order(self):
        r = validate_directives([], 3, BATTERY)
        self.assertEqual([d.note_index for d in r], [0, 1, 2])
        self.assertTrue(all(d.directive_type == "no_op" for d in r))

    def test_never_raises_on_garbage(self):
        for junk in (None, "x", 123, [1, 2, 3], [{"note_index": "a"}], [{}], [None]):
            r = validate_directives(junk, 1, BATTERY)
            self.assertEqual(len(r), 1)
            self.assertEqual(r[0].directive_type, "no_op")


if __name__ == "__main__":
    unittest.main(verbosity=2)
