"""Request serializers for the gridwise public API.

These validate the POST /optimize-energy body. A failure here is a
well-formed-but-invalid payload and the view maps it to HTTP 422. Malformed
JSON never reaches these serializers (the parser rejects it with 400).
"""
from rest_framework import serializers

# The 24 hours of a day, used to assert full 0..23 coverage.
_DAY_HOURS = set(range(24))


class HourSerializer(serializers.Serializer):
    """A single hour slot within the 24-hour horizon."""

    hour = serializers.IntegerField(min_value=0, max_value=23)
    demand_kwh = serializers.FloatField(min_value=0)
    solar_kwh = serializers.FloatField(min_value=0)
    tariff_bdt_per_kwh = serializers.FloatField(min_value=0)


class BatterySerializer(serializers.Serializer):
    """Battery parameters. All quantities are non-negative kWh / kW.

    Accepts either the short names documented in the README (``capacity``,
    ``initial_energy``, ``minimum_energy``, ``max_charge``, ``max_discharge``)
    or the verbose names with explicit units (``capacity_kwh``, etc.). Whichever
    is provided is normalized to the short names in ``validated_data`` so the
    optimizer, the replay tester, and the response serializer all read a single
    canonical shape.
    """

    capacity = serializers.FloatField(min_value=0, required=False)
    initial_energy = serializers.FloatField(min_value=0, required=False)
    minimum_energy = serializers.FloatField(min_value=0, required=False)
    max_charge = serializers.FloatField(min_value=0, required=False)
    max_discharge = serializers.FloatField(min_value=0, required=False)

    def to_internal_value(self, data):
        """Accept either the short (README-documented) battery field names or
        the verbose ``_kwh`` / ``_kwh_per_hour`` aliases and normalize to the
        short names so downstream code has a single canonical shape."""
        if not isinstance(data, dict):
            self.fail("battery must be a JSON object")
            return {}
        normalized = dict(data)
        aliases = {
            "capacity_kwh": "capacity",
            "initial_energy_kwh": "initial_energy",
            "minimum_energy_kwh": "minimum_energy",
            "max_charge_kwh_per_hour": "max_charge",
            "max_discharge_kwh_per_hour": "max_discharge",
        }
        for long_name, short_name in aliases.items():
            if short_name not in normalized and long_name in normalized:
                normalized[short_name] = normalized[long_name]
        return super().to_internal_value(normalized)

    def validate(self, attrs):
        """Cross-field consistency: every battery quantity is bounded by its
        physical limits (initial ≤ capacity; minimum ≤ capacity; max rates
        finite). These are sanity checks; the LP re-enforces hard feasibility."""
        cap = float(attrs.get("capacity") or 0.0)
        ie = float(attrs.get("initial_energy") or 0.0)
        me = float(attrs.get("minimum_energy") or 0.0)
        errors = {}
        if ie > cap:
            errors["initial_energy"] = (
                f"initial_energy ({ie}) cannot exceed capacity ({cap})."
            )
        if me > cap:
            errors["minimum_energy"] = (
                f"minimum_energy ({me}) cannot exceed capacity ({cap})."
            )
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class OptimizeEnergyRequestSerializer(serializers.Serializer):
    """Full body for POST /optimize-energy."""

    scenario_id = serializers.CharField(allow_blank=False, trim_whitespace=True)
    operator_notes = serializers.ListField(
        child=serializers.CharField(allow_blank=False, trim_whitespace=True),
        min_length=1,
        max_length=10,
    )
    hours = HourSerializer(many=True)
    battery = BatterySerializer()

    def validate_hours(self, value):
        """Require exactly 24 unique hours covering 0..23."""
        if len(value) != 24:
            raise serializers.ValidationError(
                f"Expected exactly 24 hours, got {len(value)}."
            )
        provided = [slot["hour"] for slot in value]
        if len(set(provided)) != len(provided):
            raise serializers.ValidationError("Hour indices must be unique.")
        if set(provided) != _DAY_HOURS:
            missing = sorted(_DAY_HOURS - set(provided))
            raise serializers.ValidationError(
                f"Hours must cover 0..23 exactly; missing {missing}."
            )
        # Sort by hour to return a normalized list
        return sorted(value, key=lambda x: x["hour"])

class InterpretationSerializer(serializers.Serializer):
    directive_type = serializers.CharField()
    hours = serializers.ListField(child=serializers.IntegerField(), required=False)
    factor = serializers.FloatField(required=False)
    minimum_energy_kwh = serializers.FloatField(required=False)
    max_grid_kwh = serializers.FloatField(required=False)

class HourlyPlanSerializer(serializers.Serializer):
    hour = serializers.IntegerField()
    demand_kwh = serializers.FloatField()
    solar_kwh = serializers.FloatField()
    battery_action = serializers.CharField()
    battery_kwh = serializers.FloatField()
    grid_kwh = serializers.FloatField()
    state_of_charge_kwh = serializers.FloatField()

class OptimizeEnergyResponseSerializer(serializers.Serializer):
    scenario_id = serializers.CharField()
    directive_interpretation = InterpretationSerializer(many=True)
    hourly_plan = HourlyPlanSerializer(many=True)
    total_grid_kwh = serializers.FloatField()
    total_cost_bdt = serializers.FloatField()
    peak_grid_kwh = serializers.FloatField()
    plan_summary = serializers.CharField()
