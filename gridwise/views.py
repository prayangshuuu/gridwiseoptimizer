import json
import logging
import time

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.exceptions import ParseError
from rest_framework import status
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from .serializers import OptimizeEnergyRequestSerializer
from .llm import interpret_notes, LLMError
from .fallback import interpret_notes_fallback
from .guardrails import validate_directives, GuardrailError
from .optimizer import optimize, OptimizerError

logger = logging.getLogger(__name__)


def _ms(started: float) -> int:
    """Elapsed milliseconds since a time.monotonic() mark."""
    return int((time.monotonic() - started) * 1000)


def _log_event(**fields) -> None:
    """Emit one structured log line. Only safe metadata is ever included here
    (scenario_id, path, status, latency_ms, fallback_used) — never API keys,
    prompt text, or full request/response bodies."""
    try:
        logger.info("optimize_energy %s", json.dumps(fields, sort_keys=True))
    except Exception:  # logging must never break the request
        pass


class GridwiseBaseAPIView(APIView):
    # Default to AllowAny and NO SessionAuthentication for this app's views.
    # Setting these per-view (rather than a global DRF default) keeps the
    # project's existing allauth/session auth on all other views intact.
    authentication_classes = []
    permission_classes = [AllowAny]

    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)


class HealthCheckView(GridwiseBaseAPIView):
    def get(self, request):
        return Response({"status": "ok"}, status=status.HTTP_200_OK)


class OptimizeEnergyView(GridwiseBaseAPIView):
    """POST /optimize-energy.

    Pipeline: validate body -> LLM interpret notes -> guardrail directives ->
    solve LP -> build response. Every reported total is RECOMPUTED from the
    hourly plan so the numbers are internally consistent and independently
    replayable.

    Reliability: if the LLM path fails (timeout/flaky provider -> LLMError), a
    deterministic keyword interpreter (gridwise.fallback) is used so the request
    still yields a valid schedule (fallback_used=true). Only a deeper failure
    (guardrails/optimizer/unexpected) becomes a controlled 500.

    Status codes:
      - 400 if the request body is not valid JSON.
      - 422 if the JSON is well-formed but fails validation.
      - 500 (controlled, no stack trace/secrets) on any pipeline failure.
      - 200 with the plan otherwise (possibly via the deterministic fallback).
    """

    def post(self, request):
        started = time.monotonic()
        path = request.path

        # Malformed JSON -> 400 (parser error).
        try:
            data = request.data
        except ParseError as exc:
            _log_event(path=path, status=400, latency_ms=_ms(started),
                       fallback_used=False, error="malformed_json")
            return Response(
                {"detail": "Malformed JSON.", "error": str(exc.detail)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Well-formed but invalid -> 422.
        serializer = OptimizeEnergyRequestSerializer(data=data)
        if not serializer.is_valid():
            _log_event(path=path, status=422, latency_ms=_ms(started),
                       fallback_used=False, error="validation_failed")
            return Response(
                {"detail": "Validation failed.", "errors": serializer.errors},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        validated = serializer.validated_data
        scenario_id = validated.get("scenario_id")

        # Any failure past this point is a controlled 500. Never leak internals.
        try:
            response_body, fallback_used = self._run_pipeline(validated)
        except Exception as exc:  # noqa: BLE001 - deliberately catch-all + safe
            logger.exception("optimize-energy pipeline failed: %s", type(exc).__name__)
            _log_event(scenario_id=scenario_id, path=path, status=500,
                       latency_ms=_ms(started), fallback_used=False,
                       error=type(exc).__name__)
            return Response(
                {"detail": "Failed to optimize the scenario."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # Best-effort audit log; must never block or fail the response.
        self._log_run(validated, response_body)

        _log_event(scenario_id=scenario_id, path=path, status=200,
                   latency_ms=_ms(started), fallback_used=fallback_used)
        return Response(response_body, status=status.HTTP_200_OK)

    def _run_pipeline(self, validated: dict) -> tuple[dict, bool]:
        notes = validated["operator_notes"]
        hours = validated["hours"]
        battery = validated["battery"]

        # LLM stays the primary path; the deterministic interpreter is the
        # safety net used ONLY when the LLM call finally fails.
        fallback_used = False
        try:
            raw = interpret_notes(notes)
        except LLMError as exc:
            logger.warning("LLM interpretation failed; using fallback: %s", exc)
            raw = interpret_notes_fallback(notes)
            fallback_used = True

        directives = validate_directives(
            raw, num_notes=len(notes), capacity=float(battery["capacity"])
        )
        plan = optimize(hours, battery, directives)

        # Recompute every total from the hourly plan (single source of truth).
        tariff_by_hour = {
            row["hour"]: float(row["tariff_bdt_per_kwh"]) for row in hours
        }
        total_grid_kwh = sum(p["grid_kwh"] for p in plan)
        total_cost_bdt = sum(
            p["grid_kwh"] * tariff_by_hour[p["hour"]] for p in plan
        )
        peak = max(plan, key=lambda p: p["grid_kwh"])
        peak_grid_kwh = peak["grid_kwh"]

        return {
            "scenario_id": validated["scenario_id"],
            "directive_interpretation": [d.to_dict() for d in directives],
            "hourly_plan": plan,
            "total_grid_kwh": round(total_grid_kwh, 6),
            "total_cost_bdt": round(total_cost_bdt, 6),
            "peak_grid_kwh": round(peak_grid_kwh, 6),
            "plan_summary": self._summary(
                plan, total_grid_kwh, total_cost_bdt, peak
            ),
        }, fallback_used

    @staticmethod
    def _summary(plan, total_grid_kwh, total_cost_bdt, peak) -> str:
        n_charge = sum(1 for p in plan if p["battery_action"] == "charge")
        n_discharge = sum(1 for p in plan if p["battery_action"] == "discharge")
        return (
            f"Imports {total_grid_kwh:.2f} kWh from grid at {total_cost_bdt:.2f} "
            f"BDT; peak {peak['grid_kwh']:.2f} kWh at hour {peak['hour']}. "
            f"Battery charges in {n_charge} hour(s), discharges in "
            f"{n_discharge} hour(s)."
        )

    @staticmethod
    def _log_run(validated: dict, response_body: dict) -> None:
        """Persist an audit row. Fully guarded: any failure (no DB, no table,
        serialization issue) is swallowed so it can never affect the response."""
        try:
            from .models import OptimizationRun

            # Round-trip through JSON to guarantee plain, serializable payloads.
            request_payload = json.loads(json.dumps(validated, default=str))
            OptimizationRun.objects.create(
                scenario_id=response_body["scenario_id"],
                total_grid_kwh=response_body["total_grid_kwh"],
                total_cost_bdt=response_body["total_cost_bdt"],
                peak_grid_kwh=response_body["peak_grid_kwh"],
                request_payload=request_payload,
                response_payload=response_body,
            )
        except Exception:
            logger.warning("optimize-energy audit logging skipped", exc_info=True)
