"""Activation preflight is evidence-only; cannot arm EVCC."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import ev_price_activation_preflight as pf  # noqa: E402
from ev_price_policy import PriceDecision  # noqa: E402

NOW = datetime(2026, 11, 4, 19, 30, tzinfo=timezone.utc)


def observed_evcc(**changes):
    data = {
        "timestamp": NOW.isoformat(),
        "source": "waveshare_evcc_fc03_readonly",
        "read_ok": True,
        "state": "C2",
        "configured_pwm_raw": 100,
        "pilot_pwm_raw": 100,
        "channel_currents_a": [0.1, 6, 0.1],
        "physical_control_eligible": False,
        "writes": 0,
    }
    data.update(changes)
    return data


def observed_grid(**changes):
    data = {
        "timestamp": NOW.isoformat(),
        "grid_unbalanced_output_raw": 0,
        "meter_l3_w": -1500.0,
        "grid_w": -2100.0,
    }
    data.update(changes)
    return data


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.grid = self.root / "live.json"
        self._write("evcc-readonly.json", observed_evcc())
        first = observed_evcc(timestamp=(NOW - timedelta(seconds=50)).isoformat())
        second = observed_evcc(timestamp=(NOW - timedelta(seconds=30)).isoformat())
        cable = pf.cable_tracker.advance(None, first,
                                         now=NOW - timedelta(seconds=49))
        cable = pf.cable_tracker.advance(cable, second,
                                         now=NOW - timedelta(seconds=29))
        pf.cable_tracker.store(self.root / "evcc-cable-state.json", cable)
        self.grid.write_text(json.dumps(observed_grid()))
        self._write("status.json", {
            "checked_at_utc": NOW.isoformat(),
            "energy_estimate_unverified": True,
            "remaining_ac_kwh": 16.322,
            "charge_mode": "deadline",
            "departure_local_time": "07:00",
        })
        self._write("volvo-smart-charging.json", {
            "battery_readonly": {
                "source": "chronos_get_latest_battery_readonly",
                "captured_utc": NOW.isoformat(),
                "battery_sampled_at_utc": (NOW - timedelta(seconds=50)).isoformat(),
                "target_vin_sha256": "a" * 64,
                "soc_pct": 64,
                "charging_status_v2": "charging",
                "charging_power_w": 1386,
                "charging_current_a": 6,
                "charging_voltage_v": 231,
                "charger_power_status_code": 4,
                "connection_status_code": 1,
                "observation_quality": "chronos_reported_sample_time_not_meter_calibration",
                "charge_now_override": {
                    "action": "not_applicable", "override_state": "not_active",
                    "manual_stop_proven": False,
                },
                "device_writes": 0,
            },
        })
        self.session = {
            "session_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "connection_epoch": 1,
            "charge_mode": "deadline",
            "remaining_ac_kwh": 3.5,
        }
        self.manual = {
            "manual_paused": False,
            "session_id": self.session["session_id"],
            "connection_epoch": 1,
        }

    def tearDown(self):
        self.temp.cleanup()

    def _write(self, file, data):
        (self.root / file).write_text(json.dumps(data))

    def inspect(self, *, session=None, manual=None, smart_state="off", price=True):
        with patch.object(pf, "read_ev_session", return_value=session), \
             patch.object(pf.ownership, "read_manual_intent", return_value=manual), \
             patch.object(pf.smart, "read_smart_charging",
                          return_value=(smart_state, "test diagnostic")), \
             patch.object(pf, "read_house_price_plan", return_value={} if price else None), \
             patch.object(pf, "decide_grid_price",
                          return_value=PriceDecision(6, 45.0, 10.0, "test",
                                                     charge_mode="deadline")):
            return pf.inspect(now=NOW, root=self.root, vin_hash="a" * 64,
                              price_db=self.root / "nonexistent.db",
                              grid_path=self.grid)

    def test_live_shape_without_independent_session_and_manual_audit_blocks(self):
        result = self.inspect()
        self.assertEqual(result["state"], "not_ready")
        self.assertFalse(result["checks"]["independently_calibrated_fresh_ev_session"])
        self.assertFalse(result["checks"]["lossless_manual_stop_source_verified"])
        self.assertTrue(result["checks"]["fresh_consistent_evcc_fc03"])
        self.assertTrue(result["checks"]["fresh_valid_grid_and_l3"])
        self.assertFalse(result["arm_command_issued"])
        self.assertEqual(result["device_writes"], 0)

    def test_all_mocked_operational_inputs_still_need_physical_tests(self):
        result = self.inspect(session=self.session, manual=self.manual)
        self.assertTrue(all(
            ready for label, ready in result["checks"].items()
            if label != "physical_evcc_pause_resume_and_fault_tests"
        ))
        self.assertFalse(result["checks"]["physical_evcc_pause_resume_and_fault_tests"])
        self.assertEqual(result["state"], "not_ready")

    def test_manual_pause_and_mismatched_epoch_never_qualify(self):
        for manual in ({**self.manual, "manual_paused": True},
                       {**self.manual, "connection_epoch": 2},
                       None):
            with self.subTest(manual=manual):
                self.assertFalse(self.inspect(
                    session=self.session, manual=manual)["checks"]
                    ["lossless_manual_stop_source_verified"])

    def test_stale_evcc_or_mismatched_pwm_fails(self):
        for change in ({"timestamp": (NOW - timedelta(seconds=70)).isoformat()},
                       {"pilot_pwm_raw": 1000},
                       {"state": "A1"},
                       {"channel_currents_a": [0.1, 1.0, 0.1]},
                       {"writes": 1}):
            self._write("evcc-readonly.json", observed_evcc(**change))
            with self.subTest(change=change):
                self.assertFalse(self.inspect()["checks"]["fresh_consistent_evcc_fc03"])

    def test_valid_paused_b2_and_bad_phase(self):
        self._write("evcc-readonly.json", observed_evcc(
            state="B2", configured_pwm_raw=1000, pilot_pwm_raw=267,
            channel_currents_a=[0.1, 0, 0.1]))
        self.assertTrue(self.inspect()["checks"]["fresh_consistent_evcc_fc03"])
        self.grid.write_text(json.dumps(observed_grid(grid_unbalanced_output_raw=1)))
        self.assertFalse(self.inspect()["checks"]["fresh_valid_grid_and_l3"])

    def test_paused_pilot_profile_rejects_mismatched_cp_or_current(self):
        baseline = observed_evcc(
            state="B2", configured_pwm_raw=1000, pilot_pwm_raw=267,
            channel_currents_a=[0.1, 0.1, 0.1])
        for changed in (
            {"pilot_pwm_raw": 1000},
            {"pilot_pwm_raw": 100},
            {"state": "A1"},
            {"state": "C2"},
            {"channel_currents_a": [0.1, 2.0, 0.1]},
            {"channel_currents_a": [None, 0.1, 0.1]},
        ):
            with self.subTest(changed=changed):
                self._write("evcc-readonly.json", {**baseline, **changed})
                self.assertFalse(
                    self.inspect()["checks"]["fresh_consistent_evcc_fc03"])
        self._write("evcc-readonly.json", baseline)
        self.assertTrue(self.inspect()["checks"]["fresh_consistent_evcc_fc03"])

    def test_smart_activated_or_unknown_fails(self):
        for state in ("on", "unknown"):
            with self.subTest(state=state):
                result = self.inspect(session=self.session, manual=self.manual,
                                      smart_state=state)
                self.assertFalse(result["checks"]["volvo_smart_charging_explicitly_off"])
                self.assertEqual(result["state"], "not_ready")

    def test_deadline_estimate_is_explicitly_unverified(self):
        report = self.inspect()["planning_advisory"]
        self.assertEqual(report["quality"], "unverified_display_only")
        self.assertAlmostEqual(report["unverified_hours_at_6a_230v"], 11.83)
        self.assertIn("hours_until_departure", report)
        self.assertFalse(report["indicative_deadline_reachable_at_6a"])

    def test_stale_shadow_status_cannot_estimate_deadline(self):
        self._write("status.json", {
            "checked_at_utc": (NOW - timedelta(seconds=301)).isoformat(),
            "energy_estimate_unverified": True,
            "remaining_ac_kwh": 16.322,
            "charge_mode": "deadline",
            "departure_local_time": "07:00",
        })
        self.assertEqual(self.inspect()["planning_advisory"],
                         {"quality": "unverified_display_only"})

    def test_chronos_sample_is_fresh_but_not_manual_intent_or_energy_meter(self):
        result = self.inspect(session=self.session, manual=self.manual)
        self.assertTrue(result["checks"]["fresh_chronos_battery_sample_observed"])
        self.assertEqual(result["volvo_battery"]["soc_pct"], 64)
        self.assertEqual(result["volvo_battery"]["charging_status"], "charging")
        self.assertFalse(result["volvo_battery"]["manual_stop_lossless"])
        self.assertFalse(result["volvo_battery"]["metered_ev_energy_available"])

    def test_stale_bms_timestamp_or_wrong_vehicle_blocks_chronos_evidence(self):
        baseline = json.loads((self.root / "volvo-smart-charging.json").read_text())
        for change in (
            {"battery_sampled_at_utc": (NOW - timedelta(seconds=301)).isoformat()},
            {"target_vin_sha256": "b" * 64},
            {"soc_pct": -1},
            {"charging_status_v2": "manually_stopped"},
        ):
            with self.subTest(change=change):
                item = json.loads(json.dumps(baseline))
                item["battery_readonly"].update(change)
                self._write("volvo-smart-charging.json", item)
                result = self.inspect(session=self.session, manual=self.manual)
                self.assertFalse(result["checks"]["fresh_chronos_battery_sample_observed"])
                self.assertIsNone(result["volvo_battery"])

    def test_cable_disconnected_or_tracker_missing_is_not_ready(self):
        path = self.root / "evcc-cable-state.json"
        path.unlink()
        self.assertFalse(self.inspect()["checks"]["fresh_debounced_evcc_cable_connection"])
        self._write("evcc-cable-state.json", {"schema_version": 99})
        self.assertFalse(self.inspect()["checks"]["fresh_debounced_evcc_cable_connection"])

    def test_corrupt_json_never_passes_physical_inputs(self):
        (self.root / "evcc-readonly.json").write_text("not JSON")
        self.grid.write_text("{}")
        result = self.inspect()
        self.assertFalse(result["checks"]["fresh_consistent_evcc_fc03"])
        self.assertFalse(result["checks"]["fresh_valid_grid_and_l3"])


if __name__ == "__main__":
    unittest.main()
