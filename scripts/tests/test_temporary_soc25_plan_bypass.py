"""Offline tests for one-off temporary SoC25 optimizer save-mode bypass.

Test SQLite is private in tmpdir. No real inverter, systemd or EVCC writes.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest import mock

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/"services"))
sys.path.insert(0,str(BASE/"tools"))
import temporary_soc25_plan_bypass as bypass
import start_temporary_soc25_plan_bypass as installer

NOW=datetime(2026,11,1,19,0,tzinfo=timezone.utc) # Stockholm 20:00 CET


def original_slots(start="2026-11-01T20:00:00",action="idle"):
    return [
      {"startTime":start,"action":action,"socAfter":5.42,
       "batterySaveDriftKwh":.063,"loadFromGridKwh":.5},
      {"startTime":"2026-11-01T20:15:00","action":"idle","socAfter":5.37,
       "batterySaveDriftKwh":.063,"loadFromGridKwh":.5}
    ]


class PlanBypassTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        d=Path(t.name)
        self.status=d/"status.json"
        self.code=d/"code.py"
        self.code.write_text("original root code")
        self.db=d/"telemetry.db"
        self.root=d/"reserve.json"
        self.live=d/"live.json"
        self.evcc=d/"evcc.json"
        with sqlite3.connect(self.db) as con:
            con.executescript(
                "create table optimizer_runs (id integer primary key,"
                "logged_at text not null,dispatch_json text not null);")
        self.patches=[
            mock.patch.object(bypass,"STATE",self.status),
            mock.patch.object(bypass,"DATABASE",self.db),
            mock.patch.object(bypass,"ROOT_CODE",self.code),
            mock.patch.object(bypass,"ROOT_RESERVE",self.root),
            mock.patch.object(bypass,"LIVE",self.live),
            mock.patch.object(bypass,"EVCC",self.evcc),
            mock.patch.object(bypass,"_disable_timer"),
            mock.patch.object(bypass,"utc_now",return_value=NOW),
            mock.patch.object(bypass,"boot_id",return_value="boot-test"),
        ]
        for p in self.patches:
            p.start();self.addCleanup(p.stop)
        self.write_runtime()
        self.insert(1,original_slots())
        self.status.write_text(json.dumps({
            "schema_version":1,"test_id":"soc25-bypass-20261101",
            "state":"running","started_at_utc":(NOW-timedelta(minutes=1)).isoformat(),
            "expires_at_utc":bypass.EXPIRES.isoformat(),
            "code_sha256":bypass.sha(self.code.read_bytes()),
            "boot_id":"boot-test",
            "originals":{},"physical_override_authorized":True,
        }))
    def write_runtime(self, soc=37.,ev_state="A1"):
        self.root.write_text(json.dumps({
            "state":"running","physical_control_authorized":True,
            "checked_at_utc":(NOW-timedelta(seconds=10)).isoformat(),
            "expires_at_utc":(NOW+timedelta(hours=4)).isoformat(),
        }))
        self.live.write_text(json.dumps({
            "timestamp":NOW.isoformat(),"soc_pct":soc,"battery_w":900,
            "battery_temp_c":15,"pv_w":0,"house_load_w":1100,
            "grid_w":-700,"inverter_ac_w":400,
            "meter_l1_w":-450,"meter_l2_w":-150,"meter_l3_w":-100,
            "grid_unbalanced_output_raw":0,
        }))
        self.evcc.write_text(json.dumps({
            "timestamp":NOW.isoformat(),"source":"waveshare_evcc_fc03_readonly",
            "read_ok":True,"state":ev_state,"configured_pwm_raw":1000,
            "paused_by_pwm":True,"charging_detected":False,
            "writes":0,"measured_current_a":None,
            "channel_currents_a":[None,None,None]}))

    def insert(self,id,slots):
        with sqlite3.connect(self.db) as con:
            con.execute("insert into optimizer_runs (id,logged_at,dispatch_json) values (?,?,?)",
                        (id,(NOW-timedelta(minutes=5-id)).isoformat(),json.dumps(slots)))

    def plan(self,id):
        with sqlite3.connect(self.db) as con:
            return json.loads(con.execute(
                "select dispatch_json from optimizer_runs where id=?",(id,)).fetchone()[0])

    def test_preflight_accepts_a1_with_unknown_amps_only(self):
        ok,reason,metrics=bypass.safe_now(NOW)
        self.assertTrue(ok,reason)
        self.assertEqual(metrics["soc_pct"],37)
        self.assertFalse(bypass.safe_now(NOW+timedelta(minutes=3))[0])

    def test_patch_current_slot_only_preserves_future_and_original(self):
        result=bypass.tick()
        self.assertEqual(result["state"],"running")
        self.assertTrue(result["modified"])
        slots=self.plan(1)
        self.assertNotIn("batterySaveDriftKwh",slots[0])
        self.assertEqual(slots[1]["batterySaveDriftKwh"],.063)
        status=json.loads(self.status.read_text())
        self.assertEqual(len(status["originals"]),1)
        self.assertEqual(bypass.tick()["modified"],False)
        self.assertEqual(bypass.stop(status,"finished")["state"],"stopped")
        self.assertEqual(self.plan(1),original_slots())

    def test_replanner_new_row_is_patched_too_then_both_restored(self):
        bypass.tick()
        self.insert(2,original_slots())
        self.assertTrue(bypass.tick()["modified"])
        self.assertNotIn("batterySaveDriftKwh",self.plan(2)[0])
        self.assertEqual(len(json.loads(self.status.read_text())["originals"]),2)
        self.assertEqual(bypass.tick(force_stop=True)["restored_runs"],2)
        self.assertEqual(self.plan(1),original_slots())
        self.assertEqual(self.plan(2),original_slots())

    def test_soс_threshold_auto_restores(self):
        bypass.tick()
        self.write_runtime(soc=26.)
        result=bypass.tick()
        self.assertEqual(result["state"],"stopped")
        self.assertIn("threshold",result["reason"])
        self.assertIn("batterySaveDriftKwh",self.plan(1)[0])

    def test_unplugged_only_on_any_connected_evcc_restores(self):
        bypass.tick()
        self.write_runtime(ev_state="B2")
        result=bypass.tick()
        self.assertEqual(result["state"],"stopped")
        self.assertIn("EVCC",result["reason"])

    def test_date_deadline_restores_and_disables_timer(self):
        bypass.tick()
        bypass.utc_now.return_value=bypass.EXPIRES
        result=bypass.tick()
        self.assertEqual(result["state"],"stopped")
        self.assertIn("21:45",result["reason"])
        bypass._disable_timer.assert_called()
        self.assertIn("batterySaveDriftKwh",self.plan(1)[0])

    def test_fail_closed_on_inconsistent_phase_or_nonfinite(self):
        r=json.loads(self.live.read_text())
        r["meter_l1_w"]=-900
        self.live.write_text(json.dumps(r))
        self.assertFalse(bypass.safe_now(NOW)[0])
        r["meter_l1_w"]=-450;r["soc_pct"]=float("nan")
        self.live.write_text(json.dumps(r))
        self.assertFalse(bypass.safe_now(NOW)[0])

    def test_restore_does_not_overwrite_external_rewrite(self):
        bypass.tick()
        with sqlite3.connect(self.db) as con:
            con.execute("update optimizer_runs set dispatch_json='[]' where id=1")
        result=bypass.tick(force_stop=True)
        self.assertEqual(result["changed_runs_skipped"],1)
        self.assertEqual(self.plan(1),[])

    def test_no_plan_no_unowned_writes(self):
        with sqlite3.connect(self.db) as con:
            con.execute("delete from optimizer_runs")
        self.assertEqual(bypass.tick()["state"],"stopped")
        self.assertEqual(json.loads(self.status.read_text())["originals"],{})

    def test_only_plan_dispatch_json_is_modified(self):
        source=Path(bypass.__file__).read_text()
        for forbidden in ("pymodbus","ModbusTcpClient","write_register(",
                          "force_discharge(","force_charge(","evcc.write_"):
            self.assertNotIn(forbidden,source)

    def test_unmodified_dispatch_interprets_removed_marker_as_auto(self):
        from scripts.tests.fakes import install_pymodbus_stub
        install_pymodbus_stub()
        import dispatch_loop as dl
        slot=original_slots()[0]
        with mock.patch.object(dl,"FREEZE_HOLDS",False),mock.patch.object(dl,"BATTERY_SAVE_W",50):
            self.assertEqual(dl.battery_save_target("idle",0,slot)[0],"discharge")
            unmarked=dict(slot)
            unmarked.pop("batterySaveDriftKwh")
            self.assertEqual(dl.battery_save_target("idle",0,unmarked),("idle",0))

    def test_local_slot_time_after_dst_fallback(self):
        self.assertEqual(bypass.active_slot(original_slots(),NOW),0)
        self.assertIsNone(bypass.active_slot(original_slots(),NOW-timedelta(minutes=15)))


if __name__=="__main__":
    unittest.main()
