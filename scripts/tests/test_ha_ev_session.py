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
from ev_price_policy import read_ev_session, read_shadow_ev_session


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


def oem_snapshot(*, soc=63, connection="connected", target=80,
                 soc_at=NOW, connection_at=NOW, target_at=NOW, poll_at=NOW,
                 ready=True):
    return {
        "entity_id": reader.OEM_SNAPSHOT,
        "state": "ready" if ready else "unavailable",
        "attributes": {
            "schema_version": 1,
            "timestamp_origin": "volvo_energy_state_field",
            "api_received_at_utc": poll_at.isoformat(),
            "oem_timestamps_complete": ready,
            "physical_writes": 0,
            "battery_soc_pct": soc,
            "battery_oem_at_utc": soc_at.isoformat(),
            "connection": connection,
            "connection_oem_at_utc": connection_at.isoformat(),
            "target_soc_pct": target,
            "target_oem_at_utc": target_at.isoformat(),
            "reason": "all_oem_fields_recent" if ready else "missing_oem_timestamp",
        },
    }


def advisory_snapshot(*, soc=63, target=80, connection="connected",
                      poll_at=NOW, soc_changed=NOW, target_changed=NOW,
                      changed_missing=False, observed=True):
    snap = oem_snapshot(soc=soc, target=target, connection=connection,
                        poll_at=poll_at, soc_at=soc_changed,
                        target_at=target_changed)
    snap["state"] = "observed" if observed else "ready"
    attrs = snap["attributes"]
    attrs.update({
        "schema_version": 2,
        "oem_timestamp_role": "last_changed_not_sampled",
        "api_fetch_confirmed": True,
        "advisory_only": True,
        "physical_control_eligible": False,
        "oem_timestamps_complete": not changed_missing,
        "oem_timestamps_recent": not observed,
    })
    if changed_missing:
        attrs.pop("battery_oem_at_utc")
    return snap


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

    def test_shadow_uses_recent_api_even_when_target_last_changed_ten_days_ago(self):
        with tempfile.TemporaryDirectory() as tmp:
            preferences = Path(tmp) / "preferences.json"
            preferences.write_text(json.dumps(settings(mode="economy")))
            output = Path(tmp) / "strict.json"
            shadow_path = Path(tmp) / "shadow.json"
            report = Path(tmp) / "status.json"
            env = {
                "EV_SESSION_PATH": str(output),
                "EV_SHADOW_SESSION_PATH": str(shadow_path),
                "EV_STATUS_PATH": str(report),
                "EV_SETTINGS_PATH": str(preferences),
                "HA_READONLY_TOKEN": "test-only",
                "HA_API_URL": "http://10.43.1.2:8123",
                "EV_SHADOW_ASSUMPTIONS_ENABLED": "1",
                "EV_SHADOW_ASSUMED_USABLE_KWH": "78",
                "EV_SHADOW_ASSUMED_AC_EFFICIENCY": "0.9",
            }
            snapshot = advisory_snapshot(
                soc_changed=NOW - timedelta(minutes=40),
                target_changed=NOW - timedelta(days=10))
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(reader, "_ha_state", return_value=snapshot) as getter:
                seen = reader.once(now=NOW)
                self.assertTrue(seen["shadow_observation_usable"])
                self.assertTrue(seen["shadow_estimate_ready"])
                self.assertFalse(seen["valid_for_planning"])
                self.assertFalse(seen["physical_control_eligible"])
                self.assertEqual(seen["battery_soc_pct"], 63)
                self.assertEqual(seen["energy_api_received_at_utc"], NOW.isoformat())
                self.assertEqual(getter.call_count, 1)
                self.assertEqual(getter.call_args.args[-1], reader.OEM_SNAPSHOT)
                self.assertIsNone(read_ev_session(str(output), now=NOW))
                scenario = read_shadow_ev_session(str(shadow_path), now=NOW)
                self.assertEqual(scenario["charge_mode"], "economy")
                self.assertEqual(scenario["remaining_ac_kwh"], 14.733)
                self.assertFalse(scenario["physical_control_eligible"])
                self.assertTrue(json.loads(shadow_path.read_text())[
                    "energy_estimate_unverified"])
                # A later genuinely STALE API result invalidates the shadow
                # scenario without ever producing a strict control session.
                snapshot["attributes"]["api_received_at_utc"] = (
                    NOW - timedelta(minutes=6)).isoformat()
                seen = reader.once(now=NOW)
                self.assertFalse(seen["shadow_observation_usable"])
                self.assertFalse(seen["shadow_estimate_ready"])
                self.assertFalse(json.loads(shadow_path.read_text())["connected"])
                self.assertIsNone(read_shadow_ev_session(str(shadow_path), now=NOW))
                self.assertIsNone(read_ev_session(str(output), now=NOW))

    def test_token_or_calibration_never_appear_in_session_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "session.json"
            status = Path(tmp) / "status.json"
            env = {"EV_SESSION_PATH": str(output), "EV_STATUS_PATH": str(status),
                   "EV_SHADOW_SESSION_PATH": str(Path(tmp) / "shadow.json"),
                   "EV_SETTINGS_PATH": str(Path(tmp) / "missing.json"),
                   "EV_SHADOW_ASSUMPTIONS_ENABLED": "0",
                   "HA_READONLY_TOKEN": "TOPSECRET",
                   "HA_API_URL": "http://10.43.1.2:8123",
                   "EV_USABLE_BATTERY_KWH": "78", "EV_AC_CHARGE_EFFICIENCY": "0.9"}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(reader, "_ha_state", return_value=advisory_snapshot()):
                reader.once(now=NOW)
            self.assertNotIn("TOPSECRET", output.read_text())
            self.assertNotIn("TOPSECRET", status.read_text())
            self.assertEqual(reader.load_settings(env["EV_SETTINGS_PATH"])["charge_mode"],
                             "deadline")

    def test_shadow_observation_is_valid_with_old_source_changes_but_no_physical_grade(self):
        sample = advisory_snapshot(
            soc=63, soc_changed=NOW - timedelta(hours=1),
            target_changed=NOW - timedelta(days=10))
        observation = reader.read_shadow_observation(sample, now=NOW)
        self.assertTrue(observation["api_fetch_confirmed"])
        self.assertEqual(observation["vehicle_soc_pct"], 63)
        self.assertFalse(observation["oem_timestamps_recent"])
        self.assertEqual(observation["observation_quality"],
                         "api_recent_field_change_unverified")
        self.assertEqual(observation["target_oem_at_utc"],
                         (NOW - timedelta(days=10)).isoformat())
        self.assertFalse(observation["physical_control_eligible"])
        self.assertEqual(observation["energy_api_received_at_utc"], NOW.isoformat())

    def test_shadow_observation_rejects_false_provenance_or_old_api_fetch(self):
        for field, value in [
            ("schema_version", 1),
            ("timestamp_origin", "ha_last_reported"),
            ("oem_timestamp_role", "verified_bms_sample"),
            ("api_fetch_confirmed", False),
            ("advisory_only", False),
            ("physical_control_eligible", True),
            ("physical_writes", 1),
            ("physical_writes", False),
        ]:
            with self.subTest(field=field):
                sample = advisory_snapshot()
                sample["attributes"][field] = value
                with self.assertRaises(reader.UnsafeSession):
                    reader.read_shadow_observation(sample, now=NOW)
        with self.assertRaisesRegex(reader.UnsafeSession, "stale"):
            reader.read_shadow_observation(
                advisory_snapshot(poll_at=NOW-timedelta(minutes=6)), now=NOW)
        with self.assertRaisesRegex(reader.UnsafeSession, "unavailable"):
            sample = advisory_snapshot()
            sample["state"] = "unavailable"
            reader.read_shadow_observation(sample, now=NOW)
        with self.assertRaisesRegex(reader.UnsafeSession, "snapshot missing"):
            reader.read_shadow_observation(states()[reader.BATTERY], now=NOW)

    def test_shadow_observation_handles_missing_last_change_without_inventing_it(self):
        sample = advisory_snapshot(changed_missing=True)
        observation = reader.read_shadow_observation(sample, now=NOW)
        self.assertIsNone(observation["battery_oem_at_utc"])
        self.assertTrue(observation["api_fetch_confirmed"])
        self.assertFalse(observation["oem_timestamps_complete"])
        self.assertFalse(observation["physical_control_eligible"])

    def test_assumptions_are_opt_in_and_never_physical(self):
        observation = reader.read_shadow_observation(advisory_snapshot(), now=NOW)
        with mock.patch.dict(os.environ, {"EV_SHADOW_ASSUMPTIONS_ENABLED": "0"}):
            with self.assertRaisesRegex(reader.UnsafeSession, "not explicitly"):
                reader.shadow_session_from_observation(
                    observation=observation, settings=settings(),
                    now=NOW, capacity=78, ac_efficiency=0.9)
        with mock.patch.dict(os.environ, {"EV_SHADOW_ASSUMPTIONS_ENABLED": "1"}):
            for cap, efficiency in ((None, 0.9), (78, None), (200, 0.9),
                                    (78, float("nan"))):
                with self.subTest(cap=cap, efficiency=efficiency):
                    with self.assertRaises(reader.UnsafeSession):
                        reader.shadow_session_from_observation(
                            observation=observation, settings=settings(),
                            now=NOW, capacity=cap, ac_efficiency=efficiency)
            result = reader.shadow_session_from_observation(
                observation=observation, settings=settings(),
                now=NOW, capacity=78, ac_efficiency=0.9)
            self.assertEqual(result["charge_mode"], "deadline")
            self.assertEqual(result["remaining_ac_kwh"], 14.733)
            self.assertEqual(result["departure_at_utc"],
                             "2026-10-29T06:00:00+00:00")
            self.assertEqual(result["observation_time_semantics"],
                             "api_fetch_not_bms_sample")
            self.assertTrue(result["energy_estimate_unverified"])
            self.assertFalse(result["physical_control_eligible"])
            self.assertNotEqual(result["observed_at_utc"],
                                (NOW - timedelta(days=10)).isoformat())

    def test_shadow_mode_stops_estimating_need_after_80(self):
        for soc in (80, 81, 100):
            with self.subTest(soc=soc):
                observed = reader.read_shadow_observation(
                    advisory_snapshot(soc=soc), now=NOW)
                with mock.patch.dict(os.environ, {"EV_SHADOW_ASSUMPTIONS_ENABLED": "1"}):
                    result = reader.shadow_session_from_observation(
                        observation=observed, settings=settings(mode="economy"),
                        now=NOW, capacity=78, ac_efficiency=0.9)
                    self.assertEqual(result["remaining_ac_kwh"], 0)
                    self.assertNotIn("departure_at_utc", result)

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

    def test_oem_snapshot_uses_upstream_timestamps_not_ha_poll_time(self):
        snapshot = oem_snapshot(soc_at=NOW - timedelta(minutes=8))
        snapshot["last_reported"] = NOW.isoformat()
        result = reader._oem_snapshot_states(snapshot, now=NOW)
        self.assertEqual(result[reader.BATTERY]["state"], "63")
        self.assertEqual(
            reader._utc(result[reader.BATTERY]["last_reported"]),
            NOW - timedelta(minutes=8))
        session, status = reader.make_session(
            states=result, settings=settings(mode="economy"),
            now=NOW, usable_capacity_kwh=78, ac_efficiency=0.9)
        self.assertEqual(reader._utc(session["observed_at_utc"]),
                         NOW - timedelta(minutes=8))
        self.assertEqual(status["battery_reported_at_utc"],
                         (NOW - timedelta(minutes=8)).isoformat())

    def test_snapshot_refuses_stale_source_even_on_fresh_ha_get(self):
        snapshot = oem_snapshot(soc_at=NOW - timedelta(minutes=21))
        states_read = reader._oem_snapshot_states(snapshot, now=NOW)
        with self.assertRaisesRegex(reader.UnsafeSession, "battery: stale"):
            reader.make_session(states=states_read, settings=settings(),
                                now=NOW, usable_capacity_kwh=78,
                                ac_efficiency=0.9)
        snapshot["attributes"]["api_received_at_utc"] = (
            NOW - timedelta(minutes=6)).isoformat()
        with self.assertRaisesRegex(reader.UnsafeSession, "API response stale"):
            reader._oem_snapshot_states(snapshot, now=NOW)

    def test_snapshot_does_not_allow_fallback_to_original_ha_sensor(self):
        # Former normal HA sensor requests are no longer sufficient to create
        # an EV session because HA last_reported lacks OEM provenance.
        original_sensor = states()[reader.BATTERY]
        with self.assertRaisesRegex(reader.UnsafeSession, "snapshot missing"):
            reader._oem_snapshot_states(original_sensor, now=NOW)
        partial = oem_snapshot()
        partial["attributes"].pop("battery_oem_at_utc")
        with self.assertRaisesRegex(reader.UnsafeSession, "incomplete"):
            reader._oem_snapshot_states(partial, now=NOW)

    def test_snapshot_fail_closed_on_unknown_metadata(self):
        for key, value in [
            ("schema_version", 2),
            ("timestamp_origin", "ha_poll"),
            ("oem_timestamps_complete", False),
            ("physical_writes", 1),
            ("physical_writes", False),
        ]:
            with self.subTest(key=key):
                snapshot = oem_snapshot()
                snapshot["attributes"][key] = value
                with self.assertRaisesRegex(reader.UnsafeSession, "unavailable"):
                    reader._oem_snapshot_states(snapshot, now=NOW)
        snapshot = oem_snapshot(ready=False)
        with self.assertRaisesRegex(reader.UnsafeSession, "missing_oem_timestamp"):
            reader._oem_snapshot_states(snapshot, now=NOW)

    def test_snapshot_prevents_source_timestamp_newer_than_api_reply(self):
        snapshot = oem_snapshot(soc_at=NOW + timedelta(seconds=20))
        with self.assertRaisesRegex(reader.UnsafeSession, "after its API"):
            reader._oem_snapshot_states(snapshot, now=NOW)
        snapshot = oem_snapshot()
        snapshot["attributes"]["connection_oem_at_utc"] = "invalid-time"
        with self.assertRaises(reader.UnsafeSession):
            reader._oem_snapshot_states(snapshot, now=NOW)

    def test_unknown_ev_settings_field_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"charge_mode": "economy",
                                        "target_soc_pct": 99}))
            with self.assertRaisesRegex(reader.UnsafeSession, "unknown EV settings"):
                reader.load_settings(str(path))


if __name__ == "__main__":
    unittest.main()
