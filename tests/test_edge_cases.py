from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from backend.replay import SAMPLE_CASES_PATH, TOLERANCE, nearly_equal, replay
from backend.schemas import OptimizeEnergyRequest, OptimizeEnergyResponse
from backend.service import run_optimize

LIVE = "https://the-beetles-bup-hackathon.onrender.com"

PARAPHRASES = {
    "SAMPLE-01": [
        "Rooftop PV will be washed from 12:00 until 14:00; treat available solar as about a quarter of the forecast.",
        "The sports office moved next month's registration deadline.",
    ],
    "SAMPLE-02": [
        "Isolate the battery charger from 02:00 to 05:00 for electrical work.",
    ],
    "SAMPLE-03": [
        "Hold at least half of battery capacity in reserve from 18:00 until 21:00 for emergency operations.",
    ],
    "SAMPLE-04": [
        "Battery export is forbidden from 18:00 to 20:00 during protection testing.",
    ],
    "SAMPLE-05": [
        "Hourly campus grid import must stay at or below 155 kWh from 18:00 until 21:00 because of a temporary feeder limit.",
    ],
    "SAMPLE-06": [
        "Cloud cover during inspection leaves about 50% of forecast solar from 10:00 until 12:00.",
        "The charging circuit is unavailable from 14:00 until 16:00.",
        "The library is extending book-return hours next week.",
    ],
    "SAMPLE-07": [
        "Keep at least 90 kWh stored from 18:00 until 22:00 for emergency services.",
        "Evening transformer limit is 180 kWh of grid import from 19:00 until 21:00.",
    ],
    "SAMPLE-08": [
        "Charging is disabled from 11:00 until 13:00 while technicians inspect the charger.",
        "Do not discharge from 17:00 until 19:00 during relay testing.",
    ],
    "SAMPLE-09": [
        "Panel washing from eleven until two will leave roughly one-fifth of normal solar output because of inverter work.",
        "Student affairs will publish club notices tomorrow.",
    ],
    "SAMPLE-10": [
        "The data center requires at least 80 kWh remaining in the battery from 18:00 until 22:00.",
        "Grid intake must stay at or below 190 kWh from 19:00 until 22:00 while the substation is constrained.",
        "A seminar room booking was moved to next week.",
    ],
}


def _match_interpretation(actual, expected) -> list[str]:
    errors: list[str] = []
    if len(actual) != len(expected):
        return [f"count {len(actual)} != {len(expected)}"]
    for got, want in zip(actual, expected):
        prefix = f"note {want.note_index}"
        if got.directive_type != want.directive_type:
            errors.append(f"{prefix}: type {got.directive_type} != {want.directive_type}")
        if got.applies != want.applies:
            errors.append(f"{prefix}: applies {got.applies} != {want.applies}")
        if want.directive_type == "no_op":
            continue
        got_adj = got.structured_adjustment
        want_adj = want.structured_adjustment
        if got_adj is None or want_adj is None:
            errors.append(f"{prefix}: missing structured_adjustment")
            continue
        if list(got_adj.hours) != list(want_adj.hours):
            errors.append(f"{prefix}: hours {got_adj.hours} != {want_adj.hours}")
        for key in ("factor", "minimum_energy_kwh", "max_grid_kwh"):
            if hasattr(want_adj, key):
                got_val = getattr(got_adj, key, None)
                want_val = getattr(want_adj, key)
                if got_val is None or not nearly_equal(float(got_val), float(want_val)):
                    errors.append(f"{prefix}: {key} {got_val} != {want_val}")
    return errors


def _report(label: str, ok: bool, extra: str = "") -> int:
    print(f"{label}: {'PASS' if ok else 'FAIL'} {extra}".rstrip())
    return 0 if ok else 1


def test_live_contract() -> int:
    failed = 0
    health = urllib.request.urlopen(LIVE + "/health", timeout=60)
    body = health.read().decode()
    failed += _report("LIVE /health", health.status == 200 and '"ok"' in body, body)
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                LIVE + "/optimize-energy",
                data=b"{}",
                headers={"Content-Type": "application/json"},
            ),
            timeout=30,
        )
        failed += _report("LIVE malformed {}", False, "expected 400")
    except urllib.error.HTTPError as exc:
        failed += _report("LIVE malformed {}", exc.code == 400, f"HTTP {exc.code}")
    return failed


def test_paraphrases(cases: list[dict]) -> int:
    failed = 0
    by_id = {case["id"]: case for case in cases}
    first = True
    for sample_id, notes in PARAPHRASES.items():
        if not first:
            time.sleep(2.0)
        first = False
        case = by_id[sample_id]
        payload = dict(case["input"])
        payload["operator_notes"] = notes
        payload["scenario_id"] = f"{sample_id}-PARA"
        request = OptimizeEnergyRequest.model_validate(payload)
        expected = OptimizeEnergyResponse.model_validate(case["expected_output"])
        try:
            result = run_optimize(request)
        except Exception as exc:
            failed += _report(f"PARA {sample_id}", False, f"{type(exc).__name__}: {exc}")
            continue
        interp_errors = _match_interpretation(
            result.directive_interpretation, expected.directive_interpretation
        )
        replay_result = replay(request, result, result.directive_interpretation)
        cost_ok = nearly_equal(result.total_cost_bdt, expected.total_cost_bdt) or (
            result.total_cost_bdt <= expected.total_cost_bdt + TOLERANCE
        )
        ok = not interp_errors and replay_result.ok and cost_ok
        extra = ""
        if interp_errors:
            extra = "; ".join(interp_errors)
        elif not replay_result.ok:
            extra = "; ".join(replay_result.errors)
        elif not cost_ok:
            extra = f"cost {result.total_cost_bdt} vs {expected.total_cost_bdt}"
        failed += _report(f"PARA {sample_id}", ok, extra)
    return failed


def test_official_local(cases: list[dict]) -> int:
    failed = 0
    first = True
    for case in cases:
        if not first:
            time.sleep(2.0)
        first = False
        request = OptimizeEnergyRequest.model_validate(case["input"])
        expected = OptimizeEnergyResponse.model_validate(case["expected_output"])
        try:
            result = run_optimize(request)
        except Exception as exc:
            failed += _report(case["id"], False, f"{type(exc).__name__}: {exc}")
            continue
        interp_errors = _match_interpretation(
            result.directive_interpretation, expected.directive_interpretation
        )
        replay_result = replay(request, result, result.directive_interpretation)
        cost_ok = nearly_equal(result.total_cost_bdt, expected.total_cost_bdt) or (
            result.total_cost_bdt <= expected.total_cost_bdt + TOLERANCE
        )
        ok = not interp_errors and replay_result.ok and cost_ok
        failed += _report(
            case["id"],
            ok,
            ""
            if ok
            else ("; ".join(interp_errors) or str(replay_result.errors)),
        )
    return failed


if __name__ == "__main__":
    cases = json.loads(SAMPLE_CASES_PATH.read_text(encoding="utf-8"))["cases"]
    failed = 0
    print("== live contract ==")
    failed += test_live_contract()
    print("== official samples via LLM ==")
    failed += test_official_local(cases)
    print("== paraphrased hidden-style notes ==")
    failed += test_paraphrases(cases)
    print(f"TOTAL failures: {failed}")
    raise SystemExit(1 if failed else 0)
