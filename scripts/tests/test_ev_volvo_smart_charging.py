"""Read-only Volvo Smart Charging gate: never accept ambiguous OEM state."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_volvo_smart_charging as smart  # noqa: E402

NOW = datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)


def snapshot(enabled: bool | None = False) -> dict:
    return {
        "captured_utc": NOW.isoformat(),
        "method": smart.SOURCE_METHOD,
        "set_amp_limit_calls": 0,
        "target_vin_sha256": "a" * 64,
        "chronos": {
            "locations": {
                "locations": [{"optimized_charging_enabled": enabled,
                               "timers": [], "departure_times": []}],
                "pending_locations": 0,
            },
            "timer": {"global_timer": {"active": False}, "pending_global_timer": None},
            "current_amp_limit": 8,
            "pending_amp_limit": None,
        },
    }


class VolvoSmartChargingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "smart.json"

    def tearDown(self):
        self.temp.cleanup()

    def read(self, record=None, *, now=NOW):
        if record is not None:
            self.path.write_text(json.dumps(record))
        return smart.read_smart_charging(str(self.path), now=now,
                                         expected_vin_sha256="a" * 64)

    def test_fresh_explicit_false_is_only_allow(self):
        self.assertEqual(self.read(snapshot(False))[0], "off")
        self.assertEqual(self.read(snapshot(True))[0], "on")
        self.assertEqual(self.read(snapshot(None))[0], "unknown")
        self.assertEqual(self.read(snapshot(0))[0], "unknown")

    def test_missing_corrupt_or_untrusted_source(self):
        self.assertEqual(self.read()[0], "unknown")
        self.path.write_text("{")
        self.assertEqual(self.read()[0], "unknown")
        for change in ({"method": "untrusted"}, {"set_amp_limit_calls": 1},
                       {"set_amp_limit_calls": False},
                       {"target_vin_sha256": "b" * 64}, {"target_vin_sha256": None}):
            with self.subTest(change=change):
                self.assertEqual(self.read({**snapshot(), **change})[0], "unknown")

    def test_old_future_and_naive_timestamps(self):
        for stamp in ((NOW - timedelta(seconds=601)).isoformat(),
                      (NOW + timedelta(seconds=1)).isoformat(),
                      NOW.replace(tzinfo=None).isoformat(), "invalid"):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.read({**snapshot(), "captured_utc": stamp})[0], "unknown")
        self.assertEqual(self.read(snapshot(), now=NOW + timedelta(seconds=600))[0], "off")

    def test_pending_or_ambiguous_location(self):
        for locations in ({"locations": [], "pending_locations": 0},
                          {"locations": [{}, {}], "pending_locations": 0},
                          {"locations": [{"optimized_charging_enabled": False}], "pending_locations": 1},
                          {"locations": [{"optimized_charging_enabled": False}], "pending_locations": False}):
            with self.subTest(locations=locations):
                item = snapshot()
                item["chronos"]["locations"] = locations
                self.assertEqual(self.read(item)[0], "unknown")

    def test_external_timers_and_pending_changes_block(self):
        for update in (
            {"timer": {"global_timer": {"active": True}, "pending_global_timer": None}},
            {"timer": {"global_timer": {"active": None}, "pending_global_timer": None}},
            {"timer": {"global_timer": None, "pending_global_timer": {"active": True}}},
            {"timer": {"grpc_status": "UNAVAILABLE"}},
            {"pending_amp_limit": 8},
            {"current_amp_limit": None},
            {"current_amp_limit": 0},
        ):
            with self.subTest(update=update):
                item = snapshot()
                item["chronos"].update(update)
                self.assertNotEqual(self.read(item)[0], "off")
        item = snapshot()
        item["chronos"]["timer"]["global_timer"] = None
        self.assertEqual(self.read(item)[0], "off")

    def test_local_departure_or_manual_timer_is_not_smart_off_permission(self):
        for key in ("timers", "departure_times"):
            item = snapshot()
            item["chronos"]["locations"]["locations"][0][key] = [{"active": True}]
            with self.subTest(key=key):
                self.assertEqual(self.read(item)[0], "unknown")

    def test_invalid_response_and_oversized_file(self):
        item = snapshot()
        item["chronos"] = {"locations": {"grpc_status": "UNAVAILABLE"}}
        self.assertEqual(self.read(item)[0], "unknown")
        self.path.write_text(" " * 65537)
        self.assertEqual(self.read()[0], "unknown")


if __name__ == "__main__":
    unittest.main()
