#!/usr/bin/env python3
"""Independent replay tester for POST /optimize-energy.

Mirrors the contest judge: for each sample case it POSTs ``case.input`` to a
running server, then INDEPENDENTLY re-derives every constraint from the raw
scenario and verifies the returned ``hourly_plan`` satisfies it. It also
compares ``directive_interpretation`` (structure only: type + hours + numeric
values, never free-text) against the case's ``expected_output``.

Nothing here imports the app: the replay is deliberately a second, independent
implementation so a passing result means the server's plan is genuinely valid.

Usage:
    uv run scripts/replay_check.py
    uv run scripts/replay_check.py --file path/to/cases.json --url http://localhost:8000
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOURS = 24
BALANCE_TOL = 0.01          # per-hour energy balance tolerance (kWh)
BOUND_TOL = 1e-4            # slack for battery bounds / rate limits
NUM_TOL = 1e-6             # numeric equality for directive values / totals

DEFAULT_FILENAME = "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
DEFAULT_URL = "http://localhost:8000"


# --------------------------------------------------------------------------- #
# Loading                                                                     #
# --------------------------------------------------------------------------- #
def find_cases_file(explicit: str | None) -> Path:
    """Locate the sample-cases JSON: explicit path, else common locations."""
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"Cases file not found: {p}")
        return p
    root = Path(__file__).resolve().parent.parent
    for cand in (root / DEFAULT_FILENAME, root / "scripts" / DEFAULT_FILENAME,
                 Path.cwd() / DEFAULT_FILENAME):
        if cand.is_file():
            return cand
    raise FileNotFoundError(
        f"Could not find {DEFAULT_FILENAME}. Place it at the repo root or pass "
        "--file <path>."
    )


def load_cases(path: Path) -> list[dict]:
    """Return a list of case dicts, tolerating a few container shapes."""
    data = json.loads(path.read_text())
    if isinstance(data, list):
        cases = data
    elif isinstance(data, dict):
        for key in ("cases", "sample_cases", "test_cases", "samples"):
            if isinstance(data.get(key), list):
                cases = data[key]
                break
        else:
            raise ValueError("JSON object has no recognizable cases list.")
    else:
        raise ValueError("Unexpected JSON top-level type.")
    return cases


def _get(d: dict, *keys, default=None):
    for k in keys:
        if k in d:
            return d[k]
    return default


def case_input(case: dict) -> dict:
    return _get(case, "input", "request", "body", default={})


def case_expected(case: dict):
    return _get(case, "expected_output", "expected", "output")


def case_name(case: dict, i: int) -> str:
    return str(_get(case, "name", "id", "title", default=f"case_{i}"))


# --------------------------------------------------------------------------- #
# HTTP                                                                         #
# --------------------------------------------------------------------------- #
def post_optimize(base_url: str, body: dict, timeout: float = 35.0):
    """POST to /optimize-energy. Returns (status_code, parsed_json_or_text)."""
    url = base_url.rstrip("/") + "/optimize-energy"
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST",
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


# --------------------------------------------------------------------------- #
# Independent replay of the plan                                              #
# --------------------------------------------------------------------------- #
def _bat(battery: dict, *keys) -> float:
    """Read a battery field, tolerating both the _kwh request field names and
    the older short names."""
    for k in keys:
        if k in battery:
            return float(battery[k])
    raise KeyError(f"battery missing any of {keys}")


def _derive_params(inp: dict, directives: list[dict]):
    """Re-derive per-hour effective params from the scenario + applied directives."""
    battery = inp["battery"]
    capacity = _bat(battery, "capacity_kwh", "capacity")
    base_reserve = _bat(battery, "minimum_energy_kwh", "minimum_energy")

    by_hour = {int(r["hour"]): r for r in inp["hours"]}
    eff_solar = {h: float(by_hour[h]["solar_kwh"]) for h in range(HOURS)}
    reserve = {h: base_reserve for h in range(HOURS)}
    no_charge, no_discharge = set(), set()
    grid_cap: dict[int, float] = {}

    for d in directives:
        if not d.get("applies"):
            continue
        adj = d.get("structured_adjustment") or {}
        dtype = d.get("directive_type")
        hrs = [int(h) for h in adj.get("hours", [])]
        if dtype == "solar_reduction":
            for h in hrs:
                eff_solar[h] *= float(adj["factor"])
        elif dtype == "minimum_battery_reserve":
            for h in hrs:
                reserve[h] = max(reserve[h], float(adj["minimum_energy_kwh"]))
        elif dtype == "no_charge_window":
            no_charge.update(hrs)
        elif dtype == "no_discharge_window":
            no_discharge.update(hrs)
        elif dtype == "max_grid_window":
            cap = float(adj["max_grid_kwh"])
            for h in hrs:
                grid_cap[h] = min(grid_cap.get(h, cap), cap)

    reserve = {h: min(reserve[h], capacity) for h in range(HOURS)}
    return capacity, eff_solar, reserve, no_charge, no_discharge, grid_cap


def replay_plan(inp: dict, response: dict) -> list[str]:
    """Return a list of failure messages (empty == the plan is valid)."""
    fails: list[str] = []
    plan = response.get("hourly_plan")
    if not isinstance(plan, list):
        return ["response has no hourly_plan list"]

    # 24 unique hours 0..23.
    hours_seen = [p.get("hour") for p in plan]
    if len(plan) != HOURS or sorted(hours_seen) != list(range(HOURS)):
        fails.append(f"hourly_plan must be 24 unique hours 0..23, got {sorted(hours_seen)}")
        return fails
    plan_by_hour = {int(p["hour"]): p for p in plan}

    battery = inp["battery"]
    capacity = _bat(battery, "capacity_kwh", "capacity")
    initial = _bat(battery, "initial_energy_kwh", "initial_energy")
    max_charge = _bat(battery, "max_charge_kwh_per_hour", "max_charge")
    max_discharge = _bat(battery, "max_discharge_kwh_per_hour", "max_discharge")
    by_hour = {int(r["hour"]): r for r in inp["hours"]}

    directives = response.get("directive_interpretation", [])
    _, eff_solar, reserve, no_charge, no_discharge, grid_cap = _derive_params(inp, directives)

    prev = initial
    for h in range(HOURS):
        p = plan_by_hour[h]
        grid = float(p["grid_kwh"])
        solar_used = float(p["solar_used_kwh"])
        after = float(p["battery_energy_after_kwh"])
        net = float(p["battery_kwh"])          # signed: + charge / - discharge
        charge = max(net, 0.0)
        discharge = max(-net, 0.0)
        demand = float(by_hour[h]["demand_kwh"])

        # Energy balance.
        lhs = grid + solar_used + discharge
        rhs = demand + charge
        if abs(lhs - rhs) > BALANCE_TOL:
            fails.append(f"h{h}: balance off by {lhs - rhs:.4f}")
        # Solar cap.
        if solar_used > eff_solar[h] + BALANCE_TOL:
            fails.append(f"h{h}: solar_used {solar_used:.4f} > effective {eff_solar[h]:.4f}")
        if grid < -BOUND_TOL or solar_used < -BOUND_TOL:
            fails.append(f"h{h}: negative grid/solar")
        # Rate limits.
        if charge > max_charge + BOUND_TOL:
            fails.append(f"h{h}: charge {charge:.4f} > max_charge {max_charge}")
        if discharge > max_discharge + BOUND_TOL:
            fails.append(f"h{h}: discharge {discharge:.4f} > max_discharge {max_discharge}")
        # Directive gates.
        if h in no_charge and charge > BOUND_TOL:
            fails.append(f"h{h}: charged during no_charge")
        if h in no_discharge and discharge > BOUND_TOL:
            fails.append(f"h{h}: discharged during no_discharge")
        if h in grid_cap and grid > grid_cap[h] + BOUND_TOL:
            fails.append(f"h{h}: grid {grid:.4f} > max_grid {grid_cap[h]}")
        # State transition + bounds + active reserve.
        if abs(after - (prev + net)) > BOUND_TOL:
            fails.append(f"h{h}: transition {after:.4f} != {prev + net:.4f}")
        if after < reserve[h] - BOUND_TOL:
            fails.append(f"h{h}: battery {after:.4f} < reserve {reserve[h]:.4f}")
        if after > capacity + BOUND_TOL:
            fails.append(f"h{h}: battery {after:.4f} > capacity {capacity}")
        # battery_action consistency.
        action = p.get("battery_action")
        expect = "charge" if net > 1e-6 else "discharge" if net < -1e-6 else "idle"
        if action != expect:
            fails.append(f"h{h}: battery_action '{action}' != '{expect}'")
        prev = after

    # End-of-day battery returns to initial.
    if abs(prev - initial) > BOUND_TOL:
        fails.append(f"end-of-day battery {prev:.4f} != initial {initial}")

    # Totals recomputed from the plan must match the reported totals.
    total_grid = sum(float(plan_by_hour[h]["grid_kwh"]) for h in range(HOURS))
    total_cost = sum(
        float(plan_by_hour[h]["grid_kwh"]) * float(by_hour[h]["tariff_bdt_per_kwh"])
        for h in range(HOURS)
    )
    peak_grid = max(float(plan_by_hour[h]["grid_kwh"]) for h in range(HOURS))
    for label, got, calc in (
        ("total_grid_kwh", response.get("total_grid_kwh"), total_grid),
        ("total_cost_bdt", response.get("total_cost_bdt"), total_cost),
        ("peak_grid_kwh", response.get("peak_grid_kwh"), peak_grid),
    ):
        if got is None or abs(float(got) - calc) > 1e-4:
            fails.append(f"{label} reported {got} != recomputed {calc:.4f}")
    return fails


# --------------------------------------------------------------------------- #
# Directive interpretation comparison                                         #
# --------------------------------------------------------------------------- #
def _extract_directives(obj):
    """Pull a directive list out of an expected_output of varying shape."""
    if obj is None:
        return None
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for key in ("directive_interpretation", "directives", "directive_interpretations"):
            if isinstance(obj.get(key), list):
                return obj[key]
    return None


def _norm_directive(d: dict) -> tuple:
    """Structure-only signature: type + hours + numeric values (no free-text)."""
    dtype = d.get("directive_type")
    adj = d.get("structured_adjustment") or {}
    # Some expected files may inline hours/values at the top level.
    hours = adj.get("hours", d.get("hours"))
    hours_t = tuple(sorted(int(h) for h in hours)) if hours else ()

    def num(*keys):
        for k in keys:
            if k in adj:
                return round(float(adj[k]), 6)
            if k in d and not isinstance(d[k], (dict, list)):
                return round(float(d[k]), 6)
        return None

    return (dtype, hours_t, num("factor"),
            num("minimum_energy_kwh", "reserve"), num("max_grid_kwh"))


def compare_directives(actual: list[dict], expected_output) -> list[str]:
    """Return failure messages comparing actual vs expected directives."""
    expected = _extract_directives(expected_output)
    if expected is None:
        return []  # nothing to compare against for this case
    if len(actual) != len(expected):
        return [f"directive count {len(actual)} != expected {len(expected)}"]

    a_by_idx = {int(d.get("note_index", i)): d for i, d in enumerate(actual)}
    e_by_idx = {int(d.get("note_index", i)): d for i, d in enumerate(expected)}
    fails = []
    for idx in sorted(e_by_idx):
        if idx not in a_by_idx:
            fails.append(f"note {idx}: missing in response")
            continue
        an, en = _norm_directive(a_by_idx[idx]), _norm_directive(e_by_idx[idx])
        if an != en:
            fails.append(f"note {idx}: got {an} != expected {en}")
    return fails


# --------------------------------------------------------------------------- #
# Runner                                                                       #
# --------------------------------------------------------------------------- #
def run_case(base_url: str, case: dict) -> tuple[bool, list[str]]:
    inp = case_input(case)
    status, resp = post_optimize(base_url, inp)
    if status != 200 or not isinstance(resp, dict):
        return False, [f"HTTP {status}: {str(resp)[:120]}"]
    fails = replay_plan(inp, resp)
    fails += compare_directives(resp.get("directive_interpretation", []), case_expected(case))
    return (not fails), fails


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", help=f"Path to {DEFAULT_FILENAME}")
    ap.add_argument("--url", default=DEFAULT_URL, help="Base URL of the running server")
    args = ap.parse_args()

    try:
        path = find_cases_file(args.file)
        cases = load_cases(path)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(f"Loaded {len(cases)} case(s) from {path}")
    print(f"Target: {args.url}/optimize-energy\n")

    name_w = max([len("CASE")] + [len(case_name(c, i)) for i, c in enumerate(cases)])
    print(f"{'CASE'.ljust(name_w)}  RESULT  DETAIL")
    print(f"{'-' * name_w}  ------  ------")

    passed = 0
    for i, case in enumerate(cases):
        name = case_name(case, i)
        try:
            ok, fails = run_case(args.url, case)
        except urllib.error.URLError as e:
            ok, fails = False, [f"connection error: {e.reason} (is the server up?)"]
        passed += ok
        detail = "ok" if ok else "; ".join(fails[:3])
        print(f"{name.ljust(name_w)}  {'PASS' if ok else 'FAIL'}    {detail}")

    print(f"\n{passed}/{len(cases)} cases passed.")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
