# Pre-submission checklist

Run through this against the **deployed public URL** before submitting. Replace
`$BASE_URL` with your HTTPS endpoint (e.g. `https://your-app.example.com`).

## 1. Endpoints reachable externally (no auth, no VPN)

Run from a machine that is NOT your dev box (or a phone on cellular) to prove the
judge can reach it:

```bash
scripts/smoke_external.sh "$BASE_URL"
```

- [ ] `GET  $BASE_URL/health` → `200 {"status":"ok"}`
- [ ] `POST $BASE_URL/optimize-energy` (sample body) → `200` with `hourly_plan`
- [ ] No login / token / CSRF is required (the public API is `AllowAny`, no
      SessionAuthentication, CSRF-exempt).

## 2. Automated checks pass

```bash
# Replay tester (needs the server running + the official sample file at repo root)
uv run scripts/replay_check.py --url "$BASE_URL"

# Paraphrase / interpretation accuracy (uses the configured LLM)
uv run scripts/interp_check.py           # add --delay 5 on rate-limited tiers
```

- [ ] `replay_check.py` → all sample cases PASS
- [ ] `interp_check.py` → per-directive accuracy acceptable (every case that
      reaches the model passes; only provider rate-limits/5xx cause misses)

## 3. Docker image builds, pulls, and reaches /health

```bash
docker build -t gridwise-web:latest .
docker run --rm -p 8000:8000 -e SECRET_KEY=x -e SECURE_SSL_REDIRECT=False \
  -e LLM_PROVIDER=gemini -e LLM_MODEL=gemini-3.8-flash -e LLM_API_KEY=... \
  gridwise-web:latest
curl -s http://localhost:8000/health      # {"status":"ok"}
```

- [ ] Image builds from the uv lockfile
- [ ] Container binds `0.0.0.0` on `$PORT`, serves `/health` with **no** DB
- [ ] (If published) `docker pull <registry>/gridwise-web:latest` works

## 4. README quickstart works from a clean environment

```bash
git clone <repo> && cd gridwiseoptimizer
uv sync
cp .env.example .env    # fill LLM_API_KEY etc.
uv run uvicorn core.asgi:application --host 0.0.0.0 --port 8000
```

- [ ] Fresh clone + `uv sync` + run works with only the documented env vars
- [ ] (Apple Silicon) `brew install cbc` note is followed if needed

## 5. No committed secrets

```bash
git ls-files | xargs grep -nEi 'AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9]{20,}|SECRET_KEY *= *["'"'"']?[^ "'"'"'x]' 2>/dev/null
git check-ignore .env        # must print .env (ignored)
```

- [ ] `.env` is git-ignored and NOT committed; only `.env.example` (placeholders)
- [ ] `grep` finds no API keys or real secrets in tracked files
- [ ] Image contains no baked secrets (all via runtime env)

## 6. Totals recompute matches

- [ ] For any 200 response, `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`
      recompute exactly from `hourly_plan` (verified by `replay_check.py`)
- [ ] Error mapping confirmed: `400` malformed JSON · `422` semantically invalid
      · `500` controlled (no stack traces/secrets) · `200` otherwise (LLM or
      deterministic fallback)

## 7. Performance & reliability

- [ ] p95 latency ≤ 5s and every request completes within 30s (short LLM
      timeout + one retry + deadline budget + in-memory cache)
- [ ] LLM outage does not 500: deterministic fallback yields a valid schedule
      (`fallback_used` in structured logs)
