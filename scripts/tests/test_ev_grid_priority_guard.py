"""Read-only regression and historical EVCT/L3 fixtures for 6A grid-priority trial."""
from __future__ import annotations
import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"services"))
import ev_grid_priority_guard as guard

NOW=datetime(2026,10,28,20,56,0,tzinfo=timezone.utc)
GATE={
    "schema_version":1,"mode":"shadow",
    "trial_id":"ev-grid-priority-2026-10-28",
    "max_duration_seconds":900,
    "started_utc":(NOW-timedelta(minutes=1)).isoformat(),
    "expires_utc":(NOW+timedelta(minutes=10)).isoformat(),
}
# Real observed values ~21:40, 21:45 and 21:49; normalised timestamps.
LIVE={
    "timestamp":NOW.isoformat(),
    "grid_unbalanced_output_raw":0,
    "soc_pct":64,
    "battery_temp_c":15,
    "pv_w":0,
    "meter_l1_w":269,"meter_l2_w":362,"meter_l3_w":-792,
    "grid_w":-160,
    "inverter_ac_w":2199,
    "house_load_w":2359,
    "battery_w":2155,
}
EVCC={
    "timestamp":NOW.isoformat(),
    "source":"waveshare_evcc_fc03_readonly",
    "read_ok":True,"state":"C2",
    "charging_detected":True,"physical_control_eligible":False,
    "writes":0,
    "pilot_pwm_raw":100,"configured_pwm_raw":100,
    "channel_currents_a":[.1,6.0,.1],
    "measured_current_a":6.2,
}

class EvGridPriorityTests(unittest.TestCase):
    def call(self,action="discharge",power=2300,live=None,evcc=None,gate=None,now=NOW):
        return guard.suggest(action,power, copy.deepcopy(LIVE if live is None else live),
                             copy.deepcopy(EVCC if evcc is None else evcc),
                             now=now,gate=copy.deepcopy(GATE if gate is None else gate))

    def test_shadow_reproduces_original_21_40_2300w_and_reports_50w_target(self):
        r=self.call()
        self.assertEqual((r.state,r.power_w),("shadow",50))
        self.assertEqual(r.measurements["ev_grid_trial_ev_w_est"],1380)
        self.assertLessEqual(r.measurements["ev_grid_trial_predicted_l3_import_w"],1700)
        self.assertEqual(r.measurements["ev_grid_trial_original_target_w"],2300)

    def test_active_only_reduces_battery_and_never_changes_evcc(self):
        a={**GATE,"mode":"active"}
        r=self.call(gate=a)
        self.assertEqual((r.state,r.power_w,r.action),("active",50,"discharge"))
        self.assertEqual(self.call(action="charge",power=1500,gate=a).state,"not_applicable")
        self.assertEqual(self.call(action="idle",power=0,gate=a).state,"not_applicable")
        self.assertEqual(self.call(power=50,gate=a).power_w,50)

    def test_l3_target_not_just_subtract_1380w_from_total_battery_power(self):
        r=self.call(gate={**GATE,"mode":"active"})
        self.assertLess(r.power_w,2300-1380)
        # Balanced inverter: up to one third of inverter AC reaches each phase;
        # ensuring car on grid requires much more than subtracting EV watts once.
        self.assertLessEqual(r.measurements["ev_grid_trial_non_ev_l3_w_est"],160)

    def test_recent_21_49_save_readings_keep_small_discharge(self):
        d={**LIVE,"grid_w":-2072,"inverter_ac_w":207,"house_load_w":2279,
           "meter_l1_w":-379,"meter_l2_w":-276,"meter_l3_w":-1417}
        r=self.call(live=d,gate={**GATE,"mode":"active"})
        self.assertEqual((r.state,r.power_w),("active",50))

    def test_some_other_l3_house_load_can_remain_battery_funded(self):
        d={**LIVE,"meter_l3_w":-800,"meter_l1_w":-300,"meter_l2_w":-400,
           "grid_w":-1500,"inverter_ac_w":2400,"house_load_w":3900}
        r=self.call(live=d,gate={**GATE,"mode":"active"},power=2500)
        self.assertEqual(r.state,"active")
        self.assertGreater(r.power_w,50)
        self.assertLessEqual(r.power_w,2500)

    def test_source_stale_never_claims_zero_amps(self):
        d={**EVCC,"timestamp":(NOW-timedelta(seconds=80)).isoformat()}
        r=self.call(evcc=d,gate={**GATE,"mode":"active"})
        self.assertEqual((r.state,r.power_w),("unavailable",2300))

    def test_unknown_unbalanced_inverter_rejected(self):
        for v in (None,1,True):
            with self.subTest(v=v):
                self.assertEqual(self.call(
                    live={**LIVE,"grid_unbalanced_output_raw":v},
                    gate={**GATE,"mode":"active"}).state,"unavailable")

    def test_missing_or_invalid_car_state_never_fakes_no_charge(self):
        for field,val in (("read_ok",False),("state","B2"),
                          ("pilot_pwm_raw",200),("writes",1),
                          ("physical_control_eligible",True),
                          ("source","unknown"),("channel_currents_a",[6.0,0,0])):
            with self.subTest(field=field):
                self.assertEqual(self.call(
                    evcc={**EVCC,field:val},gate={**GATE,"mode":"active"}).state,
                    "unavailable")

    def test_bad_soc_temp_or_pv_rejected(self):
        for k,v in (("soc_pct",20),("soc_pct",99),
                    ("battery_temp_c",8),("battery_temp_c",50),
                    ("pv_w",1000)):
            with self.subTest(k=k,v=v):
                self.assertEqual(self.call(live={**LIVE,k:v},gate={**GATE,"mode":"active"}).state,"unavailable")

    def test_insufficient_measurement_or_phase_consistency_rejected(self):
        for k,v in (("meter_l3_w",900),("grid_w",1000),
                    ("house_load_w",100),("inverter_ac_w",float("nan"))):
            with self.subTest(k=k):
                self.assertEqual(self.call(live={**LIVE,k:v},gate={**GATE,"mode":"active"}).state,"unavailable")

    def test_no_active_trial_gate_or_expired_noop(self):
        self.assertEqual(guard.suggest("discharge",2300,LIVE,EVCC,now=NOW,gate=None).state,"off")
        self.assertEqual(self.call(gate={**GATE,"expires_utc":(NOW-timedelta(seconds=1)).isoformat()}).state,"off")
        self.assertEqual(self.call(gate={**GATE,"started_utc":(NOW-timedelta(hours=1)).isoformat()}).state,"off")
        self.assertEqual(self.call(gate={**GATE,"max_duration_seconds":1800}).state,"off")
        self.assertEqual(self.call(gate={**GATE,"mode":"unknown"}).state,"off")

    def test_fails_closed_if_fuse_envelope_exceeded(self):
        # Distribution meter CT totals still must agree with total.
        d={**LIVE,"meter_l1_w":-4000,"meter_l2_w":-1700,"meter_l3_w":-2500,
           "grid_w":-8200,"inverter_ac_w":2199,"house_load_w":10399}
        self.assertEqual(self.call(live=d,gate={**GATE,"mode":"active"}).state,"unavailable")

    def test_dispatch_in_shadow_does_not_change_any_return_field(self):
        x=guard.guard_dispatch("discharge",2300,9.5,False,"original",{},
            NOW,live_path=Path("/nonexistent"),evcc_path=Path("/nonexistent"),gate_path=Path("/nonexistent"))
        self.assertEqual(x,("discharge",2300,9.5,False,"original",{}))

    def test_active_dispatch_is_reducing_only_and_disables_plan_soc_prediction(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            files=[folder/n for n in ("live.json","evcc.json","gate.json")]
            for p,data in zip(files,(LIVE,EVCC,{**GATE,"mode":"active"})):
                p.write_text(json.dumps(data),encoding="utf-8")
            orig=("discharge",2300,9.5,False,"original",{},NOW)
            out=guard.guard_dispatch(*orig,live_path=files[0],
                                      evcc_path=files[1],gate_path=files[2])
            self.assertEqual(out[:4],("discharge",50,None,False))
            self.assertIn("EV L3 trial cap",out[4])
            self.assertEqual(out[5]["ev_grid_trial_status"],"active")
            # Prevent overriding emergency BMS recovery.
            recovery=guard.guard_dispatch(*orig[:5],{"soc_recovery":True},NOW,
                live_path=files[0],evcc_path=files[1],gate_path=files[2])
            self.assertEqual(recovery[1],2300)

    def test_gate_shadow_never_changes_applied_tuple(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            files=[folder/n for n in ("live.json","evcc.json","gate.json")]
            for p,data in zip(files,(LIVE,EVCC,GATE)):
                p.write_text(json.dumps(data),encoding="utf-8")
            out=guard.guard_dispatch("discharge",2300,9.5,False,"original",{},NOW,
                live_path=files[0],evcc_path=files[1],gate_path=files[2])
            self.assertEqual(out[:5],("discharge",2300,9.5,False,"original"))
            self.assertEqual(out[5]["ev_grid_trial_status"],"shadow")

    def test_no_hardware_or_socket_imported(self):
        text=Path(guard.__file__).read_text().lower()
        for term in ("import pymodbus", "requests.post", "socket.", "write_register",
                     "inverter_control", "evcc_ascii", "fc10", "from pymodbus"):
            self.assertNotIn(term,text)

    def test_all_three_phases_have_individual_import_limits(self):
        d={**LIVE,"meter_l1_w":-3000,"grid_w":-3430,"house_load_w":5629}
        r=self.call(live=d,gate={**GATE,"mode":"active"})
        self.assertEqual((r.state,r.power_w),("unavailable",2300))
        self.assertIn("phase",r.reason)

    def test_permanent_gate_is_strict_and_has_no_expiry(self):
        prod={"schema_version":2,"mode":"active",
              "policy_id":guard.PRODUCTION_POLICY_ID,
              "activation_id":"test-activation-20261029",
              "activated_utc":(NOW-timedelta(days=90)).isoformat(),
              "dispatcher_sha256":"a"*64}
        self.assertEqual(guard.validate_gate(prod,NOW),"active")
        self.assertEqual(guard.validate_gate({**prod,"mode":"shadow"},NOW),"shadow")
        for changed in ({"policy_id":"unknown"}, {"activation_id":"bad"},
                        {"dispatcher_sha256":"x"*64},
                        {"activated_utc":(NOW+timedelta(days=1)).isoformat()},
                        {"mode":"always"}):
            with self.subTest(changed=changed):
                self.assertEqual(guard.validate_gate({**prod,**changed},NOW),"off")

    def test_permanent_shadow_and_watchdog_gated_active(self):
        import hashlib
        from unittest.mock import patch
        prod={"schema_version":2,"mode":"shadow",
              "policy_id":guard.PRODUCTION_POLICY_ID,
              "activation_id":"test-activation-20261029",
              "activated_utc":(NOW-timedelta(seconds=120)).isoformat()}
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            live,evcc,gate,status,dispatch=[folder/n for n in (
                "live.json","evcc.json","gate.json","status.json","dispatch.py")]
            dispatch.write_text("test dispatcher",encoding="utf-8")
            prod["dispatcher_sha256"]=hashlib.sha256(dispatch.read_bytes()).hexdigest()
            live.write_text(json.dumps(LIVE),encoding="utf-8")
            evcc.write_text(json.dumps(EVCC),encoding="utf-8")
            gate.write_text(json.dumps(prod),encoding="utf-8")
            # Only the test mocks the root-only ownership predicate.
            with patch.object(guard,"_trusted_file",side_effect=lambda p:p.is_file()):
                def check():
                    return guard.evaluate_snapshot("discharge",2300,NOW,live,evcc,gate,
                                                   status_path=status,dispatch_path=dispatch)
                self.assertEqual(check().state,"shadow")
                gate.write_text(json.dumps({**prod,"mode":"active"}),encoding="utf-8")
                self.assertEqual(check().state,"unavailable")  # no watchdog
                heartbeat={"at_utc":NOW.isoformat(),"state":"active_monitor_ok",
                           "activation_id":prod["activation_id"]}
                status.write_text(json.dumps(heartbeat),encoding="utf-8")
                self.assertEqual((check().state,check().power_w),("active",50))
                status.write_text(json.dumps({**heartbeat,"at_utc":
                    (NOW-timedelta(seconds=41)).isoformat()}),encoding="utf-8")
                self.assertEqual(check().state,"unavailable")
                status.write_text(json.dumps({**heartbeat,"activation_id":"old-session"}),
                                  encoding="utf-8")
                self.assertEqual(check().state,"unavailable")
                status.write_text(json.dumps(heartbeat),encoding="utf-8")
                dispatch.write_text("unexpected deployment",encoding="utf-8")
                self.assertEqual(check().state,"unavailable")
                out=guard.guard_dispatch("charge",1000,None,False,"charge",{},NOW,
                    live_path=live,evcc_path=evcc,gate_path=gate,
                    status_path=status,dispatch_path=dispatch)
                self.assertEqual(out[:2],("charge",1000))

if __name__=="__main__":
    unittest.main()
