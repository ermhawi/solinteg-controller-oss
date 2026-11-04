"""Offline EVCC price owner and manual-priority tests: mocked hardware only."""
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
    """No socket transport; records attempted physical FC10 semantics."""
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
        self.manual = root / "manual.json"
        self.evcc = root / "evcc.json"
        self.grid = root / "live.json"
        self.smart = root / "volvo-smart.json"
        self.cable = root / "cable.json"
        self.track_cable = False
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
                "timer": {"global_timer": {"active": False}, "pending_global_timer": None},
                "current_amp_limit": 8,
                "pending_amp_limit": None,
            },
        }
        self.write_smart()
        self.evcc.write_text(json.dumps(EVCC_CHARGING))
        self.grid.write_text(json.dumps(GRID))
        self.driver = FakeEvcc()
        self.manual_state = {
            "schema_version": 1, "source": ownership.MANUAL_SOURCE,
            "lossless": True, "session_id": SESSION_ID,
            "connection_epoch": 5, "manual_generation": 11,
            "manual_paused": False, "observed_at_utc": NOW.isoformat(),
            "continuous_since_utc": (NOW - timedelta(hours=2)).isoformat(),
        }
        self.write_manual()

    def tearDown(self):
        self.temp.cleanup()

    def write_manual(self):
        self.manual.write_text(json.dumps(self.manual_state))

    def write_smart(self):
        self.smart.write_text(json.dumps(self.smart_state))

    def invoke(self, *, price=EXPENSIVE, session=SESSION, at=NOW):
        env = {
            "EV_PRICE_CONTROL_ENABLED": "1", "EVCC_PRICE_CONTROL_ARMED": "1",
            "EVCC_HOST": "example.invalid", "EV_PRICE_PAUSE_OWNER_PATH": str(self.owner),
            "EV_PRICE_PAUSE_OWNER_LOCK": str(self.lock),
            "EV_MANUAL_INTENT_PATH": str(self.manual),
            "EVCC_READER_LOCK": str(self.evcc_lock),
            "EVCC_READONLY_PATH": str(self.evcc),
            "INVERTER_DATA_PATH": str(self.grid),
            "EV_VOLVO_SMART_CHARGING_SNAPSHOT_PATH": str(self.smart),
            "EV_VOLVO_TARGET_VIN_SHA256": "a" * 64,
        }
        if self.track_cable:
            env["EVCC_CABLE_TRACK_PATH"] = str(self.cable)
        with patch.dict(os.environ, env, clear=True), \
             patch.object(ctl, "read_ev_session", return_value=session), \
             patch.object(ctl, "decide_grid_price", return_value=price), \
             patch.object(ctl, "read_house_price_plan", return_value=None), \
             patch.object(ctl, "EvccAscii", return_value=self.driver):
            return ctl.run_once(now=at)

    def pause(self):
        out = self.invoke()
        self.assertEqual(out["state"], "pause", out)
        self.assertEqual(ownership.read_owner(self.owner)["state"], "owned")

    def test_price_pause_persists_and_resumes_only_its_own_pause(self):
        self.pause()
        self.assertEqual(self.driver.calls, [(RAW_PAUSE, 100, True)])
        # A second independent invocation reloads the owner from disk.
        self.evcc.write_text(json.dumps(EVCC_PAUSED))
        result = self.invoke(price=CHEAP)
        self.assertEqual(result["state"], "resume", result)
        self.assertEqual(self.driver.calls[-1], (100, RAW_PAUSE, True))
        self.assertEqual(ownership.read_owner(self.owner)["state"], "released")

    def test_smart_charging_on_blocks_initial_price_pause(self):
        self.smart_state["chronos"]["locations"]["locations"][0]["optimized_charging_enabled"] = True
        self.write_smart()
        result = self.invoke()
        self.assertEqual((result["state"], result["volvo_smart_charging"], result["writes"]),
                         ("inhibited", "on", 0))
        self.assertEqual(self.driver.calls, [])
        self.assertFalse(self.owner.exists())

    def test_smart_charging_lost_during_price_pause_revokes_resume(self):
        self.pause()
        self.evcc.write_text(json.dumps(EVCC_PAUSED))
        self.smart_state["captured_utc"] = (NOW - timedelta(minutes=11)).isoformat()
        self.write_smart()
        result = self.invoke(price=CHEAP)
        self.assertEqual((result["state"], result["volvo_smart_charging"], result["writes"]),
                         ("inhibited", "unknown", 0))
        self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")
        self.assertEqual(len(self.driver.calls), 1)
        self.smart_state["captured_utc"] = NOW.isoformat()
        self.write_smart()
        self.assertEqual(self.invoke(price=CHEAP)["writes"], 0)

    def test_manual_pause_while_owned_permanently_revokes_resume(self):
        self.pause()
        self.evcc.write_text(json.dumps(EVCC_PAUSED))
        self.manual_state["manual_paused"] = True
        self.manual_state["manual_generation"] += 1
        self.write_manual()
        self.assertEqual(self.invoke(price=CHEAP)["writes"], 0)
        self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")
        self.manual_state["manual_paused"] = False
        self.write_manual()
        self.assertEqual(self.invoke(price=CHEAP)["writes"], 0)
        self.assertEqual(len(self.driver.calls), 1)

    def test_new_connection_or_new_session_invalidates_owner(self):
        for change in ({"connection_epoch": 6}, {"session_id": NEW_ID}):
            with self.subTest(change=change):
                self.owner.unlink(missing_ok=True)
                self.evcc.write_text(json.dumps(EVCC_CHARGING))
                self.pause()
                self.evcc.write_text(json.dumps(EVCC_PAUSED))
                original = self.manual_state.copy()
                self.manual_state.update(change)
                self.write_manual()
                self.assertEqual(self.invoke(price=CHEAP)["writes"], 0)
                self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")
                self.manual_state = original
                self.write_manual()

    def test_unverified_manual_source_never_enables_a_pause(self):
        self.manual.unlink()
        out = self.invoke()
        self.assertEqual(out["state"], "inhibited")
        self.assertEqual(self.driver.calls, [])
        self.assertFalse(self.owner.exists())

    def test_stale_manual_observer_revokes_owner_after_outage(self):
        self.pause()
        self.evcc.write_text(json.dumps(EVCC_PAUSED))
        self.manual_state["observed_at_utc"] = (NOW - timedelta(seconds=91)).isoformat()
        self.write_manual()
        self.assertEqual(self.invoke(price=CHEAP)["writes"], 0)
        self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")

    def test_observer_restarted_after_price_pause_cannot_resume(self):
        self.pause()
        self.evcc.write_text(json.dumps(EVCC_PAUSED))
        self.manual_state["continuous_since_utc"] = (NOW + timedelta(seconds=1)).isoformat()
        self.manual_state["observed_at_utc"] = (NOW + timedelta(seconds=2)).isoformat()
        self.write_manual()
        self.assertEqual(self.invoke(price=CHEAP, at=NOW + timedelta(seconds=3))["writes"], 0)
        self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")

    def test_session_missing_revoke_across_controller_restart(self):
        self.pause()
        self.assertEqual(self.invoke(session=None)["writes"], 0)
        self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")

    def test_mode_changed_revoke_even_with_unchanged_connection(self):
        self.pause()
        self.evcc.write_text(json.dumps(EVCC_PAUSED))
        changed = {**SESSION, "charge_mode": "deadline"}
        self.assertEqual(self.invoke(price=CHEAP, session=changed)["writes"], 0)
        self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")

    def test_out_of_band_resume_revokes_instead_of_repausing(self):
        self.pause()
        self.assertEqual(self.invoke(price=EXPENSIVE)["writes"], 0)
        self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")

    def test_manual_stop_at_raw_100_never_acquires_pause_ownership(self):
        self.evcc.write_text(json.dumps({
            **EVCC_CHARGING, "state": "B2", "channel_currents_a": [0, 0, 0]}))
        self.assertEqual(self.invoke()["writes"], 0)
        self.assertFalse(self.owner.exists())

    def test_uncertain_pause_write_remains_pending_across_restart(self):
        self.driver.fail = True
        with self.assertRaises(EvccUncertainWrite):
            self.invoke()
        self.assertEqual(ownership.read_owner(self.owner)["state"], "pending")
        self.driver.fail = False
        self.evcc.write_text(json.dumps(EVCC_PAUSED))
        self.assertEqual(self.invoke(price=CHEAP)["writes"], 0)
        self.assertEqual(len(self.driver.calls), 1)

    def test_uncertain_resume_write_remains_releasing_across_restart(self):
        self.pause()
        self.evcc.write_text(json.dumps(EVCC_PAUSED))
        self.driver.fail = True
        with self.assertRaises(EvccUncertainWrite):
            self.invoke(price=CHEAP)
        self.assertEqual(ownership.read_owner(self.owner)["state"], "releasing")
        self.driver.fail = False
        self.assertEqual(self.invoke(price=CHEAP)["writes"], 0)
        self.assertEqual(len(self.driver.calls), 2)

    def test_stale_evcc_grid_and_invalid_pilot_revoke_without_fc10(self):
        for source, bad in (
            ("evcc", {**EVCC_PAUSED, "timestamp": (NOW - timedelta(seconds=70)).isoformat()}),
            ("evcc", {**EVCC_PAUSED, "pilot_pwm_raw": 100}),
            ("evcc", {**EVCC_PAUSED, "state": "A1"}),
            ("grid", {**GRID, "timestamp": (NOW - timedelta(seconds=70)).isoformat()}),
        ):
            with self.subTest(source=source, bad=bad):
                self.owner.unlink(missing_ok=True)
                self.pause()
                self.evcc.write_text(json.dumps(EVCC_PAUSED))
                p = self.evcc if source == "evcc" else self.grid
                p.write_text(json.dumps(bad))
                self.assertEqual(self.invoke(price=CHEAP)["writes"], 0)
                self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")
                self.grid.write_text(json.dumps(GRID))
                self.evcc.write_text(json.dumps(EVCC_CHARGING))

    def test_no_write_when_price_unavailable_or_expensive_while_paused(self):
        self.pause()
        self.evcc.write_text(json.dumps(EVCC_PAUSED))
        self.assertEqual(self.invoke(price=EXPENSIVE)["writes"], 0)
        self.assertEqual(len(self.driver.calls), 1)

    def test_corrupt_or_unrecognized_owner_is_never_replaced(self):
        self.owner.write_text('{"schema_version":99}')
        self.assertEqual(self.invoke()["writes"], 0)
        self.assertEqual(self.driver.calls, [])

    def test_lock_prevents_two_price_writers(self):
        with self.lock.open("a+b") as guard:
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.invoke()["writes"], 0)
        self.assertEqual(self.driver.calls, [])

    def test_manual_state_requires_complete_lossless_audit(self):
        for update in ({"lossless": False}, {"source": "home_assistant"},
                       {"manual_generation": -1}, {"manual_paused": 0}):
            with self.subTest(update=update):
                old = self.manual_state.copy()
                self.manual_state.update(update)
                self.write_manual()
                self.assertEqual(self.invoke()["writes"], 0)
                self.manual_state = old
                self.write_manual()


    def setup_cable(self):
        self.track_cable = True
        first = {**EVCC_CHARGING,
                 "timestamp": (NOW - timedelta(seconds=40)).isoformat()}
        second = {**EVCC_CHARGING,
                  "timestamp": (NOW - timedelta(seconds=20)).isoformat()}
        record = cable.advance(None, first, now=NOW - timedelta(seconds=39))
        record = cable.advance(record, second, now=NOW - timedelta(seconds=19))
        cable.store(self.cable, record)
        self.assertEqual(record["status"], "connected")
        return record

    def test_configured_cable_tracker_must_exist_and_be_fresh(self):
        self.track_cable = True
        self.assertEqual(self.invoke()["reason"], "EVCC cable connection unverified")
        self.assertEqual(self.driver.calls, [])
        self.setup_cable()
        self.assertEqual(self.invoke()["state"], "pause")

    def test_unplug_revokes_price_owner_before_any_resume(self):
        self.setup_cable()
        self.pause()
        self.assertEqual(ownership.read_owner(self.owner)["evcc_cable_epoch"], 0)
        self.evcc.write_text(json.dumps(EVCC_PAUSED))
        record = cable.read(self.cable)
        for sec in (15, 35):
            unplugged = {**EVCC_PAUSED, "state": "A1",
                         "pilot_pwm_raw": 267,
                         "timestamp": (NOW + timedelta(seconds=sec)).isoformat()}
            record = cable.advance(record, unplugged,
                                   now=NOW + timedelta(seconds=sec + 1))
        cable.store(self.cable, record)
        self.assertEqual(record["status"], "disconnected")
        result = self.invoke(price=CHEAP, at=NOW + timedelta(seconds=36))
        self.assertEqual(result["reason"], "EVCC cable connection unverified")
        self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")
        self.assertEqual(self.driver.calls, [(RAW_PAUSE, 100, True)])

    def test_replug_never_inherits_old_price_pause_owner(self):
        self.setup_cable()
        self.pause()
        self.evcc.write_text(json.dumps(EVCC_PAUSED))
        record = cable.read(self.cable)
        for sec, state in ((15, "A1"), (35, "A1"), (55, "B2"), (75, "B2")):
            snapshot = {**EVCC_PAUSED, "state": state,
                        "pilot_pwm_raw": 267,
                        "timestamp": (NOW + timedelta(seconds=sec)).isoformat()}
            record = cable.advance(record, snapshot,
                                   now=NOW + timedelta(seconds=sec + 1))
        cable.store(self.cable, record)
        self.assertTrue(record["confirmed_replug"])
        self.assertEqual(record["connection_epoch"], 1)
        self.manual_state["observed_at_utc"] = (NOW + timedelta(seconds=75)).isoformat()
        self.write_manual()
        result = self.invoke(price=CHEAP, at=NOW + timedelta(seconds=76))
        self.assertEqual(result["reason"],
                         "old price-pause ownership invalidated by cable epoch")
        self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")
        self.assertEqual(self.driver.calls, [(RAW_PAUSE, 100, True)])

    def test_one_pending_unplug_blocks_even_before_debounce(self):
        self.setup_cable()
        self.pause()
        record = cable.read(self.cable)
        unplugged = {**EVCC_PAUSED, "state": "A1",
                     "timestamp": (NOW + timedelta(seconds=15)).isoformat()}
        record = cable.advance(record, unplugged,
                               now=NOW + timedelta(seconds=16))
        cable.store(self.cable, record)
        self.assertEqual(record["candidate_status"], "disconnected")
        result = self.invoke(price=CHEAP, at=NOW + timedelta(seconds=16))
        self.assertEqual(result["reason"], "EVCC cable connection unverified")
        self.assertEqual(ownership.read_owner(self.owner)["state"], "revoked")

if __name__ == "__main__":
    unittest.main()
