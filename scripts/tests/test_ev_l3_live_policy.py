"""Live EV solar-capture policy: bounded charge-only trials, no device writes."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_l3_live_dispatch as adapter  # noqa: E402
import ev_l3_live_policy as policy  # noqa: E402
from test_ev_grid_priority_guard import EVCC, LIVE, NOW  # noqa: E402


def sun():
    return {**LIVE, "work_mode_raw": 0x101,
            "pv_w": 2026, "battery_w": 196,
            "inverter_ac_w": 2173, "house_load_w": 2137, "grid_w": 36,
            "meter_l1_w": 400, "meter_l2_w": 391, "meter_l3_w": -755,
            "soc_pct": 21, "battery_temp_c": 16}


class LiveEvPolicyTests(unittest.TestCase):
    def call(self, action="idle", power_w=0, live=None, evcc=None, **kw):
        return policy.evaluate(action, power_w, sun() if live is None else live,
                               EVCC if evcc is None else evcc, now=NOW, **kw)

    def test_daylight_pv_capture_is_bounded_600_w(self):
        s = self.call()
        self.assertEqual((s.state, s.action, s.power_w), ("live_solar_capture", "charge", 600))
        self.assertEqual(s.metrics["ev_w_est"], 1380)
        self.assertEqual(s.metrics["non_ev_house_w_est"], 757)
        self.assertEqual(s.metrics["solar_headroom_w_est"], 1269)
        self.assertLessEqual(max(s.metrics["predicted_phase_import_w"]), policy.MAX_PHASE_IMPORT_W)
        self.assertFalse(s.metrics["physical_fuse_protection"])
        self.assertEqual(s.metrics["device_writes"], 0)
        self.assertEqual(s.metrics["evcc_writes"], 0)

    def test_planned_charge_recovery_and_hold_are_untouched(self):
        for action in ("charge", "solar_charge", "freeze", "reserve_hold"):
            with self.subTest(action=action):
                s = self.call(action=action, power_w=350)
                self.assertEqual(s.state, "preserve_existing_action")
                self.assertIsNone(s.action)
        self.assertEqual(self.call(soc_recovery=True).state, "preserve_existing_action")

    def test_no_solar_pv_headroom_does_not_force_grid_battery_charge(self):
        # Balanced measurements; no verified PV surplus -> no forced charge.
        s = self.call(live={**sun(), "pv_w": 0,
                            "battery_w": 2000, "inverter_ac_w": 2000,
                            "house_load_w": 1964})
        self.assertEqual(s.state, "insufficient_verified_solar")
        self.assertIsNone(s.power_w)

    def test_solar_hysteresis_avoids_commands_below_margin(self):
        base = sun()
        # Reconcile PV-battery/AC when PV falls, keeping the CT/house totals constant.
        base.update(pv_w=1500, battery_w=722)
        s = self.call(live=base)
        self.assertEqual(s.state, "insufficient_verified_solar")
        self.assertIsNone(s.action)

    def test_stale_and_unverified_evcc_fail_closed(self):
        for e in ({**EVCC, "timestamp": (NOW-timedelta(seconds=65)).isoformat()},
                  {**EVCC, "state": "B2"},
                  {**EVCC, "read_ok": False},
                  {**EVCC, "configured_pwm_raw": 1000},
                  {**EVCC, "pilot_pwm_raw": 267},
                  {**EVCC, "channel_currents_a": [6., .1, .1]},
                  {**EVCC, "writes": 1},
                  {**EVCC, "source": "unknown"}):
            with self.subTest(evcc=e):
                self.assertEqual(self.call(evcc=e).state, "unavailable")

    def test_other_phase_or_forged_sum_cannot_be_used(self):
        for currents, total in [([.8, 6, 0], 6.8), ([.1, 10, .1], 10.2),
                                ([.1, 6, .1], 200), ([None, None, None], None)]:
            e={**EVCC, "channel_currents_a": currents, "measured_current_a": total}
            self.assertEqual(self.call(evcc=e).state, "unavailable")

    def test_unbalanced_or_stale_inverter_cannot_trigger_control(self):
        for fields in ({"grid_unbalanced_output_raw": 1}, {"grid_w": 9000},
                       {"meter_l3_w": None}, {"work_mode_raw": 500},
                       {"timestamp":(NOW-timedelta(seconds=95)).isoformat()},
                       {"battery_w":float("nan")}):
            with self.subTest(fields=fields):
                self.assertEqual(self.call(live={**sun(),**fields}).state, "unavailable")

    def test_bms_temperature_and_high_soc_veto(self):
        for fields in ({"soc_pct": 97}, {"soc_pct": 11},
                       {"battery_temp_c": 3}, {"battery_temp_c": 50}):
            self.assertEqual(self.call(live={**sun(),**fields}).state, "soc_or_bms_guard")

    def test_predicted_import_soft_bound_rejects_overdraw(self):
        base = sun()
        base.update(pv_w=5600, battery_w=-2600, inverter_ac_w=3000,
                    house_load_w=6000, grid_w=-3000,
                    meter_l1_w=-3400, meter_l2_w=1000, meter_l3_w=-600)
        # All three inferred phase loads are non-negative; L1 still exceeds
        # the provisional import envelope. Fail before any physical write.
        self.assertEqual(self.call(live=base).state, "import_guard")

    def test_discharge_uses_non_ev_house_and_l3_load_when_solar_is_insufficient(self):
        # EV is 1380 W of the 2137 W measured site load, not house demand.
        base = {**sun(), "pv_w": 1500, "battery_w": 722}
        s = self.call(action="discharge", power_w=2200, live=base)
        self.assertEqual((s.state, s.action, s.power_w),
                         ("grid_priority_discharge_cap", "discharge", 50))
        self.assertEqual(s.metrics["total_house_w"], 2137)
        self.assertEqual(s.metrics["non_ev_house_w_est"], 757)
        self.assertEqual(s.metrics["ev_grid_target_w_est"], 1380)
        self.assertEqual(s.metrics["discharge_cap_w"], 50)

    def test_grid_first_discharge_cap_does_not_override_existing_small_target(self):
        base = {**sun(), "pv_w": 1500, "battery_w": 722}
        s = self.call(action="discharge", power_w=50, live=base)
        self.assertEqual(s.state, "insufficient_verified_solar")
        self.assertIsNone(s.action)

    def test_grid_first_discharge_never_changes_soc_recovery_or_hold(self):
        base = {**sun(), "pv_w": 1500, "battery_w": 722}
        self.assertEqual(self.call("discharge", 2200, live=base,
                                   soc_recovery=True).state, "preserve_existing_action")
        self.assertEqual(self.call("freeze", 0, live=base).state, "preserve_existing_action")

    def test_discharge_cap_is_applied_by_the_existing_broker_dispatch_tuple(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            a = path / "live.json"; b = path / "evcc.json"
            a.write_text(json.dumps({**sun(), "pv_w": 1500, "battery_w": 722}))
            b.write_text(json.dumps(EVCC))
            def reader(p): return json.loads(Path(p).read_text())
            args = ("discharge", 2200, 5.0, False, "planned", {}, NOW)
            with patch.dict(os.environ, {"DISPATCH_EV_GRID_LIVE": "1"}):
                out = adapter.coordinate(*args, live_path=a, evcc_path=b,
                                         read_json=reader)
            self.assertEqual(out[:4], ("discharge", 50, None, False))
            self.assertEqual(out[5]["ev_grid_live_status"], "grid_priority_discharge_cap")
            self.assertEqual(out[5]["ev_grid_live_discharge_cap_w"], 50)
            self.assertEqual(out[5]["ev_grid_live_ev_w_est"], 1380)

    def test_discharge_is_reduced_to_battery_charge_when_solar_verified(self):
        s=self.call(action="discharge",power_w=2200)
        self.assertEqual((s.state,s.action,s.power_w),("live_solar_capture","charge",600))

    def test_adapter_enabled_only_by_explicit_env(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)
            a=path/"live.json";b=path/"evcc.json"
            a.write_text(json.dumps(sun()));b.write_text(json.dumps(EVCC))
            def reader(p): return json.loads(Path(p).read_text())
            args=("idle", 0, None, False, "initial", {}, NOW)
            with patch.dict(os.environ,{"DISPATCH_EV_GRID_LIVE":"0"}):
                out=adapter.coordinate(*args,live_path=a,evcc_path=b,read_json=reader)
                self.assertEqual(out,args[:6])
            with patch.dict(os.environ,{"DISPATCH_EV_GRID_LIVE":"1"}):
                out=adapter.coordinate(*args,live_path=a,evcc_path=b,read_json=reader)
                self.assertEqual(out[:4],("charge",600,None,False))
                self.assertIn("PV-to-house-battery",out[4])
                self.assertEqual(out[5]["ev_grid_live_status"],"live_solar_capture")
                self.assertEqual(out[5]["ev_grid_live_charge_target_w"],600)
                # EV disappeared or telemetry invalid -> original decision preserved.
                b.write_text('{"bad":true}')
                out=adapter.coordinate(*args,live_path=a,evcc_path=b,read_json=reader)
                self.assertEqual(out[:5],args[:5])
                self.assertEqual(out[5]["ev_grid_live_status"],"unavailable")

    def test_adapter_never_contains_physical_writer(self):
        s=Path(adapter.__file__).read_text()
        for forbidden in ("import inverter_control","write_register","force_charge(",
                          "ModbusTcpClient","return_to_auto("):
            self.assertNotIn(forbidden,s)


if __name__=="__main__":
    unittest.main()
