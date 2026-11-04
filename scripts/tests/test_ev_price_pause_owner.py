"""Offline price-pause provenance, ownership, cable fencing and FC10 tests."""
from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_price_controller as ctl  # noqa: E402
import evcc_cable_tracker as cable  # noqa: E402
import ev_price_pause_owner as ownership  # noqa: E402
from ev_price_policy import PriceDecision  # noqa: E402
from evcc_ascii import EvccUncertainWrite, RAW_PAUSE  # noqa: E402

NOW = datetime(2026, 11, 4, 16, 15, tzinfo=timezone.utc)
SESSION_ID = "9b8fca5c-0e2d-4702-a877-a22451fd32a1"
NEW_ID = "06ef0828-b6b1-4b35-a746-7359102bb213"
SESSION = {
    "session_id": SESSION_ID, "connection_epoch": 5,
    "charge_mode": "economy", "remaining_ac_kwh": 8.0,
}
EXPENSIVE = PriceDecision(0, 170.0, 10.0, "expensive", charge_mode="economy")
CHEAP = PriceDecision(6, 30.0, 10.0, "cheap", charge_mode="economy")
EVCC_CHARGING = {
    "timestamp": NOW.isoformat(), "source": "waveshare_evcc_fc03_readonly",
    "read_ok": True, "writes": 0, "physical_control_eligible": False,
    "state": "C2", "configured_pwm_raw": 100,
    "pilot_pwm_raw": 100, "channel_currents_a": [0.0, 6.0, 0.0],
}
EVCC_PAUSED = {
    **EVCC_CHARGING, "state": "B2", "configured_pwm_raw": RAW_PAUSE,
    "pilot_pwm_raw": 267, "channel_currents_a": [0.0, 0.1, 0.0],
}
GRID = {
    "timestamp": NOW.isoformat(), "grid_w": -400, "meter_l3_w": -200,
    "grid_unbalanced_output_raw": 0,
}


class FakeEvcc:
    """No socket transport; records simulated FC10 write intentions."""

    def __init__(self):
        self.calls = []
        self.fail = False

    def set_limit_raw(self, raw, *, expected_raw, scheduled):
        self.calls.append((raw, expected_raw, scheduled))
        if self.fail:
            raise EvccUncertainWrite("synthetic uncertain FC10")
        return SimpleNamespace(
            configured_raw=raw, pilot_raw=267 if raw == RAW_PAUSE else raw,
            state="B2" if raw == RAW_PAUSE else "C2",
            currents_a=(0.0, 0.1, 0.0) if raw == RAW_PAUSE else (0.0, 6.0, 0.0))


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.owner = root / "owner.json"
        self.lock = root / "owner.lock"
        self.evcc_lock = root / "evcc.lock"
        self.evcc = root / "evcc.json"
        self.grid = root / "live.json"
        self.smart = root / "volvo-smart.json"
        self.cable = root / "cable.json"
        self.smart_state = {
            "captured_utc": NOW.isoformat(),
            "method": "read_only_graphql_and_chronos",
            "set_amp_limit_calls": 0,
            "target_vin_sha256": "a" * 64,
            "chronos": {
                "locations": {
                    "locations": [{"optimized_charging_enabled": False,
                                   "timers": [], "departure_times": []}],
                    "pending_locations": 0,
                },
                "timer": {"global_timer": {"active": False},
                          "pending_global_timer": None},
                "current_amp_limit": 8,
                "pending_amp_limit": None,
            },
        }
        self.write_smart()
        self.evcc.write_text(json.dumps(EVCC_CHARGING))
        self.grid.write_text(json.dumps(GRID))
        self.driver = FakeEvcc()
        self.setup_cable()

    def tearDown(self):
        self.temp.cleanup()

    def write_smart(self):
        self.smart.write_text(json.dumps(self.smart_state))

    def setup_cable(self):
        record = None
        for sec in (-40, -20):
            stamp = NOW + timedelta(seconds=sec)
            snap = {**EVCC_CHARGING, "timestamp": stamp.isoformat()}
            record = cable.advance(record, snap, now=stamp + timedelta(seconds=1))
        cable.store(self.cable, record)
        self.assertEqual(record["status"], "connected")
        return record

    def cable_cp(self, state, offset):
        record = cable.read(self.cable)
        stamp = NOW + timedelta(seconds=offset)
        snap = {**(EVCC_PAUSED if state != "C2" else EVCC_CHARGING),
                "state": state, "timestamp": stamp.isoformat()}
        record = cable.advance(record, snap, now=stamp + timedelta(seconds=1))
        cable.store(self.cable, record)
        return record

    def set_paused(self):
        self.evcc.write_text(json.dumps(EVCC_PAUSED))
        self.cable_cp("B2", -5)

    def invoke(self, *, price=EXPENSIVE, session=SESSION, at=NOW):
        env = {
            "EV_PRICE_CONTROL_ENABLED": "1", "EVCC_PRICE_CONTROL_ARMED": "1",
            "EVCC_HOST": "example.invalid", "EV_PRICE_PAUSE_OWNER_PATH": str(self.owner),
            "EV_PRICE_PAUSE_OWNER_LOCK": str(self.lock),
            "EVCC_CABLE_TRACK_PATH": str(self.cable),
            "EVCC_READER_LOCK": str(self.evcc_lock),
            "EVCC_READONLY_PATH": str(self.evcc),
            "INVERTER_DATA_PATH": str(self.grid),
            "EV_VOLVO_SMART_CHARGING_SNAPSHOT_PATH": str(self.smart),
            "EV_VOLVO_TARGET_VIN_SHA256": "a" * 64,
        }
        with patch.dict(os.environ, env, clear=True), \
             patch.object(ctl, "read_ev_session", return_value=session), \
             patch.object(ctl, "decide_grid_price", return_value=price), \
             patch.object(ctl, "read_house_price_plan", return_value=None), \
             patch.object(ctl, "EvccAscii", return_value=self.driver):
            return ctl.run_once(now=at)

    def pause(self):
        out = self.invoke()
        self.assertEqual(out["state"], "pause", out)
        record = ownership.read_owner(self.owner)
        self.assertEqual(record["state"], "owned")
        self.assertTrue(record["verified_fc10_pause"])
        self.assertEqual(record["source"], ownership.SOURCE)
        self.assertEqual(record["schema_version"], 2)
        self.assertNotIn("manual_paused", record)
        self.assertNotIn("lossless", record)
        return record

    def test_price_pause_persists_and_resumes_only_own_verified_pause(self):
        record = self.pause()
        self.assertEqual((record["evcc_cable_epoch"], record["cable_coverage_epoch"]), (0, 0))
        self.assertEqual(self.driver.calls, [(RAW_PAUSE, 100, True)])
        self.set_paused()
        result = self.invoke(price=CHEAP)
        self.assertEqual(result["state"], "resume", result)
        self.assertEqual(self.driver.calls[-1], (100, RAW_PAUSE, True))
        self.assertEqual(ownership.read_owner(self.owner)["state"], "released")

    def test_no_synthetic_manual_source_is_needed_to_create_own_pause(self):
        self.assertEqual(self.invoke()["state"], "pause")
        self.assertEqual(self.driver.calls, [(RAW_PAUSE, 100, True)])

    def test_unowned_paused_evcc_never_auto_resumes(self):
        self.set_paused()
        self.assertEqual(self.invoke(price=CHEAP)["writes"], 0)
        self.assertEqual(self.driver.calls, [])
        self.assertFalse(self.owner.exists())

    def test_smart_charging_on_blocks_initial_price_pause(self):
        self.smart_state["chronos"]["locations"]["locations"][0]["optimized_charging_enabled"] = True
        self.write_smart()
        result = self.invoke()
        self.assertEqual((result["state"], result["volvo_smart_charging"], result["writes"]),
                         ("inhibited", "on", 0))
        self.assertFalse(self.owner.exists())

    def test_smart_charging_lost_while_paused_revokes_resume(self):
        self.pause()
        self.set_paused()
        self.smart_state["captured_utc"] = (NOW - timedelta(minutes=11)).isoformat()
        self.write_smart()
        result = self.invoke(price=CHEAP)
        self.assertEqual((result["state"], result["volvo_smart_charging"]),
                         ("inhibited", "unknown"))
        self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")
        self.assertEqual(len(self.driver.calls), 1)
        self.smart_state["captured_utc"] = NOW.isoformat()
        self.write_smart()
        self.assertEqual(self.invoke(price=CHEAP)["writes"], 0)

    def test_new_session_or_session_epoch_revokes_owner(self):
        for change in ({"connection_epoch": 6}, {"session_id": NEW_ID},
                       {"charge_mode": "deadline"}):
            with self.subTest(change=change):
                self.owner.unlink(missing_ok=True)
                self.evcc.write_text(json.dumps(EVCC_CHARGING))
                self.setup_cable()
                self.pause()
                self.set_paused()
                session = {**SESSION, **change}
                self.assertEqual(self.invoke(price=CHEAP, session=session)["writes"], 0)
                self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")

    def test_session_missing_revokes_across_restart(self):
        self.pause()
        self.assertEqual(self.invoke(session=None)["writes"], 0)
        self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")

    def test_out_of_band_resume_revokes_not_repause(self):
        self.pause()
        self.assertEqual(self.invoke()["writes"], 0)
        self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")
        self.assertEqual(len(self.driver.calls), 1)

    def test_external_b2_stop_at_raw100_never_acquires_owner(self):
        self.evcc.write_text(json.dumps({
            **EVCC_CHARGING, "state": "B2", "channel_currents_a": [0, 0, 0]}))
        self.assertEqual(self.invoke()["writes"], 0)
        self.assertFalse(self.owner.exists())

    def test_uncertain_pause_write_stays_pending_no_retry(self):
        self.driver.fail = True
        with self.assertRaises(EvccUncertainWrite):
            self.invoke()
        self.assertEqual(ownership.read_owner(self.owner)["state"], "pending")
        self.driver.fail = False
        self.set_paused()
        self.assertEqual(self.invoke(price=CHEAP)["writes"], 0)
        self.assertEqual(len(self.driver.calls), 1)

    def test_uncertain_resume_write_stays_releasing_no_retry(self):
        self.pause()
        self.set_paused()
        self.driver.fail = True
        with self.assertRaises(EvccUncertainWrite):
            self.invoke(price=CHEAP)
        self.assertEqual(ownership.read_owner(self.owner)["state"], "releasing")
        self.driver.fail = False
        self.assertEqual(self.invoke(price=CHEAP)["writes"], 0)
        self.assertEqual(len(self.driver.calls), 2)

    def test_fresh_current_and_pilot_required_for_commands(self):
        for source, bad in (
            ("evcc", {**EVCC_PAUSED, "timestamp": (NOW - timedelta(seconds=70)).isoformat()}),
            ("evcc", {**EVCC_PAUSED, "pilot_pwm_raw": 100}),
            ("evcc", {**EVCC_PAUSED, "state": "A1"}),
            ("grid", {**GRID, "timestamp": (NOW - timedelta(seconds=70)).isoformat()}),
        ):
            with self.subTest(source=source, bad=bad):
                self.owner.unlink(missing_ok=True)
                self.setup_cable()
                self.evcc.write_text(json.dumps(EVCC_CHARGING))
                self.pause()
                self.set_paused()
                path = self.evcc if source == "evcc" else self.grid
                path.write_text(json.dumps(bad))
                self.assertEqual(self.invoke(price=CHEAP)["writes"], 0)
                self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")
                self.grid.write_text(json.dumps(GRID))

    def test_expensive_while_paused_keeps_owner_and_does_not_write(self):
        self.pause()
        self.set_paused()
        self.assertEqual(self.invoke(price=EXPENSIVE)["writes"], 0)
        self.assertEqual(ownership.read_owner(self.owner)["state"], "owned")
        self.assertEqual(len(self.driver.calls), 1)

    def test_unverified_owner_or_legacy_schema_never_replaced(self):
        for value in ({"schema_version": 1}, {"schema_version": 99},
                      {"schema_version": 2, "state": "owned"}):
            with self.subTest(value=value):
                self.owner.write_text(json.dumps(value))
                self.owner.chmod(0o600)
                self.assertEqual(self.invoke()["writes"], 0)
                self.assertEqual(len(self.driver.calls), 0)

    def test_owner_file_permissions_and_symlink_are_rejected(self):
        self.pause()
        self.owner.chmod(0o644)
        self.assertEqual(self.invoke(price=CHEAP)["writes"], 0)
        self.owner.chmod(0o600)
        self.owner.unlink()
        self.owner.symlink_to(self.smart)
        self.assertEqual(self.invoke(price=CHEAP)["writes"], 0)

    def test_synthetic_owned_without_fc10_confirmation_is_rejected(self):
        record = ownership.new_owner(
            session=SESSION, cable=cable.read(self.cable), now=NOW, owner_id=NEW_ID)
        with self.assertRaises(ownership.OwnershipError):
            ownership.write_owner(self.owner, {**record, "state": "owned"})

    def test_global_price_owner_lock_serializes_invocations(self):
        with self.lock.open("a+b") as guard:
            self.lock.chmod(0o600)
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.invoke()["writes"], 0)
        self.assertEqual(self.driver.calls, [])

    def test_insecure_owner_lock_never_enters_fc10(self):
        self.lock.write_text("owner lock")
        self.lock.chmod(0o644)
        self.assertEqual(self.invoke()["writes"], 0)
        self.assertFalse(self.owner.exists())

    def test_cable_tracker_is_mandatory(self):
        self.cable.unlink()
        self.assertEqual(self.invoke()["reason"], "EVCC cable connection unverified")
        self.assertFalse(self.owner.exists())
        self.assertEqual(self.driver.calls, [])

    def test_unplug_revokes_price_owner_before_resume(self):
        self.pause()
        self.set_paused()
        self.cable_cp("A1", 15)
        record = self.cable_cp("A1", 35)
        self.assertEqual(record["status"], "disconnected")
        result = self.invoke(price=CHEAP, at=NOW + timedelta(seconds=36))
        self.assertEqual(result["reason"], "EVCC cable connection unverified")
        self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")
        self.assertEqual(self.driver.calls, [(RAW_PAUSE, 100, True)])

    def test_replug_cannot_inherit_previous_fc10_ownership(self):
        self.pause()
        self.set_paused()
        for second, state in ((15, "A1"), (35, "A1"), (55, "B2"), (75, "B2")):
            row = self.cable_cp(state, second)
        self.assertEqual(row["connection_epoch"], 1)
        self.assertTrue(row["confirmed_replug"])
        result = self.invoke(price=CHEAP, at=NOW + timedelta(seconds=76))
        self.assertEqual(result["reason"],
                         "old price-pause ownership invalidated by cable epoch")
        self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")
        self.assertEqual(self.driver.calls, [(RAW_PAUSE, 100, True)])

    def test_single_pending_unplug_blocks_even_before_debounce(self):
        self.pause()
        self.set_paused()
        row = self.cable_cp("A1", 15)
        self.assertEqual(row["candidate_status"], "disconnected")
        result = self.invoke(price=CHEAP, at=NOW + timedelta(seconds=16))
        self.assertEqual(result["reason"], "EVCC cable connection unverified")
        self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")

    def test_observation_gap_revokes_old_pause_even_after_recovery(self):
        self.pause()
        self.set_paused()
        row = self.cable_cp("B2", 130)
        self.assertEqual(row["coverage_epoch"], 1)
        self.cable_cp("B2", 150)
        result = self.invoke(price=CHEAP, at=NOW + timedelta(seconds=151))
        self.assertEqual(result["reason"],
                         "old price-pause ownership invalidated by cable epoch")
        self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")

    def test_deleted_tracker_state_cannot_resurrect_old_epoch_zero(self):
        self.pause()
        self.set_paused()
        old_id = cable.read(self.cable)["tracking_id"]
        self.cable.unlink()
        fresh = self.setup_cable()
        self.assertEqual(fresh["connection_epoch"], 0)
        self.assertEqual(fresh["coverage_epoch"], 0)
        self.assertNotEqual(fresh["tracking_id"], old_id)
        result = self.invoke(price=CHEAP)
        self.assertEqual(result["reason"], "old price-pause ownership invalidated by cable epoch")
        self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")
        self.assertEqual(self.driver.calls, [(RAW_PAUSE, 100, True)])

    def test_changed_cable_during_reader_lock_blocks_fc10(self):
        original_read = ctl.cable_tracker.read_fresh_connected
        calls = []

        def changed(path, *, now):
            calls.append(now)
            if len(calls) == 2:
                return {**original_read(path, now=now), "connection_epoch": 1}
            return original_read(path, now=now)

        with patch.object(ctl.cable_tracker, "read_fresh_connected", side_effect=changed):
            result = self.invoke()
        self.assertEqual(result["reason"], "cable state changed during FC10 preflight")
        self.assertFalse(self.owner.exists())
        self.assertEqual(self.driver.calls, [])

    def test_unresolved_revoked_owner_blocks_new_session_without_reconciliation(self):
        self.pause()
        self.set_paused()
        self.smart_state["captured_utc"] = (NOW - timedelta(minutes=11)).isoformat()
        self.write_smart()
        self.invoke(price=CHEAP)
        self.smart_state["captured_utc"] = NOW.isoformat()
        self.write_smart()
        self.evcc.write_text(json.dumps(EVCC_CHARGING))
        self.assertEqual(self.invoke(session={**SESSION, "session_id": NEW_ID})["writes"], 0)
        self.assertEqual(len(self.driver.calls), 1)


if __name__ == "__main__":
    unittest.main()
