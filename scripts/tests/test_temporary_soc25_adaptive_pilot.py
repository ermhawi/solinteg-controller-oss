"""Offline verification of the approved 1.5kW adaptive SoC recovery pilot."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

import l3_charge_guard
import temporary_soc25_adaptive_dispatch as launcher
import temporary_soc25_adaptive_watchdog as adaptive
import temporary_soc25_watchdog as common

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


class DispatcherGuardTests(unittest.TestCase):
    def setUp(self):
        self.live = {
            "timestamp": NOW.isoformat(), "meter_l1_w": -400,
            "meter_l2_w": -300, "meter_l3_w": -2000,
            "grid_w": -2700, "inverter_ac_w": 600,
            "house_load_w": 3300, "pv_w": 200,
            "grid_unbalanced_output_raw": 0,
        }
        self.source = SimpleNamespace(
            SOC_FLOOR_RECOVERY_W=1500,
            L3_CHARGE_GUARD_MODE="1",
            L3_CHARGE_BUDGET_W=3000,
            LOOP_INTERVAL_S=30,
        )

        def normal_guard(action, watt, detail, numbers, now):
            if action != "charge" or watt <= self.source.SOC_FLOOR_RECOVERY_W:
                return watt, detail, numbers, False
            measured = l3_charge_guard.observe_l3_house_w(self.live, now)
            balanced = self.live.get("grid_unbalanced_output_raw") == 0
            target, reason = l3_charge_guard.cap_grid_charge_w(
                watt, house_l3_w=measured, balanced_output=balanced,
                budget_w=self.source.L3_CHARGE_BUDGET_W,
                min_charge_w=self.source.SOC_FLOOR_RECOVERY_W)
            return target, reason, numbers, target < watt

        def normal_needs_apply(target, last_target, action, power, due, *, soc_recovery=False):
            return due or last_target is None or (abs(power-last_target[2]) >= 250)

        self.source.guard_l3_charge = normal_guard
        self.source.needs_apply = normal_needs_apply
        launcher.install_adaptive_guard(self.source)

    def request(self, watts=1500):
        return self.source.guard_l3_charge("charge", watts, "", {}, NOW)

    def test_low_l3_charge_at_1500(self):
        requested, _, numbers, limited = self.request()
        self.assertEqual(requested, 1500)
        self.assertFalse(limited)
        self.assertEqual(numbers["temp_soc25_adaptive_charge_max_w"], 1500)
        self.assertEqual(self.source.SOC_FLOOR_RECOVERY_W, 1500)

    def test_phase_loading_derates_to_1100(self):
        self.live.update(meter_l3_w=-2400, grid_w=-3100, house_load_w=3700)
        commanded, _, metrics, limited = self.request()
        self.assertEqual(commanded, 1100)
        self.assertTrue(limited)
        self.assertEqual(metrics["temp_soc25_adaptive_applied_w"], 1100)

    def test_phase_loading_derates_to_original_300w_floor(self):
        self.live.update(meter_l3_w=-2800, grid_w=-3500, house_load_w=4100)
        commanded, _, _, limited = self.request()
        self.assertEqual(commanded, 300)
        self.assertTrue(limited)

    def test_invalid_balance_or_stale_sample_derates_to_300(self):
        for patch in (
            {"grid_unbalanced_output_raw": 1},
            {"timestamp": (NOW-timedelta(minutes=3)).isoformat()},
            {"meter_l3_w": None},
        ):
            with self.subTest(change=patch):
                self.live.update({
                    "timestamp": NOW.isoformat(),
                    "grid_unbalanced_output_raw": 0,
                    "meter_l3_w": -2000,
                })
                self.live.update(patch)
                self.assertEqual(self.request()[0], 300)
                self.assertEqual(self.source.SOC_FLOOR_RECOVERY_W, 1500)

    def test_any_downward_charge_retarget_is_not_deadbanded(self):
        last = ("slot", "charge", 1500)
        self.assertTrue(self.source.needs_apply(
            ("slot", "charge", 1400), last, "charge", 1400, False))
        self.assertFalse(self.source.needs_apply(
            ("slot", "charge", 1500), last, "charge", 1500, False))

    def test_non_charge_actions_unchanged(self):
        watts, _, _, limited = self.source.guard_l3_charge(
            "discharge", 500, "", {}, NOW)
        self.assertEqual((watts, limited), (500, False))

    def test_original_minimum_restored_after_exception(self):
        self.source.guard_l3_charge = mock.Mock(side_effect=RuntimeError("bad CT"))
        # A new fake object avoids accidentally replacing the already-wrapped
        # guard (the real module only installs once at process startup).
        other = SimpleNamespace(**vars(self.source))
        other.guard_l3_charge = mock.Mock(side_effect=RuntimeError("broken"))
        launcher.install_adaptive_guard(other)
        with self.assertRaises(RuntimeError):
            other.guard_l3_charge("charge", 1500, "", {}, NOW)
        self.assertEqual(other.SOC_FLOOR_RECOVERY_W, 1500)

    def test_rejects_bad_process_mode_and_env(self):
        for key, value in (
            ("SOLINTEG_BATTERY_MAX_W", "2000"),
            ("DISPATCH_SOC_RECOVERY_EXIT_PCT", "21"),
            ("DISPATCH_L3_CHARGE_GUARD_MODE", "shadow"),
            ("SOLINTEG_TEMP_SOC25_ADAPTIVE", None),
        ):
            with self.subTest(key=key):
                e=dict(launcher.REQUIRED_ENV)
                if value is None:
                    e.pop(key)
                else:
                    e[key]=value
                with self.assertRaises(RuntimeError):
                    launcher.validate_configuration(e)
        launcher.validate_configuration(launcher.REQUIRED_ENV)

    def test_invalid_dispatcher_guard_mode_refused(self):
        x=SimpleNamespace(**vars(self.source))
        x.L3_CHARGE_GUARD_MODE="shadow"
        with self.assertRaises(RuntimeError):
            launcher.install_adaptive_guard(x)


class AdaptiveWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name)
        self.statusfile=self.folder/"status.json"
        self.dropin=self.folder/"dropin.conf"
        self.wrapper=self.folder/"wrapper.py"
        self.dispatch=self.folder/"dispatch.py"
        self.gate=self.folder/"gate.json"
        self.livefile=self.folder/"live.json"
        self.evfile=self.folder/"evcc.json"
        self.wrapper.write_text("print('wrapper')\n")
        self.dispatch.write_text("print('dispatch')\n")
        self.gate.write_text(json.dumps({
            "mode":"shadow","schema_version":2,
            "dispatcher_sha256":common._checksum(self.dispatch)}))
        self.live={
            "timestamp": NOW.isoformat(), "soc_pct":21.0,
            "battery_w":-1480, "pv_w":500,
            "grid_w":-3500,"meter_l1_w":-800,"meter_l2_w":-500,
            "meter_l3_w":-2200,"inverter_ac_w":300,
            "house_load_w":3800,"grid_unbalanced_output_raw":0,
            "battery_temp_c":13,
            "work_mode_raw":0x303,
        }
        self.evcc={
            "timestamp":NOW.isoformat(), "read_ok":True,
            "state":"C2","charging_detected":True,
            "configured_pwm_raw":100,"pilot_pwm_raw":100,
            "channel_currents_a":[.1,6.0,.1],
            "writes":0
        }
        self.livefile.write_text(json.dumps(self.live))
        self.evfile.write_text(json.dumps(self.evcc))
        self.dropin.write_text(adaptive.MARKER+"\n[Service]\n")
        self.state={
            "schema_version":1,"pilot_id":adaptive.PILOT_ID,
            "state":"running",
            "started_at_utc":(NOW-timedelta(seconds=180)).isoformat(),
            "expires_at_utc":(NOW+timedelta(hours=2)).isoformat(),
            "dispatcher_sha256":common._checksum(self.dispatch),
            "dropin_sha256":common._checksum(self.dropin),
            "adaptive_wrapper_sha256":common._checksum(self.wrapper),
        }
        self.statusfile.write_text(json.dumps(self.state))
        self.patches=[
            mock.patch.object(adaptive, "STATE", self.statusfile),
            mock.patch.object(adaptive, "DROPIN", self.dropin),
            mock.patch.object(adaptive, "WRAPPER", self.wrapper),
            mock.patch.object(adaptive, "wrapper_process_ok", return_value=True),
            mock.patch.object(adaptive, "original_safety_watchdogs_ok", return_value=True),
            mock.patch.object(common, "GATE", self.gate),
            mock.patch.object(common, "DISPATCH", self.dispatch),
            mock.patch.object(common, "LIVE", self.livefile),
            mock.patch.object(common, "EVCC", self.evfile),
            mock.patch.object(common, "_dispatch_running", return_value=True),
            mock.patch.object(common, "_process_overrides_present", return_value=True),
        ]
        for name in ("STATE","DROPIN","TIMER","PILOT_MARKER","OVERRIDE_ENV",
                     "MAX_CHARGE_MEASURED_W","MAX_PHASE_GRID_IMPORT_W","MAX_SITE_IMPORT_W"):
            original=getattr(common,name)
            self.addCleanup(setattr,common,name,original)
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def run_tick(self):
        with mock.patch.object(adaptive, "datetime") as date:
            date.now.return_value=NOW
            return adaptive.tick()

    def test_normal_1500w_observation_continues(self):
        result=self.run_tick()
        self.assertEqual(result["state"],"running")
        self.assertTrue(result["measurements"]["adaptive_wrapper_verified"])
        self.assertEqual(common.MAX_CHARGE_MEASURED_W,1850)
        self.assertEqual(common.MAX_PHASE_GRID_IMPORT_W,3200)
        self.assertEqual(common.MAX_SITE_IMPORT_W,6500)

    def test_measured_1900w_charge_aborts_not_continue(self):
        self.live["battery_w"]=-1900
        self.livefile.write_text(json.dumps(self.live))
        with mock.patch.object(common,"_restore",return_value={"state":"aborted"}) as revert:
            self.assertEqual(self.run_tick()["state"],"aborted")
            self.assertIn("1850 W",revert.call_args.kwargs["reason"])

    def test_same_3200w_phase_stop_as_old_pilot(self):
        self.live.update(meter_l3_w=-3277,grid_w=-4577,house_load_w=4877)
        self.livefile.write_text(json.dumps(self.live))
        with mock.patch.object(common,"_restore",return_value={"state":"aborted"}) as revert:
            self.assertEqual(self.run_tick()["state"],"aborted")
            self.assertIn("L1/L2/L3",revert.call_args.kwargs["reason"])

    def test_reaches_25_and_restores_completed(self):
        self.live["soc_pct"]=25
        self.livefile.write_text(json.dumps(self.live))
        with mock.patch.object(common,"_restore",return_value={"state":"completed"}) as restore:
            self.assertEqual(self.run_tick()["state"],"completed")
            self.assertTrue(restore.call_args.kwargs["completed"])

    def test_wrapper_tamper_aborts(self):
        self.wrapper.write_text("tampered\n")
        with mock.patch.object(common,"_restore",return_value={"state":"aborted"}) as revert:
            self.assertEqual(self.run_tick()["state"],"aborted")
            self.assertIn("launcher changed",revert.call_args.kwargs["reason"])

    def test_process_not_wrapper_aborts_after_settle(self):
        with mock.patch.object(adaptive,"wrapper_process_ok",return_value=False), \
             mock.patch.object(common,"_restore",return_value={"state":"aborted"}) as revert:
            self.assertEqual(self.run_tick()["state"],"aborted")
            self.assertIn("clamp",revert.call_args.kwargs["reason"])

    def test_deadline_aborts(self):
        self.state["expires_at_utc"]=(NOW-timedelta(seconds=1)).isoformat()
        self.statusfile.write_text(json.dumps(self.state))
        with mock.patch.object(common,"_restore",return_value={"state":"aborted"}) as revert:
            self.assertEqual(self.run_tick()["state"],"aborted")
            self.assertIn("deadline",revert.call_args.kwargs["reason"])

    def test_original_independent_watchdog_unhealthy_aborts(self):
        with mock.patch.object(adaptive, "original_safety_watchdogs_ok", return_value=False), \
             mock.patch.object(common, "_restore", return_value={"state": "aborted"}) as revert:
            self.assertEqual(self.run_tick()["state"], "aborted")
            self.assertIn("watchdog unhealthy", revert.call_args.kwargs["reason"])

    def test_unexpected_gate_change_aborts(self):
        self.gate.write_text(json.dumps({
            "schema_version":2,"mode":"active",
            "dispatcher_sha256":common._checksum(self.dispatch)}))
        with mock.patch.object(common,"_restore",return_value={"state":"aborted"}) as revert:
            self.assertEqual(self.run_tick()["state"],"aborted")
            self.assertIn("gate",revert.call_args.kwargs["reason"])

    def test_stop_only_rolls_back_own_dropin(self):
        with mock.patch.object(common,"_service") as service, \
             mock.patch.object(common,"_dispatch_running",return_value=True), \
             mock.patch.object(common,"_process_overrides_present",return_value=False), \
             mock.patch.object(common.subprocess,"run"):
            result=adaptive.tick(force_stop=True)
        self.assertEqual(result["state"],"aborted")
        self.assertFalse(self.dropin.exists())
        service.assert_any_call("restart",common.SERVICE)
        self.assertEqual(json.loads(self.statusfile.read_text())["physical_control_authorized"],
                         False)

    def test_no_separate_modbus_writer(self):
        for module in (adaptive, launcher):
            source=Path(module.__file__).read_text()
            self.assertNotIn("ModbusTcpClient",source)
            self.assertNotIn("write_register(",source)
        self.assertEqual(adaptive.OVERRIDE_ENV,launcher.REQUIRED_ENV)


if __name__ == "__main__":
    unittest.main()
