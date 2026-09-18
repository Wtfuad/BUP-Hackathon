from __future__ import annotations

import json
import math
from typing import Any

from backend.replay import SAMPLE_CASES_PATH
from backend.schemas import (
    DirectiveInterpretation,
    DirectiveType,
    HoursOnlyAdjustment,
    MaxGridWindowAdjustment,
    MinimumBatteryReserveAdjustment,
    OptimizeEnergyRequest,
    OptimizeEnergyResponse,
    SolarReductionAdjustment,
)

ALLOWED_TYPES: set[str] = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}


class GuardrailError(ValueError):
    """LLM output is malformed or violates Section 08. Do not map this to no_op."""


def validate_interpretations(
    raw_items: list[Any],
    request: OptimizeEnergyRequest,
) -> list[DirectiveInterpretation]:
    note_count = len(request.operator_notes)
    if not isinstance(raw_items, list):
        raise GuardrailError("directive_interpretation must be a list")
    if len(raw_items) != note_count:
        raise GuardrailError(
            f"expected {note_count} interpretation(s), got {len(raw_items)}"
        )

    parsed = [_parse_raw_item(item) for item in raw_items]
    by_index = {item["note_index"]: item for item in parsed}
    if set(by_index) != set(range(note_count)):
        raise GuardrailError(
            f"note_index values must be 0..{note_count - 1} with no duplicates"
        )

    validated: list[DirectiveInterpretation] = []
    for note_index in range(note_count):
        validated.append(
            _validate_one(by_index[note_index], request.battery.capacity_kwh)
        )
    return validated


def _parse_raw_item(item: Any) -> dict[str, Any]:
    if isinstance(item, DirectiveInterpretation):
        item = item.model_dump()
    if not isinstance(item, dict):
        raise GuardrailError("each interpretation must be an object")

    if "note_index" not in item:
        raise GuardrailError("note_index is required")
    try:
        note_index = int(item["note_index"])
    except (TypeError, ValueError) as exc:
        raise GuardrailError("note_index must be an integer") from exc

    directive_type = item.get("directive_type")
    if directive_type not in ALLOWED_TYPES:
        raise GuardrailError(f"unsupported directive_type: {directive_type}")

    if "applies" not in item:
        raise GuardrailError("applies is required")
    applies = item["applies"]
    if not isinstance(applies, bool):
        raise GuardrailError("applies must be a boolean")

    explanation = item.get("explanation", "")
    if explanation is None:
        explanation = ""
    if not isinstance(explanation, str):
        raise GuardrailError("explanation must be a string")

    return {
        "note_index": note_index,
        "applies": applies,
        "directive_type": directive_type,
        "structured_adjustment": item.get("structured_adjustment"),
        "explanation": explanation.strip() or "Interpreted operator note.",
    }


def _as_noop(item: dict[str, Any]) -> DirectiveInterpretation:
    """Drop one illegal note. Do not fail the rest of the request."""
    return DirectiveInterpretation(
        note_index=item["note_index"],
        applies=False,
        directive_type="no_op",
        structured_adjustment=None,
        explanation=item["explanation"] or "Invalid operator constraint ignored.",
    )


def _validate_one(item: dict[str, Any], capacity_kwh: float) -> DirectiveInterpretation:
    directive_type: DirectiveType = item["directive_type"]
    applies: bool = item["applies"]
    adjustment = item["structured_adjustment"]

    if directive_type == "no_op":
        if applies:
            raise GuardrailError("no_op requires applies=false")
        if adjustment is not None:
            raise GuardrailError("no_op requires structured_adjustment=null")
        return DirectiveInterpretation(
            note_index=item["note_index"],
            applies=False,
            directive_type="no_op",
            structured_adjustment=None,
            explanation=item["explanation"],
        )

    if not applies:
        raise GuardrailError(f"{directive_type} requires applies=true")
    if not isinstance(adjustment, dict):
        raise GuardrailError(f"{directive_type} requires a structured_adjustment object")

    hours = _normalize_hours(adjustment.get("hours"))
    if not hours:
        return _as_noop(item)

    structured: (
        SolarReductionAdjustment
        | MinimumBatteryReserveAdjustment
        | MaxGridWindowAdjustment
        | HoursOnlyAdjustment
    )
    if directive_type == "solar_reduction":
        try:
            factor = _finite_number(adjustment.get("factor"), "factor")
        except GuardrailError:
            return _as_noop(item)
        if not 0.0 <= factor <= 1.0:
            return _as_noop(item)
        structured = SolarReductionAdjustment(hours=hours, factor=factor)
    elif directive_type == "minimum_battery_reserve":
        try:
            reserve = _finite_number(
                adjustment.get("minimum_energy_kwh"), "minimum_energy_kwh"
            )
        except GuardrailError:
            return _as_noop(item)
        if reserve < 0:
            return _as_noop(item)
        if reserve > capacity_kwh:
            reserve = capacity_kwh
        structured = MinimumBatteryReserveAdjustment(
            hours=hours,
            minimum_energy_kwh=reserve,
        )
    elif directive_type == "max_grid_window":
        try:
            cap = _finite_number(adjustment.get("max_grid_kwh"), "max_grid_kwh")
        except GuardrailError:
            return _as_noop(item)
        if cap < 0:
            return _as_noop(item)
        structured = MaxGridWindowAdjustment(hours=hours, max_grid_kwh=cap)
    elif directive_type in {"no_charge_window", "no_discharge_window"}:
        extra_keys = set(adjustment.keys()) - {"hours"}
        # Extra keys are ignored; hours-only shape is required.
        structured = HoursOnlyAdjustment(hours=hours)
        _ = extra_keys
    else:
        raise GuardrailError(f"unsupported directive_type: {directive_type}")

    return DirectiveInterpretation(
        note_index=item["note_index"],
        applies=True,
        directive_type=directive_type,
        structured_adjustment=structured,
        explanation=item["explanation"],
    )


def _normalize_hours(raw_hours: Any) -> list[int]:
    if not isinstance(raw_hours, list):
        raise GuardrailError("hours must be a list")
    hours: list[int] = []
    for value in raw_hours:
        try:
            hour = _coerce_hour(value)
        except GuardrailError:
            continue
        if 0 <= hour <= 23:
            hours.append(hour)
    return sorted(set(hours))


def _coerce_hour(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        raise GuardrailError(f"invalid hour value: {value}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return int(text)
        if ":" in text:
            prefix = text.split(":", 1)[0]
            if prefix.isdigit():
                return int(prefix)
    raise GuardrailError(f"hour values must be integers 0..23, got {value!r}")


def _finite_number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise GuardrailError(f"{name} must be a number") from exc
    if not math.isfinite(number):
        raise GuardrailError(f"{name} must be finite")
    return number


def _expect_error(raw: list[Any], request: OptimizeEnergyRequest, fragment: str) -> str:
    try:
        validate_interpretations(raw, request)
    except GuardrailError as exc:
        if fragment.lower() not in str(exc).lower():
            return f"FAIL expected error containing {fragment!r}, got {exc}"
        return "PASS"
    return f"FAIL expected GuardrailError containing {fragment!r}"


if __name__ == "__main__":
    payload = json.loads(SAMPLE_CASES_PATH.read_text(encoding="utf-8"))
    failed = 0
    for case in payload["cases"]:
        request = OptimizeEnergyRequest.model_validate(case["input"])
        expected = OptimizeEnergyResponse.model_validate(case["expected_output"])
        try:
            validated = validate_interpretations(
                expected.directive_interpretation, request
            )
            types = [item.directive_type for item in validated]
            print(f"{case['id']}: PASS gold guardrails {types}")
        except GuardrailError as exc:
            failed += 1
            print(f"{case['id']}: FAIL gold guardrails ({exc})")

    sample = payload["cases"][0]
    request = OptimizeEnergyRequest.model_validate(sample["input"])
    gold = OptimizeEnergyResponse.model_validate(sample["expected_output"])
    unsorted = gold.directive_interpretation[0].model_dump()
    unsorted["structured_adjustment"]["hours"] = [13, 12, 12]
    normalized = validate_interpretations(
        [unsorted, gold.directive_interpretation[1].model_dump()],
        request,
    )
    if normalized[0].structured_adjustment.hours == [12, 13]:
        print("NORMALIZE: PASS unsorted hours -> [12, 13]")
    else:
        failed += 1
        print("NORMALIZE: FAIL hours were not sorted unique")

    negative_cases = [
        (
            [{"note_index": 0, "applies": True, "directive_type": "invented", "structured_adjustment": None, "explanation": "x"}],
            "unsupported",
        ),
        (
            [
                {
                    "note_index": 0,
                    "applies": False,
                    "directive_type": "no_op",
                    "structured_adjustment": {"hours": [1]},
                    "explanation": "x",
                },
                gold.directive_interpretation[1].model_dump(),
            ],
            "structured_adjustment=null",
        ),
        (
            [
                {
                    "note_index": 0,
                    "applies": True,
                    "directive_type": "solar_reduction",
                    "structured_adjustment": {"hours": "13-14", "factor": 0.2},
                    "explanation": "x",
                },
                gold.directive_interpretation[1].model_dump(),
            ],
            "hours must be a list",
        ),
    ]
    # SAMPLE-01 has 2 notes, so first negative case needs 2 entries too.
    negative_cases[0] = (
        [
            {
                "note_index": 0,
                "applies": True,
                "directive_type": "invented",
                "structured_adjustment": None,
                "explanation": "x",
            },
            gold.directive_interpretation[1].model_dump(),
        ],
        "unsupported",
    )
    for raw, fragment in negative_cases:
        status = _expect_error(raw, request, fragment)
        print(f"REJECT: {status} ({fragment})")
        if status != "PASS":
            failed += 1

    raise SystemExit(1 if failed else 0)
