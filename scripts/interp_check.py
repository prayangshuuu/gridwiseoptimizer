#!/usr/bin/env python3
"""Paraphrase-robustness checker for the interpretation layer.

Runs ``interpret_notes`` (OpenRouter via ``OPENROUTER_API_KEY``) ->
``validate_directives`` over tests/paraphrase_cases.json and checks each result
against the expected directive on structure only: directive_type + hours + numeric
values. Free-text explanations are ignored.

Usage:
    uv run scripts/interp_check.py
    uv run scripts/interp_check.py --file tests/paraphrase_cases.json
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LARGE_CAPACITY = 1e9  # so reserve bounds never coerce a test directive


def _sig(directive_type, hours, factor=None, minimum_energy_kwh=None, max_grid_kwh=None):
    hrs = tuple(sorted(int(h) for h in (hours or [])))
    nums = tuple(None if v is None else round(float(v), 6)
                 for v in (factor, minimum_energy_kwh, max_grid_kwh))
    return (directive_type, hrs, *nums)


def _expected_sig(exp: dict):
    return _sig(exp.get("directive_type"), exp.get("hours"),
                exp.get("factor"), exp.get("minimum_energy_kwh"), exp.get("max_grid_kwh"))


def _actual_sig(directive) -> tuple:
    from gridwise.guardrails import to_interpretation
    d = to_interpretation(directive)
    adj = d.get("structured_adjustment") or {}
    return _sig(d.get("directive_type"), adj.get("hours"),
                adj.get("factor"), adj.get("minimum_energy_kwh"), adj.get("max_grid_kwh"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=str(ROOT / "tests" / "paraphrase_cases.json"))
    ap.add_argument("--delay", type=float, default=0.0,
                    help="Seconds to wait between cases (use on rate-limited "
                         "free tiers, e.g. --delay 5).")
    args = ap.parse_args()

    from gridwise.llm_env import load_dotenv

    load_dotenv()

    import json
    import time
    from gridwise.llm import interpret_notes, LLMError, clear_cache
    from gridwise.guardrails import validate_directives

    battery = {"capacity_kwh": LARGE_CAPACITY, "capacity": LARGE_CAPACITY}

    path = Path(args.file)
    if not path.is_file():
        print(f"ERROR: cases file not found: {path}", file=sys.stderr)
        return 2
    data = json.loads(path.read_text())
    cases = data["cases"] if isinstance(data, dict) else data

    clear_cache()
    print(f"Loaded {len(cases)} case(s) from {path}\n")
    name_w = max([len("CASE")] + [len(c.get("name", "?")) for c in cases])
    print(f"{'CASE'.ljust(name_w)}  RESULT  DETAIL")
    print(f"{'-' * name_w}  ------  ------")

    per_type = defaultdict(lambda: [0, 0])  # type -> [passed, total]
    cases_passed = 0

    for ci, case in enumerate(cases):
        if args.delay and ci:
            time.sleep(args.delay)
        name = case.get("name", "?")
        notes = case["notes"]
        expected = case["expected"]
        try:
            raw = interpret_notes(notes, battery=battery)
        except LLMError as e:
            print(f"{name.ljust(name_w)}  FAIL    LLM: {type(e).__name__}: {str(e)[:80]}")
            continue

        directives = validate_directives(raw, num_notes=len(notes),
                                         battery=battery)

        ok = True
        mism = []
        for i, exp in enumerate(expected):
            per_type[exp.get("directive_type")][1] += 1
            if i < len(directives) and _actual_sig(directives[i]) == _expected_sig(exp):
                per_type[exp.get("directive_type")][0] += 1
            else:
                ok = False
                got = _actual_sig(directives[i]) if i < len(directives) else None
                mism.append(f"note{i} got {got} != {_expected_sig(exp)}")
        cases_passed += ok
        print(f"{name.ljust(name_w)}  {'PASS' if ok else 'FAIL'}    "
              f"{'ok' if ok else '; '.join(mism)[:100]}")

    print(f"\n{cases_passed}/{len(cases)} cases passed.\n")
    print("Per-directive accuracy:")
    for dtype in sorted(per_type):
        p, t = per_type[dtype]
        pct = (100.0 * p / t) if t else 0.0
        print(f"  {dtype:<26} {p}/{t}  ({pct:.0f}%)")

    return 0 if cases_passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
