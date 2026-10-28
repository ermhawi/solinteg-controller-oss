"""Solar-first charging decisions tested without physical controls."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))
from ev_solar_policy import decide_solar

NOW = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)


def live(**changes):
    values = dict(timestamp=NOW.isoformat(), soc_pct=100, grid_w=1700,
                  battery_w=0, pv_w=2600, house_load_w=900)
    values.update(changes)
    return values


def decide(reading=None, *, ev=0, running=False, stable=3, phase=False,
           headroom=None, **kwargs):
    return decide_solar(
        live() if reading is None else reading, now=NOW, ev_measured_a=ev,
        ev_running=running, stable_surplus_samples=stable,
        phase_guard_verified=phase, phase_headroom_a=headroom, **kwargs,
    )


class SolarTests(unittest.TestCase):
    def test_full_battery_and_export_recommends_six_a(self):
        result = decide()
        self.assertEqual(result.suggested_amps, 6)
        self.assertEqual(result.permitted_amps, 0)
        self.assertIn("L3", result.reason)

    def test_enables_only_with_independent_phase_budget(self):
        self.assertEqual(decide(phase=True, headroom=6).permitted_amps, 6)
        self.assertEqual(decide(phase=True, headroom=5).permitted_amps, 0)
        self.assertEqual(decide(phase=True, headroom=None).permitted_amps, 0)
        for invalid in (float("nan"), float("inf"), -1, "unknown"):
            with self.subTest(headroom=invalid):
                self.assertEqual(decide(phase=True, headroom=invalid).permitted_amps, 0)

    def test_no_start_before_house_battery_full(self):
        for soc in (20, 67, 98, 99.4):
            with self.subTest(soc=soc):
                self.assertEqual(decide(live(soc_pct=soc)).suggested_amps, 0)

    def test_three_stable_samples_to_start(self):
        self.assertEqual(decide(stable=1).suggested_amps, 0)
        self.assertEqual(decide(stable=2).suggested_amps, 0)
        self.assertEqual(decide(stable=3).suggested_amps, 6)

    def test_not_enough_solar_to_supply_6a(self):
        self.assertEqual(decide(live(grid_w=1300)).suggested_amps, 0)
        self.assertEqual(decide(live(grid_w=-100)).suggested_amps, 0)

    def test_running_ev_does_not_create_solar_surplus_oscillation(self):
        # A running 6 A charger consumes 1380 W, so export decreases from
        # 1700 W to 320 W. Available budget must include measured EV watts.
        result = decide(live(grid_w=320, house_load_w=2280),
                        ev=6, running=True, stable=0)
        self.assertEqual(result.suggested_amps, 6)
        self.assertEqual(result.surplus_w, 1500)

    def test_grid_import_reduces_or_pauses(self):
        self.assertEqual(decide(live(grid_w=-350),
                                ev=6, running=True).suggested_amps, 0)

    def test_house_battery_discharging_always_pauses(self):
        self.assertEqual(decide(live(battery_w=250),
                                ev=6, running=True).suggested_amps, 0)

    def test_restart_below_99_5_but_keep_running_to_99(self):
        reading = live(soc_pct=99.2, grid_w=350)
        self.assertEqual(decide(reading, ev=6, running=True).suggested_amps, 6)
        self.assertEqual(decide(reading, ev=0, running=False).suggested_amps, 0)

    def test_missing_old_and_nonfinite_measurements_fail_closed(self):
        invalid = [
            live(timestamp=(NOW - timedelta(seconds=91)).isoformat()),
            live(grid_w=None),
            live(pv_w=float("nan")),
            live(battery_w="unavailable"),
            live(house_load_w=-10),
            live(soc_pct=102),
        ]
        for reading in invalid:
            with self.subTest(reading=reading):
                self.assertEqual(decide(reading, ev=6, running=True).suggested_amps, 0)
        self.assertEqual(decide(None, ev=None, running=True).suggested_amps, 0)

    def test_low_price_grid_can_charge_without_solar_surplus(self):
        result = decide(live(grid_w=-200, pv_w=0), grid_topup_a=6, stable=0)
        self.assertEqual(result.suggested_amps, 6)
        self.assertEqual(result.permitted_amps, 0)  # Phase protection still unverified.
        self.assertIn("price-scheduled grid", result.reason)

    def test_solar_and_low_price_grid_combine_within_current_cap(self):
        result = decide(live(grid_w=900), grid_topup_a=3, stable=3,
                        max_current_a=10)
        # (900-200)/230 gives 3 A solar, plus 3 A economically bought.
        self.assertEqual(result.suggested_amps, 6)
        self.assertIn("solar and price-scheduled grid", result.reason)

    def test_expensive_grid_never_subsidizes_missing_solar(self):
        self.assertEqual(decide(live(grid_w=100), grid_topup_a=0).suggested_amps, 0)
        self.assertEqual(decide(live(grid_w=900), grid_topup_a=3,
                                stable=0, max_current_a=10).suggested_amps, 0)

    def test_house_battery_priority_and_protection_also_apply_to_cheap_grid(self):
        self.assertEqual(decide(live(soc_pct=97, grid_w=-100),
                                grid_topup_a=6).suggested_amps, 0)
        self.assertEqual(decide(live(battery_w=220, grid_w=-100),
                                grid_topup_a=6).suggested_amps, 0)
        self.assertEqual(decide(live(timestamp=(NOW - timedelta(minutes=3)).isoformat()),
                                grid_topup_a=6).suggested_amps, 0)
        self.assertEqual(decide(live(grid_w=-100), grid_topup_a=6,
                                phase=True, headroom=5).permitted_amps, 0)

    def test_headroom_is_always_capped(self):
        self.assertEqual(decide(phase=True, headroom=16,
                                max_current_a=16).permitted_amps, 6)
        self.assertEqual(decide(live(grid_w=4000), phase=True,
                                headroom=11, max_current_a=16).permitted_amps, 11)


if __name__ == "__main__":
    unittest.main()
