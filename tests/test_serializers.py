import unittest
from gridwise.serializers import OptimizeEnergyRequestSerializer

class TestSerializer(unittest.TestCase):
    def test_valid_body(self):
        data = {
            "scenario_id": "test-1",
            "operator_notes": ["note 1", "note 2"],
            "hours": [
                {
                    "hour": i,
                    "demand_kwh": 5.0,
                    "solar_kwh": 2.0,
                    "tariff_bdt_per_kwh": 10.0
                } for i in range(24)
            ],
            "battery": {
                "capacity_kwh": 10.0,
                "initial_energy_kwh": 5.0,
                "minimum_energy_kwh": 2.0,
                "max_charge_kwh_per_hour": 3.0,
                "max_discharge_kwh_per_hour": 3.0
            }
        }
        # Shuffle hours to test sorting
        data["hours"] = data["hours"][12:] + data["hours"][:12]
        
        serializer = OptimizeEnergyRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        # Ensure it's sorted
        self.assertEqual([h["hour"] for h in serializer.validated_data["hours"]], list(range(24)))
        # Ensure battery is mapped correctly
        self.assertEqual(serializer.validated_data["battery"]["capacity"], 10.0)

    def test_invalid_23_hours(self):
        data = {
            "scenario_id": "test-1",
            "operator_notes": ["note 1"],
            "hours": [
                {
                    "hour": i,
                    "demand_kwh": 5.0,
                    "solar_kwh": 2.0,
                    "tariff_bdt_per_kwh": 10.0
                } for i in range(23)
            ],
            "battery": {
                "capacity_kwh": 10.0,
                "initial_energy_kwh": 5.0,
                "minimum_energy_kwh": 2.0,
                "max_charge_kwh_per_hour": 3.0,
                "max_discharge_kwh_per_hour": 3.0
            }
        }
        serializer = OptimizeEnergyRequestSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("Expected exactly 24 hours", str(serializer.errors["hours"]))

    def test_duplicate_hour(self):
        hours = [
            {
                "hour": i,
                "demand_kwh": 5.0,
                "solar_kwh": 2.0,
                "tariff_bdt_per_kwh": 10.0
            } for i in range(24)
        ]
        hours[1] = hours[0].copy()  # duplicate hour 0
        
        data = {
            "scenario_id": "test-1",
            "operator_notes": ["note 1"],
            "hours": hours,
            "battery": {
                "capacity_kwh": 10.0,
                "initial_energy_kwh": 5.0,
                "minimum_energy_kwh": 2.0,
                "max_charge_kwh_per_hour": 3.0,
                "max_discharge_kwh_per_hour": 3.0
            }
        }
        serializer = OptimizeEnergyRequestSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("Hour indices must be unique", str(serializer.errors["hours"]))

    def test_0_notes(self):
        data = {
            "scenario_id": "test-1",
            "operator_notes": [],
            "hours": [
                {"hour": i, "demand_kwh": 5.0, "solar_kwh": 2.0, "tariff_bdt_per_kwh": 10.0}
                for i in range(24)
            ],
            "battery": {
                "capacity_kwh": 10.0, "initial_energy_kwh": 5.0, "minimum_energy_kwh": 2.0,
                "max_charge_kwh_per_hour": 3.0, "max_discharge_kwh_per_hour": 3.0
            }
        }
        serializer = OptimizeEnergyRequestSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("Ensure this field has at least 1 elements.", str(serializer.errors["operator_notes"]))

    def test_short_names_accepted(self):
        data = {
            "scenario_id": "test-1",
            "operator_notes": ["note 1"],
            "hours": [
                {"hour": i, "demand_kwh": 5.0, "solar_kwh": 2.0, "tariff_bdt_per_kwh": 10.0}
                for i in range(24)
            ],
            "battery": {
                "capacity": 10.0, "initial_energy": 5.0, "minimum_energy": 2.0,
                "max_charge": 3.0, "max_discharge": 3.0,
            },
        }
        s = OptimizeEnergyRequestSerializer(data=data)
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["battery"]["capacity"], 10.0)
        self.assertEqual(s.validated_data["battery"]["initial_energy"], 5.0)

    def test_long_names_accepted(self):
        data = {
            "scenario_id": "test-1",
            "operator_notes": ["note 1"],
            "hours": [
                {"hour": i, "demand_kwh": 5.0, "solar_kwh": 2.0, "tariff_bdt_per_kwh": 10.0}
                for i in range(24)
            ],
            "battery": {
                "capacity_kwh": 10.0, "initial_energy_kwh": 5.0,
                "minimum_energy_kwh": 2.0,
                "max_charge_kwh_per_hour": 3.0, "max_discharge_kwh_per_hour": 3.0,
            },
        }
        s = OptimizeEnergyRequestSerializer(data=data)
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["battery"]["capacity"], 10.0)

    def test_initial_exceeds_capacity_rejected(self):
        data = {
            "scenario_id": "test-1",
            "operator_notes": ["n"],
            "hours": [
                {"hour": i, "demand_kwh": 5.0, "solar_kwh": 2.0, "tariff_bdt_per_kwh": 10.0}
                for i in range(24)
            ],
            "battery": {
                "capacity": 10.0, "initial_energy": 99.0, "minimum_energy": 1.0,
                "max_charge": 3.0, "max_discharge": 3.0,
            },
        }
        s = OptimizeEnergyRequestSerializer(data=data)
        self.assertFalse(s.is_valid())
        self.assertIn("initial_energy", str(s.errors["battery"]))
