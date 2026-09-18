# GridWise — BUP CSE Fest 2026 Preliminary

HTTP API for the Smart Campus Energy Optimization Challenge.

The service takes a 24-hour demand/solar/tariff/battery scenario plus 1–3 operator notes. An LLM interprets the notes into structured directives. Deterministic guardrails validate that output. A PuLP/CBC linear program then returns a valid, cost-minimized 24-hour schedule.

```
operator notes
    → LLM interpreter (untrusted)
    → deterministic guardrails
    → PuLP optimizer
    → replay verifier
    → JSON response
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Readiness. Returns `{"status":"ok"}`. No LLM call. |
| `POST` | `/optimize-energy` | Interpret notes and return a 24-hour plan. |

No other routes are required by the judge.

## Architecture

1. **LLM interpreter** (`backend/llm_interpreter.py`)  
   One JSON-mode chat completion per request. The model sees the operator notes and battery specs (needed for percentage reserves). It does **not** invent demand, tariff, or new directive types.

2. **Guardrails** (`backend/validator.py`)  
   Enforces allowed types, `applies` semantics, hour lists `0..23` sorted unique, solar `factor` in `[0,1]`, reserve ≤ capacity, and `no_op` ⇒ `structured_adjustment = null`. Invalid model output is retried once. It is **not** silently rewritten as `no_op`.

3. **Optimizer** (`backend/optimizer.py`)  
   PuLP + CBC. Minimizes `sum(grid_kwh[h] * tariff[h])` subject to energy balance, effective solar, battery bounds/rates, end-of-day neutrality (`E[23] = initial`), and every applicable directive.

4. **Replay** (`backend/replay.py`)  
   Independent hour-by-hour check before the response is returned. Totals are recomputed from `hourly_plan`.

## Local setup

Python 3.11+ recommended.

```powershell
cd "e:\BUP Hackathon"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

On macOS/Linux:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` (do not commit this file):

```env
LLM_API_KEY=your_key_here
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=openai/gpt-oss-20b
```

`LLM_BASE_URL` may be any OpenAI-compatible endpoint (OpenAI, Groq, Gemini OpenAI mode, etc.).

### Run

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

### Health

```powershell
curl http://127.0.0.1:8000/health
```

Expected:

```json
{"status":"ok"}
```

### Optimize (sample file)

The public sample pack is:

`BUP_CSE_FEST_2026_Participant_Docs/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json`

Each `cases[i].input` is a valid `POST /optimize-energy` body. Example using SAMPLE-01 from Python:

```powershell
.\.venv\Scripts\python.exe -c "import json,urllib.request; cases=json.load(open('BUP_CSE_FEST_2026_Participant_Docs/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json',encoding='utf-8'))['cases']; req=urllib.request.Request('http://127.0.0.1:8000/optimize-energy', data=json.dumps(cases[0]['input']).encode(), headers={'Content-Type':'application/json'}); print(urllib.request.urlopen(req).read().decode()[:500])"
```

A live `/optimize-energy` call **requires** `LLM_API_KEY`. Without it the API returns HTTP 500 `interpretation_unavailable`. `/health` still works.

## Tests

Public-sample math, schema, guardrails, and API wiring (gold interpretations, no LLM key required):

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_samples -v
.\.venv\Scripts\python.exe -m backend.replay
.\.venv\Scripts\python.exe -m backend.optimizer
.\.venv\Scripts\python.exe -m backend.validator
```

Expected: all 10 public cases pass; replay-valid plans; costs match the official optimal values within 0.01 BDT.

These tests prove energy accounting and the HTTP contract. Hidden judge notes are paraphrases, so the deployed service must use the LLM path, not the test override.

## Docker fallback

Build:

```powershell
docker build -t gridwise:2026 .
```

Run (pass secrets at runtime; they are not baked into the image):

```powershell
docker run --rm -p 8000:8000 -e LLM_API_KEY -e LLM_BASE_URL -e LLM_MODEL gridwise:2026
```

Or:

```powershell
docker run --rm -p 8000:8000 --env-file .env gridwise:2026
```

Then `curl http://127.0.0.1:8000/health` should return `{"status":"ok"}`.

Image requirements:
- listens on `0.0.0.0:8000`
- no secrets in layers
- CBC solver installed in the image

## Environment variables

| Name | Required | Meaning |
|---|---|---|
| `LLM_API_KEY` | yes for `/optimize-energy` | Provider secret. Never commit it. |
| `LLM_BASE_URL` | no | OpenAI-compatible base URL. Default `https://api.openai.com/v1`. |
| `LLM_MODEL` | no | Model id. Default `openai/gpt-oss-20b`. |

## Dependencies

See `requirements.txt`. Core pieces:

- FastAPI / uvicorn — HTTP API
- Pydantic v2 — request/response schema
- httpx — LLM HTTP client
- PuLP + CBC — 24-hour LP
- python-dotenv — local `.env` loading

## Limitations

- Scoring cases are assumed feasible, as stated in the problem statement. Contradictory hard directives are not supported.
- LLM latency dominates; `/health` does not call the model.
- Equivalent optimal schedules may differ from the public reference `hourly_plan`. The judge checks interpretation, validity, and recalculated cost, not byte-for-byte plan equality.
- Do not hard-code public sample wording. Hidden notes paraphrase the same six directive types.
