from __future__ import annotations

import json
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from backend.replay import SAMPLE_CASES_PATH, TOLERANCE, nearly_equal, replay
from backend.schemas import OptimizeEnergyRequest, OptimizeEnergyResponse
from backend.service import run_optimize


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


def main() -> int:
    payload = json.loads(SAMPLE_CASES_PATH.read_text(encoding="utf-8"))
    failed = 0
    for case in payload["cases"]:
        if case is not payload["cases"][0]:
            time.sleep(2.0)
        case_id = case["id"]
        request = OptimizeEnergyRequest.model_validate(case["input"])
        expected = OptimizeEnergyResponse.model_validate(case["expected_output"])
        try:
            result = run_optimize(request)
        except Exception as exc:
            failed += 1
            print(f"{case_id}: FAIL exception {type(exc).__name__}: {exc}")
            continue
        interp_errors = _match_interpretation(
            result.directive_interpretation, expected.directive_interpretation
        )
        replay_result = replay(request, result, result.directive_interpretation)
        cost_ok = nearly_equal(result.total_cost_bdt, expected.total_cost_bdt) or (
            result.total_cost_bdt <= expected.total_cost_bdt + TOLERANCE
        )
        ok = not interp_errors and replay_result.ok and cost_ok
        if ok:
            print(
                f"{case_id}: PASS cost={result.total_cost_bdt:.2f} "
                f"types={[item.directive_type for item in result.directive_interpretation]}"
            )
        else:
            failed += 1
            print(f"{case_id}: FAIL")
            for error in interp_errors:
                print(f"  interp: {error}")
            if not replay_result.ok:
                for error in replay_result.errors:
                    print(f"  replay: {error}")
            if not cost_ok:
                print(
                    f"  cost ours={result.total_cost_bdt} sample={expected.total_cost_bdt}"
                )
            for item in result.directive_interpretation:
                print(f"  got[{item.note_index}] {item.directive_type} {item.structured_adjustment}")
    print(f"{len(payload['cases']) - failed}/{len(payload['cases'])} live LLM cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
