"""AC coverage fragments, historical model-price UTC alignment and safe uncertainty."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_ac_capture as ac
import ev_ac_sessions as sessions
import ev_cost_price_capture as price_capture

UTC = timezone.utc
START = datetime(2026, 10, 28, 19, 14, 45, tzinfo=UTC)


def record(start: datetime, offset: int = 0, *, current: float = 6.0,
           idle: bool = False, read_failed: bool = False, raw: int = 100) -> dict:
    instant = start + timedelta(seconds=offset)
    currents = [0.1, current, 0.1]
    payload = {
        "timestamp": instant.isoformat(),
        "source": "waveshare_evcc_fc03_readonly",
        "read_ok": not read_failed,
        "state": "A1" if idle else "C2",
        "configured_pwm_raw": raw,
        "pilot_pwm_raw": raw,
        "channel_currents_a": [None, None, None] if idle else currents,
        "measured_current_a": None if idle else round(sum(currents), 2),
        "charging_detected": not idle and not read_failed,
        "physical_control_eligible": False,
        "modbus_function": "FC03",
        "writes": 0,
    }
    return ac.parse(payload, now=instant + timedelta(seconds=1))


def price_row(start: datetime, buy: float = 100.0) -> dict:
    ts = int(start.timestamp() * 1000)
    return {
        "slot_start_epoch_ms": ts,
        "slot_end_epoch_ms": ts + 900000,
        "source_date": start.date().isoformat(),
        "source_logged_at_utc": "2026-10-28T17:00:00+00:00",
        "source_sha256": "a" * 64,
        "buy_before_grid_ore": str(buy),
        "sell_model_ore": "10",
        "quality": price_capture.QUALITY,
    }


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ac = str(Path(self.temp.name) / "ac.sqlite")
        self.prices = str(Path(self.temp.name) / "prices.sqlite")
        self.now = START + timedelta(hours=1)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, *points: tuple[int, dict]):
        for offset, kwargs in points:
            ac.store(self.ac, record(START, offset, **kwargs))

    def report(self, days=1):
        return sessions.analyze(self.ac, self.prices, now=self.now, days=days)

    def test_no_source_files_remains_unavailable_and_readonly(self):
        result = self.report()
        self.assertEqual(result["observations"], 0)
        self.assertIsNone(result["estimated_ac_kwh_covered_only"])
        self.assertIsNone(result["actual_cost_ore"])
        self.assertFalse(Path(self.ac).exists())
        self.assertFalse(Path(self.prices).exists())

    def test_contiguous_active_samples_are_covered_fragment_not_full_session(self):
        self.put((0, {}), (30, {}), (60, {}))
        result = self.report()
        self.assertEqual(result["observations"], 3)
        self.assertEqual(result["covered_intervals"], 2)
        self.assertEqual(result["fragment_count"], 1)
        self.assertEqual(result["covered_seconds"], 60)
        self.assertEqual(result["estimated_ac_kwh_covered_only"], 0.023)
        fragment = result["observed_charging_fragments"][0]
        self.assertFalse(fragment["start_bounded_by_idle_read"])
        self.assertFalse(fragment["end_bounded_by_idle_read"])
        self.assertFalse(fragment["full_session_energy_known"])
        self.assertIsNone(result["full_session_ac_kwh"])
        self.assertIsNone(result["actual_cost_ore"])
        self.assertIsNone(result["vehicle_battery_dc_kwh"])

    def test_idle_transitions_bound_observations_but_not_exact_session_energy(self):
        self.put((-30, {"idle": True}), (0, {}), (30, {}), (60, {"idle": True}))
        result = self.report()
        fragment = result["observed_charging_fragments"][0]
        self.assertTrue(fragment["start_bounded_by_idle_read"])
        self.assertTrue(fragment["end_bounded_by_idle_read"])
        self.assertFalse(fragment["full_session_energy_known"])
        self.assertEqual(fragment["covered_seconds"], 30)
        self.assertEqual(result["estimated_ac_kwh_covered_only"], 0.0115)

    def test_gap_splits_coverage_and_never_assumes_full_charge(self):
        self.put((0, {}), (30, {}), (95, {}), (125, {}))
        result = self.report()
        self.assertEqual(result["fragment_count"], 2)
        self.assertEqual(result["covered_intervals"], 2)
        self.assertEqual(result["covered_seconds"], 60)
        self.assertEqual(result["skipped_source_transitions_or_gaps"], 1)
        self.assertEqual(result["energy_not_observed_during_gaps"], "unknown")

    def test_failed_read_splits_and_is_not_interpolated(self):
        self.put((0, {}), (30, {"read_failed": True}), (60, {}), (90, {}))
        result = self.report()
        self.assertEqual(result["covered_intervals"], 1)
        self.assertEqual(result["estimated_ac_kwh_covered_only"], 0.0115)
        self.assertEqual(result["skipped_source_transitions_or_gaps"], 2)

    def test_non_six_amp_observations_do_not_create_billable_energy(self):
        self.put((0, {}), (30, {"raw": 166}), (60, {}))
        result = self.report()
        self.assertEqual(result["covered_intervals"], 0)
        self.assertIsNone(result["estimated_ac_kwh_covered_only"])
        self.assertIsNone(result["actual_cost_ore"])

    def test_local_price_model_crosses_quarter_hour_in_utc_without_guessing(self):
        self.put((0, {}), (30, {}))
        first = datetime(2026,10,28,19,0,tzinfo=UTC)
        second = first + timedelta(minutes=15)
        price_capture.append(self.prices, [price_row(first,100),price_row(second,300)])
        result = self.report()
        self.assertEqual(result["estimated_ac_kwh_covered_only"], 0.0115)
        self.assertEqual(result["hypothetical_grid_only_spot_model_ore"], 2.3)
        self.assertEqual(result["observed_charging_fragments"][0][
            "hypothetical_grid_only_spot_model_ore"], 2.3)
        self.assertIsNone(result["actual_cost_ore"])
        self.assertIn("all_covered_AC_is_purchased_grid_energy",
                      result["hypothetical_pricing_assumption"])
        self.assertEqual(result["source_price_quality"],price_capture.QUALITY)

    def test_price_integral_uses_linear_current_not_flat_split(self):
        self.put((0, {"current": 5.0}), (30, {"current": 6.0}))
        first = datetime(2026,10,28,19,0,tzinfo=UTC)
        price_capture.append(self.prices, [price_row(first,100),
                                           price_row(first+timedelta(minutes=15),300)])
        result = self.report()
        # 15s from 5.0 to 5.5 A + 15s from 5.5 to 6.0 A.
        self.assertAlmostEqual(result["hypothetical_grid_only_spot_model_ore"],
                               2.15625, places=4)

    def test_missing_second_price_half_forces_null_full_preview(self):
        self.put((0, {}), (30, {}))
        first=datetime(2026,10,28,19,0,tzinfo=UTC)
        price_capture.append(self.prices,[price_row(first,100)])
        result=self.report()
        self.assertIsNone(result["hypothetical_grid_only_spot_model_ore"])
        self.assertIsNone(result["observed_charging_fragments"][0][
            "hypothetical_grid_only_spot_model_ore"])
        self.assertIsNone(result["actual_cost_ore"])

    def test_negative_model_price_allowed_but_never_actual_billing(self):
        self.put((0, {}), (30, {}))
        first=datetime(2026,10,28,19,0,tzinfo=UTC)
        price_capture.append(self.prices,[price_row(first,-30),
                                          price_row(first+timedelta(minutes=15),-10)])
        result=self.report()
        self.assertEqual(result["hypothetical_grid_only_spot_model_ore"],-0.23)
        self.assertIsNone(result["actual_cost_ore"])

    def test_fall_back_duplicate_local_hour_remains_distinct_utc_prices(self):
        # Both intervals look like Swedish 02:xx, but are an hour apart in UTC.
        start=datetime(2026,10,25,0,59,45,tzinfo=UTC)
        ac.store(self.ac,record(start,0))
        ac.store(self.ac,record(start,30))
        first=datetime(2026,10,25,0,45,tzinfo=UTC)
        price_capture.append(self.prices,[price_row(first,100),
                                         price_row(first+timedelta(minutes=15),300)])
        result=sessions.analyze(self.ac,self.prices,now=self.now,days=7)
        self.assertEqual(result["hypothetical_grid_only_spot_model_ore"],2.3)

    def test_corrupt_original_ac_integral_is_rejected_not_silently_recomputed(self):
        self.put((0, {}), (30, {}))
        with sqlite3.connect(self.ac) as db:
            db.execute("UPDATE ev_ac_estimated_intervals SET estimated_ac_kwh='9'")
        with self.assertRaisesRegex(sessions.SessionEvidenceError, "does not match"):
            self.report()

    def test_tampered_evidence_source_writes_is_rejected(self):
        self.put((0, {}), (30, {}))
        with sqlite3.connect(self.ac) as db:
            row=db.execute("SELECT observed_epoch_ms,payload_json FROM ev_ac_observations "
                           "ORDER BY observed_epoch_ms LIMIT 1").fetchone()
            d=json.loads(row[1]);d["source_writes"]=1
            db.execute("UPDATE ev_ac_observations SET payload_json=? "
                       "WHERE observed_epoch_ms=?",(json.dumps(d),row[0]))
        with self.assertRaisesRegex(sessions.SessionEvidenceError, "unsafe source"):
            self.report()

    def test_malformed_price_evidence_quality_fails_closed(self):
        self.put((0, {}), (30, {}))
        first=datetime(2026,10,28,19,0,tzinfo=UTC)
        price_capture.append(self.prices,[price_row(first,100)])
        with sqlite3.connect(self.prices) as db:
            db.execute("UPDATE ev_cost_price_evidence SET quality='metered'")
        with self.assertRaisesRegex(sessions.SessionEvidenceError, "untrusted"):
            self.report()

    def test_recent_charging_flag_requires_fresh_sample(self):
        self.put((0, {}), (30, {}))
        early=sessions.analyze(self.ac,self.prices,
                               now=START+timedelta(seconds=70),days=1)
        late=self.report()
        self.assertTrue(early["charging_observed_recently"])
        self.assertFalse(late["charging_observed_recently"])

    def test_future_evidence_is_not_counted_in_current_window(self):
        self.put((0, {}), (30, {}), (120, {}), (150, {}))
        at_first_pair=sessions.analyze(
            self.ac,self.prices,now=START+timedelta(seconds=60),days=1)
        self.assertEqual(at_first_pair["observations"],2)
        self.assertEqual(at_first_pair["covered_intervals"],1)
        self.assertEqual(at_first_pair["estimated_ac_kwh_covered_only"],0.0115)

    def test_invalid_window_refused(self):
        with self.assertRaises(ValueError):
            self.report(days=31)
        with self.assertRaises(ValueError):
            self.report(days=0)

    def test_never_accesses_hardware_or_mutates_source_db(self):
        text=Path(sessions.__file__).read_text().lower()
        for forbidden in ("socket.create_connection", "import inverter_control",
                          "import evcc_observer", "fc10", "write_register",
                          "requests.post", "create table"):
            self.assertNotIn(forbidden,text)
        self.put((0,{}),(30,{}))
        self.report()
        with sqlite3.connect(self.ac) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0],"ok")


if __name__ == "__main__":
    unittest.main()
