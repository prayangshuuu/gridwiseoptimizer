import unittest
from gridwise.fallback import interpret_notes_fallback

class TestFallback(unittest.TestCase):
    def test_solar_reduction(self):
        notes = [
            "Cut solar by 30% from noon to 4pm",
            "Reduce PV generation to 50% between 10:00 and 14:00",
            "Cloud cover, drop solar by half from 1pm to 3pm"
        ]
        results = interpret_notes_fallback(notes)
        
        self.assertEqual(results[0]["directive_type"], "solar_reduction")
        self.assertEqual(results[0]["structured_adjustment"]["hours"], [12, 13, 14, 15])
        self.assertEqual(results[0]["structured_adjustment"]["factor"], 0.7)
        
        self.assertEqual(results[1]["directive_type"], "solar_reduction")
        self.assertEqual(results[1]["structured_adjustment"]["hours"], [10, 11, 12, 13])
        self.assertEqual(results[1]["structured_adjustment"]["factor"], 0.5)

        self.assertEqual(results[2]["directive_type"], "solar_reduction")
        self.assertEqual(results[2]["structured_adjustment"]["hours"], [13, 14])
        self.assertEqual(results[2]["structured_adjustment"]["factor"], 0.5)

    def test_no_charge(self):
        notes = [
            "Do not charge the battery from 6pm to 9pm",
            "Stop charging between 18:00 and 21:00",
            "Prevent battery charge overnight"
        ]
        results = interpret_notes_fallback(notes)
        
        self.assertEqual(results[0]["directive_type"], "no_charge_window")
        self.assertEqual(results[0]["structured_adjustment"]["hours"], [18, 19, 20])
        
        self.assertEqual(results[1]["directive_type"], "no_charge_window")
        self.assertEqual(results[1]["structured_adjustment"]["hours"], [18, 19, 20])
        
        self.assertEqual(results[2]["directive_type"], "no_charge_window")
        self.assertEqual(results[2]["structured_adjustment"]["hours"], [0, 1, 2, 3, 4, 5])

    def test_no_discharge(self):
        notes = [
            "Do not discharge the battery from 6am to 9am",
            "Stop discharging between 06:00 and 09:00",
            "Prevent battery discharge morning"
        ]
        results = interpret_notes_fallback(notes)
        
        self.assertEqual(results[0]["directive_type"], "no_discharge_window")
        self.assertEqual(results[0]["structured_adjustment"]["hours"], [6, 7, 8])

        self.assertEqual(results[1]["directive_type"], "no_discharge_window")
        self.assertEqual(results[1]["structured_adjustment"]["hours"], [6, 7, 8])
        
        self.assertEqual(results[2]["directive_type"], "no_discharge_window")
        self.assertEqual(results[2]["structured_adjustment"]["hours"], [6, 7, 8, 9, 10, 11])

    def test_max_grid(self):
        notes = [
            "Cap grid import to 50 kwh from 5pm to 8pm",
            "Limit grid to 100kW between 17:00 and 20:00"
        ]
        results = interpret_notes_fallback(notes)
        
        self.assertEqual(results[0]["directive_type"], "max_grid_window")
        self.assertEqual(results[0]["structured_adjustment"]["hours"], [17, 18, 19])
        self.assertEqual(results[0]["structured_adjustment"]["max_grid_kwh"], 50.0)

        self.assertEqual(results[1]["directive_type"], "max_grid_window")
        self.assertEqual(results[1]["structured_adjustment"]["hours"], [17, 18, 19])
        self.assertEqual(results[1]["structured_adjustment"]["max_grid_kwh"], 100.0)

    def test_minimum_battery_reserve(self):
        notes = [
            "Keep reserve at least 20 kwh from midnight to 5am",
            "Maintain minimum 15 kwh below 05:00"
        ]
        results = interpret_notes_fallback(notes)
        
        self.assertEqual(results[0]["directive_type"], "minimum_battery_reserve")
        self.assertEqual(results[0]["structured_adjustment"]["hours"], [0, 1, 2, 3, 4])
        self.assertEqual(results[0]["structured_adjustment"]["minimum_energy_kwh"], 20.0)

    def test_distractor(self):
        notes = [
            "Thanks for the hard work today, see you tomorrow!",
            "Weather is nice but wind is picking up",
            "System online"
        ]
        results = interpret_notes_fallback(notes)
        for res in results:
            self.assertEqual(res["directive_type"], "no_op")
            self.assertFalse(res["applies"])
            self.assertIsNone(res["structured_adjustment"])
