# BUP CSE Fest 2026: LLM-Powered Smart Campus Energy Optimization & Scheduling Platform

## Tech Stack

![Python](https://img.shields.io/badge/Python_3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Django](https://img.shields.io/badge/Django-092E20?style=for-the-badge&logo=django&logoColor=white)
![Uvicorn](https://img.shields.io/badge/Uvicorn_(ASGI)-499848?style=for-the-badge)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-316192?style=for-the-badge&logo=postgresql&logoColor=white)
![Django Allauth](https://img.shields.io/badge/Django_Allauth-092E20?style=for-the-badge&logo=django&logoColor=white)
![uv](https://img.shields.io/badge/uv-2C2D30?style=for-the-badge)
![Docker](https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![Docker Compose](https://img.shields.io/badge/Docker_Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![LLM Optimization](https://img.shields.io/badge/LLM_Optimization-FF9900?style=for-the-badge&logo=openai&logoColor=white)

---

## Overview

A REST service that turns a 24-hour campus energy scenario plus free-text
operator notes into a cost-minimal battery-and-grid dispatch plan. Two public
endpoints:

| Method | Path               | Purpose                                             |
|--------|--------------------|-----------------------------------------------------|
| GET    | `/health`          | Liveness probe → `200 {"status":"ok"}` (no DB).     |
| POST   | `/optimize-energy` | Validate → interpret notes → optimize → return plan.|

The public API uses DRF with `AllowAny` and **no** SessionAuthentication (set
per-view so the rest of the site's allauth/session auth is untouched); it is
CSRF-exempt and `/health` has no database dependency.

## Architecture: LLM → Guardrails → Optimizer

```
POST /optimize-energy
        │
        ▼
1. Serializer validation        gridwise/serializers.py
        │   400 malformed JSON · 422 well-formed-but-invalid
        ▼
2. LLM interpretation           gridwise/llm.py   →  interpret_notes()
        │   free-text notes → RAW structured directives (UNTRUSTED)
        │   provider/model/key from env; short timeout; typed LLMError
        ▼
3. Deterministic guardrails     gridwise/guardrails.py → validate_directives()
        │   pure Python: enforce shape/ranges; coerce anything
        │   malformed/unsupported to a safe no_op; never trust the LLM
        ▼
4. Linear program               gridwise/optimizer.py → optimize()
        │   PuLP + CBC; minimize Σ grid[h]·tariff[h]
        ▼
5. Response (totals RECOMPUTED from hourly_plan)     gridwise/views.py
```

**Why guardrails between the LLM and the optimizer?** The LLM output is treated
as untrusted. Guardrails validate every field (directive type in the allowed
set, hours as unique ints 0–23, `factor ∈ [0,1]`, `reserve ∈ [0, capacity]`,
`max_grid_kwh ≥ 0`) and coerce any malformed or unsupported entry into an inert
`no_op` instead of failing or inventing behavior. The optimizer only ever sees
clean, typed directives, and the LP itself is what the judge independently
replays.

### The six directive types

| directive_type            | structured_adjustment          | Effect in the LP                                    |
|---------------------------|--------------------------------|-----------------------------------------------------|
| `solar_reduction`         | `{hours, factor}` (0–1)        | `effective_solar[h] *= factor`                      |
| `minimum_battery_reserve` | `{hours, reserve}` (≤ capacity)| raise SoC floor: `after[h] ≥ max(base, reserve)`    |
| `no_charge`               | `{hours}`                      | `charge[h] = 0`                                      |
| `no_discharge`            | `{hours}`                      | `discharge[h] = 0`                                   |
| `max_grid`                | `{hours, max_grid_kwh}`        | `grid[h] ≤ max_grid_kwh`                             |
| `no_op`                   | `null`                         | none (note carried no actionable instruction)       |

## Local quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync                        # install from uv.lock
cp .env.example .env           # then edit .env (see env vars below)

# Apply migrations only if you use a database (optional; not needed for /health).
uv run python manage.py migrate

# Run the ASGI server (binds 0.0.0.0)
uv run uvicorn core.asgi:application --host 0.0.0.0 --port 8000
```

> On Apple Silicon, PuLP's bundled CBC solver is x86-64 and will not run
> natively. Install a native solver with `brew install cbc` — the optimizer
> auto-detects a `cbc` on `PATH` and otherwise falls back to the bundled one
> (which works on the Linux deploy image).

### Environment variables (names only — never commit real values)

| Variable                | Required | Purpose                                                        |
|-------------------------|----------|----------------------------------------------------------------|
| `SECRET_KEY`            | prod     | Django secret key.                                             |
| `DEBUG`                 | no       | `True`/`False` (default `False`).                              |
| `ALLOWED_HOSTS`         | no       | Comma-separated hosts.                                         |
| `DATABASE_URL`          | no       | Postgres DSN. Absent → SQLite; `/health` needs no DB.          |
| `LLM_PROVIDER`          | yes      | LLM provider, e.g. `gemini` or `openai`.                       |
| `LLM_MODEL`             | yes      | Primary model id.                                              |
| `LLM_MODEL_FALLBACK`    | no       | Model tried if the primary call fails.                         |
| `LLM_API_KEY`           | yes      | API key (or `<PROVIDER>_API_KEY`, e.g. `OPENAI_API_KEY`).      |
| `LLM_BASE_URL`          | no       | OpenAI-compatible base URL (auto-set for `gemini`).            |
| `LLM_TIMEOUT`           | no       | Per-call timeout seconds (default `8`, inside a 30s budget).   |

## Model / provider and how to switch

The LLM layer speaks the **OpenAI-compatible** Chat Completions API, so any
provider exposing that surface works by setting env vars only — no code change.

```bash
# Google Gemini (default in this repo)
LLM_PROVIDER=gemini
LLM_MODEL=gemini-3.8-flash
LLM_MODEL_FALLBACK=gemini-3.6-flash
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_API_KEY=<your-key>

# OpenAI
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
LLM_BASE_URL=            # leave blank; SDK default
LLM_API_KEY=<your-key>
```

Notes are interpreted in JSON mode at `temperature=0` with three few-shot
examples. If the primary model errors or times out, `LLM_MODEL_FALLBACK` is
tried once.

## API examples

### `GET /health`

```bash
curl -s https://gridwiseoptimizer-a38603e4c359.herokuapp.com/health
# {"status": "ok"}
```

### `POST /optimize-energy`

```bash
curl -s -X POST https://gridwiseoptimizer-a38603e4c359.herokuapp.com/optimize-energy \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_id": "demo-1",
    "operator_notes": [
      "Cloudy from noon to 2pm, cut solar by half.",
      "Do not charge the battery during the 5pm-9pm peak.",
      "Great work on the night shift, thanks all!"
    ],
    "hours": [
      {"hour": 0, "demand_kwh": 2.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 5.0}
      /* ... exactly 24 entries, hours 0..23 unique ... */
    ],
    "battery": {
      "capacity": 10, "initial_energy": 4, "minimum_energy": 1,
      "max_charge": 3, "max_discharge": 3
    }
  }'
```

Response (abridged):

```json
{
  "scenario_id": "demo-1",
  "directive_interpretation": [
    {"note_index": 0, "applies": true, "directive_type": "solar_reduction",
     "structured_adjustment": {"hours": [12, 13], "factor": 0.5}},
    {"note_index": 1, "applies": true, "directive_type": "no_charge",
     "structured_adjustment": {"hours": [17, 18, 19, 20, 21]}},
    {"note_index": 2, "applies": false, "directive_type": "no_op",
     "structured_adjustment": null}
  ],
  "hourly_plan": [
    {"hour": 0, "grid_kwh": 1.0, "solar_used_kwh": 0.0,
     "battery_action": "discharge", "battery_kwh": -1.0,
     "battery_energy_after_kwh": 3.0}
    /* ... 24 entries ... */
  ],
  "total_grid_kwh": 25.0,
  "total_cost_bdt": 135.0,
  "peak_grid_kwh": 5.0,
  "plan_summary": "Imports 25.00 kWh from grid at 135.00 BDT; peak 5.00 kWh at hour 23. Battery charges in 6 hour(s), discharges in 7 hour(s)."
}
```

`battery_kwh` is signed: **positive = charge into the battery, negative =
discharge**, `0` when idle, so `battery_energy_after_kwh[h] =
battery_energy_after_kwh[h-1] + battery_kwh[h]`. Every total is recomputed from
`hourly_plan`.

**Status codes:** `200` success (via the LLM, or the deterministic fallback if
the LLM failed) · `400` malformed JSON · `422` well-formed but invalid · `500`
controlled error (guardrail/optimizer/unexpected failure; no stack traces or
secrets, never crashes).

## Reliability & performance

Everything is engineered to stay up within a **30s/request** budget and target
**≤5s p95**, and to protect interpretation quality when the LLM is slow or flaky:

- **Hard timeout + one retry + deadline.** Each provider call has a hard
  per-attempt timeout (`LLM_TIMEOUT`); transient errors (timeout, `429`, `5xx`)
  are retried up to `LLM_MAX_RETRIES`, and an optional `LLM_MODEL_FALLBACK` model
  is tried — all bounded by an overall `LLM_DEADLINE` well under 30s. Final
  failure raises a typed `LLMError` (never a crash).
- **Deterministic fallback** (`gridwise/fallback.py`). Used **only** on
  `LLMError`: a keyword/clock/number rule interpreter that still yields a valid
  schedule, so a provider outage degrades gracefully instead of 500-ing. The LLM
  remains the primary path; `fallback_used` is recorded in the logs. No public
  note wording is hard-coded — it is generic keyword parsing.
- **In-memory cache** keyed by the exact `operator_notes` list, cutting p95 for
  repeated interpretations (`LLM_CACHE_SIZE` entries, thread-safe LRU).
- **Structured logging** of `scenario_id`, `path`, `status`, `latency_ms`,
  `fallback_used` — and never API keys, prompt text, or full request bodies.

## Reproducibility test — replay checker

`scripts/replay_check.py` mirrors the contest judge. For each sample case it
POSTs `input` to a running server, then **independently re-derives** every
constraint and verifies the returned `hourly_plan`: 24 unique hours; per-hour
energy balance within 0.01; `solar_used ≤ effective solar`; battery bounds,
rate limits, state transitions and active reserve; `no_charge` / `no_discharge`
/ `max_grid` obeyed; end-of-day battery = initial; and reported totals match the
plan. It also compares `directive_interpretation` (type + hours + numeric
values, ignoring free-text) against each case's `expected_output`.

```bash
# 1. Start the server in one shell:
uv run uvicorn core.asgi:application --host 0.0.0.0 --port 8000

# 2. Place the official sample file at the repo root, then in another shell:
uv run scripts/replay_check.py
#   optional overrides:
#   uv run scripts/replay_check.py --file path/to/cases.json --url https://gridwiseoptimizer-a38603e4c359.herokuapp.com
```

Expected output — a per-case table and an overall count:

```
Loaded N case(s) from .../BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json
Target: https://gridwiseoptimizer-a38603e4c359.herokuapp.com/optimize-energy

CASE        RESULT  DETAIL
----------  ------  ------
solar+peak  PASS    ok
...

N/N cases passed.
```

The script exits `0` when all cases pass, `1` if any fail, `2` if the cases file
is missing. It needs only the Python standard library.

## Paraphrase-robustness test — interpretation checker

`scripts/interp_check.py` runs `interpret_notes` → `validate_directives` over
`tests/paraphrase_cases.json` (4–6 differently-worded notes per directive type —
varied phrasing, 12h/24h clocks, percentages/fractions — plus `no_op`
distractors and two hard combos). It compares each result on **directive_type +
hours + numeric values** (free-text ignored) and reports per-directive accuracy.

```bash
uv run scripts/interp_check.py
# On rate-limited free tiers, space out the calls:
uv run scripts/interp_check.py --delay 5
```

```
CASE                     RESULT  DETAIL
-----------------------  ------  ------
solar_reduction/percent  PASS    ok
...
N/N cases passed.

Per-directive accuracy:
  solar_reduction            6/6  (100%)
  ...
```

Hour convention (shared by the prompt, the fallback, and these cases): a range
**"A to B" covers hour A up to but NOT including hour B** (endpoint-exclusive),
and `solar_reduction.factor` is the **fraction of solar remaining** after the
cut. Use failures here to tighten `gridwise/llm.py`'s prompt.

## Deploy to a public HTTPS host

The judge calls a **public base URL with no auth/VPN**, so deploy anywhere that
gives HTTPS and injects env vars. The repo ships a container build
(`Dockerfile` + `heroku.yml`) that binds `0.0.0.0` on `$PORT` and bakes in no
secrets.

**Heroku (container stack)** — example:

```bash
heroku create your-app
heroku stack:set container -a your-app
# Secrets via platform config vars (never in the repo/image):
heroku config:set -a your-app \
  SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(50))')" \
  DEBUG=False \
  ALLOWED_HOSTS="your-app.herokuapp.com" \
  LLM_PROVIDER=gemini LLM_MODEL=gemini-3.8-flash \
  LLM_MODEL_FALLBACK=gemini-3.6-flash \
  LLM_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/" \
  LLM_API_KEY="<your-key>"
git push heroku main          # builds Dockerfile; release runs migrate + seed
```

The same image runs on any container host (Render, Railway, Fly.io, Cloud Run):
build the `Dockerfile`, expose the container's `$PORT`, and set the env vars
below. TLS/HTTPS is terminated by the platform; keep `SECURE_SSL_REDIRECT`
enabled in production (the app trusts `X-Forwarded-Proto`).

**Exact env vars to set on the platform** (none live in the repo or image):

| Variable | Notes |
|----------|-------|
| `SECRET_KEY` | required in production |
| `DEBUG` | `False` in production |
| `ALLOWED_HOSTS` | your public host(s), comma-separated |
| `DATABASE_URL` | optional; set it to enable audit logging + migrations |
| `LLM_PROVIDER`, `LLM_MODEL` | required |
| `LLM_API_KEY` | required (or `<PROVIDER>_API_KEY`) |
| `LLM_MODEL_FALLBACK`, `LLM_BASE_URL`, `LLM_TIMEOUT`, `LLM_MAX_RETRIES`, `LLM_DEADLINE`, `LLM_CACHE_SIZE` | optional tuning |

**No authentication is required on the judging path.** `/health` and
`/optimize-energy` are `AllowAny`, have no SessionAuthentication, and are
CSRF-exempt — a plain `curl` from anywhere works once deployed.

## External verification

After deploying, confirm both endpoints from **outside** your machine (another
host, or a phone on cellular) before locking the URL:

```bash
scripts/smoke_external.sh https://your-app.example.com
# or:  BASE_URL=https://your-app.example.com scripts/smoke_external.sh
```

It curls `GET /health` (expects `200 {"status":"ok"}`) and `POST
/optimize-energy` with a sample body, prints the status codes and PASS/FAIL, and
exits non-zero if anything fails. See `docs/SUBMISSION_CHECKLIST.md` for the full
pre-submit checklist and `docs/VIDEO_OUTLINE.md` for the 3-minute demo script.

## Docker fallback

The image installs from the uv lockfile, binds `0.0.0.0` on `$PORT`, runs
migrations **only when `DATABASE_URL` is set**, and bakes in **no secrets**
(they come from runtime env).

```bash
# Build
docker build -t gridwise-web:latest .

# Run (no database needed for /health), then probe it. Port: 8000 in-container.
docker run --rm -p 8000:8000 \
  -e SECRET_KEY=change-me \
  -e SECURE_SSL_REDIRECT=False \
  -e LLM_PROVIDER=gemini -e LLM_MODEL=gemini-3.8-flash \
  -e LLM_API_KEY=<your-key> \
  gridwise-web:latest

curl -s https://gridwiseoptimizer-a38603e4c359.herokuapp.com/health      # {"status": "ok"}
```

With Compose (Postgres is optional, behind a profile):

```bash
docker compose up --build                       # web only; /health works, no DB
docker compose --profile postgres up --build    # web + Postgres
```

> When running the container directly over plain HTTP, pass
> `-e SECURE_SSL_REDIRECT=False` (or `-e DEBUG=True`); otherwise Django's
> production HTTPS redirect turns `/health` into a 301. Behind a TLS proxy
> (e.g. Heroku) leave it enabled.

## Credited dependencies

- [Django](https://www.djangoproject.com/) — web framework
- [Django REST framework](https://www.django-rest-framework.org/) — API layer
- [PuLP](https://coin-or.github.io/pulp/) with the
  [CBC](https://github.com/coin-or/Cbc) solver — the linear program
- [openai](https://github.com/openai/openai-python) — OpenAI-compatible client
  (used for Gemini and OpenAI)
- [django-environ](https://django-environ.readthedocs.io/) — env-based settings
- [Uvicorn](https://www.uvicorn.org/) — ASGI server
- [psycopg](https://www.psycopg.org/) — PostgreSQL driver
- [WhiteNoise](https://whitenoise.readthedocs.io/) — static files
- [django-allauth](https://docs.allauth.org/) — site auth (unrelated to the public API)
- [uv](https://docs.astral.sh/uv/) — packaging / lockfile

## Known limitations

- **LLM availability**: interpretation depends on the configured provider. On a
  timeout or provider error (e.g. a transient `503`) the request returns a
  controlled `500`; the fallback model mitigates but does not eliminate this.
- **CBC architecture**: PuLP's bundled CBC is x86-64; native Apple Silicon needs
  `brew install cbc` (auto-detected). The Linux deploy image runs the bundled
  solver.
- **Model assumptions**: lossless battery (no round-trip efficiency), whole-hour
  granularity, and a hard end-of-day = initial state-of-charge constraint.
- **Audit logging is best-effort**: the `OptimizationRun` row is written in a
  guarded `try/except` and is silently skipped if no database is reachable — it
  never blocks or fails the response.

## Secret handling

- **No real keys in the repo.** `.env` is git-ignored and Docker-ignored; only
  `.env.example` (placeholders) is committed.
- Secrets are read from the runtime environment (`-e` / `--env-file` / platform
  config vars), never baked into the image or logged.
- The `500` error path and all responses are scrubbed of stack traces and
  secrets.
- If a key is ever exposed, rotate it at the provider immediately.
