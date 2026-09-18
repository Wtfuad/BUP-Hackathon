from __future__ import annotations

import json
import unittest

from fastapi.testclient import TestClient

from backend.main import app, get_interpreter
from backend.replay import SAMPLE_CASES_PATH, TOLERANCE, nearly_equal, replay
from backend.schemas import OptimizeEnergyRequest, OptimizeEnergyResponse
from backend.service import gold_interpreter_factory

CASES = json.loads(SAMPLE_CASES_PATH.read_text(encoding="utf-8"))["cases"]
GOLD = {
    case["input"]["scenario_id"]: case["expected_output"]["directive_interpretation"]
    for case in CASES
}


def _interpretations_match(actual: list[dict], expected: list[dict]) -> list[str]:
    errors: list[str] = []
    if len(actual) != len(expected):
        return [f"count {len(actual)} != {len(expected)}"]
    for got, want in zip(actual, expected):
        prefix = f"note {want['note_index']}"
        if got.get("directive_type") != want["directive_type"]:
            errors.append(f"{prefix}: type {got.get('directive_type')} != {want['directive_type']}")
        if got.get("applies") != want["applies"]:
            errors.append(f"{prefix}: applies {got.get('applies')} != {want['applies']}")
        if want["directive_type"] == "no_op":
            if got.get("structured_adjustment") is not None:
                errors.append(f"{prefix}: no_op adjustment should be null")
            continue
        got_adj = got.get("structured_adjustment") or {}
        want_adj = want.get("structured_adjustment") or {}
        if sorted(got_adj.get("hours", [])) != sorted(want_adj.get("hours", [])):
            errors.append(f"{prefix}: hours {got_adj.get('hours')} != {want_adj.get('hours')}")
        for key in ("factor", "minimum_energy_kwh", "max_grid_kwh"):
            if key in want_adj and not nearly_equal(float(got_adj.get(key, -1)), float(want_adj[key])):
                errors.append(f"{prefix}: {key} {got_adj.get(key)} != {want_adj[key]}")
    return errors


class PublicSampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app.dependency_overrides[get_interpreter] = lambda: gold_interpreter_factory(GOLD)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        app.dependency_overrides.clear()

    def test_health(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_malformed_request_is_400(self) -> None:
        response = self.client.post("/optimize-energy", json={"scenario_id": "bad"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "error")

    def test_all_public_samples(self) -> None:
        for case in CASES:
            with self.subTest(case["id"]):
                response = self.client.post("/optimize-energy", json=case["input"])
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                expected = case["expected_output"]
                interp_errors = _interpretations_match(
                    body["directive_interpretation"],
                    expected["directive_interpretation"],
                )
                self.assertEqual(interp_errors, [], interp_errors)
                request = OptimizeEnergyRequest.model_validate(case["input"])
                parsed = OptimizeEnergyResponse.model_validate(body)
                replay_result = replay(
                    request, parsed, parsed.directive_interpretation
                )
                self.assertTrue(replay_result.ok, replay_result.errors)
                self.assertTrue(
                    nearly_equal(parsed.total_cost_bdt, expected["total_cost_bdt"])
                    or parsed.total_cost_bdt <= expected["total_cost_bdt"] + TOLERANCE
                )
                self.assertEqual(parsed.scenario_id, case["input"]["scenario_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
