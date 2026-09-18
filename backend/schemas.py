from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DirectiveType = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]

BatteryAction = Literal["charge", "discharge", "idle"]


class HourEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hour: int = Field(ge=0, le=23)
    demand_kwh: float = Field(ge=0)
    solar_kwh: float = Field(ge=0)
    tariff_bdt_per_kwh: float = Field(ge=0)


class Battery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    capacity_kwh: float = Field(ge=0)
    initial_energy_kwh: float = Field(ge=0)
    minimum_energy_kwh: float = Field(ge=0)
    max_charge_kwh_per_hour: float = Field(ge=0)
    max_discharge_kwh_per_hour: float = Field(ge=0)

    @model_validator(mode="after")
    def check_battery_bounds(self) -> "Battery":
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("minimum_energy_kwh cannot exceed capacity_kwh")
        if not (
            self.minimum_energy_kwh
            <= self.initial_energy_kwh
            <= self.capacity_kwh
        ):
            raise ValueError(
                "initial_energy_kwh must be between minimum_energy_kwh and capacity_kwh"
            )
        return self


class OptimizeEnergyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scenario_id: str = Field(min_length=1)
    operator_notes: list[str] = Field(min_length=1, max_length=3)
    hours: list[HourEntry]
    battery: Battery

    @field_validator("operator_notes")
    @classmethod
    def notes_must_be_nonempty(cls, notes: list[str]) -> list[str]:
        cleaned = [note.strip() for note in notes]
        if any(not note for note in cleaned):
            raise ValueError("operator_notes must contain 1-3 non-empty strings")
        return cleaned

    @field_validator("hours")
    @classmethod
    def hours_must_cover_full_day(cls, hours: list[HourEntry]) -> list[HourEntry]:
        if len(hours) != 24:
            raise ValueError("hours must contain exactly 24 entries")
        seen = [entry.hour for entry in hours]
        if sorted(seen) != list(range(24)):
            raise ValueError("hours must contain unique hour values 0 through 23")
        return sorted(hours, key=lambda entry: entry.hour)


class SolarReductionAdjustment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hours: list[int]
    factor: float = Field(ge=0, le=1)


class MinimumBatteryReserveAdjustment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hours: list[int]
    minimum_energy_kwh: float = Field(ge=0)


class HoursOnlyAdjustment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hours: list[int]


class MaxGridWindowAdjustment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hours: list[int]
    max_grid_kwh: float = Field(ge=0)


StructuredAdjustment = (
    SolarReductionAdjustment
    | MinimumBatteryReserveAdjustment
    | MaxGridWindowAdjustment
    | HoursOnlyAdjustment
)


class DirectiveInterpretation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    note_index: int = Field(ge=0)
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: StructuredAdjustment | None
    explanation: str


class HourlyPlanEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hour: int = Field(ge=0, le=23)
    grid_kwh: float = Field(ge=0)
    solar_used_kwh: float = Field(ge=0)
    battery_action: BatteryAction
    battery_kwh: float = Field(ge=0)
    battery_energy_after_kwh: float = Field(ge=0)


class OptimizeEnergyResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourlyPlanEntry]
    total_grid_kwh: float = Field(ge=0)
    total_cost_bdt: float = Field(ge=0)
    peak_grid_kwh: float = Field(ge=0)
    plan_summary: str


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ErrorResponse(BaseModel):
    status: Literal["error"]
    message: str
