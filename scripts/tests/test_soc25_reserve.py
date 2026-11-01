"""Read-only 25% reserve regression: IEC A1 vs B2, PV, startup and rollback.

All paths and commands are faked; no inverter, charger or systemd writes.
"""
from __future__ import annotations

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

import soc25_reserve_policy as policy
import soc25_reserve_dispatch as wrapper
import soc25_reserve_watchdog as watchdog
import preflight_soc25_reserve as preflight
import start_soc25_reserve as installer
import temporary_soc25_watchdog as common

NOW = datetime(2026, 11, 1, 19, tzinfo=timezone.utc)
LIVE = {
    "timestamp": NOW.isoformat(), "soc_pct": 39., "battery_w": 900.,
    "battery_temp_c": 16., "pv_w": 0., "house_load_w": 1100.,
    "grid_w": -700., "inverter_ac_w": 400.,
    "meter_l1_w": -450., "meter_l2_w": -150., "meter_l3_w": -100.,
    "grid_unbalanced_output_raw": 0, "work_mode_raw": 0x303,
}
A1 = {
    "timestamp": NOW.isoformat(), "source": "waveshare_evcc_fc03_readonly",
    "read_ok": True, "state": "A1", "configured_pwm_raw": 1000,
    "paused_by_pwm": True, "charging_detected": False,
    "channel_currents_a": [None, None, None], "measured_current_a": None,
    "pilot_pwm_raw": 267, "writes": 0,
}
B2 = {**A1, "state": "B2", "measured_current_a": .3}
C2 = {**A1, "state": "C2", "charging_detected": True,
      "measured_current_a": 6.1, "configured_pwm_raw": 100}


def charge_live(soc=26., watts=-1500.) -> dict:
    # PV=0. Grid matches the current 1.5kW charge plus the 1.1kW load.
    grid = watts - 1100.
    return {**LIVE, "soc_pct": soc, "battery_w": watts,
            "inverter_ac_w": watts, "grid_w": grid,
            "meter_l1_w": grid/3, "meter_l2_w": grid/3,
            "meter_l3_w": grid/3}


class EvccAndPolicyTests(unittest.TestCase):
    def test_unplugged_a1_is_valid_without_current_sensor(self):
        self.assertEqual(policy.verified_evcc_safe(A1), "disconnected")
        self.assertIsNone(A1["measured_current_a"])

    def test_paused_b2_is_valid_with_independent_current(self):
        self.assertEqual(policy.verified_evcc_safe(B2), "paused")

    def test_active_c2_and_ambiguous_evcc_fail_closed(self):
        for evcc in (C2, {**A1, "state": "B1"}, {**A1, "read_ok": False},
                     {**A1, "configured_pwm_raw": 100},
                     {**A1, "charging_detected": True},
                     {**A1, "writes": 1}, {**A1, "writes": False},
                     {**A1, "measured_current_a": 0.0},
                     {**A1, "channel_currents_a": [0., 0., 0.]},
                     {**A1, "channel_currents_a": None},
                     {**B2, "measured_current_a": None},
                     {**B2, "measured_current_a": float("nan")},
                     {**B2, "measured_current_a": 1.1},
                     {**B2, "paused_by_pwm": False}, {}, None):
            with self.subTest(evcc=evcc):
                self.assertIsNone(policy.verified_evcc_safe(evcc))

    def test_recovery_charges_at_1500w_even_if_original_floor_is_300w(self):
        self.assertEqual(policy.cap_recovery_charge(300, 0.), 1500)
        self.assertEqual(policy.cap_recovery_charge(1000, None), 1500)
        self.assertEqual(policy.cap_recovery_charge(7500, 0.), 1500)

    def test_pv_surplus_preserved_not_capped_at_1500w(self):
        self.assertEqual(policy.cap_recovery_charge(3000, 3400.), 3000)
        self.assertEqual(policy.cap_recovery_charge(300, 3400.), 1500)
        self.assertEqual(policy.cap_recovery_charge(7500, 5500.), 5500)
        self.assertEqual(policy.cap_recovery_charge(7500, 9999.), 5500)

    def test_hysteresis(self):
        self.assertFalse(policy.reserve_state(39., active=False))
        self.assertFalse(policy.reserve_state(27., active=False))
        self.assertTrue(policy.reserve_state(26., active=False))
        self.assertTrue(policy.reserve_state(27., active=True))
        self.assertFalse(policy.reserve_state(28., active=True))
        self.assertTrue(policy.reserve_state(25., active=False))

    def test_malformed_request_rejected(self):
        for request in (-100, 1.2, True):
            with self.subTest(request=request):
                with self.assertRaises(ValueError):
                    policy.cap_recovery_charge(request, 0)


class ReserveWrapperTests(unittest.TestCase):
    def setUp(self):
        self.last_request = None
        self.dispatch = SimpleNamespace(
            ARMED=True, SOC_FLOOR_PCT=12, BATTERY_KWH=15.055,
            PLANNER_RESERVE_PCT=20, SOC_RECOVERY_ENTRY_PCT=19.5,
            SOC_RECOVERY_EXIT_PCT=21, SOC_FLOOR_RECOVERY_W=300,
            L3_CHARGE_GUARD_MODE="1", L3_CHARGE_BUDGET_W=4000,
            LOOP_INTERVAL_S=30, _soc_recovery_active=False,
            read_live_solar_surplus_w=lambda now: 0.,
        )
        def original_guard(action, watts, detail, numbers, now):
            self.last_request = watts
            if action != "charge":
                return watts, detail, numbers, False
            return min(watts, 1200), detail, numbers, watts > 1200
        self.dispatch.guard_l3_charge = original_guard
        self.dispatch.needs_apply = lambda target, last, a, p, due, *, soc_recovery=False: due

    def install(self):
        wrapper.install_reserve(self.dispatch, policy)

    def test_near_floor_requests_1500_l3_guard_limits_to_1200(self):
        self.install()
        watts, _, numbers, limited = self.dispatch.guard_l3_charge(
            "charge", 300, "", {"soc_recovery": True}, NOW)
        self.assertEqual(self.last_request, 1500)
        self.assertEqual(watts, 1200)
        self.assertTrue(limited)
        self.assertEqual(numbers["soc25_reserve_entry_pct"], 26.5)
        self.assertEqual(self.dispatch.L3_CHARGE_BUDGET_W, 4000)
        self.assertEqual(self.dispatch.SOC_RECOVERY_EXIT_PCT, 28)
        self.assertTrue(self.dispatch._soc_recovery_active)

    def test_outside_recovery_original_plan_untouched(self):
        self.install()
        self.dispatch.guard_l3_charge("charge", 4000, "", {}, NOW)
        self.assertEqual(self.last_request, 4000)

    def test_pv_surplus_keeps_original_grid_guard(self):
        self.dispatch.read_live_solar_surplus_w = lambda now: 4000.
        self.install()
        self.dispatch.guard_l3_charge("charge", 4000, "", {"soc_recovery": True}, NOW)
        self.assertEqual(self.last_request, 4000)

    def test_downward_recovery_change_skips_economic_deadband(self):
        self.install()
        self.assertTrue(self.dispatch.needs_apply(
            ("slot", "charge", 1000), ("slot", "charge", 1100),
            "charge", 1000, False))

    def test_refuses_different_installed_dispatcher(self):
        self.dispatch.ARMED = False
        with self.assertRaises(RuntimeError):
            self.install()


class ReservePreflightTests(unittest.TestCase):
    def history(self):
        return [{**LIVE, "timestamp": (NOW-timedelta(seconds=30*i)).isoformat()}
                for i in range(11)]

    def check(self, evcc=A1, live=None, history=None, **gates):
        return preflight.admit(
            LIVE if live is None else live, evcc,
            self.history() if history is None else history,
            now=NOW, installed_sha_ok=gates.get("installed_sha_ok", True),
            units_ok=gates.get("units_ok", True),
            no_conflicts=gates.get("no_conflicts", True))

    def test_a1_and_b2_admitted_with_no_writes(self):
        for evcc, mode in ((A1, "disconnected"), (B2, "paused")):
            result = self.check(evcc)
            self.assertTrue(result["eligible"], result["reasons"])
            self.assertEqual(result["measurements"]["evcc_no_charge_mode"], mode)
            self.assertEqual(result["device_writes"], 0)
            self.assertFalse(result["physical_control_authorized"])

    def test_charging_and_unverified_evcc_rejected(self):
        for evcc in (C2, {**A1, "read_ok": False}, {**A1, "measured_current_a": 0}):
            self.assertFalse(self.check(evcc)["eligible"])

    def test_stale_or_unbalanced_phase_rejected(self):
        self.assertFalse(self.check(live={**LIVE, "timestamp": (NOW-timedelta(minutes=4)).isoformat()})["eligible"])
        self.assertFalse(self.check(live={**LIVE, "meter_l3_w": -700})["eligible"])
        self.assertFalse(self.check(live={**LIVE, "grid_unbalanced_output_raw": 1})["eligible"])
        self.assertFalse(self.check(history=self.history()[:3])["eligible"])

    def test_rejects_unsafe_phase_or_missing_runtime(self):
        l = {**LIVE, "soc_pct": 24, "battery_w": 900,
             "inverter_ac_w": 400, "house_load_w": 3100,
             "grid_w": -2700, "meter_l1_w": -100, "meter_l2_w": -100,
             "meter_l3_w": -2500}
        self.assertFalse(self.check(live=l)["eligible"])
        for key in ("installed_sha_ok", "units_ok", "no_conflicts"):
            self.assertFalse(self.check(**{key: False})["eligible"])

    def test_rejects_below_24_in_history(self):
        rows = self.history()
        rows[2]["soc_pct"] = 23
        self.assertFalse(self.check(history=rows)["eligible"])


class ReserveWatchdogDecisionTests(unittest.TestCase):
    def evaluate(self, live=None, evcc=A1, *, elapsed=180, service=True, process=True):
        return watchdog.evaluate(
            LIVE if live is None else live, evcc,
            now=NOW, started=NOW-timedelta(seconds=elapsed),
            service_ok=service, process_ok=process)

    def test_safe_disconnected_39pct_no_physical_charge_required(self):
        result = self.evaluate()
        self.assertEqual(result.state, "safe", result.reason)
        self.assertEqual(result.measurements["evcc_no_charge_mode"], "disconnected")

    def test_safe_paused_b2(self):
        self.assertEqual(self.evaluate(evcc=B2).state, "safe")

    def test_active_charger_aborts_immediately(self):
        self.assertEqual(self.evaluate(evcc=C2, elapsed=15).state, "abort")

    def test_real_startup_auto_sample_is_bounded_nonresponse_not_immediate_abort(self):
        # Exact native poller sample during the 20:57:13-20:57:20
        # dispatcher restart. Site CT/house sum is consistent, but
        # AC and battery power were sampled at different transition times.
        observed = {
            **LIVE, "soc_pct": 26., "battery_w": 1050.,
            "house_load_w": 1036., "grid_w": -880.,
            "inverter_ac_w": 156., "meter_l1_w": -542.,
            "meter_l2_w": -311., "meter_l3_w": -26.,
            "work_mode_raw": 0x101,
        }
        decision = self.evaluate(live=observed, elapsed=10)
        self.assertEqual(decision.state, "reserve_nonresponse", decision.reason)
        self.assertIn("snapshot mismatch", decision.reason)
        self.assertEqual(self.evaluate(live=observed, elapsed=200).state, "abort")

    def test_startup_snapshot_grace_never_masks_real_safety_faults(self):
        transient = {**LIVE, "soc_pct": 26., "battery_w": 1050.,
                     "house_load_w": 1036., "grid_w": -880.,
                     "inverter_ac_w": 156., "meter_l1_w": -542.,
                     "meter_l2_w": -311., "meter_l3_w": -26.,
                     "work_mode_raw": 0x101}
        variants = (
            {**transient, "battery_w": 1800.},
            {**transient, "battery_temp_c": 55.},
            {**transient, "meter_l3_w": -3200.},
            {**transient, "grid_unbalanced_output_raw": 1},
        )
        for sample in variants:
            with self.subTest(sample=sample):
                self.assertEqual(self.evaluate(live=sample, elapsed=10).state, "abort")
        self.assertEqual(self.evaluate(live=transient, evcc=C2, elapsed=10).state, "abort")

    def test_real_charge_transition_mismatch_remains_immediate_abort(self):
        # At 21:09:52 CET the native Modbus poller reported a real
        # negative battery_w (-1692W) but +978W inverter AC with 0W PV.
        # The site CT/house/AC sum was consistent, so this is not
        # proof of a safe sustained charge; do NOT extend the special
        # General/Auto discharge-only grace to this EMS charging case.
        observed = {
            **LIVE, "soc_pct": 25., "battery_w": -1692.,
            "house_load_w": 989., "grid_w": -11.,
            "inverter_ac_w": 978., "meter_l1_w": -115.,
            "meter_l2_w": -126., "meter_l3_w": 231.,
            "work_mode_raw": 0x303,
        }
        decision = self.evaluate(live=observed, elapsed=35)
        self.assertEqual(decision.state, "abort")
        self.assertIn("battery/inverter AC", decision.reason)

    def test_near_25_requires_real_charge(self):
        self.assertEqual(self.evaluate(live=charge_live()).state, "safe")
        x = {**LIVE, "soc_pct": 25.}
        result = self.evaluate(live=x)
        self.assertEqual(result.state, "reserve_nonresponse")

    def test_newly_crossed_26_waits_for_next_dispatch_tick(self):
        # At 20:38:03 the 30s poller first observed 26%; watchdog ran
        # before the next dispatch iteration and used to abort instantly.
        x = {**LIVE, "soc_pct": 26., "work_mode_raw": 0x101}
        result = self.evaluate(live=x)
        self.assertEqual(result.state, "reserve_nonresponse")
        self.assertIn("awaiting EMS", result.reason)

    def test_wrong_mode_does_not_bypass_immediate_overcharge_stop(self):
        x = {**charge_live(watts=-2100.), "work_mode_raw": 0x101}
        self.assertEqual(self.evaluate(live=x).state, "abort")

    def test_phase_stop_and_battery_temperature_abort(self):
        x = {**LIVE, "battery_temp_c": 55}
        self.assertEqual(self.evaluate(live=x).state, "abort")
        x = {**LIVE, "house_load_w": 3800., "grid_w": -3400.,
             "meter_l1_w": -100., "meter_l2_w": -100., "meter_l3_w": -3200.}
        self.assertEqual(self.evaluate(live=x).state, "abort")

    def test_running_process_verified_after_startup(self):
        self.assertEqual(self.evaluate(elapsed=30, process=False).state, "safe")
        self.assertEqual(self.evaluate(elapsed=180, process=False).state, "abort")

    def test_normal_39pct_optimizer_charge_above_1500_not_false_positive(self):
        x = {**LIVE, "battery_w": -2500., "inverter_ac_w": -2500.,
             "grid_w": -3600., "meter_l1_w": -1200.,
             "meter_l2_w": -1200., "meter_l3_w": -1200.}
        self.assertEqual(self.evaluate(live=x).state, "safe")

    def test_reserve_charging_above_supervised_1900_aborts(self):
        x = charge_live(watts=-2100.)
        self.assertEqual(self.evaluate(live=x).state, "abort")


class ReserveWatchdogStateTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.folder = Path(d.name)
        self.state = self.folder/"status.json"
        self.dropin = self.folder/"48.conf"
        self.wrapper = self.folder/"wrapper.py"
        self.policy = self.folder/"policy.py"
        self.dispatcher = self.folder/"dispatch_loop.py"
        self.live = self.folder/"live.json"
        self.evcc = self.folder/"evcc.json"
        self.gate = self.folder/"not-existing-gate"
        self.dropin.write_text(watchdog.MARKER+"\n[Service]\n")
        self.wrapper.write_text("print('wrapper')\n")
        self.policy.write_text("print('policy')\n")
        self.dispatcher.write_text("print('original')\n")
        self.live.write_text(json.dumps(LIVE))
        self.evcc.write_text(json.dumps(A1))
        self.state.write_text(json.dumps({
            "schema_version": 1, "pilot_id": watchdog.ID, "state": "running",
            "boot_id": "boot-test", "started_at_utc": (NOW-timedelta(minutes=5)).isoformat(),
            "expires_at_utc": (NOW+timedelta(hours=5)).isoformat(),
            "dropin_sha256": common._checksum(self.dropin),
            "dispatcher_sha256": common._checksum(self.dispatcher),
            "wrapper_sha256": common._checksum(self.wrapper),
            "policy_sha256": common._checksum(self.policy),
        }))
        for key in ("STATE", "DROPIN", "TIMER", "PILOT_MARKER", "OVERRIDE_ENV"):
            self.addCleanup(setattr, common, key, getattr(common, key))
        patches = [
            mock.patch.object(watchdog, "STATE", self.state),
            mock.patch.object(watchdog, "DROPIN", self.dropin),
            mock.patch.object(watchdog, "WRAPPER", self.wrapper),
            mock.patch.object(watchdog, "POLICY", self.policy),
            mock.patch.object(watchdog, "_boot_id", return_value="boot-test"),
            mock.patch.object(watchdog, "safety_services_ok", return_value=True),
            mock.patch.object(watchdog, "wrapper_process_ok", return_value=True),
            mock.patch.object(watchdog, "_disable_deadline_timer"),
            mock.patch.object(common, "DISPATCH", self.dispatcher),
            mock.patch.object(common, "LIVE", self.live),
            mock.patch.object(common, "EVCC", self.evcc),
            mock.patch.object(common, "GATE", self.gate),
            mock.patch.object(common, "_dispatch_running", return_value=True),
            mock.patch.object(common, "_process_overrides_present", return_value=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tick(self, *, at=NOW, **kwargs):
        with mock.patch.object(watchdog, "datetime") as dt:
            dt.now.return_value = at
            return watchdog.tick(**kwargs)

    def test_running_record_only_no_writes(self):
        result = self.tick()
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["measurements"]["evcc_no_charge_mode"], "disconnected")

    def test_operator_stop_calls_owned_restore(self):
        with mock.patch.object(common, "_restore", return_value={"state": "aborted"}) as restore:
            self.assertEqual(self.tick(stop=True)["state"], "aborted")
            restore.assert_called_once()
            self.assertIn("operator", restore.call_args.kwargs["reason"])

    def test_expiry_and_boot_change_fail_closed(self):
        d=json.loads(self.state.read_text())
        d["expires_at_utc"]=(NOW-timedelta(seconds=1)).isoformat()
        self.state.write_text(json.dumps(d))
        with mock.patch.object(common, "_restore", return_value={"state": "aborted"}) as restore:
            self.assertEqual(self.tick()["state"], "aborted")
            self.assertIn("deadline", restore.call_args.kwargs["reason"])

    def test_tampered_dropin_or_original_dispatch_aborts(self):
        self.dispatcher.write_text("changed")
        with mock.patch.object(common, "_restore", return_value={"state": "aborted"}) as restore:
            self.assertEqual(self.tick()["state"], "aborted")
            self.assertIn("changed", restore.call_args.kwargs["reason"])

    def test_unplugged_to_active_evcc_aborts(self):
        self.evcc.write_text(json.dumps(C2))
        with mock.patch.object(common, "_restore", return_value={"state": "aborted"}) as restore:
            self.assertEqual(self.tick()["state"], "aborted")
            self.assertIn("EVCC", restore.call_args.kwargs["reason"])

    def test_26pct_mode_transition_waits_for_dispatch_then_clears_timer(self):
        self.live.write_text(json.dumps({**LIVE, "soc_pct": 26.,
                                         "work_mode_raw": 0x101}))
        first = self.tick()
        self.assertEqual(first["state"], "running")
        self.assertIn("awaiting EMS", first["check_reason"])
        self.assertEqual(json.loads(self.state.read_text())["nonresponse_since_utc"],
                         NOW.isoformat())
        # The next dispatcher iteration activates a real charging target.
        self.live.write_text(json.dumps(charge_live()))
        second = self.tick()
        self.assertEqual(second["state"], "running")
        self.assertNotIn("nonresponse_since_utc", json.loads(self.state.read_text()))

    def test_26pct_wrong_mode_rollback_after_180s_nonresponse(self):
        self.live.write_text(json.dumps({**LIVE, "soc_pct": 26.,
                                         "work_mode_raw": 0x101}))
        self.assertEqual(self.tick()["state"], "running")
        later = NOW + timedelta(seconds=181)
        live = {**LIVE, "soc_pct": 26., "work_mode_raw": 0x101,
                "timestamp": later.isoformat()}
        self.live.write_text(json.dumps(live))
        self.evcc.write_text(json.dumps({**A1, "timestamp": later.isoformat()}))
        with mock.patch.object(common, "_restore",
                               return_value={"state": "aborted"}) as restore:
            self.assertEqual(self.tick(at=later)["state"], "aborted")
            self.assertIn("over 180 seconds", restore.call_args.kwargs["reason"])

    def test_low_soc_below_23_still_aborts_immediately(self):
        self.live.write_text(json.dumps({**LIVE, "soc_pct": 22.,
                                         "work_mode_raw": 0x101}))
        with mock.patch.object(common, "_restore",
                               return_value={"state": "aborted"}) as restore:
            self.assertEqual(self.tick()["state"], "aborted")
            self.assertIn("23-100%", restore.call_args.kwargs["reason"])

    def test_no_unowned_file_deleted_by_real_rollback(self):
        other = self.folder / "unrelated.conf"
        other.write_text("preserve")
        with mock.patch.object(common, "_service") as ctl:
            with mock.patch.object(common, "_process_overrides_present", return_value=False):
                with mock.patch.object(common.subprocess, "run"):
                    result = self.tick(stop=True)
        self.assertEqual(result["state"], "aborted")
        self.assertFalse(self.dropin.exists())
        self.assertEqual(other.read_text(), "preserve")
        ctl.assert_any_call("restart", common.SERVICE)

    def test_no_device_api_in_supervisor_or_policy(self):
        source=(Path(watchdog.__file__).read_text()+Path(policy.__file__).read_text())
        for forbidden in ("pymodbus", "ModbusTcpClient", "write_register(", "evcc_ascii"):
            self.assertNotIn(forbidden, source)


class ReserveInstallerTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.folder = Path(d.name)
        self.statusdir = self.folder/"state"
        self.state = self.statusdir/"status.json"
        self.dropin = self.folder/"dropins"/"48-reserve.conf"
        self.rootlib = self.folder/"lib"
        self.policy = self.rootlib/"policy.py"
        self.wrapper = self.rootlib/"wrapper.py"
        self.dispatcher = self.folder/"installed-dispatch.py"
        self.dispatcher.write_text("print('unchanged')\n")
        self.python = self.folder/"python"
        self.python.write_text("original virtual env")
        self.src_policy = self.folder/"src-policy.py"
        self.src_policy.write_text("print('policy')\n")
        self.src_wrapper = self.folder/"src-wrapper.py"
        self.src_wrapper.write_text("print('wrapper')\n")
        self.systemd_calls = []

        def fake_install(path, data):
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                raise RuntimeError("will not overwrite")
            path.write_bytes(data)

        def fake_service(*args):
            self.systemd_calls.append(args)

        patches = [
            mock.patch.object(installer.os, "geteuid", return_value=0),
            mock.patch.object(installer, "STATUS_DIR", self.statusdir),
            mock.patch.object(installer, "PYTHON", self.python),
            mock.patch.object(installer, "_root_dirs_secure", return_value=True),
            mock.patch.object(installer, "_install_owned", side_effect=fake_install),
            mock.patch.object(installer, "_service", side_effect=fake_service),
            mock.patch.object(installer, "_sources", return_value=[
                (self.src_policy, self.policy),
                (self.src_wrapper, self.wrapper)]),
            mock.patch.object(installer.preflight, "report", return_value={"eligible": True, "reasons": []}),
            mock.patch.object(installer.watchdog, "STATE", self.state),
            mock.patch.object(installer.watchdog, "DROPIN", self.dropin),
            mock.patch.object(installer.watchdog, "ROOT_LIB", self.rootlib),
            mock.patch.object(installer.watchdog, "WRAPPER", self.wrapper),
            mock.patch.object(installer.watchdog, "POLICY", self.policy),
            mock.patch.object(installer.watchdog, "_boot_id", return_value="boot-test"),
            mock.patch.object(installer.watchdog, "tick", return_value={"state": "running"}),
            mock.patch.object(installer.common, "DISPATCH", self.dispatcher),
        ]
        for key in ("STATE", "DROPIN", "TIMER", "PILOT_MARKER", "OVERRIDE_ENV"):
            self.addCleanup(setattr, common, key, getattr(common, key))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_failed_readonly_preflight_never_creates_root_overlay(self):
        with mock.patch.object(installer.preflight, "report",
                               return_value={"eligible": False, "reasons": ["unknown CT"]}):
            result = installer.activate()
        self.assertEqual(result["state"], "blocked")
        self.assertFalse(self.dropin.exists())
        self.assertEqual(self.systemd_calls, [])

    def test_start_is_boot_scoped_and_six_hours_only(self):
        result = installer.activate()
        self.assertEqual(result["state"], "started_supervised")
        data = json.loads(self.state.read_text())
        started = datetime.fromisoformat(data["started_at_utc"])
        expires = datetime.fromisoformat(data["expires_at_utc"])
        self.assertEqual(expires-started, timedelta(hours=6))
        self.assertEqual(data["dropin_sha256"], common._checksum(self.dropin))
        self.assertEqual(data["wrapper_sha256"], common._checksum(self.wrapper))
        self.assertNotIn(b"RuntimeMaxSec", self.dropin.read_bytes())
        self.assertEqual(self.systemd_calls, [
            ("daemon-reload",),
            ("enable", "--now", watchdog.DEADLINE_TIMER),
            ("enable", "--now", watchdog.TIMER),
            ("restart", common.SERVICE)])
        installer.watchdog.tick.assert_called_once_with()

    def test_second_preflight_failure_never_installs_dispatch_dropin(self):
        with mock.patch.object(installer.preflight, "report", side_effect=[
            {"eligible": True, "reasons": []},
            {"eligible": False, "reasons": ["new load spike"]}]):
            result = installer.activate()
        self.assertEqual(result["state"], "blocked")
        self.assertFalse(self.dropin.exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(self.systemd_calls, [])

    def test_restart_failure_calls_independent_owned_rollback(self):
        with mock.patch.object(installer, "_service", side_effect=[
            None, None, None, RuntimeError("dispatcher restart failed")]):
            with self.assertRaises(RuntimeError):
                installer.activate()
        installer.watchdog.tick.assert_called_once_with(stop=True)


if __name__ == "__main__":
    unittest.main()
