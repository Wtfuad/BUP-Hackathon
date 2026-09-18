from __future__ import annotations

import json
from pathlib import Path

import pulp

from backend.directives import HourConstraints, build_hour_constraints
from backend.replay import SAMPLE_CASES_PATH, TOLERANCE, nearly_equal, replay
from backend.schemas import (
    DirectiveInterpretation,
    HourlyPlanEntry,
    OptimizeEnergyRequest,
    OptimizeEnergyResponse,
)

ROUND_DIGITS = 6
NET_EPS = 1e-6


class OptimizationError(RuntimeError):
    pass


def _round(value: float) -> float:
    return round(float(value), ROUND_DIGITS)


def _solver_value(variable: pulp.LpVariable) -> float:
    value = variable.value()
    if value is None:
        raise OptimizationError(f"solver returned no value for {variable.name}")
    return float(value)


def _plan_summary(interpretations: list[DirectiveInterpretation], cost: float) -> str:
    applied = [
        item.directive_type
        for item in interpretations
        if item.applies and item.directive_type != "no_op"
    ]
    if applied:
        return (
            "Applied "
            + ", ".join(applied)
            + f"; minimized 24-hour grid cost to {cost:.2f} BDT."
        )
    return f"No operator constraints applied; minimized 24-hour grid cost to {cost:.2f} BDT."


def optimize(
    request: OptimizeEnergyRequest,
    interpretations: list[DirectiveInterpretation],
) -> OptimizeEnergyResponse:
    constraints = build_hour_constraints(request, interpretations)
    battery = request.battery
    problem = pulp.LpProblem("gridwise", pulp.LpMinimize)

    grid = [pulp.LpVariable(f"grid_{hour}", lowBound=0) for hour in range(24)]
    solar = [pulp.LpVariable(f"solar_{hour}", lowBound=0) for hour in range(24)]
    charge = [pulp.LpVariable(f"charge_{hour}", lowBound=0) for hour in range(24)]
    discharge = [pulp.LpVariable(f"discharge_{hour}", lowBound=0) for hour in range(24)]
    energy = [pulp.LpVariable(f"energy_{hour}", lowBound=0) for hour in range(24)]

    problem += pulp.lpSum(
        grid[hour] * constraints[hour].tariff_bdt_per_kwh for hour in range(24)
    )

    for hour, hour_constraints in enumerate(constraints):
        _add_hour_constraints(
            problem,
            hour,
            hour_constraints,
            battery.initial_energy_kwh,
            grid[hour],
            solar[hour],
            charge[hour],
            discharge[hour],
            energy[hour],
            energy[hour - 1] if hour else None,
        )

    problem += energy[23] == battery.initial_energy_kwh

    status = problem.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=10, gapRel=0))
    if pulp.LpStatus[status] != "Optimal":
        raise OptimizationError(f"optimizer status={pulp.LpStatus[status]}")

    hourly_plan = _build_hourly_plan(
        battery.initial_energy_kwh,
        constraints,
        grid,
        solar,
        charge,
        discharge,
    )
    total_grid = _round(sum(entry.grid_kwh for entry in hourly_plan))
    total_cost = _round(
        sum(
            entry.grid_kwh * constraints[entry.hour].tariff_bdt_per_kwh
            for entry in hourly_plan
        )
    )
    peak_grid = _round(max(entry.grid_kwh for entry in hourly_plan))

    return OptimizeEnergyResponse(
        scenario_id=request.scenario_id,
        directive_interpretation=interpretations,
        hourly_plan=hourly_plan,
        total_grid_kwh=total_grid,
        total_cost_bdt=total_cost,
        peak_grid_kwh=peak_grid,
        plan_summary=_plan_summary(interpretations, total_cost),
    )


def _add_hour_constraints(
    problem: pulp.LpProblem,
    hour: int,
    hour_constraints: HourConstraints,
    initial_energy: float,
    grid: pulp.LpVariable,
    solar: pulp.LpVariable,
    charge: pulp.LpVariable,
    discharge: pulp.LpVariable,
    energy: pulp.LpVariable,
    previous_energy: pulp.LpVariable | None,
) -> None:
    problem += (
        grid + solar + discharge == hour_constraints.demand_kwh + charge,
        f"balance_{hour}",
    )
    problem += solar <= hour_constraints.effective_solar_kwh, f"solar_{hour}"
    problem += energy <= hour_constraints.capacity_kwh, f"capacity_{hour}"
    problem += energy >= hour_constraints.min_energy_kwh, f"reserve_{hour}"

    if previous_energy is None:
        problem += energy == initial_energy + charge - discharge, f"state_{hour}"
    else:
        problem += energy == previous_energy + charge - discharge, f"state_{hour}"

    if hour_constraints.can_charge:
        problem += charge <= hour_constraints.max_charge_kwh, f"charge_rate_{hour}"
    else:
        problem += charge == 0, f"no_charge_{hour}"

    if hour_constraints.can_discharge:
        problem += discharge <= hour_constraints.max_discharge_kwh, f"discharge_rate_{hour}"
    else:
        problem += discharge == 0, f"no_discharge_{hour}"

    if hour_constraints.grid_cap_kwh is not None:
        problem += grid <= hour_constraints.grid_cap_kwh, f"grid_cap_{hour}"


def _build_hourly_plan(
    initial_energy: float,
    constraints: list[HourConstraints],
    grid: list[pulp.LpVariable],
    solar: list[pulp.LpVariable],
    charge: list[pulp.LpVariable],
    discharge: list[pulp.LpVariable],
) -> list[HourlyPlanEntry]:
    energy = initial_energy
    plan: list[HourlyPlanEntry] = []
    for hour in range(24):
        grid_kwh = max(0.0, _round(_solver_value(grid[hour])))
        solar_used = max(0.0, _round(_solver_value(solar[hour])))
        net = _round(_solver_value(charge[hour]) - _solver_value(discharge[hour]))
        if net > NET_EPS:
            action = "charge"
            battery_kwh = net
            energy = _round(energy + battery_kwh)
        elif net < -NET_EPS:
            action = "discharge"
            battery_kwh = -net
            energy = _round(energy - battery_kwh)
        else:
            action = "idle"
            battery_kwh = 0.0
        solar_cap = constraints[hour].effective_solar_kwh
        if solar_used > solar_cap:
            solar_used = _round(solar_cap)
        plan.append(
            HourlyPlanEntry(
                hour=hour,
                grid_kwh=grid_kwh,
                solar_used_kwh=solar_used,
                battery_action=action,
                battery_kwh=battery_kwh,
                battery_energy_after_kwh=energy,
            )
        )
    return plan


def check_optimizer_on_public_samples(
    path: Path = SAMPLE_CASES_PATH,
) -> list[tuple[str, bool, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    reports: list[tuple[str, bool, str]] = []
    for case in payload["cases"]:
        request = OptimizeEnergyRequest.model_validate(case["input"])
        expected = OptimizeEnergyResponse.model_validate(case["expected_output"])
        result = optimize(request, expected.directive_interpretation)
        replay_result = replay(request, result, expected.directive_interpretation)
        cost_ok = nearly_equal(result.total_cost_bdt, expected.total_cost_bdt) or (
            result.total_cost_bdt <= expected.total_cost_bdt + TOLERANCE
        )
        ok = replay_result.ok and cost_ok
        detail = (
            f"cost ours={result.total_cost_bdt:.4f} "
            f"sample={expected.total_cost_bdt:.4f} "
            f"replay={'ok' if replay_result.ok else replay_result.errors}"
        )
        reports.append((case["id"], ok, detail))
    return reports


if __name__ == "__main__":
    failed = 0
    for case_id, ok, detail in check_optimizer_on_public_samples():
        print(f"{case_id}: {'PASS' if ok else 'FAIL'} ({detail})")
        if not ok:
            failed += 1
    raise SystemExit(1 if failed else 0)
