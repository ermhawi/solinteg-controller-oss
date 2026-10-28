"""Fail-closed SoC and L3 evidence audit, without inventing DC energy."""
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
import ev_ac_quality_audit as audit
import ev_history as history

UTC = timezone.utc
START = datetime(2026, 10, 28, 19, 0, tzinfo=UTC)


def sample(seconds: int, *, soc: float = 57.0, charging: bool = False,
           l3: float = -80.0, battery: float = 220.0,
           source_quality: str = audit.SOURCE_QUALITY,
           changed: datetime | None = None, writes: int = 0) -> dict:
    when = START + timedelta(seconds=seconds)
    changed = changed or START
    return {
        "captured_epoch_ms": int(when.timestamp()*1000),
        "captured_at_utc": when.isoformat(),
        "inverter_fresh": 1,
        "l3_grid_w": l3, "battery_w": battery,
        "house_load_w": 2200, "pv_w": 0,
        "evcc_read_ok": 1,
        "evcc_charging_detected": 1 if charging else 0,
        "evcc_state": "C2" if charging else "B2",
        "evcc_configured_raw": 100,
        "evcc_pilot_raw": 100,
        "ha_record_fresh": 1,
        "volvo_observation_usable": 1,
        "volvo_quality": source_quality,
        "volvo_soc_pct": soc,
        "volvo_connected": 1,
        "volvo_battery_last_changed_utc": changed.isoformat(),
        "volvo_api_received_at_utc": when.isoformat(),
        "physical_writes": writes,
    }


def evcc_ac(ts: datetime) -> dict:
    return {
        "observed_epoch_ms": int(ts.timestamp()*1000),
        "quality": "estimated", "state": "C2",
        "active_channel": "Ict2", "active_current_a": "6.0",
        "configured_pwm_raw": 100, "pilot_pwm_raw": 100,
        "source_writes": 0,
    }


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.hist = str(base/"history.sqlite")
        self.ac = str(base/"ac.sqlite")
        with sqlite3.connect(self.hist) as db:
            db.executescript(history.SCHEMA)
        self.now = START + timedelta(minutes=40)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, rows: list[dict]):
        with sqlite3.connect(self.hist) as db:
            for row in rows:
                keys=list(row)
                db.execute("INSERT INTO ev_samples ("+",".join(keys)+") VALUES ("+
                           ",".join("?" for _ in keys)+")",list(row.values()))

    def make_ac(self):
        start = START+timedelta(minutes=10)
        for sec in range(0, 601, 30):
            ac.store(self.ac, evcc_ac(start+timedelta(seconds=sec)))

    def fake_history(self, *, changed_quality=audit.SOURCE_QUALITY):
        rows=[]
        for sec in range(0, 37*60, 30):
            charge=sec>=9*60
            soc=58.0 if sec>=16*60 else 57.0
            change_at=START+timedelta(minutes=16) if soc==58 else START
            rows.append(sample(sec, soc=soc, charging=charge,
                               l3=-1460 if charge else -80,
                               battery=217 if charge else 220,
                               changed=change_at,
                               source_quality=changed_quality))
        self.put(rows)

    def test_no_ac_source_means_no_dc_calibration(self):
        self.fake_history()
        out=audit.audit(self.ac,self.hist,now=self.now,hours=24)
        self.assertIsNone(out["ac_kwh_covered_only"])
        self.assertIsNone(out["vehicle_net_dc_kwh"])
        self.assertIsNone(out["cost_per_mile_kr"])
        self.assertIsNone(out["soc_evidence"]["delta_soc_percentage_points"])
        self.assertEqual(out["device_writes"],0)

    def test_one_percent_soc_step_is_not_battery_capacity_or_efficiency(self):
        self.fake_history()
        self.make_ac()
        result=audit.audit(self.ac,self.hist,now=self.now,hours=24)
        soc=result["soc_evidence"]
        self.assertEqual(soc["first_soc_pct"],57)
        self.assertEqual(soc["last_soc_pct"],58)
        self.assertEqual(soc["delta_soc_percentage_points"],1)
        self.assertFalse(soc["minimum_trend_resolution_met"])
        self.assertEqual(soc["reason"],"insufficient_soc_steps_and_unverified_usable_capacity")
        self.assertIsNone(soc["calibrated_ac_dc_efficiency"])
        self.assertIsNone(soc["verified_net_battery_dc_kwh"])
        self.assertAlmostEqual(result["ac_kwh_covered_only"],0.23,places=6)
        self.assertEqual(result["quality"],"diagnostic_not_calibration")
        self.assertEqual(result["device_writes"],0)

    def test_l3_change_is_indicative_only_even_when_matching_6a(self):
        self.fake_history()
        result=audit.l3_transition_evidence(
            audit.read_samples(self.hist,
                               since_ms=int(START.timestamp()*1000),
                               until_ms=int(self.now.timestamp()*1000)))
        self.assertEqual(result["quality"],"indicative_only")
        self.assertEqual(result["incremental_l3_import_w"],1380)
        self.assertEqual(result["change_house_battery_discharge_w"],-3)
        self.assertTrue(result["l3_delta_consistent_with_6a_230v"])
        self.assertTrue(result["house_battery_discharge_not_increasing"])
        self.assertFalse(result["source_allocation_verified"])
        self.assertFalse(result["independent_fuse_rms_verified"])
        self.assertTrue(result["precharge_evcc_idle_readback_confirmed"])

    def test_source_report_timestamp_not_mistaken_for_bms_sample(self):
        self.fake_history()
        self.make_ac()
        result=audit.audit(self.ac,self.hist,now=self.now,hours=24)
        self.assertEqual(result["soc_evidence"]["timestamp_role"],
                         "last_changed_not_verified_bms_sample")
        self.assertEqual(result["soc_evidence"]["distinct_reported_soc_levels"],2)

    def test_even_five_soc_points_do_not_prove_battery_capacity(self):
        levels=[57.,58.,59.,60.,61.,62.]
        points=[sample(60*k,soc=level,changed=START+timedelta(seconds=60*k))
                for k,level in enumerate(levels)]
        result=audit.soc_evidence(
            points,start_ms=int(START.timestamp()*1000),
            end_ms=int((START+timedelta(minutes=10)).timestamp()*1000))
        self.assertTrue(result["minimum_trend_resolution_met"])
        self.assertEqual(result["delta_soc_percentage_points"],5)
        self.assertIsNone(result["calibrated_ac_dc_efficiency"])
        self.assertIsNone(result["calibrated_usable_capacity_kwh"])
        self.assertIsNone(result["verified_net_battery_dc_kwh"])

    def test_stale_or_untrusted_soc_never_calibrates(self):
        self.fake_history(changed_quality="untrusted")
        self.make_ac()
        result=audit.audit(self.ac,self.hist,now=self.now,hours=24)
        self.assertEqual(result["soc_evidence"]["observation_quality"],"unavailable")
        self.assertIsNone(result["soc_evidence"]["first_soc_pct"])
        self.assertIsNone(result["soc_evidence"]["delta_soc_percentage_points"])

    def test_not_enough_baseline_is_explicitly_unavailable(self):
        self.put([sample(9*60+30*x,charging=True,l3=-1400)
                  for x in range(12)])
        data=audit.read_samples(self.hist,since_ms=int(START.timestamp()*1000),
                                until_ms=int(self.now.timestamp()*1000))
        result=audit.l3_transition_evidence(data)
        self.assertEqual(result["quality"],"unavailable")
        self.assertEqual(result["reason"],"insufficient_synced_baseline_or_active_data")

    def test_missing_historian_returns_empty_not_create_it(self):
        missing=self.hist+"missing"
        self.assertEqual(audit.read_samples(missing,since_ms=0,until_ms=3),[])
        self.assertFalse(Path(missing).exists())

    def test_historian_write_flag_fails_closed(self):
        self.put([sample(1,writes=1)])
        with self.assertRaisesRegex(audit.QualityError,"write flag"):
            audit.read_samples(self.hist,since_ms=int(START.timestamp()*1000),
                               until_ms=int(self.now.timestamp()*1000))

    def test_bad_source_timezone_or_api_age_never_trusted(self):
        r1=sample(12*60,soc=58)
        r1["volvo_battery_last_changed_utc"]="2026-10-28T19:04:00"
        r2=sample(13*60,soc=59)
        r2["volvo_api_received_at_utc"]=(START+timedelta(minutes=1)).isoformat()
        s=audit.soc_evidence([r1,r2],start_ms=0,end_ms=int(self.now.timestamp()*1000))
        self.assertEqual(s["usable_historian_rows"],0)
        self.assertIsNone(s["delta_soc_percentage_points"])

    def test_bad_hours_does_not_create_or_update_databases(self):
        with self.assertRaises(ValueError):
            audit.audit(self.ac,self.hist,now=self.now,hours=73)
        with self.assertRaises(ValueError):
            audit.audit(self.ac,self.hist,now=self.now,hours=0)
        with self.assertRaises(ValueError):
            audit.audit(self.ac,self.hist,now=self.now,hours=25)
        with sqlite3.connect(self.hist) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ev_samples").fetchone()[0],0)

    def test_no_network_or_hardware_imports(self):
        text=Path(audit.__file__).read_text().lower()
        for forbidden in ("socket.create_connection","import inverter_control",
                          "import evcc_observer", "fc10", "write_register",
                          "requests.post","create table"):
            self.assertNotIn(forbidden,text)


if __name__=="__main__":
    unittest.main()
