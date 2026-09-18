from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from backend.directives import build_hour_constraints
from backend.schemas import (
    DirectiveInterpretation,
    HourlyPlanEntry,
    OptimizeEnergyRequest,
    OptimizeEnergyResponse,
)

TOLERANCE = 0.01
SAMPLE_CASES_PATH = (
    Path(__file__).resolve().parents[1]
    / "BUP_CSE_FEST_2026_Participant_Docs"
    / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
)


def nearly_equal(left: float, right: float, tolerance: float = TOLERANCE) -> bool:
    return abs(left - right) <= tolerance


def within_lower_bound(value: float, lower: float, tolerance: float = TOLERANCE) -> bool:
    return value + tolerance >= lower


def within_upper_bound(value: float, upper: float, tolerance: float = TOLERANCE) -> bool:
    return value - tolerance <= upper


@dataclass
class ReplayResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


def _charge_discharge(entry: HourlyPlanEntry) -> tuple[float, float]:
    if entry.battery_action == "charge":
        return entry.battery_kwh, 0.0
    if entry.battery_action == "discharge":
        return 0.0, entry.battery_kwh
    return 0.0, 0.0


def replay(
    request: OptimizeEnergyRequest,
    response: OptimizeEnergyResponse,
    interpretations: list[DirectiveInterpretation] | None = None,
) -> ReplayResult:
    errors: list[str] = []
    directives = (
        interpretations
        if interpretations is not None
        else response.directive_interpretation
    )
    constraints = build_hour_constraints(request, directives)

    if response.scenario_id != request.scenario_id:
        errors.append(
            f"scenario_id mismatch: request={request.scenario_id} response={response.scenario_id}"
        )

    plan_by_hour = {entry.hour: entry for entry in response.hourly_plan}
    if len(response.hourly_plan) != 24 or set(plan_by_hour) != set(range(24)):
        errors.append("hourly_plan must contain exactly 24 unique hours 0 through 23")
        return ReplayResult(ok=False, errors=errors)

    energy_before = request.battery.initial_energy_kwh
    grid_sum = 0.0
    cost_sum = 0.0
    peak_grid = 0.0

    for hour in range(24):
        entry = plan_by_hour[hour]
        hour_constraints = constraints[hour]
        charge_kwh, discharge_kwh = _charge_discharge(entry)
        prefix = f"hour {hour}"

        if entry.battery_action == "idle" and not nearly_equal(entry.battery_kwh, 0.0):
            errors.append(f"{prefix}: idle requires battery_kwh = 0")

        if entry.battery_action == "charge":
            if not hour_constraints.can_charge:
                errors.append(f"{prefix}: charge forbidden by no_charge_window")
            if not within_upper_bound(entry.battery_kwh, hour_constraints.max_charge_kwh):
                errors.append(
                    f"{prefix}: charge {entry.battery_kwh} exceeds max {hour_constraints.max_charge_kwh}"
                )
            expected_energy = energy_before + entry.battery_kwh
        elif entry.battery_action == "discharge":
            if not hour_constraints.can_discharge:
                errors.append(f"{prefix}: discharge forbidden by no_discharge_window")
            if not within_upper_bound(
                entry.battery_kwh, hour_constraints.max_discharge_kwh
            ):
                errors.append(
                    f"{prefix}: discharge {entry.battery_kwh} exceeds max {hour_constraints.max_discharge_kwh}"
                )
            expected_energy = energy_before - entry.battery_kwh
        else:
            expected_energy = energy_before

        if not nearly_equal(entry.battery_energy_after_kwh, expected_energy):
            errors.append(
                f"{prefix}: battery_energy_after_kwh {entry.battery_energy_after_kwh} != {expected_energy}"
            )

        if not within_lower_bound(entry.battery_energy_after_kwh, hour_constraints.min_energy_kwh):
            errors.append(
                f"{prefix}: battery {entry.battery_energy_after_kwh} below reserve {hour_constraints.min_energy_kwh}"
            )
        if not within_upper_bound(entry.battery_energy_after_kwh, hour_constraints.capacity_kwh):
            errors.append(
                f"{prefix}: battery {entry.battery_energy_after_kwh} above capacity {hour_constraints.capacity_kwh}"
            )

        if entry.solar_used_kwh < -TOLERANCE:
            errors.append(f"{prefix}: solar_used_kwh is negative")
        if not within_upper_bound(entry.solar_used_kwh, hour_constraints.effective_solar_kwh):
            errors.append(
                f"{prefix}: solar_used {entry.solar_used_kwh} exceeds effective solar {hour_constraints.effective_solar_kwh}"
            )

        if entry.grid_kwh < -TOLERANCE:
            errors.append(f"{prefix}: grid_kwh is negative")
        if hour_constraints.grid_cap_kwh is not None and not within_upper_bound(
            entry.grid_kwh, hour_constraints.grid_cap_kwh
        ):
            errors.append(
                f"{prefix}: grid {entry.grid_kwh} exceeds cap {hour_constraints.grid_cap_kwh}"
            )

        supplied = entry.grid_kwh + entry.solar_used_kwh + discharge_kwh
        consumed = hour_constraints.demand_kwh + charge_kwh
        if not nearly_equal(supplied, consumed):
            errors.append(
                f"{prefix}: energy balance {supplied} != {consumed}"
            )

        grid_sum += entry.grid_kwh
        cost_sum += entry.grid_kwh * hour_constraints.tariff_bdt_per_kwh
        peak_grid = max(peak_grid, entry.grid_kwh)
        energy_before = entry.battery_energy_after_kwh

    if not nearly_equal(energy_before, request.battery.initial_energy_kwh):
        errors.append(
            f"end-of-day battery {energy_before} != initial {request.battery.initial_energy_kwh}"
        )
    if not nearly_equal(response.total_grid_kwh, grid_sum):
        errors.append(f"total_grid_kwh {response.total_grid_kwh} != {grid_sum}")
    if not nearly_equal(response.total_cost_bdt, cost_sum):
        errors.append(f"total_cost_bdt {response.total_cost_bdt} != {cost_sum}")
    if not nearly_equal(response.peak_grid_kwh, peak_grid):
        errors.append(f"peak_grid_kwh {response.peak_grid_kwh} != {peak_grid}")

    return ReplayResult(ok=len(errors) == 0, errors=errors)


def check_public_sample_cases(path: Path = SAMPLE_CASES_PATH) -> list[tuple[str, ReplayResult]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    results: list[tuple[str, ReplayResult]] = []
    for case in payload["cases"]:
        request = OptimizeEnergyRequest.model_validate(case["input"])
        response = OptimizeEnergyResponse.model_validate(case["expected_output"])
        results.append((case["id"], replay(request, response)))
    return results


if __name__ == "__main__":
    all_results = check_public_sample_cases()
    failed = 0
    for case_id, result in all_results:
        if result.ok:
            print(f"{case_id}: PASS")
        else:
            failed += 1
            print(f"{case_id}: FAIL")
            for error in result.errors:
                print(f"  - {error}")
    print(f"{len(all_results) - failed}/{len(all_results)} public sample plans replayed cleanly")
    raise SystemExit(1 if failed else 0)
