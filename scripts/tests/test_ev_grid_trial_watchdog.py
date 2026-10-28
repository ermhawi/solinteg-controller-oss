"""Mock-only safety checks for independent EV grid-priority trial watchdog."""
from __future__ import annotations
import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"services"))
import ev_grid_trial_watchdog as w

NOW=datetime(2026,10,28,21,0,0,tzinfo=timezone.utc)
ACTIVE={
    "schema_version":1,"mode":"active",
    "trial_id":"ev-grid-priority-2026-10-28",
    "max_duration_seconds":900,
    "started_utc":(NOW-timedelta(seconds=120)).isoformat(),
    "expires_utc":(NOW+timedelta(minutes=9)).isoformat(),
}

class WatchdogTests(unittest.TestCase):
    def test_no_gate_no_action(self):
        with patch.object(w,"read_json",side_effect=RuntimeError("missing")):
            self.assertEqual(w.monitor(NOW),"no_active_trial")

    def test_shadow_does_not_restart_service(self):
        with patch.object(w,"read_json",return_value={**ACTIVE,"mode":"shadow"}),patch.object(w,"restore") as restore:
            self.assertEqual(w.monitor(NOW),"not_armed")
            restore.assert_not_called()

    def test_expiry_reverts_independent_of_dispatch(self):
        expired={**ACTIVE,"expires_utc":(NOW-timedelta(seconds=1)).isoformat()}
        with patch.object(w,"read_json",return_value=expired),patch.object(w,"restore") as restore:
            self.assertEqual(w.monitor(NOW),"rolled_back")
            restore.assert_called_once_with("scheduled trial deadline",NOW)

    def test_invalid_manifest_forces_revert(self):
        broken={**ACTIVE,"trial_id":"wrong"}
        with patch.object(w,"read_json",return_value=broken),patch.object(w,"restore") as restore:
            self.assertEqual(w.monitor(NOW),"rolled_back")
            self.assertIn("invalid",restore.call_args[0][0])

    def test_failed_phase_bms_evcc_quality_aborts(self):
        with patch.object(w,"read_json",return_value=ACTIVE),patch.object(w,"quality_reason",return_value="EVCC stale"),patch.object(w,"restore") as restore:
            self.assertEqual(w.monitor(NOW),"rolled_back")
            restore.assert_called_once_with("EVCC stale",NOW)

    def test_healthy_trial_writes_status_but_never_restores(self):
        with patch.object(w,"read_json",return_value=ACTIVE),patch.object(w,"quality_reason",return_value=None),patch.object(w,"atomic_status") as status,patch.object(w,"restore") as restore:
            self.assertEqual(w.monitor(NOW),"active_monitor_ok")
            self.assertEqual(status.call_args[0][0]["state"],"active_monitor_ok")
            restore.assert_not_called()

    def test_real_quality_predicate_mocked_systemctl(self):
        import ev_grid_priority_guard as g
        live={
            "timestamp":NOW.isoformat(),"grid_unbalanced_output_raw":0,
            "soc_pct":64,"battery_temp_c":15,"pv_w":0,"grid_w":-160,
            "meter_l1_w":269,"meter_l2_w":362,"meter_l3_w":-792,
            "inverter_ac_w":2199,"house_load_w":2359,
            "battery_w":235,"work_mode":"EMS BattCtrl",
        }
        ev={
            "timestamp":NOW.isoformat(),"source":"waveshare_evcc_fc03_readonly",
            "read_ok":True,"state":"C2","charging_detected":True,
            "physical_control_eligible":False,"writes":0,
            "configured_pwm_raw":100,"pilot_pwm_raw":100,
            "channel_currents_a":[.1,6.0,.1],"measured_current_a":6.2,
        }
        with patch.object(w.DISPATCH.__class__,"read_text",return_value="from ev_grid_priority_guard import"),patch.object(w,"run"),patch.object(w,"read_json",side_effect=[live,ev]):
            self.assertIsNone(w.quality_reason(NOW,ACTIVE))
        too_high={**live,"battery_w":1900}
        with patch.object(w.DISPATCH.__class__,"read_text",return_value="from ev_grid_priority_guard import"),patch.object(w,"run"),patch.object(w,"read_json",side_effect=[too_high,ev]):
            self.assertIn("above",w.quality_reason(NOW,ACTIVE))

    def test_rollback_restores_backup_and_disarms_before_service_restart(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)
            dispatch=folder/"dispatch_loop.py"
            backup=folder/"ev-grid-priority-dispatch-before.py"
            gate=folder/"ev-grid-priority-trial.json"
            status=folder/"ev-grid-priority-trial-status.json"
            original="def main() -> None:\\n    pass\\n"
            original=original.replace("\\n","\n")
            backup.write_text(original,encoding="utf-8")
            dispatch.write_text("from ev_grid_priority_guard import something\n"+original,
                                encoding="utf-8")
            gate.write_text(json.dumps(ACTIVE),encoding="utf-8")
            with patch.multiple(w,ROOT=folder,DISPATCH=dispatch,
                                BACKUP=backup,GATE=gate,STATUS=status),patch.object(w,"run") as run:
                w.restore("time_limit",NOW)
                self.assertFalse(gate.exists())
                self.assertTrue((folder/"ev-grid-priority-trial.finished.json").exists())
                self.assertEqual(dispatch.read_text(),original)
                self.assertEqual(json.loads(status.read_text())["state"],
                                 "restored_original_dispatch")
                self.assertEqual(run.call_count,3)
                self.assertEqual(run.call_args_list[0][0][0][-2:],
                                 ["stop",w.SERVICE])
                self.assertEqual(run.call_args_list[1][0][0][-2:],
                                 ["start",w.SERVICE])

    def test_failed_service_restore_disarms_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)
            dispatch=folder/"dispatch_loop.py"
            backup=folder/"ev-grid-priority-dispatch-before.py"
            gate=folder/"ev-grid-priority-trial.json"
            status=folder/"ev-grid-priority-trial-status.json"
            backup.write_text("def main():\n    pass\n",encoding="utf-8")
            dispatch.write_text("from ev_grid_priority_guard import test\ndef main():\n    pass\n",
                                encoding="utf-8")
            gate.write_text(json.dumps(ACTIVE),encoding="utf-8")
            with patch.multiple(w,ROOT=folder,DISPATCH=dispatch,
                                BACKUP=backup,GATE=gate,STATUS=status),patch.object(
                                    w,"run",side_effect=RuntimeError("systemctl failure")):
                w.restore("bad_source",NOW)
                self.assertFalse(gate.exists())
                saved=json.loads(status.read_text())
                self.assertEqual(saved["state"],"rollback_failed_dispatch_may_be_stopped")
                self.assertFalse(saved["rollback_ok"])

if __name__=="__main__":
    unittest.main()
