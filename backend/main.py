from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.llm_interpreter import MissingLLMConfigError, interpret_operator_notes
from backend.optimizer import OptimizationError
from backend.schemas import ErrorResponse, HealthResponse, OptimizeEnergyRequest, OptimizeEnergyResponse
from backend.service import Interpreter, run_optimize
from backend.validator import GuardrailError

app = FastAPI(
    title="GridWise",
    description="BUP CSE Fest 2026 preliminary energy optimization API",
    version="0.2.0",
)


def get_interpreter() -> Interpreter:
    return interpret_operator_notes


@app.exception_handler(RequestValidationError)
async def malformed_request_handler(
    _request: Request, _exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content=ErrorResponse(status="error", message="malformed_request").model_dump(),
    )


@app.exception_handler(GuardrailError)
async def guardrail_handler(_request: Request, exc: GuardrailError) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(status="error", message="interpretation_failed").model_dump(),
    )


@app.exception_handler(MissingLLMConfigError)
async def llm_config_handler(
    _request: Request, _exc: MissingLLMConfigError
) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            status="error", message="interpretation_unavailable"
        ).model_dump(),
    )


@app.exception_handler(OptimizationError)
async def optimizer_handler(_request: Request, _exc: OptimizationError) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(status="error", message="optimization_failed").model_dump(),
    )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/optimize-energy", response_model=OptimizeEnergyResponse)
def optimize_energy(
    payload: OptimizeEnergyRequest,
    interpreter: Interpreter = Depends(get_interpreter),
) -> OptimizeEnergyResponse:
    return run_optimize(payload, interpreter=interpreter)
