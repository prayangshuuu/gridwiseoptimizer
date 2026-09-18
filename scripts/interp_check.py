#!/usr/bin/env python3
"""Paraphrase-robustness checker for the interpretation layer.

Default mode POSTs each case to ``/optimize-energy`` (same base URL rule as
``replay_check.py``: **localhost:8000 if `/health` is up, else public Heroku**)
and compares ``directive_interpretation`` on structure only.

Use ``--local`` to run ``interpret_notes`` in-process instead (needs
``OPENROUTER_API_KEY`` in the environment).

Usage:
    uv run scripts/interp_check.py
    uv run scripts/interp_check.py --url http://localhost:8000
    uv run scripts/interp_check.py --local
    uv run scripts/interp_check.py --file tests/paraphrase_cases.json --delay 5
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

from base_url import resolve_base_url

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
    if not d.get("applies") or d.get("directive_type") == "no_op":
        return _sig("no_op", [], None, None, None)
    return _sig(d.get("directive_type"), adj.get("hours"),
                adj.get("factor"), adj.get("minimum_energy_kwh"), adj.get("max_grid_kwh"))


def _response_entry_sig(entry: dict) -> tuple:
    if not entry.get("applies") or entry.get("directive_type") == "no_op":
        return _sig("no_op", [], None, None, None)
    adj = entry.get("structured_adjustment") or {}
    return _sig(
        entry.get("directive_type"),
        adj.get("hours"),
        adj.get("factor"),
        adj.get("minimum_energy_kwh"),
        adj.get("max_grid_kwh"),
    )


def _minimal_request(operator_notes: list[str]) -> dict:
    """Synthetic 24h scenario for interpretation-only checks."""
    return {
        "scenario_id": "interp-check",
        "operator_notes": operator_notes,
        "hours": [
            {
                "hour": h,
                "demand_kwh": 10.0,
                "solar_kwh": 5.0,
                "tariff_bdt_per_kwh": 1.0,
            }
            for h in range(24)
        ],
        "battery": {
            "capacity_kwh": 100.0,
            "initial_energy_kwh": 50.0,
            "minimum_energy_kwh": 10.0,
            "max_charge_kwh_per_hour": 20.0,
            "max_discharge_kwh_per_hour": 20.0,
        },
    }


def _post_optimize(base_url: str, body: dict, timeout: float = 120.0) -> tuple[int, dict | str]:
    url = base_url.rstrip("/") + "/optimize-energy"
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def _run_remote_case(
    base_url: str, notes: list[str], expected: list[dict]
) -> tuple[bool, list[str], list[dict] | None]:
    status, resp = _post_optimize(base_url, _minimal_request(notes))
    if status != 200:
        detail = resp if isinstance(resp, str) else json.dumps(resp)[:120]
        return False, [f"HTTP {status}: {detail}"], None
    if not isinstance(resp, dict):
        return False, [f"HTTP {status}: non-JSON body"], None
    interp = resp.get("directive_interpretation")
    if not isinstance(interp, list):
        return False, ["missing directive_interpretation"], None
    if len(interp) != len(notes):
        return False, [f"len {len(interp)} != {len(notes)} notes"], interp

    mism = []
    for i, exp in enumerate(expected):
        if _response_entry_sig(interp[i]) != _expected_sig(exp):
            mism.append(
                f"note{i} got {_response_entry_sig(interp[i])} != {_expected_sig(exp)}"
            )
    return (not mism, mism, interp)


def _run_local_case(
    notes: list[str], expected: list[dict], battery: dict
) -> tuple[bool, list[str], object | None]:
    from gridwise.llm import interpret_notes, LLMError
    from gridwise.guardrails import validate_directives

    try:
        raw = interpret_notes(notes, battery=battery)
    except LLMError as e:
        return False, [f"LLM: {type(e).__name__}: {str(e)[:80]}"], None

    directives = validate_directives(raw, num_notes=len(notes), battery=battery)
    mism = []
    for i, exp in enumerate(expected):
        if i >= len(directives) or _actual_sig(directives[i]) != _expected_sig(exp):
            got = _actual_sig(directives[i]) if i < len(directives) else None
            mism.append(f"note{i} got {got} != {_expected_sig(exp)}")
    return (not mism, mism, directives)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=str(ROOT / "tests" / "paraphrase_cases.json"))
    ap.add_argument(
        "--url",
        default=None,
        help="Base URL for POST /optimize-energy (default: local if up, else Heroku)",
    )
    ap.add_argument(
        "--local",
        action="store_true",
        help="Run interpret_notes in-process instead of HTTP (needs OPENROUTER_API_KEY).",
    )
    ap.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Seconds to wait between cases (use on rate-limited tiers, e.g. --delay 5).",
    )
    args = ap.parse_args()

    import time

    path = Path(args.file)
    if not path.is_file():
        print(f"ERROR: cases file not found: {path}", file=sys.stderr)
        return 2
    data = json.loads(path.read_text())
    cases = data["cases"] if isinstance(data, dict) else data

    if args.local:
        from gridwise.llm import clear_cache
        from gridwise.llm_env import load_dotenv

        load_dotenv()
        clear_cache()
        mode = "local interpret_notes"
        base_url = None
    else:
        base_url = resolve_base_url(args.url)
        mode = base_url.rstrip("/")

    battery = {"capacity_kwh": LARGE_CAPACITY, "capacity": LARGE_CAPACITY}

    print(f"Loaded {len(cases)} case(s) from {path}")
    print(f"Target: {mode}\n")

    name_w = max([len("CASE")] + [len(c.get("name", "?")) for c in cases])
    print(f"{'CASE'.ljust(name_w)}  RESULT  DETAIL")
    print(f"{'-' * name_w}  ------  ------")

    per_type = defaultdict(lambda: [0, 0])
    cases_passed = 0

    for ci, case in enumerate(cases):
        if args.delay and ci:
            time.sleep(args.delay)
        name = case.get("name", "?")
        notes = case["notes"]
        expected = case["expected"]

        if args.local:
            ok, mism, directives = _run_local_case(notes, expected, battery)
            if directives is None:
                for exp in expected:
                    per_type[exp.get("directive_type")][1] += 1
            else:
                for i, exp in enumerate(expected):
                    per_type[exp.get("directive_type")][1] += 1
                    if i < len(directives) and _actual_sig(directives[i]) == _expected_sig(exp):
                        per_type[exp.get("directive_type")][0] += 1
        else:
            ok, mism, interp = _run_remote_case(base_url, notes, expected)
            for i, exp in enumerate(expected):
                per_type[exp.get("directive_type")][1] += 1
                if (
                    interp is not None
                    and i < len(interp)
                    and _response_entry_sig(interp[i]) == _expected_sig(exp)
                ):
                    per_type[exp.get("directive_type")][0] += 1

        cases_passed += ok
        detail = "ok" if ok else "; ".join(mism)[:100]
        print(f"{name.ljust(name_w)}  {'PASS' if ok else 'FAIL'}    {detail}")

    print(f"\n{cases_passed}/{len(cases)} cases passed.\n")
    print("Per-directive accuracy:")
    for dtype in sorted(per_type):
        p, t = per_type[dtype]
        pct = (100.0 * p / t) if t else 0.0
        print(f"  {dtype:<26} {p}/{t}  ({pct:.0f}%)")

    return 0 if cases_passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
