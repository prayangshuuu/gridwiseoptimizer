"""Linear program for the 24-hour energy plan.

`optimize` builds and solves a cost-minimizing LP with PuLP/CBC. It is
deterministic and self-contained so an independent judge can replay it from the
same inputs. It consumes only validated directives (see gridwise.guardrails);
it does not trust or call the LLM.

Model (per hour h in 0..23), all variables >= 0:
    grid[h], solar_used[h], charge[h], discharge[h]

Objective:      minimize  sum_h grid[h] * tariff[h]
Balance:        grid[h] + solar_used[h] + discharge[h] == demand[h] + charge[h]
Solar cap:      solar_used[h] <= effective_solar[h]      (solar_reduction applied)
Battery state:  after[h] == after[h-1] + charge[h] - discharge[h], after[-1]=initial
Reserve/cap:    reserve[h] <= after[h] <= capacity
Charge cap:     charge[h] <= max_charge   (0 in no_charge hours)
Discharge cap:  discharge[h] <= max_discharge   (0 in no_discharge hours)
Grid cap:       grid[h] <= max_grid_kwh   (only in max_grid hours)
Cycle close:    after[23] == initial
"""
from __future__ import annotations

import shutil

import pulp

HOURS = 24
_EPS = 1e-6


class OptimizerError(Exception):
    """Raised when the LP cannot be solved (e.g. infeasible constraints)."""


def _cbc_solver():
    """Return a silent CBC solver.

    Prefer a native ``cbc`` on PATH (needed on platforms where PuLP's bundled
    binary is the wrong CPU architecture, e.g. Apple Silicon); otherwise use
    the bundled CBC (the case on the Linux deploy target)."""
    system_cbc = shutil.which("cbc")
    if system_cbc:
        return pulp.COIN_CMD(path=system_cbc, msg=0)
    return pulp.PULP_CBC_CMD(msg=0)


def _hourly_arrays(hours):
    """Index the validated hour rows by hour 0..23 into parallel lists."""
    by_hour = {row["hour"]: row for row in hours}
    demand = [float(by_hour[h]["demand_kwh"]) for h in range(HOURS)]
    solar = [float(by_hour[h]["solar_kwh"]) for h in range(HOURS)]
    tariff = [float(by_hour[h]["tariff_bdt_per_kwh"]) for h in range(HOURS)]
    return demand, solar, tariff


def _apply_directives(directives, base_solar, base_reserve, capacity):
    """Fold directives into per-hour parameters.

    Returns:
        effective_solar[h], reserve[h], no_charge_hours, no_discharge_hours,
        grid_cap[h] (or None for uncapped).
    """
    effective_solar = list(base_solar)
    reserve = [base_reserve] * HOURS
    no_charge = [False] * HOURS
    no_discharge = [False] * HOURS
    grid_cap: list[float | None] = [None] * HOURS

    for d in directives:
        if not d.applies:
            continue
        if d.directive_type == "solar_reduction":
            for h in d.hours:
                effective_solar[h] *= d.factor
        elif d.directive_type == "minimum_battery_reserve":
            for h in d.hours:
                reserve[h] = max(reserve[h], d.reserve)
        elif d.directive_type == "no_charge":
            for h in d.hours:
                no_charge[h] = True
        elif d.directive_type == "no_discharge":
            for h in d.hours:
                no_discharge[h] = True
        elif d.directive_type == "max_grid":
            for h in d.hours:
                cap = d.max_grid_kwh
                grid_cap[h] = cap if grid_cap[h] is None else min(grid_cap[h], cap)

    # A reserve can never exceed capacity in the model; clamp to keep feasible
    # bounds well-formed (guardrails already bounds directive reserves).
    reserve = [min(r, capacity) for r in reserve]
    return effective_solar, reserve, no_charge, no_discharge, grid_cap


def optimize(hours, battery, directives):
    """Solve the 24-hour cost-minimizing dispatch LP.

    Args:
        hours: list of 24 validated hour rows (hour, demand_kwh, solar_kwh,
            tariff_bdt_per_kwh).
        battery: dict with capacity, initial_energy, minimum_energy, max_charge,
            max_discharge (all >= 0).
        directives: list of gridwise.guardrails.Directive.

    Returns:
        list of 24 dicts: hour, grid_kwh, solar_used_kwh, battery_action,
        battery_kwh (signed: + charge / - discharge), battery_energy_after_kwh.

    Raises:
        OptimizerError: if the LP is infeasible or the solver does not reach an
            optimal solution.
    """
    demand, solar, tariff = _hourly_arrays(hours)

    capacity = float(battery["capacity"])
    initial = float(battery["initial_energy"])
    base_reserve = float(battery["minimum_energy"])
    max_charge = float(battery["max_charge"])
    max_discharge = float(battery["max_discharge"])

    effective_solar, reserve, no_charge, no_discharge, grid_cap = _apply_directives(
        directives, solar, base_reserve, capacity
    )

    prob = pulp.LpProblem("gridwise_dispatch", pulp.LpMinimize)

    grid, solar_used, charge, discharge, after = {}, {}, {}, {}, {}
    for h in range(HOURS):
        grid[h] = pulp.LpVariable(f"grid_{h}", lowBound=0)
        solar_used[h] = pulp.LpVariable(f"solar_used_{h}", lowBound=0)
        charge[h] = pulp.LpVariable(
            f"charge_{h}", lowBound=0,
            upBound=0.0 if no_charge[h] else max_charge,
        )
        discharge[h] = pulp.LpVariable(
            f"discharge_{h}", lowBound=0,
            upBound=0.0 if no_discharge[h] else max_discharge,
        )
        after[h] = pulp.LpVariable(f"after_{h}", lowBound=0, upBound=capacity)

    # Objective: total grid cost.
    prob += pulp.lpSum(grid[h] * tariff[h] for h in range(HOURS))

    for h in range(HOURS):
        # Energy balance: supply meets demand plus what we store.
        prob += (
            grid[h] + solar_used[h] + discharge[h] == demand[h] + charge[h],
            f"balance_{h}",
        )
        # Solar usable is capped by (reduced) available solar.
        prob += solar_used[h] <= effective_solar[h], f"solar_cap_{h}"
        # Battery state of charge recursion.
        prev = initial if h == 0 else after[h - 1]
        prob += after[h] == prev + charge[h] - discharge[h], f"soc_{h}"
        # Reserve floor (upper bound = capacity is on the variable already).
        prob += after[h] >= reserve[h], f"reserve_{h}"
        # Grid import cap where requested.
        if grid_cap[h] is not None:
            prob += grid[h] <= grid_cap[h], f"grid_cap_{h}"

    # Close the cycle: end where we started.
    prob += after[HOURS - 1] == initial, "cycle_close"

    status = prob.solve(_cbc_solver())
    if pulp.LpStatus[status] != "Optimal":
        raise OptimizerError(
            f"No optimal dispatch found (status: {pulp.LpStatus[status]}); "
            "constraints may be infeasible."
        )

    plan = []
    for h in range(HOURS):
        c = pulp.value(charge[h]) or 0.0
        d = pulp.value(discharge[h]) or 0.0
        after_val = pulp.value(after[h]) or 0.0
        net = c - d  # signed battery flow: + charge, - discharge

        if c > _EPS and d <= _EPS:
            action = "charge"
        elif d > _EPS and c <= _EPS:
            action = "discharge"
        elif c > _EPS and d > _EPS:  # both active: classify by net flow
            action = "charge" if net > _EPS else "discharge" if net < -_EPS else "idle"
        else:
            action = "idle"
        if action == "idle":
            net = 0.0

        plan.append({
            "hour": h,
            "grid_kwh": round(pulp.value(grid[h]) or 0.0, 6),
            "solar_used_kwh": round(pulp.value(solar_used[h]) or 0.0, 6),
            "battery_action": action,
            "battery_kwh": round(net, 6),
            "battery_energy_after_kwh": round(after_val, 6),
        })
    return plan
