from dataclasses import dataclass

from backend.schemas import (
    DirectiveInterpretation,
    MaxGridWindowAdjustment,
    MinimumBatteryReserveAdjustment,
    OptimizeEnergyRequest,
    SolarReductionAdjustment,
)


@dataclass
class HourConstraints:
    demand_kwh: float
    tariff_bdt_per_kwh: float
    effective_solar_kwh: float
    min_energy_kwh: float
    capacity_kwh: float
    max_charge_kwh: float
    max_discharge_kwh: float
    can_charge: bool
    can_discharge: bool
    grid_cap_kwh: float | None


def build_hour_constraints(
    request: OptimizeEnergyRequest,
    interpretations: list[DirectiveInterpretation],
) -> list[HourConstraints]:
    battery = request.battery
    hours = request.hours
    effective_solar = [entry.solar_kwh for entry in hours]
    min_energy = [battery.minimum_energy_kwh] * 24
    can_charge = [True] * 24
    can_discharge = [True] * 24
    grid_cap: list[float | None] = [None] * 24

    for item in interpretations:
        if not item.applies or item.directive_type == "no_op":
            continue
        if item.structured_adjustment is None:
            continue

        listed_hours = item.structured_adjustment.hours
        if item.directive_type == "solar_reduction":
            if not isinstance(item.structured_adjustment, SolarReductionAdjustment):
                continue
            factor = item.structured_adjustment.factor
            for hour in listed_hours:
                effective_solar[hour] *= factor
        elif item.directive_type == "minimum_battery_reserve":
            if not isinstance(item.structured_adjustment, MinimumBatteryReserveAdjustment):
                continue
            reserve = item.structured_adjustment.minimum_energy_kwh
            for hour in listed_hours:
                min_energy[hour] = max(min_energy[hour], reserve)
        elif item.directive_type == "no_charge_window":
            for hour in listed_hours:
                can_charge[hour] = False
        elif item.directive_type == "no_discharge_window":
            for hour in listed_hours:
                can_discharge[hour] = False
        elif item.directive_type == "max_grid_window":
            if not isinstance(item.structured_adjustment, MaxGridWindowAdjustment):
                continue
            cap = item.structured_adjustment.max_grid_kwh
            for hour in listed_hours:
                current = grid_cap[hour]
                grid_cap[hour] = cap if current is None else min(current, cap)

    return [
        HourConstraints(
            demand_kwh=hours[hour].demand_kwh,
            tariff_bdt_per_kwh=hours[hour].tariff_bdt_per_kwh,
            effective_solar_kwh=effective_solar[hour],
            min_energy_kwh=min_energy[hour],
            capacity_kwh=battery.capacity_kwh,
            max_charge_kwh=battery.max_charge_kwh_per_hour,
            max_discharge_kwh=battery.max_discharge_kwh_per_hour,
            can_charge=can_charge[hour],
            can_discharge=can_discharge[hour],
            grid_cap_kwh=grid_cap[hour],
        )
        for hour in range(24)
    ]
