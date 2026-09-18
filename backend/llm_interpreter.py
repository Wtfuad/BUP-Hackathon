from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import httpx
from dotenv import load_dotenv

from backend.schemas import OptimizeEnergyRequest
from backend.validator import GuardrailError, validate_interpretations

load_dotenv()

SYSTEM_PROMPT = """
You convert campus operator notes into structured energy directives for a 24-hour (hours 0-23) schedule.

Return ONLY JSON with this shape:
{"directive_interpretation":[...]}

There must be exactly one entry per operator note, in note_index order 0,1,... 

Each entry:
{
  "note_index": <int>,
  "applies": <bool>,
  "directive_type": "<type>",
  "structured_adjustment": <object or null>,
  "explanation": "<short string>"
}

Allowed directive_type values:
- solar_reduction: structured_adjustment {"hours":[...],"factor": number}
  factor is the usable solar FRACTION REMAINING, in [0,1].
  "80% reduction" or "leave one-fifth" => factor 0.2
  "drop to about 20%" / "20% of forecast" => factor 0.2
  "roughly 25% of the forecast" => factor 0.25
  "about half" / "50% of forecast" => factor 0.5
- minimum_battery_reserve: {"hours":[...],"minimum_energy_kwh": number}
  If the note gives a percentage of battery capacity, compute kWh from battery.capacity_kwh.
  Example: 50% of a 200 kWh battery => 100
- no_charge_window: {"hours":[...]}
  charging unavailable / charger isolated / do not charge
- no_discharge_window: {"hours":[...]}
  do not discharge / discharge disabled / protection testing
- max_grid_window: {"hours":[...],"max_grid_kwh": number}
  per-hour grid import cap, not a sum across hours
- no_op: applies=false and structured_adjustment=null
  Notes that do NOT change today's solar/battery/grid schedule:
  menus, library hours, sports/office deadlines, club notices, seminar moved, "next week", "tomorrow" non-energy items.

applies=true for every non-no_op directive. applies=false only for no_op.

hours must be unique integers 0 through 23 in ascending order.
Time windows use whole hours, start inclusive, end exclusive:
- 1 PM to 3 PM => [13,14]
- noon until 2 PM => [12,13]
- 2 AM until 5 AM => [2,3,4]
- 6 PM until 9 PM => [18,19,20]
- 6 PM until 8 PM => [18,19]
- 6 PM until 10 PM => [18,19,20,21]
- 10 AM until noon => [10,11]
- 2 PM until 4 PM => [14,15]
- 11 AM until 1 PM => [11,12]
- 5 PM until 7 PM => [17,18]
- 7 PM until 9 PM => [19,20]
- 7 PM until 10 PM => [19,20,21]
- between 11 AM and 2 PM => [11,12,13]
- 13:00 to 15:00 => [13,14]
noon=12, midnight=0. 12-hour and 24-hour clocks are both valid.

Do not invent demand, tariff, battery hardware limits, or new directive types.

Examples:
"Solar output will drop to about 20% from 1 PM to 3 PM."
-> solar_reduction, hours [13,14], factor 0.2, applies true

"Do not charge the battery between 2 PM and 4 PM."
-> no_charge_window, hours [14,15], applies true

"Keep at least 120 kWh in reserve from 6 PM until 9 PM."
-> minimum_battery_reserve, hours [18,19,20], minimum_energy_kwh 120, applies true

"The cafeteria menu changes tomorrow."
-> no_op, applies false, structured_adjustment null
""".strip()


class MissingLLMConfigError(RuntimeError):
    pass


def interpret_operator_notes(request: OptimizeEnergyRequest):
    last_error = None
    for _ in range(2):
        content = _complete(request, last_error)
        try:
            payload = _extract_json(content)
            items = payload.get("directive_interpretation", payload)
            return validate_interpretations(items, request)
        except (GuardrailError, json.JSONDecodeError, TypeError, ValueError, AttributeError) as exc:
            last_error = str(exc)
    raise GuardrailError("interpretation_failed")


def _complete(request: OptimizeEnergyRequest, last_error: str | None) -> str:
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise MissingLLMConfigError("LLM_API_KEY is not set")
    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("LLM_MODEL", "openai/gpt-oss-20b")
    user = _user_payload(request, last_error)
    body: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
    }
    if "anthropic" not in base_url:
        body["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    last_http_error = "llm_request_failed"
    with httpx.Client(timeout=httpx.Timeout(12.0)) as client:
        for attempt in range(4):
            try:
                response = client.post(
                    f"{base_url}/chat/completions", headers=headers, json=body
                )
                if response.status_code >= 400 and "response_format" in body:
                    body.pop("response_format", None)
                    response = client.post(
                        f"{base_url}/chat/completions", headers=headers, json=body
                    )
                if response.status_code in {429, 500, 502, 503}:
                    last_http_error = f"llm_request_failed:{response.status_code}"
                    time.sleep(1.5 * (2 ** attempt))
                    continue
                if response.status_code >= 400:
                    raise GuardrailError(f"llm_request_failed:{response.status_code}")
                data = response.json()
                break
            except httpx.HTTPError as exc:
                last_http_error = "llm_request_failed"
                if attempt == 3:
                    raise GuardrailError(last_http_error) from exc
                time.sleep(0.6 * (2 ** attempt))
        else:
            raise GuardrailError(last_http_error)

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GuardrailError("llm_response_missing_content") from exc


def _user_payload(request: OptimizeEnergyRequest, last_error: str | None) -> str:
    notes = "\n".join(
        f"{index}: {note}" for index, note in enumerate(request.operator_notes)
    )
    battery = request.battery.model_dump()
    text = (
        f"scenario_id: {request.scenario_id}\n"
        f"battery: {json.dumps(battery)}\n"
        f"operator_notes:\n{notes}\n"
    )
    if last_error:
        text += (
            "\nYour previous JSON failed validation:\n"
            f"{last_error}\n"
            "Return corrected JSON only. Do not use no_op just to avoid a validation error "
            "if the note is energy-related.\n"
        )
    return text


def _extract_json(content: str) -> dict[str, Any]:
    if not content or not content.strip():
        raise GuardrailError("empty_llm_content")
    text = content.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise GuardrailError("llm_content_not_json")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise GuardrailError("llm_json_must_be_object")
    return parsed
