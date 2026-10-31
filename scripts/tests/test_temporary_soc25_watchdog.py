"""Pure and local-only checks for the bounded physical 25% recovery pilot."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import temporary_soc25_watchdog as pilot

NOW = datetime(2026, 10, 31, 11, 20, tzinfo=timezone.utc)
LIVE = {
    "timestamp": NOW.isoformat(), "soc_pct": 20, "battery_w": -995,
    "battery_temp_c": 10, "pv_w": 900,
    "grid_w": -2200, "meter_l1_w": -500, "meter_l2_w": -300,
    "meter_l3_w": -1400, "inverter_ac_w": 500,
    "house_load_w": 2700, "grid_unbalanced_output_raw": 0,
    "work_mode_raw": 0x303,
}
EVCC = {
    "timestamp": NOW.isoformat(), "read_ok": True, "state": "C2",
    "charging_detected": True, "configured_pwm_raw": 100,
    "pilot_pwm_raw": 100, "channel_currents_a": [.1, 6.0, .1],
    "measured_current_a": 6.2, "writes": 0,
}


class DecisionTests(unittest.TestCase):
    def check(self, *, live=None, evcc=None, now=NOW, elapsed=130,
              expires=7200, process=True, active=True):
        return pilot.evaluate(
            copy.deepcopy(LIVE if live is None else live),
            copy.deepcopy(EVCC if evcc is None else evcc),
            now=now, started=NOW - timedelta(seconds=elapsed),
            expires=NOW + timedelta(seconds=expires),
            service_ok=active, process_overrides_ok=process)

    def test_nominal_charge_continues(self):
        r = self.check()
        self.assertEqual((r.action, r.reason),
                         ("continue", "within temporary 1000 W recovery envelope"))
        self.assertEqual(r.metrics["ev_a"], 6.0)
        self.assertEqual(r.metrics["phase_import_w"], [500, 300, 1400])

    def test_target_soc_restores_regardless_of_other_health(self):
        d = {**LIVE, "soc_pct": 25}
        self.assertEqual(self.check(live=d).action, "complete")

    def test_no_early_restore_at_24point9(self):
        d = {**LIVE, "soc_pct": 24.9}
        self.assertEqual(self.check(live=d).action, "continue")

    def test_deadline_always_stops(self):
        self.assertEqual(self.check(expires=-1).action, "abort")

    def test_soc_drop_or_abnormal_temperature_stops(self):
        for key, value in (("soc_pct", 18.9), ("battery_temp_c", 4),
                           ("battery_temp_c", 46), ("soc_pct", None)):
            with self.subTest(field=key, value=value):
                d = {**LIVE, key: value}
                self.assertEqual(self.check(live=d).action, "abort")

    def test_stale_or_invalid_ct_fails_closed(self):
        variants = (
            {"timestamp": (NOW-timedelta(seconds=120)).isoformat()},
            {"meter_l1_w": None},
            {"meter_l3_w": -2200},
            {"grid_unbalanced_output_raw": 1},
            {"grid_unbalanced_output_raw": True},
            {"house_load_w": -100},
        )
        for change in variants:
            with self.subTest(change=change):
                self.assertEqual(self.check(live={**LIVE, **change}).action, "abort")

    def test_one_phase_over_envelope_stops(self):
        d = {**LIVE, "meter_l3_w": -3500, "grid_w": -4300,
             "house_load_w": 4800}
        r = self.check(live=d)
        self.assertEqual(r.action, "abort")
        self.assertIn("L1/L2/L3", r.reason)

    def test_site_import_over_envelope_stops(self):
        d = {**LIVE, "meter_l1_w": -2300, "meter_l2_w": -2300,
             "meter_l3_w": -2300, "grid_w": -6900, "house_load_w": 7400}
        self.assertIn("total grid import", self.check(live=d).reason)

    def test_EVCC_disconnect_or_change_6A_stops(self):
        for change in ({"read_ok": False}, {"state": "B2"},
                       {"configured_pwm_raw": 267},
                       {"channel_currents_a": [0, 15.9, 0]},
                       {"writes": 1}, {"timestamp": (NOW-timedelta(minutes=3)).isoformat()}):
            with self.subTest(change=change):
                self.assertEqual(self.check(evcc={**EVCC, **change}).action, "abort")

    def test_non_1000_measured_charge_or_discharge_stops(self):
        for power in (-1300, 300, -25):
            with self.subTest(power=power):
                self.assertEqual(self.check(live={**LIVE, "battery_w": power}).action,
                                 "abort")

    def test_initial_settle_grace_not_permanent(self):
        self.assertEqual(self.check(elapsed=30, active=False, process=False).action,
                         "continue")
        self.assertEqual(self.check(elapsed=130, active=False).action, "abort")
        self.assertEqual(self.check(elapsed=130, process=False).action, "abort")

    def test_actual_inverter_mode_required_after_settle(self):
        d = {**LIVE, "work_mode_raw": 0x101}
        self.assertEqual(self.check(live=d, elapsed=40).action, "continue")
        self.assertEqual(self.check(live=d, elapsed=130).action, "abort")

    def test_naive_timestamps_rejected(self):
        self.assertEqual(self.check(live={**LIVE, "timestamp": NOW.replace(tzinfo=None).isoformat()}).action,
                         "abort")

    def test_no_modbus_or_charger_control_imported(self):
        source = Path(pilot.__file__).read_text()
        for forbidden in ("pymodbus", "ModbusTcpClient", "write_register",
                          "force_charge(", "force_discharge("):
            self.assertNotIn(forbidden, source)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name)
        self.dropin = folder / "pilot.conf"
        self.statusfile = folder / "status.json"
        self.dropin.write_text(pilot.PILOT_MARKER + "\n[Service]\n")
        self.state = {
            "schema_version": 1, "pilot_id": "soc25-20261031",
            "state": "running", "dropin_sha256": pilot._checksum(self.dropin),
            "started_at_utc": NOW.isoformat(),
            "expires_at_utc": (NOW + timedelta(hours=2)).isoformat(),
        }
        self.statusfile.write_text(json.dumps(self.state))

    def test_atomic_status_0600(self):
        pilot._atomic_json(self.statusfile, self.state)
        self.assertEqual(self.statusfile.stat().st_mode & 0o777, 0o600)

    def test_rollback_removes_exact_owned_dropin_and_restarts_dispatcher(self):
        with mock.patch.object(pilot, "DROPIN", self.dropin), \
             mock.patch.object(pilot, "STATE", self.statusfile), \
             mock.patch.object(pilot, "_service") as service, \
             mock.patch.object(pilot, "_dispatch_running", return_value=True), \
             mock.patch.object(pilot, "_process_overrides_present", return_value=False), \
             mock.patch.object(pilot.subprocess, "run"):
            result = pilot._restore(self.state, reason="target reached", completed=True)
        self.assertEqual(result["state"], "completed")
        self.assertFalse(self.dropin.exists())
        self.assertEqual(json.loads(self.statusfile.read_text())["physical_control_authorized"],
                         False)
        self.assertEqual([x.args[0] for x in service.call_args_list],
                         ["daemon-reload", "restart"])

    def test_interrupted_restore_is_idempotent(self):
        self.dropin.unlink()
        with mock.patch.object(pilot, "DROPIN", self.dropin), \
             mock.patch.object(pilot, "STATE", self.statusfile), \
             mock.patch.object(pilot, "_service") as service, \
             mock.patch.object(pilot, "_dispatch_running", return_value=True), \
             mock.patch.object(pilot, "_process_overrides_present", return_value=False), \
             mock.patch.object(pilot.subprocess, "run"):
            result = pilot._restore(self.state, reason="interrupted", completed=False)
        self.assertEqual(result["state"], "aborted")
        service.assert_any_call("restart", pilot.SERVICE)

    def test_modified_dropin_cannot_be_removed(self):
        self.dropin.write_text(pilot.PILOT_MARKER + "\nmodified by someone else\n")
        with mock.patch.object(pilot, "DROPIN", self.dropin), \
             mock.patch.object(pilot, "STATE", self.statusfile):
            with self.assertRaises(RuntimeError):
                pilot._restore(self.state, reason="unexpected", completed=False)
        self.assertTrue(self.dropin.exists())
        self.assertEqual(json.loads(self.statusfile.read_text())["state"],
                         "restore_blocked")


if __name__ == "__main__":
    unittest.main()
