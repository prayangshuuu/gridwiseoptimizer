# BUP CSE Fest 2026: LLM-Powered Smart Campus Energy Optimization & Scheduling Platform

## Public judging URL

| | |
|---|---|
| **Public deployment** | `https://gridwiseoptimizer-5541fa80b3e4.herokuapp.com` |
| **Local dev (default when running)** | `http://localhost:8000` |
| **Health** | `GET /health` → `200` and `{"status":"ok"}` |
| **Optimize** | `POST /optimize-energy` → JSON plan (no auth, no CSRF) |

**Base URL resolution** (used by `scripts/smoke_external.sh`, `replay_check.py`, and `interp_check.py` when you do not pass `--url` or `$BASE_URL`):

1. If `http://localhost:8000/health` returns `{"status":"ok"}` → use **local**.
2. Otherwise → use the **Heroku** URL above.

Override anytime:

```bash
export BASE_URL=http://localhost:8000          # or the Heroku URL
scripts/smoke_external.sh
uv run scripts/replay_check.py --url "$BASE_URL"
```

Quick check (prints which host it chose — local if uvicorn is running, else Heroku):

```bash
scripts/smoke_external.sh
curl -s http://localhost:8000/health
curl -s https://gridwiseoptimizer-5541fa80b3e4.herokuapp.com/health
```

Judging contract semantics match the official **Problem Statement** and the worked examples in `BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json` at the repo root.

---

## Tech stack

![Python](https://img.shields.io/badge/Python_3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Django](https://img.shields.io/badge/Django-092E20?style=for-the-badge&logo=django&logoColor=white)
![Uvicorn](https://img.shields.io/badge/Uvicorn_(ASGI)-499848?style=for-the-badge)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-316192?style=for-the-badge&logo=postgresql&logoColor=white)
![Django Allauth](https://img.shields.io/badge/Django_Allauth-092E20?style=for-the-badge&logo=django&logoColor=white)
![uv](https://img.shields.io/badge/uv-2C2D30?style=for-the-badge)
![Docker](https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![Docker Compose](https://img.shields.io/badge/Docker_Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![OpenRouter](https://img.shields.io/badge/OpenRouter-DeepSeek_v4.1_Flash-7C3AED?style=for-the-badge)

---

## Overview

A REST service that turns a **24-hour** campus energy scenario plus **1–3 free-text operator notes** into a **cost-minimal**, constraint-safe battery-and-grid dispatch plan.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Readiness → `200` and `{"status":"ok"}` (no database). |
| `POST` | `/optimize-energy` | Validate → LLM interpret notes → guardrails → optimize → JSON response. |

The judging endpoints use Django REST framework with **`AllowAny`**, **no** `SessionAuthentication` (configured per view so site allauth remains unchanged), and are **CSRF-exempt**. Optional OpenAPI UI lives at `/api/docs/` and `/api/redoc/`; scoring uses the Problem Statement JSON contract, not Swagger field names.

---

## Architecture: LLM → guardrails → optimizer

```
POST /optimize-energy
        │
        ▼
1. Request validation          gridwise/serializers.py
        │   400 malformed JSON · 422 well-formed but invalid
        ▼
2. LLM interpretation          gridwise/llm.py → interpret_notes()
        │   OpenRouter (OpenAI-compatible chat completions, JSON mode)
        │   Structured output validated in gridwise/llm_contract.py
        ▼
3. Reserve alignment (determ.) gridwise/llm_contract.py → align_minimum_reserves_from_notes()
        │   Raises under-specified kWh reserves when the note text implies more
        ▼
4. Deterministic guardrails    gridwise/guardrails.py → validate_directives()
        │   Coerce malformed / unsupported entries to safe no_op; never trust raw LLM JSON
        ▼
5. Linear program              gridwise/optimizer.py → optimize()
        │   PuLP + CBC; minimize Σ grid[h] × tariff[h]
        ▼
6. Response                    gridwise/views.py
        │   Totals recomputed from hourly_plan (single source of truth)
```

**Why guardrails?** The LLM proposes directives; guardrails enforce allowed types, hour lists (unique ints `0…23`, ascending), numeric ranges, and `no_op` semantics before anything reaches the LP.

**API path:** interpretation is **always** via OpenRouter on `POST /optimize-energy`. The module `gridwise/fallback.py` is a **deterministic keyword parser used in unit tests only** — it is not wired into the live HTTP pipeline.

### Six directive types

| `directive_type` | `structured_adjustment` when `applies: true` | Effect in the LP |
|------------------|-----------------------------------------------|------------------|
| `solar_reduction` | `{ "hours", "factor" }` — `factor` ∈ [0,1] is **usable solar remaining** (80% cut → `0.2`) | `effective_solar[h] *= factor` on listed hours |
| `minimum_battery_reserve` | `{ "hours", "minimum_energy_kwh" }` (≤ capacity) | Raise SoC floor on listed hours |
| `no_charge_window` | `{ "hours" }` | `charge[h] = 0` |
| `no_discharge_window` | `{ "hours" }` | `discharge[h] = 0` |
| `max_grid_window` | `{ "hours", "max_grid_kwh" }` | `grid[h] ≤ max_grid_kwh` |
| `no_op` | `null` — **`applies` must be `false`** | No change |

**Time windows:** ranges like “1 PM to 3 PM” are **start-inclusive, end-exclusive** → hours `[13, 14]`. Same rule in the LLM prompt, guardrails, public sample pack, and `tests/paraphrase_cases.json`.

---

## JSON contract (summary)

Canonical detail is in the Problem Statement and in `BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json` → `_meta.schema_notes`.

**Request (required):** `scenario_id`, `operator_notes` (1–3 strings), `hours` (exactly 24 objects with `hour`, `demand_kwh`, `solar_kwh`, `tariff_bdt_per_kwh`), `battery`.

**Battery** — either Problem Statement names or short aliases (normalized internally):

| Canonical (Problem Statement) | Also accepted |
|------------------------------|---------------|
| `capacity_kwh` | `capacity` |
| `initial_energy_kwh` | `initial_energy` |
| `minimum_energy_kwh` | `minimum_energy` |
| `max_charge_kwh_per_hour` | `max_charge` |
| `max_discharge_kwh_per_hour` | `max_discharge` |

**Response (required):** `scenario_id`, `directive_interpretation`, `hourly_plan`, `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`, `plan_summary`.

Each **`directive_interpretation`** entry: `note_index`, `applies`, `directive_type`, `structured_adjustment`, `explanation` — **one entry per operator note**, indices `0…N-1`.

Each **`hourly_plan`** hour: `hour`, `grid_kwh`, `solar_used_kwh`, `battery_action` (`charge` | `discharge` | `idle`), `battery_kwh` (signed: + charge, − discharge), `battery_energy_after_kwh`.

Signed battery rule: `battery_energy_after_kwh[h] = battery_energy_after_kwh[h-1] + battery_kwh[h]` (hour `0` uses `initial_energy` as prior state).

---

## Local quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync
cp .env.example .env          # set OPENROUTER_API_KEY (see below)

# Optional: only if you use Postgres/SQLite audit logging
uv run python manage.py migrate

uv run uvicorn core.asgi:application --host 0.0.0.0 --port 8000
```

Apple Silicon: PuLP’s bundled CBC is x86-64. Install native CBC with `brew install cbc` (auto-detected on `PATH`); Linux deploy images use the bundled solver.

### Environment variables (names only — never commit secrets)

All LLM settings are read from `gridwise/llm_env.py`. **OpenRouter is the only provider.**

| Variable | Required | Purpose |
|----------|----------|---------|
| `SECRET_KEY` | production | Django secret key |
| `DEBUG` | no | `True` / `False` (default `False`) |
| `ALLOWED_HOSTS` | production | Comma-separated hosts (e.g. `gridwiseoptimizer-5541fa80b3e4.herokuapp.com`) |
| `DATABASE_URL` | no | Postgres DSN; omit for SQLite. `/health` needs no DB |
| `OPENROUTER_API_KEY` | yes* | Comma-separated keys (rotation on auth / `429`) |
| `LLM_MODEL` | no | Default `deepseek/deepseek-v4.1-flash` |
| `LLM_BASE_URL` | no | Default `https://openrouter.ai/api/v1` |
| `LLM_REASONING` | no | `off` (default) · `low` / `medium` / `high` |
| `LLM_TIMEOUT` / `LLM_TIMEOUT_SECONDS` | no | Read timeout seconds; `0` = no read cap |
| `LLM_CONNECT_TIMEOUT` | no | Connect timeout when read timeout off (default `30`) |
| `LLM_MAX_RETRIES` | no | Default `3` |
| `LLM_RETRY_BACKOFF` | no | Default `0.5` |
| `LLM_DEADLINE` | no | Overall retry budget; `0` = unlimited |
| `LLM_MAX_TOKENS` | no | Cap completion tokens |
| `LLM_CACHE_SIZE` | no | LRU cache entries for identical note lists (default `256`) |

\* Required for `POST /optimize-energy` (not for `GET /health`).

Example `.env` fragment:

```bash
OPENROUTER_API_KEY=<your-key>
LLM_MODEL=deepseek/deepseek-v4.1-flash
LLM_REASONING=off
LLM_TIMEOUT=0
LLM_MAX_RETRIES=3
LLM_DEADLINE=0
```

---

## API examples

### `GET /health`

```bash
# Uses local server if uvicorn is running; otherwise Heroku (see smoke_external.sh)
scripts/smoke_external.sh
# Or force a host:
curl -s http://localhost:8000/health
curl -s https://gridwiseoptimizer-5541fa80b3e4.herokuapp.com/health
```

### `POST /optimize-energy`

Use a **valid** 24-hour body. Fastest checks:

```bash
scripts/smoke_external.sh
uv run scripts/replay_check.py          # all cases in BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json
```

Minimal manual POST (same shape as `scripts/smoke_external.sh`):

```bash
```bash
curl -s -X POST "${BASE_URL:-http://localhost:8000}/optimize-energy" \
  -H "Content-Type: application/json" \
  -d @- <<'EOF'
{
  "scenario_id": "demo-1",
  "operator_notes": [
    "Cloudy from noon to 4pm, cut solar by 30%.",
    "Do not charge the battery from 5pm to 9pm.",
    "Great work on the night shift, thanks all!"
  ],
  "hours": [
    {"hour":0,"demand_kwh":2,"solar_kwh":0,"tariff_bdt_per_kwh":5},
    {"hour":1,"demand_kwh":2,"solar_kwh":0,"tariff_bdt_per_kwh":5},
    {"hour":2,"demand_kwh":2,"solar_kwh":0,"tariff_bdt_per_kwh":5},
    {"hour":3,"demand_kwh":2,"solar_kwh":0,"tariff_bdt_per_kwh":5},
    {"hour":4,"demand_kwh":2,"solar_kwh":0,"tariff_bdt_per_kwh":5},
    {"hour":5,"demand_kwh":2,"solar_kwh":0,"tariff_bdt_per_kwh":5},
    {"hour":6,"demand_kwh":2,"solar_kwh":1,"tariff_bdt_per_kwh":5},
    {"hour":7,"demand_kwh":2,"solar_kwh":1,"tariff_bdt_per_kwh":5},
    {"hour":8,"demand_kwh":2,"solar_kwh":2,"tariff_bdt_per_kwh":5},
    {"hour":9,"demand_kwh":2,"solar_kwh":3,"tariff_bdt_per_kwh":5},
    {"hour":10,"demand_kwh":2,"solar_kwh":4,"tariff_bdt_per_kwh":5},
    {"hour":11,"demand_kwh":2,"solar_kwh":4,"tariff_bdt_per_kwh":5},
    {"hour":12,"demand_kwh":2,"solar_kwh":4,"tariff_bdt_per_kwh":5},
    {"hour":13,"demand_kwh":2,"solar_kwh":4,"tariff_bdt_per_kwh":5},
    {"hour":14,"demand_kwh":2,"solar_kwh":4,"tariff_bdt_per_kwh":5},
    {"hour":15,"demand_kwh":2,"solar_kwh":3,"tariff_bdt_per_kwh":5},
    {"hour":16,"demand_kwh":2,"solar_kwh":2,"tariff_bdt_per_kwh":5},
    {"hour":17,"demand_kwh":2,"solar_kwh":1,"tariff_bdt_per_kwh":15},
    {"hour":18,"demand_kwh":2,"solar_kwh":0,"tariff_bdt_per_kwh":15},
    {"hour":19,"demand_kwh":2,"solar_kwh":0,"tariff_bdt_per_kwh":15},
    {"hour":20,"demand_kwh":2,"solar_kwh":0,"tariff_bdt_per_kwh":15},
    {"hour":21,"demand_kwh":2,"solar_kwh":0,"tariff_bdt_per_kwh":15},
    {"hour":22,"demand_kwh":2,"solar_kwh":0,"tariff_bdt_per_kwh":5},
    {"hour":23,"demand_kwh":2,"solar_kwh":0,"tariff_bdt_per_kwh":5}
  ],
  "battery": {
    "capacity_kwh": 10,
    "initial_energy_kwh": 4,
    "minimum_energy_kwh": 1,
    "max_charge_kwh_per_hour": 3,
    "max_discharge_kwh_per_hour": 3
  }
}
EOF
```

Response shape (abridged):

```json
{
  "scenario_id": "demo-1",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {"hours": [12, 13, 14, 15], "factor": 0.7},
      "explanation": "..."
    },
    {
      "note_index": 1,
      "applies": true,
      "directive_type": "no_charge_window",
      "structured_adjustment": {"hours": [17, 18, 19, 20]},
      "explanation": "..."
    },
    {
      "note_index": 2,
      "applies": false,
      "directive_type": "no_op",
      "structured_adjustment": null,
      "explanation": "..."
    }
  ],
  "hourly_plan": [
    {
      "hour": 0,
      "grid_kwh": 1.0,
      "solar_used_kwh": 0.0,
      "battery_action": "discharge",
      "battery_kwh": -1.0,
      "battery_energy_after_kwh": 3.0
    }
  ],
  "total_grid_kwh": 25.0,
  "total_cost_bdt": 135.0,
  "peak_grid_kwh": 5.0,
  "plan_summary": "Imports 25.00 kWh from grid at 135.00 BDT; ..."
}
```

**HTTP status codes:** `200` success · `400` malformed JSON · `422` validation error · `500` controlled failure (LLM exhausted retries, infeasible LP, or unexpected error — no stack traces or secrets in the body).

---

## Reliability and performance

- Target **≤ 5 s p95** when the model responds quickly; hard per-request budget **30 s** on the judging side.
- **Retries:** transient OpenRouter errors, bad JSON, and `5xx` with exponential backoff (`LLM_MAX_RETRIES`, `LLM_RETRY_BACKOFF`, optional `LLM_DEADLINE`).
- **Key rotation:** comma-separated `OPENROUTER_API_KEY` on auth failure and `429`.
- **`LLM_REASONING=off`** avoids slow chain-of-thought on free-tier routes.
- **LRU cache** keyed by operator notes (+ battery capacity), size `LLM_CACHE_SIZE`.
- **Logs:** `scenario_id`, path, status, `latency_ms`, `llm_provider` — never keys, prompts, or full bodies.

If OpenRouter remains unavailable after retries, the API returns **`500`** with `{"detail":"Failed to optimize the scenario."}` (no silent non-LLM fallback on the HTTP path).

---

## Verification scripts (judge-style)

Place `BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json` at the repo root (included in this repository).

### `scripts/replay_check.py`

Independent replay of constraints and directive semantics for all **10 public sample cases**. Default target: **localhost:8000 if `/health` is up, else Heroku** (override with `--url`).

```bash
uv run scripts/replay_check.py
uv run scripts/replay_check.py --url http://localhost:8000
uv run scripts/replay_check.py --url https://gridwiseoptimizer-5541fa80b3e4.herokuapp.com
uv run scripts/replay_check.py --file path/to/cases.json
```

Exit codes: `0` all pass · `1` any fail · `2` missing cases file.

### `scripts/interp_check.py`

Paraphrase robustness over `tests/paraphrase_cases.json`. Default HTTP target: same rule as replay (**local if up, else Heroku**). **`--local`:** in-process `interpret_notes` (needs `OPENROUTER_API_KEY` in `.env`).

```bash
uv run scripts/interp_check.py
uv run scripts/interp_check.py --delay 5
uv run scripts/interp_check.py --local
uv run scripts/interp_check.py --url http://localhost:8000
```

### `scripts/smoke_external.sh`

External smoke test: `GET /health` and `POST /optimize-energy` with a built-in valid body. **No args:** probes local, then Heroku.

```bash
scripts/smoke_external.sh
scripts/smoke_external.sh http://localhost:8000
scripts/smoke_external.sh https://gridwiseoptimizer-5541fa80b3e4.herokuapp.com
```

See also `docs/SUBMISSION_CHECKLIST.md` and `docs/VIDEO_OUTLINE.md`.

---

## Deploy (Heroku container)

```bash
heroku stack:set container -a gridwiseoptimizer-5541fa80b3e4
heroku config:set -a gridwiseoptimizer-5541fa80b3e4 \
  SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(50))')" \
  DEBUG=False \
  ALLOWED_HOSTS="gridwiseoptimizer-5541fa80b3e4.herokuapp.com" \
  OPENROUTER_API_KEY="<your-key>" \
  LLM_MODEL=deepseek/deepseek-v4.1-flash \
  LLM_REASONING=off
git push heroku main
```

The `Dockerfile` binds **`0.0.0.0`** on **`$PORT`**. Release phase runs migrations (and optional `seed` when configured) per `heroku.yml`. **No secrets** are baked into the image.

The same container pattern works on Render, Railway, Fly.io, Cloud Run, etc.: build `Dockerfile`, expose `$PORT`, set the env vars above, terminate TLS at the edge.

---

## Docker fallback (local)

```bash
docker build -t gridwise-web:latest .

docker run --rm -p 8000:8000 \
  -e SECRET_KEY=change-me \
  -e SECURE_SSL_REDIRECT=False \
  -e OPENROUTER_API_KEY=<your-key> \
  -e LLM_MODEL=deepseek/deepseek-v4.1-flash \
  -e LLM_REASONING=off \
  gridwise-web:latest

curl -s http://127.0.0.1:8000/health    # {"status":"ok"}
```

Compose (optional Postgres profile):

```bash
docker compose up --build
docker compose --profile postgres up --build
```

For plain HTTP locally, keep `SECURE_SSL_REDIRECT=False` (or `DEBUG=True`). Behind Heroku’s TLS proxy, leave SSL redirect enabled in production.

After rebuilding, re-run `uv run scripts/replay_check.py --url http://127.0.0.1:8000` before relying on a stale local image.

---

## Credited dependencies

- [Django](https://www.djangoproject.com/) — web framework  
- [Django REST framework](https://www.django-rest-framework.org/) — API layer  
- [PuLP](https://coin-or.github.io/pulp/) + [CBC](https://github.com/coin-or/Cbc) — linear program  
- [openai](https://github.com/openai/openai-python) — OpenAI-compatible client (OpenRouter)  
- [django-environ](https://django-environ.readthedocs.io/) — settings from env  
- [Uvicorn](https://www.uvicorn.org/) — ASGI server  
- [psycopg](https://www.psycopg.org/) — PostgreSQL driver  
- [WhiteNoise](https://whitenoise.readthedocs.io/) — static files  
- [django-allauth](https://docs.allauth.org/) — site auth (not used on judging endpoints)  
- [uv](https://docs.astral.sh/uv/) — lockfile and installs  

AI coding assistants and public libraries/APIs were used during development; core pipeline design is team-owned.

---

## Known limitations

- **OpenRouter dependency:** persistent provider failure → controlled `500` after retries.  
- **CBC on Apple Silicon:** install system `cbc` via Homebrew when needed.  
- **Model:** lossless battery, hourly granularity, end-of-day SoC equals initial energy.  
- **Audit logging:** `OptimizationRun` DB rows are best-effort and skipped if no DB is reachable.  

---

## Secret handling

- **Never commit real keys.** `.env` is git-ignored; only `.env.example` (placeholders) is tracked.  
- Inject secrets via platform config vars, `docker run -e`, or `--env-file` at runtime.  
- Error responses do not include stack traces, file paths, or provider secrets.  
- Rotate any exposed key at OpenRouter immediately.  
