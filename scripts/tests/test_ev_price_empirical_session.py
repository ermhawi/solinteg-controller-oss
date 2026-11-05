"""Operator-accepted empirical EV session: explicit risk contract, no fake calibration."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_price_empirical_session as empirical  # noqa: E402
import evcc_cable_tracker as cable  # noqa: E402

NOW = datetime(2026, 11, 4, 21, 45, tzinfo=timezone.utc)
VIN_HASH = "a" * 64


class EmpiricalSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.status = root / "status.json"
        self.settings = root / "settings.json"
        self.cable = root / "cable.json"
        self.ledger = root / "session.json"
        self.evcc = root / "evcc-readonly.json"
        self.data = {
            "source": "home_assistant",
            "mode": "shadow",
            "advisory_only": True,
            "physical_control_eligible": False,
            "writes_to_devices": 0,
            "shadow_observation_usable": True,
            "connected": True,
            "target_soc_pct": 80,
            "oem_timestamp_role": "last_changed_not_sampled",
            "timestamp_origin": "volvo_energy_state_field",
            "observation_quality": "api_recent_field_change_unverified",
            "checked_at_utc": NOW.isoformat(),
            "energy_api_received_at_utc": (NOW - timedelta(seconds=30)).isoformat(),
            "battery_reported_at_utc": (NOW - timedelta(minutes=20)).isoformat(),
            "battery_soc_pct": 66.0,
            "charge_mode": "deadline",
            "departure_local_time": "07:00",
        }
        self.save_status()
        self.settings.write_text(json.dumps({
            "charge_mode": "deadline", "departure_local_time": "07:00",
            "max_buy_ore_per_kwh": None,
        }))
        record = None
        for sec in (-60, -30):
            seen = NOW + timedelta(seconds=sec)
            record = cable.advance(record, {
                "source": "waveshare_evcc_fc03_readonly",
                "timestamp": seen.isoformat(), "read_ok": True, "writes": 0,
                "physical_control_eligible": False,
                "state": "C2", "configured_pwm_raw": 100,
                "pilot_pwm_raw": 100, "channel_currents_a": [0.1, 6.0, 0.1],
            }, now=seen + timedelta(seconds=1))
        cable.store(self.cable, record)
        self.evcc.write_text(json.dumps({
            "source": "waveshare_evcc_fc03_readonly", "timestamp": NOW.isoformat(),
            "read_ok": True, "writes": 0, "physical_control_eligible": False,
            "state": "C2", "configured_pwm_raw": 100,
            "pilot_pwm_raw": 100, "channel_currents_a": [0.1, 6.0, 0.1],
        }))

    def tearDown(self):
        self.tmp.cleanup()

    def save_status(self):
        self.status.write_text(json.dumps(self.data))

    def session(self, *, accepted=True, now=NOW, vin=VIN_HASH):
        with patch.dict(os.environ, {"EV_PRICE_ACCEPT_EMPIRICAL_ENERGY":
                                     "1" if accepted else "0"}):
            return empirical.read_session(
                status_path=self.status, settings_path=self.settings,
                cable_path=self.cable, ledger_path=self.ledger,
                now=now, expected_vin_sha256=vin)

    def test_explicit_acceptance_and_stable_locked_departure(self):
        self.assertIsNone(self.session(accepted=False))
        self.assertFalse(self.ledger.exists())
        first = self.session()
        self.assertEqual(first["energy_quality"], empirical.QUALITY)
        self.assertEqual(first["remaining_ac_kwh"], 12.463)
        self.assertFalse(first["independent_ac_meter_verified"])
        self.assertFalse(first["physical_sample_verified"])
        self.assertEqual(first["departure_at_utc"].isoformat(),
                         "2026-11-05T06:00:00+00:00")
        self.assertEqual(self.ledger.stat().st_mode & 0o777, 0o600)
        later = self.session(now=NOW + timedelta(seconds=20))
        self.assertEqual(later["session_id"], first["session_id"])
        self.assertEqual(later["departure_at_utc"], first["departure_at_utc"])

    def test_bad_status_never_generates_or_refreshes_session(self):
        for key, invalid in (
            ("physical_control_eligible", True),
            ("advisory_only", False),
            ("shadow_observation_usable", False),
            ("connected", False),
            ("target_soc_pct", 90),
            ("battery_soc_pct", float("nan")),
            ("oem_timestamp_role", "verified_physical_sample"),
            ("energy_api_received_at_utc", (NOW - timedelta(minutes=6)).isoformat()),
            ("battery_reported_at_utc", (NOW - timedelta(hours=13)).isoformat()),
            ("checked_at_utc", (NOW - timedelta(minutes=6)).isoformat()),
            ("charge_mode", "economy"),
        ):
            with self.subTest(key=key):
                original = self.data[key]
                self.data[key] = invalid
                self.save_status()
                self.assertIsNone(self.session())
                self.data[key] = original
        self.save_status()
        self.assertFalse(self.ledger.exists())

    def test_web_change_of_departure_replaces_same_cable_mission_only_while_charging(self):
        initial = self.session()
        self.assertEqual(initial["departure_at_utc"].hour, 6)
        value = {
            "charge_mode": "deadline", "departure_local_time": "10:00",
            "max_buy_ore_per_kwh": None,
            "revision": "dfe2a2a2-6a68-4d68-bd43-d451de5e801b",
        }
        self.settings.write_text(json.dumps(value))
        # Until the HA observer has reread settings, the status is stale
        # relative to new intent and no FC10 may be authorized.
        self.assertIsNone(self.session())
        self.data["departure_local_time"] = "10:00"
        self.save_status()
        updated = self.session()
        self.assertIsNotNone(updated)
        self.assertNotEqual(updated["session_id"], initial["session_id"])
        self.assertEqual(updated["departure_at_utc"].isoformat(),
                         "2026-11-05T09:00:00+00:00")
        self.assertEqual(updated["settings_revision"], value["revision"])
        self.assertEqual(self.session()["session_id"], updated["session_id"])

    def test_web_switch_to_economy_removes_departure(self):
        before = self.session()
        self.settings.write_text(json.dumps({
            "charge_mode": "economy", "departure_local_time": "07:00",
            "max_buy_ore_per_kwh": 90,
            "revision": "75bb2981-40b4-4c05-a048-7e141fb8c73e",
        }))
        self.data["charge_mode"] = "economy"
        self.save_status()
        after = self.session()
        self.assertIsNotNone(after)
        self.assertNotEqual(after["session_id"], before["session_id"])
        self.assertEqual(after["charge_mode"], "economy")
        self.assertIsNone(after["departure_at_utc"])
        self.assertEqual(after["max_buy_ore_per_kwh"], 90)

    def test_web_change_cannot_inherit_an_owned_pause(self):
        import ev_price_pause_owner as ownership
        original = self.session()
        record = ownership.new_owner(
            session=original, cable=cable.read(self.cable), now=NOW,
            owner_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        ownership.write_owner(self.status.parent / "ev-price-pause-owner.json",
                              {**record, "verified_fc10_pause": True,
                               "state": "owned"})
        self.settings.write_text(json.dumps({
            "charge_mode": "economy", "departure_local_time": "07:00",
            "max_buy_ore_per_kwh": None,
            "revision": "3b532005-eaf2-4d41-b231-67b1b5dbca39",
        }))
        self.data["charge_mode"] = "economy"
        self.save_status()
        self.assertIsNone(self.session())
        self.assertEqual(ownership.read_owner(
            self.status.parent / "ev-price-pause-owner.json")["state"], "owned")

    def test_epoch_or_tracker_file_replacement_cannot_reuse_old_session(self):
        first = self.session()
        self.assertIsNotNone(first)
        state = cable.read(self.cable)
        cable.store(self.cable, {**state, "connection_epoch": 1})
        self.assertIsNone(self.session())
        cable.store(self.cable, {**state, "tracking_id":
                                 "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"})
        self.assertIsNone(self.session())
        cable.store(self.cable, state)
        self.assertEqual(self.session()["session_id"], first["session_id"])

    def test_confirmed_replug_with_existing_6a_restarts_mission_safely(self):
        first = self.session()
        state = cable.read(self.cable)
        cable.store(self.cable, {
            **state, "connection_epoch": 1, "confirmed_replug": True,
            "observed_state": "C2",
            "last_reconnected_at_utc": NOW.isoformat(),
        })
        second = self.session()
        self.assertIsNotNone(second)
        self.assertNotEqual(second["session_id"], first["session_id"])
        self.assertEqual(second["connection_epoch"], 1)
        self.assertEqual(second["remaining_ac_kwh"], 12.463)
        self.assertFalse(second["independent_ac_meter_verified"])
        self.assertEqual(self.session()["session_id"], second["session_id"])

    def test_price_owned_or_revoked_pause_blocks_replug_session_rotation(self):
        import ev_price_pause_owner as ownership
        original_session = self.session()
        original_cable = cable.read(self.cable)
        owned = ownership.new_owner(
            session=original_session, cable=original_cable, now=NOW,
            owner_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        owner_file = self.status.parent / "ev-price-pause-owner.json"
        cable.store(self.cable, {
            **original_cable, "connection_epoch": 1, "confirmed_replug": True,
            "observed_state": "C2", "last_reconnected_at_utc": NOW.isoformat(),
        })
        for state in ("owned", "revoked", "pending", "releasing"):
            with self.subTest(state=state):
                record = {**owned, "state": state,
                          "verified_fc10_pause": state != "pending"}
                ownership.write_owner(owner_file, record)
                self.assertIsNone(self.session())

    def test_replug_while_paused_never_rotates_old_session(self):
        self.session()
        state = cable.read(self.cable)
        cable.store(self.cable, {
            **state, "connection_epoch": 1, "confirmed_replug": True,
            "observed_state": "B2",
            "last_reconnected_at_utc": NOW.isoformat(),
        })
        self.evcc.write_text(json.dumps({
            "source": "waveshare_evcc_fc03_readonly",
            "timestamp": NOW.isoformat(), "read_ok": True, "writes": 0,
            "physical_control_eligible": False,
            "state": "B2", "configured_pwm_raw": 1000,
            "pilot_pwm_raw": 267, "channel_currents_a": [0.1, 0.1, 0.1],
        }))
        self.assertIsNone(self.session())

    def test_expiry_and_invalid_ledgers_are_fail_closed(self):
        self.session()
        self.assertIsNone(self.session(now=NOW + timedelta(hours=10)))
        self.ledger.write_text('{"schema_version":1}')
        self.assertIsNone(self.session())
        self.ledger.unlink()
        self.ledger.symlink_to(self.settings)
        self.assertIsNone(self.session())

    def test_vin_and_cable_required(self):
        self.assertIsNone(self.session(vin=None))
        self.cable.unlink()
        self.assertIsNone(self.session())

    def test_soc_can_reach_target_but_is_still_estimate(self):
        self.data["battery_soc_pct"] = 80.0
        self.save_status()
        row = self.session()
        self.assertEqual(row["remaining_ac_kwh"], 0.0)
        self.assertFalse(row["physical_sample_verified"])


if __name__ == "__main__":
    unittest.main()
