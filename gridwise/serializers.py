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
    """Battery parameters. All quantities are non-negative kWh / kW."""

    capacity = serializers.FloatField(min_value=0)
    initial_energy = serializers.FloatField(min_value=0)
    minimum_energy = serializers.FloatField(min_value=0)
    max_charge = serializers.FloatField(min_value=0)
    max_discharge = serializers.FloatField(min_value=0)


class OptimizeEnergyRequestSerializer(serializers.Serializer):
    """Full body for POST /optimize-energy."""

    scenario_id = serializers.CharField(allow_blank=False, trim_whitespace=True)
    operator_notes = serializers.ListField(
        child=serializers.CharField(allow_blank=False, trim_whitespace=True),
        min_length=1,
        max_length=3,
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
        return value
