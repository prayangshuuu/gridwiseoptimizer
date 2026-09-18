# 3-minute demo video outline (tie-break only)

Total 3:00. Keep it tight; screen-record the live run.

## 0:00–0:20 — Problem (20s)
- One line: campus energy operators write plans as free-text notes; we turn a
  24-hour scenario + notes into a **cost-minimal, constraint-safe** battery/grid
  schedule.
- Show the two endpoints: `GET /health`, `POST /optimize-energy`.

## 0:20–1:20 — Architecture: LLM → guardrails → optimizer (60s)
- **LLM** interprets each note into structured directives (JSON mode, env-driven
  provider/model). Output is treated as **untrusted**.
- **Guardrails** (pure Python): enforce the 6 directive types, hour/factor/
  reserve/grid ranges; coerce anything malformed/unsupported to a safe `no_op`.
  Never trust, never crash.
- **Optimizer**: a PuLP/CBC linear program minimizing `Σ grid·tariff` under
  energy-balance, battery bounds/rate-limits, reserve, and directive constraints.
- Emphasize: every total is **recomputed from `hourly_plan`**, so the judge can
  replay it independently.

## 1:20–2:20 — Key choices (60s)
- **Safe failure**: short LLM timeout + one retry + deadline budget; on failure a
  **deterministic keyword fallback** still yields a valid schedule (no 500).
- **Performance**: in-memory cache keyed by the exact notes list → low p95;
  everything inside a 30s budget.
- **Correctness boundary**: LLM proposes, guardrails dispose, LP guarantees
  feasibility. Structured logs (scenario_id, latency_ms, fallback_used) with no
  secrets.
- **Reproducibility**: `replay_check.py` mirrors the judge; `interp_check.py`
  measures paraphrase robustness.

## 2:20–3:00 — Live run + replay (40s)
- Public URL: `https://gridwiseoptimizer-5541fa80b3e4.herokuapp.com`
- `curl` the public `POST /optimize-energy` with a sample body → show the plan,
  directive_interpretation, and totals.
- Run `uv run scripts/replay_check.py` → **all cases PASS** (defaults to the live public URL).
- Close on `scripts/smoke_external.sh` proving the public URL needs no auth.
