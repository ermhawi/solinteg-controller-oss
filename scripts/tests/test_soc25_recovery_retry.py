"""Offline regression coverage for independent 26->28% recovery retry."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"services"))
sys.path.insert(0,str(ROOT/"tools"))
import soc25_recovery_retry_watchdog as retry
import soc25_reserve_watchdog as base
import temporary_soc25_watchdog as common
import start_soc25_recovery_retry as installer

NOW=datetime(2026,11,1,20,tzinfo=timezone.utc)
SAMPLE={
    "timestamp":NOW.isoformat(),"soc_pct":26.,"battery_w":-1450,
    "work_mode_raw":0x303,
}


class RecoveryRetryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder=Path(tmp.name)
        self.state=folder/"status.json"
        self.dropin=folder/"49.conf"
        self.lib=folder/"lib"
        self.lib.mkdir()
        self.wrapper=self.lib/"retry-dispatch.py"
        self.policy=self.lib/"policy.py"
        self.base_wrapper=self.lib/"soc25_reserve_dispatch.py"
        for p in (self.wrapper,self.policy,self.base_wrapper):
            p.write_text("test-root-file\n")
        self.state.write_text(json.dumps({
            "schema_version":1,"pilot_id":retry.ID,
            "state":"running","initial_soc_pct":26.,"charge_samples":0,
            "base_wrapper_sha256":hashlib.sha256(self.base_wrapper.read_bytes()).hexdigest(),
        }))
        self.variables=[
            (retry,"STATE",self.state),(retry,"ROOT_LIB",self.lib),
            (retry,"WRAPPER",self.wrapper),(retry,"POLICY",self.policy),
            (retry,"DROPIN",self.dropin),
        ]
        for obj,key,val in self.variables:
            original=getattr(obj,key)
            setattr(obj,key,val)
            self.addCleanup(setattr,obj,key,original)
        for key in ("ID","STATE","DROPIN","ROOT_LIB","WRAPPER","POLICY",
                    "TIMER","DEADLINE_TIMER","MARKER"):
            original=getattr(base,key)
            self.addCleanup(setattr,base,key,original)
        for key in ("STATE","DROPIN","TIMER","PILOT_MARKER","OVERRIDE_ENV"):
            original=getattr(common,key)
            self.addCleanup(setattr,common,key,original)

    def status(self):
        return json.loads(self.state.read_text())

    def test_fresh_charging_requires_separated_real_negative_samples(self):
        status={}
        self.assertTrue(retry._fresh_distinct_charge(SAMPLE,status))
        self.assertEqual(status["charge_samples"],1)
        self.assertFalse(retry._fresh_distinct_charge(SAMPLE,status))
        older={**SAMPLE,"timestamp":(NOW+timedelta(seconds=15)).isoformat()}
        self.assertFalse(retry._fresh_distinct_charge(older,status))
        newer={**SAMPLE,"timestamp":(NOW+timedelta(seconds=30)).isoformat()}
        self.assertTrue(retry._fresh_distinct_charge(newer,status))
        self.assertEqual(status["charge_samples"],2)

    def test_discharge_auto_wrong_soc_or_low_power_never_counts(self):
        for reading in ({**SAMPLE,"battery_w":+1200},
                        {**SAMPLE,"battery_w":-80},
                        {**SAMPLE,"battery_w":-2100},
                        {**SAMPLE,"work_mode_raw":0x101},
                        {**SAMPLE,"soc_pct":28.0},
                        {**SAMPLE,"soc_pct":23.0},
                        {**SAMPLE,"battery_w":float("nan")}):
            with self.subTest(reading=reading):
                self.assertFalse(retry._fresh_distinct_charge(reading,{}))

    def supervisor(self, live, state=None, mode=None):
        if state is not None:
            self.state.write_text(json.dumps(state))
        with mock.patch.object(base,"tick",return_value={"state":"running","measurements":{}}),\
             mock.patch.object(base,"_age_ok",return_value=True),\
             mock.patch.object(base.common,"LIVE",self.lib/"live.json"),\
             mock.patch.object(base,"_restore_owned",
                               return_value={"state":"completed" if mode=="success" else "aborted"}) as restore:
            (self.lib/"live.json").write_text(json.dumps(live))
            result=retry.tick()
            return result,restore.call_args

    def test_at_28_with_two_real_samples_completes_and_restores(self):
        result,restore=self.supervisor(
            {**SAMPLE,"soc_pct":28.,"battery_w":+100,"work_mode_raw":0x101},
            {**self.status(),"charge_samples":2,
             "charge_last_sample_utc":NOW.isoformat(),
             "physical_charge_verified":True},
            mode="success")
        self.assertEqual(result["state"],"completed")
        self.assertIsNotNone(restore)
        self.assertTrue(restore.kwargs["completed"])
        self.assertEqual(self.status()["completion_soc_pct"],28.)

    def test_at_28_without_charging_proof_rolls_back_not_success(self):
        result,restore=self.supervisor({**SAMPLE,"soc_pct":28.},
                                       self.status())
        self.assertEqual(result["state"],"aborted")
        self.assertFalse(restore.kwargs["completed"])
        self.assertIn("without two",restore.kwargs["reason"])

    def test_rising_soc_without_prior_26_entry_is_not_success(self):
        result,restore=self.supervisor(
            {**SAMPLE,"soc_pct":28.},
            {**self.status(),"initial_soc_pct":27.,
             "charge_samples":3})
        self.assertEqual(result["state"],"aborted")
        self.assertFalse(restore.kwargs["completed"])

    def test_nonterminal_watchdog_writes_charge_sample_to_own_root_state(self):
        result,restore=self.supervisor(SAMPLE,self.status())
        self.assertEqual(result["state"],"running")
        self.assertEqual(result["physical_charge_samples"],1)
        self.assertEqual(self.status()["charge_samples"],1)
        self.assertIsNone(restore)

    def test_inactive_underlying_supervisor_never_claims_success(self):
        with mock.patch.object(base,"tick",return_value={"state":"aborted"}),\
             mock.patch.object(base,"_restore_owned") as restore:
            self.assertEqual(retry.tick()["state"],"aborted")
            restore.assert_not_called()

    def test_reread_state_uses_new_paths_not_aborted_original(self):
        retry.configure()
        self.assertEqual(base.ID,"soc25-recovery-retry-20261101")
        self.assertEqual(base.STATE,self.state)
        self.assertEqual(base.common.STATE,self.state)
        self.assertEqual(base.TIMER,retry.TIMER)
        self.assertEqual(base.DEADLINE_TIMER,retry.DEADLINE_TIMER)
        self.assertEqual(base.WRAPPER,self.wrapper)

    def test_new_identity_and_dropin_do_not_overlap_prior_abort(self):
        self.assertNotEqual(retry.ID,"soc25-reserve-20261101")
        self.assertNotEqual(retry.STATE,Path("/var/lib/solinteg-soc25-reserve/status.json"))
        self.assertEqual(retry.DROPIN.name,"49.conf")
        self.assertEqual(retry.MARKER,
                         "# solinteg-soc25-recovery-retry-20261101 (do not edit)")
        self.assertEqual(installer.MAX_TRIAL_DURATION,timedelta(minutes=90))

    def test_installer_checks_25_to_26_only(self):
        self.assertEqual(installer.TARGET,28.0)
        self.assertEqual(installer.ORIGINAL_ENTRY,26.5)
        self.assertNotIn("RuntimeMaxSec",installer._dropin().decode())
        self.assertIn(str(retry.WRAPPER),installer._dropin().decode())

    def test_previous_abort_and_bypass_must_be_terminal(self):
        old=self.lib/"old.json"
        bp=self.lib/"bypass.json"
        old.write_text(json.dumps({"state":"aborted"}))
        bp.write_text(json.dumps({"state":"stopped","restored_runs":49,
                                  "changed_runs_skipped":0}))
        with mock.patch.object(installer,"PREVIOUS",old),\
             mock.patch.object(installer,"PREVIOUS_BYPASS",bp),\
             mock.patch.object(installer,"_active",return_value=False):
            installer._require_previous_trial_restored()
            old.write_text(json.dumps({"state":"running"}))
            with self.assertRaises(RuntimeError):
                installer._require_previous_trial_restored()
            old.write_text(json.dumps({"state":"aborted"}))
            bp.write_text(json.dumps({"state":"stopped","restored_runs":48,
                                      "changed_runs_skipped":1}))
            with self.assertRaises(RuntimeError):
                installer._require_previous_trial_restored()

    def test_tampered_underlying_wrapper_aborts_before_new_tick(self):
        self.base_wrapper.write_text("changed\n")
        with mock.patch.object(base,"tick") as supervisor,\
             mock.patch.object(base,"_restore_owned",
                               return_value={"state":"aborted"}) as restore:
            self.assertEqual(retry.tick()["state"],"aborted")
            supervisor.assert_not_called()
            self.assertIn("underlying",restore.call_args.kwargs["reason"])

    def test_first_tick_precedes_enabling_periodic_timer(self):
        import inspect
        src=inspect.getsource(installer.activate)
        self.assertLess(
            src.index('_service("enable","--now",watchdog.DEADLINE_TIMER)'),
            src.index("initial=watchdog.tick()"))
        self.assertLess(src.index("initial=watchdog.tick()"),
                        src.index('_service("enable","--now",watchdog.TIMER)'))

    def test_no_modbus_or_evcc_writers_in_retry_supervisor(self):
        source=Path(retry.__file__).read_text()
        for forbidden in ("ModbusTcpClient", "pymodbus", "write_register(",
                          "write_u16(", "evcc_ascii", "force_charge("):
            self.assertNotIn(forbidden,source)


if __name__ == "__main__":
    unittest.main()
