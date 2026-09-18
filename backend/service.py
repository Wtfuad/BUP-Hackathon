from collections.abc import Callable
from typing import Any

from backend.llm_interpreter import interpret_operator_notes
from backend.optimizer import OptimizationError, optimize
from backend.replay import replay
from backend.schemas import DirectiveInterpretation, OptimizeEnergyRequest, OptimizeEnergyResponse
from backend.validator import validate_interpretations

Interpreter = Callable[[OptimizeEnergyRequest], Any]


def run_optimize(
    request: OptimizeEnergyRequest,
    interpreter: Interpreter = interpret_operator_notes,
) -> OptimizeEnergyResponse:
    raw = interpreter(request)
    validated = validate_interpretations(raw, request)
    response = optimize(request, validated)
    check = replay(request, response, validated)
    if not check.ok:
        raise OptimizationError("replay_failed: " + "; ".join(check.errors))
    return response


def gold_interpreter_factory(
    gold_by_scenario: dict[str, list[DirectiveInterpretation] | list[dict[str, Any]]],
) -> Interpreter:
    def _interpreter(request: OptimizeEnergyRequest):
        try:
            return gold_by_scenario[request.scenario_id]
        except KeyError as exc:
            raise OptimizationError(
                f"no gold interpretation for {request.scenario_id}"
            ) from exc

    return _interpreter
