"""Fail-closed tests for the read-only Volvo HA -> local EV-session bridge."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))
import ha_ev_session as reader
from ev_price_policy import read_ev_session


NOW = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)


def state(name, value, *, stamp=NOW):
    return {"entity_id": name, "state": str(value),
            "last_reported": stamp.isoformat(),
            "last_updated": NOW.isoformat()}


def states(*, soc=63, connection="connected", target=80, soc_at=NOW):
    return {
        reader.BATTERY: state(reader.BATTERY, soc, stamp=soc_at),
        reader.CONNECTED: state(reader.CONNECTED, connection),
        reader.TARGET: state(reader.TARGET, target),
    }


def settings(*, mode="deadline", time="07:00", cap=None):
    return {"charge_mode": mode, "departure_local_time": time,
            "max_buy_ore_per_kwh": cap}


class HaEnergyTests(unittest.TestCase):
    def test_deadline_energy_is_calibrated_and_uses_actual_source_time(self):
        s, status = reader.make_session(
            states=states(soc_at=NOW - timedelta(minutes=5)),
            settings=settings(), now=NOW,
            usable_capacity_kwh=78.0, ac_efficiency=0.9,
        )
        self.assertEqual(s["source"], "home_assistant")
        self.assertEqual(s["charge_mode"], "deadline")
        self.assertEqual(s["remaining_ac_kwh"], 14.733)
        self.assertEqual(s["target_soc_pct"], 80)
        self.assertEqual(s["vehicle_soc_pct"], 63)
        self.assertEqual(reader._utc(s["observed_at_utc"]),
                         NOW - timedelta(minutes=5))
        self.assertEqual(reader._utc(s["departure_at_utc"]),
                         datetime(2026, 10, 29, 6, 0, tzinfo=timezone.utc))
        self.assertTrue(status["connected"])

    def test_economy_does_not_get_artificial_departure(self):
        s, status = reader.make_session(
            states=states(), settings=settings(mode="economy", cap=85),
            now=NOW, usable_capacity_kwh=78, ac_efficiency=0.9,
        )
        self.assertEqual(s["charge_mode"], "economy")
        self.assertNotIn("departure_at_utc", s)
        self.assertEqual(s["max_buy_ore_per_kwh"], 85)
        self.assertEqual(read_ev_session.__name__, "read_ev_session")
        self.assertEqual(status["remaining_ac_kwh"], 14.733)

    def test_80_percent_is_hard_upper_target_even_if_soc_above(self):
        for soc in (80, 81.2, 100):
            with self.subTest(soc=soc):
                s, _ = reader.make_session(
                    states=states(soc=soc), settings=settings(mode="economy"),
                    now=NOW, usable_capacity_kwh=78, ac_efficiency=0.9,
                )
                self.assertEqual(s["remaining_ac_kwh"], 0)

    def test_stale_unchanged_soc_does_not_become_fresh_due_to_last_updated(self):
        # Simulates observed HA state where recent charging_status/coordinator
        # updates coexist with much older last_reported of the SoC entity.
        stale = states(soc_at=NOW - timedelta(minutes=21))
        stale[reader.BATTERY]["last_updated"] = NOW.isoformat()
        with self.assertRaisesRegex(reader.UnsafeSession, "battery: stale"):
            reader.make_session(states=stale, settings=settings(),
                                now=NOW, usable_capacity_kwh=78, ac_efficiency=0.9)

    def test_cable_sensor_target_sensor_and_calibration_must_be_verified(self):
        examples = [
            (states(connection="disconnected"), 78, 0.9),
            (states(target=85), 78, 0.9),
            (states(), None, 0.9),
            (states(), 78, None),
            (states(), 150, 0.9),
            (states(), 78, 0.3),
            (states(soc=float("nan")), 78, 0.9),
            (states(soc=101), 78, 0.9),
        ]
        for item, capacity, efficiency in examples:
            with self.subTest(item=item[reader.BATTERY]["state"],
                              capacity=capacity, efficiency=efficiency):
                with self.assertRaises(reader.UnsafeSession):
                    reader.make_session(states=item, settings=settings(),
                                        now=NOW, usable_capacity_kwh=capacity,
                                        ac_efficiency=efficiency)

    def test_missing_report_timestamp_is_not_equivalent_to_recent_get(self):
        values = states()
        values[reader.CONNECTED].pop("last_reported")
        with self.assertRaises(reader.UnsafeSession):
            reader.make_session(states=values, settings=settings(),
                                now=NOW, usable_capacity_kwh=78, ac_efficiency=0.9)

    def test_stale_target_sensor_fails_even_if_battery_fresh(self):
        values = states()
        values[reader.TARGET]["last_reported"] = (
            NOW - timedelta(minutes=25)).isoformat()
        with self.assertRaises(reader.UnsafeSession):
            reader.make_session(states=values, settings=settings(),
                                now=NOW, usable_capacity_kwh=78, ac_efficiency=0.9)

    def test_departure_handles_swedish_local_time_and_dst(self):
        at = datetime(2026, 10, 28, 7, 0, tzinfo=timezone.utc)
        self.assertEqual(reader.next_departure("07:00", now=at),
                         datetime(2026, 10, 29, 6, 0, tzinfo=timezone.utc))
        after_fall = datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc)
        self.assertEqual(reader.next_departure("07:00", now=after_fall),
                         datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc))
        # The repeated 02:30 on autumn DST-change day cannot be guessed.
        before_fall = datetime(2026, 10, 24, 23, 0, tzinfo=timezone.utc)
        with self.assertRaisesRegex(reader.UnsafeSession, "ambiguous"):
            reader.next_departure("02:30", now=before_fall)
        before_spring = datetime(2027, 3, 27, 23, 0, tzinfo=timezone.utc)
        with self.assertRaisesRegex(reader.UnsafeSession, "nonexistent"):
            reader.next_departure("02:30", now=before_spring)

    def test_settings_defaults_validation_and_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "settings.json")
            self.assertEqual(reader.load_settings(path), reader.DEFAULT_SETTINGS)
            for invalid in [
                {"charge_mode": "fast"},
                {"departure_local_time": "25:25"},
                {"max_buy_ore_per_kwh": float("nan")},
                {"max_buy_ore_per_kwh": "unknown"},
            ]:
                with self.subTest(invalid=invalid):
                    Path(path).write_text(json.dumps(invalid), encoding="utf-8")
                    with self.assertRaises(reader.UnsafeSession):
                        reader.load_settings(path)
            Path(path).write_text(json.dumps({"charge_mode": "economy",
                                               "max_buy_ore_per_kwh": 77.5}),
                                  encoding="utf-8")
            self.assertEqual(reader.load_settings(path)["max_buy_ore_per_kwh"], 77.5)

    def test_session_is_immediately_invalidated_when_any_source_goes_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            preferences = Path(tmp) / "preferences.json"
            preferences.write_text(json.dumps(settings(mode="economy")))
            output = Path(tmp) / "session.json"
            report = Path(tmp) / "status.json"
            env = {"EV_SESSION_PATH": str(output), "EV_STATUS_PATH": str(report),
                   "EV_SETTINGS_PATH": str(preferences), "HA_READONLY_TOKEN": "test-only",
                   "HA_API_URL": "http://10.43.1.2:8123",
                   "EV_USABLE_BATTERY_KWH": "78", "EV_AC_CHARGE_EFFICIENCY": "0.9"}
            results = states()
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(reader, "_ha_state", side_effect=lambda b, t, n: results[n]) as getter:
                valid = reader.once(now=NOW)
                self.assertTrue(valid["valid_for_planning"])
                self.assertEqual(read_ev_session(str(output), now=NOW)["charge_mode"], "economy")
                self.assertEqual(getter.call_count, 3)
                results[reader.BATTERY]["last_reported"] = (
                    NOW - timedelta(minutes=30)).isoformat()
                invalid = reader.once(now=NOW)
                self.assertFalse(invalid["valid_for_planning"])
                self.assertIsNone(read_ev_session(str(output), now=NOW))
                self.assertFalse(json.loads(output.read_text())["connected"])
                self.assertIn("stale", json.loads(report.read_text())["reason"])

    def test_token_or_calibration_never_appear_in_session_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "session.json"
            status = Path(tmp) / "status.json"
            env = {"EV_SESSION_PATH": str(output), "EV_STATUS_PATH": str(status),
                   "EV_SETTINGS_PATH": str(Path(tmp) / "missing.json"),
                   "HA_READONLY_TOKEN": "TOPSECRET",
                   "HA_API_URL": "http://10.43.1.2:8123",
                   "EV_USABLE_BATTERY_KWH": "78", "EV_AC_CHARGE_EFFICIENCY": "0.9"}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(reader, "_ha_state",
                                   side_effect=lambda b, t, n: states()[n]):
                reader.once(now=NOW)
            self.assertNotIn("TOPSECRET", output.read_text())
            self.assertNotIn("TOPSECRET", status.read_text())
            self.assertEqual(reader.load_settings(env["EV_SETTINGS_PATH"])["charge_mode"],
                             "deadline")

    def test_only_private_http_addresses_and_direct_get_without_proxy(self):
        for forbidden in (
            "http://172.8.1.2:8123",
            "http://127.0.0.1:8123",
            "http://public.example:8123",
            "http://192.168.1.2:8123/path",
            "http://user:password@10.43.1.2:8123",
        ):
            with self.subTest(url=forbidden), self.assertRaises(reader.UnsafeSession):
                reader._ha_state(forbidden, "SECRET", reader.BATTERY)

        class FakeResponse:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *unused):
                return False
            def read(self, size):
                return json.dumps(state(reader.BATTERY, "63")).encode()

        opener = mock.Mock()
        opener.open.return_value = FakeResponse()
        with mock.patch.object(reader.urllib.request, "build_opener",
                               return_value=opener) as build:
            result = reader._ha_state("http://10.0.0.20:8123",
                                      "TOPSECRET", reader.BATTERY)
        self.assertEqual(result["state"], "63")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url,
                         "http://10.0.0.20:8123/api/states/" + reader.BATTERY)
        self.assertEqual(request.get_header("Authorization"), "Bearer TOPSECRET")
        self.assertEqual(len(build.call_args.args), 2)  # no proxies + no redirects

    def test_unknown_ev_settings_field_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"charge_mode": "economy",
                                        "target_soc_pct": 99}))
            with self.assertRaisesRegex(reader.UnsafeSession, "unknown EV settings"):
                reader.load_settings(str(path))


if __name__ == "__main__":
    unittest.main()
