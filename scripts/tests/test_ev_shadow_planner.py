"""No-Waveshare, no-controller tests of the separate EV price-only scenario."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))
import ev_shadow_planner as planner

NOW = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)
LOCAL = ZoneInfo("Europe/Stockholm")


def plan(buys, *, sell=5):
    p = dict(logged_at=NOW.isoformat(), inputs=[], dispatch=[])
    for i, buy in enumerate(buys):
        time_local = (NOW + timedelta(minutes=15 * i)).astimezone(LOCAL)
        stamp = time_local.strftime("%Y-%m-%dT%H:%M:%S")
        p["inputs"].append(dict(startTime=stamp, buyPrice=buy,
                                sellPrice=sell, priceSource="actual"))
        p["dispatch"].append(dict(startTime=stamp, solarExportKwh=0))
    return p


def session(*, mode="economy", remaining=1):
    return dict(charge_mode=mode, remaining_ac_kwh=remaining,
                departure_at_utc=(NOW + timedelta(minutes=10)
                                  if mode == "deadline" else None),
                target_soc_pct=80, max_buy_ore_per_kwh=None,
                observation_quality="api_recent_field_change_unverified",
                shadow_only=True, physical_control_eligible=False,
                energy_estimate_unverified=True)


def live(*, soc=100, grid_w=0, battery_w=0, fresh=True):
    return dict(timestamp=(NOW if fresh else NOW - timedelta(minutes=3)).isoformat(),
                soc_pct=soc, grid_w=grid_w, battery_w=battery_w,
                pv_w=4000, house_load_w=900)


class ShadowEconomicTests(unittest.TestCase):
    def tick(self, *, sess=None, price=None, inverter=None, streak=0):
        if sess is None:
            sess = session()
        if price is None:
            price = plan((20,) + (180,) * 31)
        if inverter is None:
            inverter = live(soc=30)
        with mock.patch.object(planner, "read_shadow_ev_session", return_value=sess) as read, \
             mock.patch.object(planner, "read_house_price_plan", return_value=price) as plan_read, \
             mock.patch.object(planner.common, "read_json", return_value=inverter):
            result, new_streak = planner.monitor_once(now=NOW, streak=streak)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(plan_read.call_count, 1)
        self.assertFalse(result["phase_guard_verified"])
        self.assertEqual(result["charging_permitted_a"], 0)
        self.assertEqual(result["solar_permitted_a"], 0)
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["source"], "ha_price_scenario_no_evcc")
        self.assertTrue(result["energy_estimate_unverified"])
        return result, new_streak

    def test_economy_charges_only_cheap_window_without_battery_discharge(self):
        result, _ = self.tick()
        self.assertEqual(result["charge_mode"], "economy")
        self.assertEqual(result["grid_topup_suggested_a"], 6)
        self.assertEqual(result["charging_suggested_a"], 6)
        self.assertEqual(result["charging_permitted_a"], 0)

        result, _ = self.tick(price=plan((180,) + (20,) * 31))
        self.assertEqual(result["grid_topup_suggested_a"], 0)
        self.assertEqual(result["charging_suggested_a"], 0)

    def test_deadline_urgency_beats_expensive_price_but_not_battery_guard(self):
        result, _ = self.tick(sess=session(mode="deadline"),
                              price=plan((1000,) + (10,) * 31))
        self.assertEqual(result["grid_topup_suggested_a"], 6)
        self.assertEqual(result["charging_suggested_a"], 6)
        self.assertEqual(result["charging_permitted_a"], 0)

        blocked, _ = self.tick(sess=session(mode="deadline"),
                               inverter=live(battery_w=300))
        self.assertEqual(blocked["charging_suggested_a"], 0)
        self.assertIn("house battery discharging", blocked["reason"])

    def test_solar_requires_stable_surplus_and_full_house(self):
        p = plan((180,) + (20,) * 31)
        inv = live(soc=100, grid_w=2500)
        for streak in (0, 1):
            result, next_streak = self.tick(price=p, inverter=inv, streak=streak)
            self.assertEqual(result["solar_suggested_a"], 0)
            self.assertEqual(next_streak, streak + 1)
        result, next_streak = self.tick(price=p, inverter=inv, streak=2)
        self.assertEqual(result["solar_suggested_a"], 6)
        self.assertEqual(result["charging_suggested_a"], 6)
        self.assertEqual(result["charging_permitted_a"], 0)
        self.assertEqual(next_streak, 3)

    def test_shadow_stops_at_80_even_with_export_and_negative_price(self):
        result, _ = self.tick(sess=session(remaining=0),
                              price=plan((0,) * 32),
                              inverter=live(soc=100, grid_w=5000))
        self.assertEqual(result["charging_suggested_a"], 0)
        self.assertEqual(result["solar_suggested_a"], 0)
        self.assertEqual(result["grid_topup_suggested_a"], 0)

    def test_stale_inverter_refuses_even_economy_cheap_grid(self):
        result, _ = self.tick(inverter=live(fresh=False))
        self.assertEqual(result["charging_suggested_a"], 0)
        self.assertEqual(result["grid_topup_suggested_a"], 6)
        self.assertIn("stale inverter", result["reason"])

    def test_no_session_never_opens_price_file_or_funds_grid(self):
        with mock.patch.object(planner, "read_shadow_ev_session", return_value=None), \
             mock.patch.object(planner, "read_house_price_plan") as price_read, \
             mock.patch.object(planner.common, "read_json", return_value=live()):
            result, streak = planner.monitor_once(now=NOW)
        price_read.assert_not_called()
        self.assertEqual(result["charging_suggested_a"], 0)
        self.assertEqual(result["grid_topup_suggested_a"], 0)
        self.assertIsNone(result["energy_estimate_unverified"])
        self.assertEqual(streak, 0)

    def test_evcc_reader_is_read_only_and_stale_observations_are_discarded(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evcc.json"
            valid = {
                "timestamp": NOW.isoformat(),
                "source": "waveshare_evcc_fc03_readonly",
                "read_ok": True,
                "modbus_function": "FC03",
                "physical_control_eligible": False,
                "writes": 0,
                "pilot_matches_config": True,
                "measured_current_a": 9.6,
                "charging_detected": True,
                "configured_pwm_raw": 166,
                "pilot_pwm_raw": 166,
                "state": "B2",
            }
            path.write_text(json.dumps(valid))
            parsed = planner.read_evcc_observation(str(path), now=NOW)
            self.assertEqual(parsed["measured_current_a"], 9.6)
            self.assertTrue(parsed["charging_detected"])
            self.assertFalse(parsed["physical_control_eligible"])
            self.assertIsNone(planner.read_evcc_observation(
                str(path), now=NOW + timedelta(minutes=3)))
            for key, value in (("writes", 1), ("physical_control_eligible", True),
                               ("modbus_function", "FC10"),
                               ("read_ok", False), ("charging_detected", False)):
                with self.subTest(key=key):
                    path.write_text(json.dumps({**valid, key: value}))
                    self.assertIsNone(planner.read_evcc_observation(str(path), now=NOW))

    def test_observed_evcc_traffic_updates_shadow_not_physical_permission(self):
        observed = dict(
            measured_current_a=6.0,
            charging_detected=True,
            configured_pwm_raw=100,
            pilot_pwm_raw=100,
            pilot_matches_config=True,
            state="C2",
            physical_control_eligible=False,
        )
        with mock.patch.object(planner, "read_shadow_ev_session", return_value=session()), \
             mock.patch.object(planner, "read_house_price_plan",
                               return_value=plan((0,) * 32)), \
             mock.patch.object(planner, "read_evcc_observation",
                               return_value=observed), \
             mock.patch.object(planner.common, "read_json",
                               return_value=live(grid_w=-300, soc=100)):
            result, _ = planner.monitor_once(now=NOW, streak=3)
        self.assertEqual(result["source"], "ha_price_scenario_with_evcc")
        self.assertEqual(result["ev_measured_current_a"], 6)
        self.assertTrue(result["ev_measured_current_available"])
        self.assertEqual(result["evcc_state"], "C2")
        self.assertEqual(result["evcc_configured_pwm_raw"], 100)
        self.assertEqual(result["charging_permitted_a"], 0)
        self.assertEqual(result["solar_permitted_a"], 0)
        self.assertEqual(result["writes"], 0)

    def test_service_is_disabled_by_default_and_never_touches_hardware(self):
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, {"EV_SHADOW_PLANNER_ENABLED": "0"}), \
             mock.patch.object(sys, "argv", ["ev_shadow_planner.py"]), \
             mock.patch.object(planner, "monitor_once") as monitor, \
             redirect_stdout(buffer):
            planner.main()
        monitor.assert_not_called()
        result = json.loads(buffer.getvalue())
        self.assertEqual(result["mode"], "disabled")
        self.assertEqual(result["writes"], 0)
        source = Path(planner.__file__).read_text()
        self.assertNotIn("import evcc_ascii", source)
        self.assertNotIn("import volvo_mobile", source)
        self.assertNotIn("import modbus", source)


if __name__ == "__main__":
    unittest.main()
