#!/usr/bin/env bash
# External smoke test: verify a DEPLOYED public base URL with no auth.
#
# Usage:
#   scripts/smoke_external.sh https://your-app.example.com
#   BASE_URL=https://your-app.example.com scripts/smoke_external.sh
#
# Checks:
#   1. GET  /health           -> 200 and body {"status":"ok"}
#   2. POST /optimize-energy   -> 200 with a sample 24-hour body
# Prints status codes and PASS/FAIL; exits non-zero if any check fails.

set -u

BASE_URL="${1:-${BASE_URL:-}}"
if [ -z "$BASE_URL" ]; then
  echo "ERROR: provide BASE_URL as arg 1 or env var." >&2
  echo "  scripts/smoke_external.sh https://your-app.example.com" >&2
  exit 2
fi
BASE_URL="${BASE_URL%/}"   # strip trailing slash

pass=0
fail=0
ok()   { echo "PASS  $1"; pass=$((pass+1)); }
bad()  { echo "FAIL  $1"; fail=$((fail+1)); }

echo "Target: $BASE_URL"
echo

# --- 1) GET /health ---------------------------------------------------------
health_body="$(curl -sS -m 15 -w $'\n%{http_code}' "$BASE_URL/health" 2>/dev/null)"
health_code="$(printf '%s' "$health_body" | tail -n1)"
health_json="$(printf '%s' "$health_body" | sed '$d')"
echo "GET /health -> HTTP ${health_code:-000}: ${health_json}"
if [ "$health_code" = "200" ] && printf '%s' "$health_json" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
  ok "/health returns 200 {\"status\":\"ok\"}"
else
  bad "/health did not return 200 {\"status\":\"ok\"}"
fi
echo

# --- 2) POST /optimize-energy ----------------------------------------------
# Build a minimal-but-valid 24-hour body (hours 0..23).
read -r -d '' BODY <<'JSON'
{
  "scenario_id": "smoke-external",
  "operator_notes": ["Cut solar by 20% from noon to 4pm.", "Thanks team!"],
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
  "battery": {"capacity":10,"initial_energy":4,"minimum_energy":1,"max_charge":3,"max_discharge":3}
}
JSON

opt_body="$(curl -sS -m 35 -w $'\n%{http_code}' \
  -H 'Content-Type: application/json' \
  -X POST "$BASE_URL/optimize-energy" -d "$BODY" 2>/dev/null)"
opt_code="$(printf '%s' "$opt_body" | tail -n1)"
opt_json="$(printf '%s' "$opt_body" | sed '$d')"
echo "POST /optimize-energy -> HTTP ${opt_code:-000}"
echo "  ${opt_json:0:200}"
if [ "$opt_code" = "200" ] && printf '%s' "$opt_json" | grep -q '"hourly_plan"'; then
  ok "/optimize-energy returns 200 with hourly_plan"
else
  bad "/optimize-energy did not return 200 with hourly_plan"
fi

echo
echo "-------------------------------------"
echo "Result: $pass passed, $fail failed."
[ "$fail" -eq 0 ] && echo "SMOKE PASS" || echo "SMOKE FAIL"
exit "$fail"
