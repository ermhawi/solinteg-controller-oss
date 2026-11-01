"""Offline regression for the EV-paused 1.5kW SoC recovery trial.

All tests use fakes/snapshots: no real inverter, EVCC or systemd writes.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "services"))
sys.path.insert(0, str(BASE / "tools"))

import l3_charge_guard
import temporary_soc25_evpaused_dispatch as launcher
import temporary_soc25_evpaused_watchdog as pilot
import temporary_soc25_watchdog as common
import preflight_soc25_evpaused as preflight
import start_soc25_evpaused_pilot as installer

NOW = datetime(2026, 11, 1, 10, 30, tzinfo=timezone.utc)
LIVE = {
    "timestamp": NOW.isoformat(), "soc_pct": 20, "battery_w": -700,
    "battery_temp_c": 14, "pv_w": 400, "inverter_ac_w": -300,
    "house_load_w": 2200, "grid_w": -2500,
    "meter_l1_w": -700, "meter_l2_w": -600, "meter_l3_w": -1200,
    "grid_unbalanced_output_raw": 0, "work_mode_raw": 0x303,
}
EVCC = {
    "timestamp": NOW.isoformat(),
    "source": "waveshare_evcc_fc03_readonly", "read_ok": True,
    "state": "B2", "paused_by_pwm": True,
    "configured_pwm_raw": 1000, "pilot_pwm_raw": 267,
    "measured_current_a": .3, "charging_detected": False, "writes": 0,
}


class PreflightTests(unittest.TestCase):
    def history(self):
        return [{**LIVE, "timestamp": (NOW-timedelta(seconds=30*i)).isoformat()}
                for i in range(11)]

    def check(self, live=None, evcc=None, hist=None, **kwargs):
        return preflight.admit(
            LIVE if live is None else live, EVCC if evcc is None else evcc,
            self.history() if hist is None else hist, now=NOW,
            runtime_ok=kwargs.get("runtime_ok", True),
            gate_absent=kwargs.get("gate_absent", True),
            dispatcher_sha_ok=kwargs.get("dispatcher_sha_ok", True),
            dropins_absent=kwargs.get("dropins_absent", True))

    def test_nominal_preflight_only_admits_no_physical_writes(self):
        r = self.check()
        self.assertTrue(r["eligible"], r["reasons"])
        self.assertEqual(r["device_writes"], 0)
        self.assertFalse(r["physical_control_authorized"])

    def test_live_or_history_soc_below_19_fails(self):
        self.assertFalse(self.check(live={**LIVE, "soc_pct": 18})["eligible"])
        hist = self.history()
        hist[3]["soc_pct"] = 18.9
        r = self.check(hist=hist)
        self.assertIn("house SoC below 19% in recent history", r["reasons"])

    def test_wrong_evcc_pause_state_never_admits(self):
        for e in ({"state": "C2"}, {"configured_pwm_raw": 100},
                  {"measured_current_a": 6}, {"charging_detected": True},
                  {"paused_by_pwm": False}, {"writes": 1}):
            with self.subTest(e=e):
                self.assertFalse(self.check(evcc={**EVCC, **e})["eligible"])

    def test_preflight_allows_preexisting_charge_to_be_reduced_from_2100w(self):
        high = {**LIVE, "pv_w": 400, "battery_w": -2100,
                "inverter_ac_w": -1700, "house_load_w": 2200,
                "grid_w": -3900, "meter_l1_w": -1000,
                "meter_l2_w": -1100, "meter_l3_w": -1800}
        result = self.check(live=high)
        self.assertTrue(result["eligible"], result["reasons"])
        self.assertEqual(result["measurements"]["existing_battery_charge_w"], 2100)

    def test_preflight_rejects_original_charge_above_3000w(self):
        high = {**LIVE, "pv_w": 400, "battery_w": -3100,
                "inverter_ac_w": -2700, "house_load_w": 1200,
                "grid_w": -3900, "meter_l1_w": -1000,
                "meter_l2_w": -1100, "meter_l3_w": -1800}
        result = self.check(live=high)
        self.assertFalse(result["eligible"])
        self.assertIn("existing battery charging outside 100-3000W admission envelope",
                      result["reasons"])


    def test_preflight_rejects_high_phase_or_missing_history(self):
        d = {**LIVE, "pv_w": 0, "battery_w": -300,
             "inverter_ac_w": -300, "meter_l1_w": -300,
             "meter_l2_w": -300, "meter_l3_w": -3000,
             "grid_w": -3600, "house_load_w": 3300}
        self.assertFalse(self.check(live=d)["eligible"])
        self.assertFalse(self.check(hist=self.history()[:3])["eligible"])

    def test_runtime_gate_hash_or_dropin_mismatch_stops(self):
        for key in ("runtime_ok", "gate_absent", "dispatcher_sha_ok", "dropins_absent"):
            with self.subTest(key=key):
                self.assertFalse(self.check(**{key: False})["eligible"])

    def test_stale_and_inconsistent_balanced_ct_fail(self):
        self.assertFalse(self.check(live={**LIVE, "timestamp": (NOW-timedelta(minutes=3)).isoformat()})["eligible"])
        self.assertFalse(self.check(live={**LIVE, "meter_l3_w": -2000})["eligible"])
        self.assertFalse(self.check(live={**LIVE, "grid_unbalanced_output_raw": 1})["eligible"])


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.live = dict(LIVE)
        self.fake = SimpleNamespace(
            SOC_FLOOR_RECOVERY_W=1500,
            SOC_RECOVERY_EXIT_PCT=27,
            L3_CHARGE_GUARD_MODE="1",
            L3_CHARGE_BUDGET_W=2700,
            LOOP_INTERVAL_S=30,
            ARMED=True,
        )
        def guard(action, watts, detail, numbers, now):
            if action != "charge" or watts <= self.fake.SOC_FLOOR_RECOVERY_W:
                return watts, detail, numbers, False
            house_l3 = l3_charge_guard.observe_l3_house_w(self.live, now)
            target, _ = l3_charge_guard.cap_grid_charge_w(
                watts, house_l3_w=house_l3, balanced_output=True,
                budget_w=2700, min_charge_w=self.fake.SOC_FLOOR_RECOVERY_W)
            return target, detail, numbers, target < watts
        self.fake.guard_l3_charge = guard
        self.fake.needs_apply = lambda target, last, action, power, due, *, soc_recovery=False: due
        launcher.install_guard(self.fake)

    def test_under_budget_allows_1500_but_remains_bounded(self):
        watts, _, numbers, reduced = self.fake.guard_l3_charge("charge", 1500, "", {}, NOW)
        self.assertEqual(watts, 1500)
        self.assertFalse(reduced)
        self.assertEqual(numbers["temp_evpaused_charge_max_w"], 1500)
        self.assertEqual(self.fake.SOC_FLOOR_RECOVERY_W, 1500)

    def test_overloaded_phase_reduces_to_300(self):
        self.live.update(meter_l3_w=-3000, grid_w=-4300,
                         house_load_w=4000)
        watts, _, _, limited = self.fake.guard_l3_charge("charge", 1500, "", {}, NOW)
        self.assertEqual(watts, 300)
        self.assertTrue(limited)
        self.assertEqual(self.fake.SOC_FLOOR_RECOVERY_W, 1500)

    def test_downward_charge_change_is_not_deadbanded(self):
        self.assertTrue(self.fake.needs_apply(
            ("slot", "charge", 1200), ("slot", "charge", 1300), "charge", 1200, False))

    def test_dispatcher_must_have_exact_physical_config(self):
        launcher.validate_configuration(launcher.REQUIRED_ENV)
        with self.assertRaises(RuntimeError):
            launcher.validate_configuration({**launcher.REQUIRED_ENV,
                                            "DISPATCH_SOC_RECOVERY_EXIT_PCT": "25"})
        self.fake.SOC_RECOVERY_EXIT_PCT = 25
        with self.assertRaises(RuntimeError):
            launcher.install_guard(self.fake)


class WatchdogDecisionTests(unittest.TestCase):
    def decide(self, live=None, evcc=None, *, elapsed=180, service=True,
               process=True, expires=7200):
        return pilot.evaluate(
            copy.deepcopy(LIVE if live is None else live),
            copy.deepcopy(EVCC if evcc is None else evcc),
            now=NOW, started=NOW-timedelta(seconds=elapsed),
            expires=NOW+timedelta(seconds=expires),
            service_ok=service, process_ok=process)

    def test_nominal_paused_battery_charge_continues(self):
        r = self.decide()
        self.assertEqual(r.action, "continue")
        self.assertEqual(r.metrics["soc_pct"], 20)
        self.assertEqual(r.metrics["phase_import_w"], [700, 600, 1200])

    def test_completes_at_26_before_dispatcher_exit_27(self):
        self.assertEqual(self.decide(live={**LIVE, "soc_pct": 25.0}).action, "continue")
        self.assertEqual(self.decide(live={**LIVE, "soc_pct": 26.0}).action, "complete")

    def test_soc_below_19_or_deadline_aborts(self):
        self.assertEqual(self.decide(live={**LIVE, "soc_pct": 18.0}).action, "abort")
        self.assertEqual(self.decide(expires=-1).action, "abort")

    def test_b2_pause_loss_aborts(self):
        for update in ({"state": "C2"}, {"configured_pwm_raw": 100},
                       {"charging_detected": True}, {"measured_current_a": 6},
                       {"paused_by_pwm": False}, {"read_ok": False}):
            with self.subTest(update=update):
                self.assertEqual(self.decide(evcc={**EVCC, **update}).action, "abort")

    def test_phase_over_3200_and_total_over_6500_abort(self):
        phase = {**LIVE, "meter_l3_w": -3250, "grid_w": -4550,
                 "house_load_w": 4250}
        self.assertIn("3200", self.decide(live=phase).reason)
        total = {**LIVE, "meter_l1_w": -2300, "meter_l2_w": -2300,
                 "meter_l3_w": -2100, "grid_w": -6700,
                 "house_load_w": 6400}
        self.assertIn("6500", self.decide(live=total).reason)

    def test_stale_energy_mismatch_or_inverter_change_aborts(self):
        for change in ({"timestamp": (NOW-timedelta(minutes=3)).isoformat()},
                       {"grid_unbalanced_output_raw": 1}, {"meter_l3_w": -2000},
                       {"battery_temp_c": 46}, {"battery_w": -1900}):
            with self.subTest(change=change):
                self.assertEqual(self.decide(live={**LIVE, **change}).action, "abort")

    def test_existing_2100w_charge_must_derate_within_120_seconds(self):
        live = {**LIVE, "battery_w": -2100, "inverter_ac_w": -1700,
                "house_load_w": 800}
        self.assertEqual(self.decide(live=live, elapsed=30).action, "continue")
        late = self.decide(live=live, elapsed=180)
        self.assertEqual(late.action, "abort")
        self.assertIn("1850W", late.reason)

    def test_unsafe_existing_3100w_charge_always_aborts(self):
        live = {**LIVE, "battery_w": -3100, "inverter_ac_w": -2700,
                "grid_w": -3900, "house_load_w": 1200,
                "meter_l1_w": -1000, "meter_l2_w": -1100,
                "meter_l3_w": -1800}
        self.assertEqual(self.decide(live=live, elapsed=30).action, "abort")

    def test_startup_grace_not_permanent(self):
        self.assertEqual(self.decide(elapsed=30, service=False, process=False).action, "continue")
        self.assertEqual(self.decide(elapsed=180, service=False).action, "abort")
        self.assertEqual(self.decide(elapsed=180, process=False).action, "abort")
        self.assertEqual(self.decide(live={**LIVE, "work_mode_raw": 0x101}).action, "abort")
        self.assertEqual(self.decide(live={**LIVE, "battery_w": -30}).action, "abort")


class WatchdogStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.statefile = folder / "status.json"
        self.dropin = folder / "dropin.conf"
        self.wrapper = folder / "wrapper.py"
        self.dispatch = folder / "dispatch.py"
        self.livefile = folder / "live.json"
        self.evfile = folder / "evcc.json"
        self.gate = folder / "gate.json"  # absent until unexpected creation.
        self.dropin.write_text(pilot.MARKER + "\n[Service]\n")
        self.wrapper.write_text("print('wrapper')\n")
        self.dispatch.write_text("print('installed')\n")
        self.livefile.write_text(json.dumps(LIVE))
        self.evfile.write_text(json.dumps(EVCC))
        status = {
            "schema_version": 1, "pilot_id": pilot.PILOT_ID,
            "state": "running", "boot_id": "boot-test",
            "started_at_utc": (NOW-timedelta(minutes=3)).isoformat(),
            "expires_at_utc": (NOW+timedelta(hours=2)).isoformat(),
            "dropin_sha256": common._checksum(self.dropin),
            "wrapper_sha256": common._checksum(self.wrapper),
            "dispatcher_sha256": common._checksum(self.dispatch),
        }
        self.statefile.write_text(json.dumps(status))
        for key in ("STATE", "DROPIN", "TIMER", "PILOT_MARKER", "OVERRIDE_ENV"):
            self.addCleanup(setattr, common, key, getattr(common, key))
        self.patches = [
            mock.patch.object(pilot, "STATE", self.statefile),
            mock.patch.object(pilot, "DROPIN", self.dropin),
            mock.patch.object(pilot, "WRAPPER", self.wrapper),
            mock.patch.object(pilot, "_boot_id", return_value="boot-test"),
            mock.patch.object(common, "LIVE", self.livefile),
            mock.patch.object(common, "EVCC", self.evfile),
            mock.patch.object(common, "DISPATCH", self.dispatch),
            mock.patch.object(common, "GATE", self.gate),
            mock.patch.object(common, "_process_overrides_present", return_value=True),
            mock.patch.object(common, "_dispatch_running", return_value=True),
            mock.patch.object(pilot, "safety_services_ok", return_value=True),
            mock.patch.object(pilot, "wrapper_process_ok", return_value=True),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def tick(self, **kwargs):
        with mock.patch.object(pilot, "datetime") as dt:
            dt.now.return_value = NOW
            return pilot.tick(**kwargs)

    def test_active_lifecycle_without_writes(self):
        r = self.tick()
        self.assertEqual(r["state"], "running")
        self.assertEqual(json.loads(self.statefile.read_text())["measurements"]["soc_pct"], 20)

    def test_root_gate_reappearing_aborts(self):
        self.gate.write_text('{"mode":"active"}')
        with mock.patch.object(common, "_restore", return_value={"state": "aborted"}) as rollback:
            self.assertEqual(self.tick()["state"], "aborted")
            self.assertIn("gate", rollback.call_args.kwargs["reason"])

    def test_reboot_always_aborts_prior_one_off_trial(self):
        with mock.patch.object(pilot, "_boot_id", return_value="different-boot"):
            with mock.patch.object(common, "_restore", return_value={"state": "aborted"}) as rollback:
                self.assertEqual(self.tick()["state"], "aborted")
                self.assertIn("reboot", rollback.call_args.kwargs["reason"])

    def test_wrapper_hash_change_aborts(self):
        self.wrapper.write_text("changed")
        with mock.patch.object(common, "_restore", return_value={"state": "aborted"}) as rollback:
            self.assertEqual(self.tick()["state"], "aborted")
            self.assertIn("wrapper", rollback.call_args.kwargs["reason"])

    def test_own_dropin_tamper_aborts_without_deleting_it(self):
        self.dropin.write_text(pilot.MARKER + "\nother\n")
        with mock.patch.object(common, "_restore", return_value={"state": "aborted"}) as rollback:
            self.assertEqual(self.tick()["state"], "aborted")
            self.assertIn("drop-in", rollback.call_args.kwargs["reason"])

    def test_explicit_stop_uses_only_owned_rollback(self):
        with mock.patch.object(common, "_restore", return_value={"state": "aborted"}) as rollback:
            self.assertEqual(self.tick(force_stop=True)["state"], "aborted")
            self.assertFalse(rollback.call_args.kwargs["completed"])

    def test_real_rollback_restores_original_only_and_preserves_unrelated_files(self):
        other = self.dropin.parent / "unrelated.conf"
        other.write_text("do not modify")
        with mock.patch.object(common, "_service") as ctl:
            with mock.patch.object(common, "_process_overrides_present", return_value=False):
                with mock.patch.object(common.subprocess, "run"):
                    result = self.tick(force_stop=True)
        self.assertEqual(result["state"], "aborted")
        self.assertFalse(self.dropin.exists())
        self.assertEqual(other.read_text(), "do not modify")
        self.assertFalse(json.loads(self.statefile.read_text())["physical_control_authorized"])
        ctl.assert_any_call("daemon-reload")
        ctl.assert_any_call("restart", common.SERVICE)

    def test_missing_root_status_with_dropin_does_not_guess(self):
        self.statefile.unlink()
        with self.assertRaises(RuntimeError):
            self.tick()

    def test_no_modbus_or_evcc_write_imports(self):
        code = Path(pilot.__file__).read_text() + Path(launcher.__file__).read_text()
        for forbidden in ("pymodbus", "ModbusTcpClient", "write_register(", "evcc_ascii"):
            self.assertNotIn(forbidden, code)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.source = self.dir / "source.py"
        self.source.write_text("print('isolated')\n")
        self.wrapper = self.dir / "installed" / "wrapper.py"
        self.watchdog_source = self.dir / "wd.py"
        self.watchdog_source.write_text("print('watchdog')\n")
        self.watchdog_file = self.dir / "installed" / "wd.py"
        self.unit_source = self.dir / "unit.service"
        self.unit_source.write_text("[Service]\nType=oneshot\n")
        self.unit_file = self.dir / "unit.service.installed"
        self.timer_source = self.dir / "timer.timer"
        self.timer_source.write_text("[Timer]\nOnUnitInactiveSec=15s\n")
        self.timer_file = self.dir / "timer.timer.installed"
        self.status_dir = self.dir / "private_status"
        self.state = self.status_dir / "status.json"
        self.dropin = self.dir / "overrides" / "47-temp.conf"
        self.dispatch = self.dir / "original_dispatch.py"
        self.dispatch.write_text("print('original')\n")
        self.python = self.dir / "venv" / "python"
        self.python.parent.mkdir(parents=True)
        self.python.write_text("not executable in tests\n")
        pairs = [(self.source, self.wrapper), (self.watchdog_source, self.watchdog_file),
                 (self.unit_source, self.unit_file), (self.timer_source, self.timer_file)]
        self.patches = [
            mock.patch.object(installer.os, "geteuid", return_value=0),
            mock.patch.object(installer, "ROOT_LIB", self.wrapper.parent),
            mock.patch.object(installer, "SYSTEMD", self.dir),
            mock.patch.object(installer, "STATUS_DIR", self.status_dir),
            mock.patch.object(installer, "DISPATCH_EXE", self.python),
            mock.patch.object(installer.watchdog, "_boot_id", return_value="boot-test"),
            mock.patch.object(installer, "_status_directory_secure", return_value=True),
            mock.patch.object(installer, "_safe_parent", return_value=True),
            mock.patch.object(installer, "_sources", return_value=pairs),
            mock.patch.object(installer.preflight, "report",
                              return_value={"eligible": True, "reasons": []}),
            mock.patch.object(pilot, "STATE", self.state),
            mock.patch.object(pilot, "DROPIN", self.dropin),
            mock.patch.object(pilot, "WRAPPER", self.wrapper),
            mock.patch.object(common, "DISPATCH", self.dispatch),
            mock.patch.object(installer, "_systemctl"),
            mock.patch.object(installer.watchdog, "tick", return_value={"state": "running"}),
        ]
        for name in ("STATE", "DROPIN", "TIMER", "PILOT_MARKER", "OVERRIDE_ENV"):
            self.addCleanup(setattr, common, name, getattr(common, name))
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_refuses_failed_preflight_without_creating_armed_files(self):
        with mock.patch.object(installer.preflight, "report",
                               return_value={"eligible": False, "reasons": ["SoC < 19%"]}), \
             mock.patch.object(installer, "_systemctl") as systemctl:
            result = installer.activate()
        self.assertEqual(result["state"], "blocked")
        self.assertFalse(self.dropin.exists())
        self.assertFalse(self.state.exists())
        systemctl.assert_not_called()

    def test_exact_start_installs_hash_owned_dropin_and_timer(self):
        result = installer.activate()
        self.assertEqual(result["state"], "started_supervised")
        self.assertTrue(result["physical_charge_requires_readback"])
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.status_dir.stat().st_mode & 0o777, 0o700)
        data = json.loads(self.state.read_text())
        self.assertEqual(data["state"], "prepared")
        self.assertEqual(data["dropin_sha256"], common._checksum(self.dropin))
        self.assertEqual(data["wrapper_sha256"], common._checksum(self.wrapper))
        self.assertIn(b"DISPATCH_SOC_RECOVERY_EXIT_PCT=27", self.dropin.read_bytes())
        self.assertIn(b"DISPATCH_L3_CHARGE_BUDGET_W=2700", self.dropin.read_bytes())
        self.assertIn(b"Restart=no", self.dropin.read_bytes())
        self.assertIn(b"RuntimeMaxSec=2h", self.dropin.read_bytes())
        installer._systemctl.assert_any_call("enable", "--now", pilot.TIMER)
        installer._systemctl.assert_any_call("restart", common.SERVICE)

    def test_root_does_not_overwrite_unrelated_existing_file(self):
        self.wrapper.parent.mkdir(parents=True)
        self.wrapper.write_text("unrelated")
        with self.assertRaises(RuntimeError):
            installer.activate()
        self.assertFalse(self.state.exists())
        installer._systemctl.assert_not_called()

    def test_failure_after_arming_calls_watchdog_rollback(self):
        installer._systemctl.side_effect = [
            None, None, RuntimeError("restart refused")]
        with self.assertRaises(RuntimeError):
            installer.activate()
        installer.watchdog.tick.assert_called_with(force_stop=True)

    def test_never_arms_if_second_preflight_fails(self):
        with mock.patch.object(installer.preflight, "report",
                               side_effect=[{"eligible": True}, {"eligible": True},
                                            {"eligible": False,
                                             "reasons": ["load changed"]}]):
            r = installer.activate()
        self.assertEqual(r["state"], "blocked")
        self.assertTrue(r["units_installed_inactive"])
        self.assertFalse(self.dropin.exists())
        installer._systemctl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
