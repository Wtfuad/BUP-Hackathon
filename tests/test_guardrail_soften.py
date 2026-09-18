"""Validator safety net: illegal LLM numbers become no_op, gold samples stay intact.

No Groq calls. Official SAMPLE gold still has to pass unchanged.
"""
from __future__ import annotations

import json
import unittest

from backend.replay import SAMPLE_CASES_PATH
from backend.schemas import OptimizeEnergyRequest, OptimizeEnergyResponse
from backend.validator import GuardrailError, validate_interpretations

PAYLOAD = json.loads(SAMPLE_CASES_PATH.read_text(encoding="utf-8"))
HOURS = PAYLOAD["cases"][0]["input"]["hours"]
BATTERY = PAYLOAD["cases"][0]["input"]["battery"]


def _request(*notes: str) -> OptimizeEnergyRequest:
    return OptimizeEnergyRequest.model_validate(
        {
            "scenario_id": "SOFTEN",
            "operator_notes": list(notes),
            "hours": HOURS,
            "battery": BATTERY,
        }
    )


def _item(directive_type: str, hours, extra=None):
    adj = {"hours": hours}
    if extra:
        adj.update(extra)
    return {
        "note_index": 0,
        "applies": True,
        "directive_type": directive_type,
        "structured_adjustment": adj,
        "explanation": "x",
    }


class GuardrailSoftenTests(unittest.TestCase):
    def test_gold_samples_unchanged(self) -> None:
        for case in PAYLOAD["cases"]:
            request = OptimizeEnergyRequest.model_validate(case["input"])
            expected = OptimizeEnergyResponse.model_validate(case["expected_output"])
            got = validate_interpretations(expected.directive_interpretation, request)
            types = [item.directive_type for item in got]
            want = [item.directive_type for item in expected.directive_interpretation]
            self.assertEqual(types, want, case["id"])
            for actual, gold in zip(got, expected.directive_interpretation):
                if gold.directive_type == "no_op":
                    continue
                self.assertEqual(
                    actual.structured_adjustment.hours,
                    gold.structured_adjustment.hours,
                    case["id"],
                )

    def test_invalid_factor_becomes_noop(self) -> None:
        req = _request("bad")
        for factor in (1.2, 1.5, 2.5, -0.2, 200, float("inf"), float("nan")):
            got = validate_interpretations(
                [_item("solar_reduction", [13], {"factor": factor})], req
            )[0]
            self.assertEqual(got.directive_type, "no_op", factor)

    def test_negative_reserve_and_grid_become_noop(self) -> None:
        req = _request("bad")
        got = validate_interpretations(
            [_item("minimum_battery_reserve", [14], {"minimum_energy_kwh": -50})], req
        )[0]
        self.assertEqual(got.directive_type, "no_op")
        got = validate_interpretations(
            [_item("max_grid_window", [16], {"max_grid_kwh": -100})], req
        )[0]
        self.assertEqual(got.directive_type, "no_op")
        got = validate_interpretations(
            [_item("max_grid_window", [14], {"max_grid_kwh": float("nan")})], req
        )[0]
        self.assertEqual(got.directive_type, "no_op")

    def test_oversize_reserve_is_clamped(self) -> None:
        req = _request("bad")
        cap = req.battery.capacity_kwh
        got = validate_interpretations(
            [_item("minimum_battery_reserve", [10], {"minimum_energy_kwh": 1_000_000})],
            req,
        )[0]
        self.assertEqual(got.directive_type, "minimum_battery_reserve")
        self.assertEqual(got.structured_adjustment.minimum_energy_kwh, cap)
        self.assertEqual(got.structured_adjustment.hours, [10])

    def test_invalid_hours_dropped_or_noop(self) -> None:
        req = _request("bad")
        got = validate_interpretations(
            [_item("no_charge_window", [24, 25])], req
        )[0]
        self.assertEqual(got.directive_type, "no_op")
        got = validate_interpretations(
            [_item("no_discharge_window", [99, 100])], req
        )[0]
        self.assertEqual(got.directive_type, "no_op")
        got = validate_interpretations(
            [_item("solar_reduction", [-1], {"factor": 0.9})], req
        )[0]
        self.assertEqual(got.directive_type, "no_op")
        got = validate_interpretations(
            [_item("minimum_battery_reserve", [23, 24, 25], {"minimum_energy_kwh": 100})],
            req,
        )[0]
        self.assertEqual(got.directive_type, "minimum_battery_reserve")
        self.assertEqual(got.structured_adjustment.hours, [23])
        got = validate_interpretations(
            [_item("solar_reduction", list(range(25)), {"factor": 0.5})], req
        )[0]
        self.assertEqual(got.structured_adjustment.hours, list(range(24)))
        got = validate_interpretations(
            [_item("max_grid_window", [13, 13], {"max_grid_kwh": 10})], req
        )[0]
        self.assertEqual(got.directive_type, "max_grid_window")
        self.assertEqual(got.structured_adjustment.hours, [13])

    def test_poison_note_does_not_kill_valid_sibling(self) -> None:
        req = _request("good", "poison")
        raw = [
            {
                "note_index": 0,
                "applies": True,
                "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": [13, 14], "factor": 0.25},
                "explanation": "wash",
            },
            {
                "note_index": 1,
                "applies": True,
                "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": [13], "factor": 1.5},
                "explanation": "illegal",
            },
        ]
        got = validate_interpretations(raw, req)
        self.assertEqual(got[0].directive_type, "solar_reduction")
        self.assertEqual(got[0].structured_adjustment.factor, 0.25)
        self.assertEqual(got[1].directive_type, "no_op")

    def test_schema_errors_still_raise(self) -> None:
        req = _request("bad")
        with self.assertRaises(GuardrailError):
            validate_interpretations(
                [
                    {
                        "note_index": 0,
                        "applies": True,
                        "directive_type": "invented",
                        "structured_adjustment": {"hours": [1]},
                        "explanation": "x",
                    }
                ],
                req,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
