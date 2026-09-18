from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.exceptions import ParseError
from rest_framework import status
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from .serializers import OptimizeEnergyRequestSerializer
from .llm import interpret_notes, LLMError


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
    """Stub for POST /optimize-energy.

    For now this validates the request body and runs the mandatory LLM
    interpretation layer, then echoes both back. The optimizer is not wired
    in yet.

    Status codes:
      - 400 if the request body is not valid JSON (parser error).
      - 422 if the JSON is well-formed but fails validation.
      - 502 if the LLM interpretation layer fails.
      - 200 with the validated input and raw LLM interpretations otherwise.
    """

    def post(self, request):
        # Accessing request.data triggers JSON parsing; malformed JSON raises
        # ParseError -> map to 400 explicitly.
        try:
            data = request.data
        except ParseError as exc:
            return Response(
                {"detail": "Malformed JSON.", "error": str(exc.detail)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = OptimizeEnergyRequestSerializer(data=data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Validation failed.", "errors": serializer.errors},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        validated = serializer.validated_data

        try:
            interpretations = interpret_notes(validated["operator_notes"])
        except LLMError as exc:
            return Response(
                {"detail": "LLM interpretation failed.", "error": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Echo for now. `note_interpretations` is raw, untrusted LLM output.
        return Response(
            {
                "scenario_id": validated["scenario_id"],
                "note_interpretations": interpretations,
                "echo": validated,
            },
            status=status.HTTP_200_OK,
        )
