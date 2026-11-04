"""No-vehicle-write, atomic Volvo Smart Charging snapshot publisher tests."""
from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import publish_ev_volvo_smart_charging as pub  # noqa: E402

NOW = datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)
VIN_HASH = "a" * 64


def sample(enabled=False):
    return {
        "captured_utc": NOW.isoformat(),
        "method": "read_only_graphql_and_chronos",
        "set_amp_limit_calls": 0,
        "target_vin_sha256": VIN_HASH,
        "sensitive_field": "never-publish-this",
        "chronos": {
            "locations": {
                "locations": [{
                    "optimized_charging_enabled": enabled,
                    "timers": [],
                    "departure_times": [{"active": False, "departure": "07:00",
                                         "location_id": "secret-location"}],
                    "alias": "private-home",
                }],
                "pending_locations": 0,
            },
            "timer": {"global_timer": None, "pending_global_timer": None},
            "current_amp_limit": 8,
            "pending_amp_limit": None,
        },
    }


def battery_sample(**changes):
    data = {
        "source": "chronos_get_latest_battery_readonly",
        "captured_utc": NOW.isoformat(),
        "battery_sampled_at_utc": (NOW - timedelta(seconds=50)).isoformat(),
        "target_vin_sha256": VIN_HASH,
        "soc_pct": 64,
        "charging_status_v2": "charging",
        "charging_power_w": 1386,
        "charging_current_a": 6,
        "charging_voltage_v": 231,
        "charger_power_status_code": 4,
        "connection_status_code": 1,
        "observation_quality": "chronos_reported_sample_time_not_meter_calibration",
        "device_writes": 0,
        "charge_now_override": {
            "action": "not_applicable", "override_state": "not_active",
            "manual_stop_proven": False,
        },
        "raw_vin": "PRIVATE-IDENTIFIER-DO-NOT-PUBLISH",
        "access_token": "PRIVATE-SECRET-DO-NOT-PUBLISH",
    }
    data.update(changes)
    return data


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "private-source.json"
        self.target = root / "private-status.json"
        self.write_source(sample())

    def tearDown(self):
        self.temp.cleanup()

    def write_source(self, item):
        self.source.write_text(json.dumps(item))
        self.source.chmod(0o600)

    def publish(self, *, now=NOW):
        return pub.publish(self.source, self.target, now=now,
                           expected_vin_sha256=VIN_HASH)

    def test_off_published_atomically_and_private_fields_stripped(self):
        result = self.publish()
        self.assertEqual((result["state"], result["device_writes"]), ("off", 0))
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertEqual(self.target.stat().st_uid, self.target.parent.stat().st_uid)
        text = self.target.read_text()
        self.assertNotIn("never-publish-this", text)
        self.assertNotIn("secret-location", text)
        self.assertNotIn("private-home", text)
        self.assertNotIn("departure", text.replace("departure_times", ""))
        self.assertEqual(json.loads(text)["chronos"]["locations"]["locations"][0]
                         ["optimized_charging_enabled"], False)

    def test_fresh_chronos_battery_report_is_allowlisted_not_an_intent_source(self):
        source = sample()
        source["battery_readonly"] = battery_sample()
        self.write_source(source)
        self.assertEqual(self.publish()["state"], "off")
        published = json.loads(self.target.read_text())
        battery = published["battery_readonly"]
        self.assertEqual(battery["soc_pct"], 64)
        self.assertEqual(battery["charging_status_v2"], "charging")
        self.assertFalse(battery["manual_stop_lossless"])
        self.assertFalse(battery["metered_ev_energy_available"])
        self.assertFalse(battery["charge_now_override"]["manual_stop_proven"])
        self.assertNotIn("PRIVATE-IDENTIFIER", self.target.read_text())
        self.assertNotIn("PRIVATE-SECRET", self.target.read_text())

    def test_mismatched_or_stale_optional_battery_is_omitted_not_promoted(self):
        for changed in (
            {"target_vin_sha256": "b" * 64},
            {"battery_sampled_at_utc": (NOW - timedelta(seconds=301)).isoformat()},
            {"soc_pct": 110},
            {"device_writes": 1},
            {"charging_status_v2": "user_manually_paused"},
            {"observation_quality": "inferred_from_ha_field_change"},
        ):
            source = sample()
            source["battery_readonly"] = battery_sample(**changed)
            self.write_source(source)
            with self.subTest(changed=changed):
                self.assertEqual(self.publish()["state"], "off")
                self.assertNotIn("battery_readonly", json.loads(self.target.read_text()))

    def test_on_overwrites_prior_off_and_blocks_price_control(self):
        self.publish()
        self.write_source(sample(True))
        self.assertEqual(self.publish()["state"], "on")
        self.assertTrue(json.loads(self.target.read_text())["chronos"]
                        ["locations"]["locations"][0]["optimized_charging_enabled"])

    def test_stale_or_mismatched_snapshot_cannot_replace_last_good(self):
        self.publish()
        baseline = self.target.read_bytes()
        stale = sample()
        stale["captured_utc"] = (NOW - timedelta(minutes=11)).isoformat()
        self.write_source(stale)
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.target.read_bytes(), baseline)
        self.write_source({**sample(), "target_vin_sha256": "b" * 64})
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.target.read_bytes(), baseline)

    def test_missing_pending_semantics_rejected(self):
        for section, key in (("chronos", "pending_amp_limit"),
                             ("timer", "pending_global_timer"),
                             ("locations", "pending_locations")):
            item = sample()
            record = (item["chronos"] if section == "chronos" else
                      item["chronos"][section])
            del record[key]
            self.write_source(item)
            with self.subTest(section=section), self.assertRaises(ValueError):
                self.publish()

    def test_insecure_source_rejected(self):
        self.source.chmod(0o644)
        with self.assertRaises(ValueError):
            self.publish()
        self.assertFalse(self.target.exists())

    def test_symlink_source_rejected(self):
        copied = self.source.with_suffix(".copy")
        self.source.rename(copied)
        self.source.symlink_to(copied)
        with self.assertRaises(OSError):
            self.publish()
        self.assertFalse(self.target.exists())

    def test_pending_or_active_timers_rejected(self):
        for mutate in ("pending", "active_departure"):
            item = sample()
            if mutate == "pending":
                item["chronos"]["locations"]["pending_locations"] = 1
            else:
                item["chronos"]["locations"]["locations"][0]["departure_times"][0]["active"] = True
            self.write_source(item)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.publish()


if __name__ == "__main__":
    unittest.main()
