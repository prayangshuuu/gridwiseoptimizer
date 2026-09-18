import json
import logging

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.exceptions import ParseError
from rest_framework import status
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from .serializers import OptimizeEnergyRequestSerializer
from .llm import interpret_notes, LLMError
from .guardrails import validate_directives, GuardrailError
from .optimizer import optimize, OptimizerError

logger = logging.getLogger(__name__)


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

    Status codes:
      - 400 if the request body is not valid JSON.
      - 422 if the JSON is well-formed but fails validation.
      - 500 (controlled, no stack trace/secrets) on any pipeline failure.
      - 200 with the plan otherwise.
    """

    def post(self, request):
        # Malformed JSON -> 400 (parser error).
        try:
            data = request.data
        except ParseError as exc:
            return Response(
                {"detail": "Malformed JSON.", "error": str(exc.detail)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Well-formed but invalid -> 422.
        serializer = OptimizeEnergyRequestSerializer(data=data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Validation failed.", "errors": serializer.errors},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        validated = serializer.validated_data

        # Any failure past this point is a controlled 500. Never leak internals.
        try:
            response_body = self._run_pipeline(validated)
        except (LLMError, GuardrailError, OptimizerError, Exception) as exc:
            logger.exception("optimize-energy pipeline failed: %s", type(exc).__name__)
            return Response(
                {"detail": "Failed to optimize the scenario."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # Best-effort audit log; must never block or fail the response.
        self._log_run(validated, response_body)

        return Response(response_body, status=status.HTTP_200_OK)

    def _run_pipeline(self, validated: dict) -> dict:
        notes = validated["operator_notes"]
        hours = validated["hours"]
        battery = validated["battery"]

        raw = interpret_notes(notes)
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
        }

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
