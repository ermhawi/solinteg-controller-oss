"""Mock-only tests for permanent root-owned EV-grid watchdog and rollback."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_grid_priority_guard as guard
import ev_grid_priority_watchdog as w

NOW = datetime(2026,10,29,18,0,tzinfo=timezone.utc)
LIVE = {
    "timestamp":NOW.isoformat(),"grid_unbalanced_output_raw":0,
    "soc_pct":64,"battery_temp_c":15,"pv_w":0,
    "meter_l1_w":269,"meter_l2_w":362,"meter_l3_w":-792,
    "grid_w":-160,"inverter_ac_w":2199,"house_load_w":2359,
    "battery_w":225,"work_mode":"EMS BattCtrl",
}
EVCC = {
    "timestamp":NOW.isoformat(),"source":"waveshare_evcc_fc03_readonly",
    "read_ok":True,"state":"C2","charging_detected":True,
    "physical_control_eligible":False,"writes":0,
    "pilot_pwm_raw":100,"configured_pwm_raw":100,
    "channel_currents_a":[.1,6.0,.1],"measured_current_a":6.2,
}


class PermanentWatchdogTests(unittest.TestCase):
    def fixture(self, folder):
        paths={key:folder / name for key,name in {
            "GATE":"gate.json","STATUS":"status.json","BACKUP":"dispatch-before.py",
            "DISPATCH":"dispatch_loop.py","LIVE":"live.json","EVCC":"evcc.json",
            "LOCK":"watchdog.lock","POLICY_TRUSTED":"trusted-policy.py",
            "POLICY_DEPLOYED":"deployed-policy.py"}.items()}
        original="def main():\n    pass\n"
        installed="from ev_grid_priority_guard import guard_dispatch\n"+original
        paths["BACKUP"].write_text(original,encoding="utf-8")
        paths["DISPATCH"].write_text(installed,encoding="utf-8")
        gate={"schema_version":2,"policy_id":guard.PRODUCTION_POLICY_ID,
              "mode":"active","activation_id":"rollout-20261029-safe",
              "activated_utc":(NOW-timedelta(minutes=5)).isoformat(),
              "dispatcher_sha256":hashlib.sha256(installed.encode()).hexdigest()}
        paths["GATE"].write_text(json.dumps(gate),encoding="utf-8")
        paths["LIVE"].write_text(json.dumps(LIVE),encoding="utf-8")
        paths["EVCC"].write_text(json.dumps(EVCC),encoding="utf-8")
        paths["POLICY_TRUSTED"].write_text("trusted policy",encoding="utf-8")
        paths["POLICY_DEPLOYED"].write_text("trusted policy",encoding="utf-8")
        return paths,gate,original,installed

    def setup_watchdog(self, paths, folder):
        return patch.multiple(w, ROOT=folder, **paths)

    def test_not_armed_does_not_touch_service(self):
        with patch.object(w,"read_json",side_effect=RuntimeError("missing")),patch.object(
                w,"run") as run:
            self.assertEqual(w.monitor(NOW),"not_armed")
            run.assert_not_called()

    def test_shadow_monitors_integrity_without_requiring_night_conditions(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)
            paths,gate,_,_=self.fixture(folder)
            gate["mode"]="shadow"
            paths["GATE"].write_text(json.dumps(gate),encoding="utf-8")
            paths["EVCC"].unlink()
            paths["LIVE"].unlink()
            with self.setup_watchdog(paths,folder),patch.object(
                    guard,"_trusted_file",side_effect=lambda p:p.is_file()),patch.object(
                    w,"run") as run,patch.object(w,"rollback") as rollback:
                self.assertEqual(w.monitor(NOW),"shadow_monitor_ok")
                status=json.loads(paths["STATUS"].read_text())
                self.assertEqual(status["state"],"shadow_monitor_ok")
                self.assertEqual(status["activation_id"],gate["activation_id"])
                run.assert_called_once_with(
                    ["/usr/bin/systemctl","is-active","--quiet",w.SERVICE],timeout=8)
                rollback.assert_not_called()

    def test_shadow_fails_closed_on_dispatch_or_service_failure(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)
            paths,gate,_,_=self.fixture(folder)
            gate["mode"]="shadow"
            paths["GATE"].write_text(json.dumps(gate),encoding="utf-8")
            with self.setup_watchdog(paths,folder),patch.object(
                    guard,"_trusted_file",side_effect=lambda p:p.is_file()),patch.object(
                    w,"rollback") as rollback:
                paths["DISPATCH"].write_text("unexpected change",encoding="utf-8")
                self.assertEqual(w.monitor(NOW),"rolled_back")
                self.assertIn("hash",rollback.call_args[0][0])
                rollback.reset_mock()
                paths["DISPATCH"].write_text("from ev_grid_priority_guard import guard_dispatch\ndef main():\n    pass\n",
                                              encoding="utf-8")
                with patch.object(w,"run",side_effect=RuntimeError("dispatch down")):
                    self.assertEqual(w.monitor(NOW),"rolled_back")
                self.assertIn("dispatch down",rollback.call_args[0][0])

    def test_healthy_monitor_creates_matching_fresh_heartbeat(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)
            paths,gate,_,_=self.fixture(folder)
            with self.setup_watchdog(paths,folder),patch.object(
                    guard,"_trusted_file",side_effect=lambda p:p.is_file()),patch.object(w,"run"):
                self.assertEqual(w.monitor(NOW),"active_monitor_ok")
                heartbeat=json.loads(paths["STATUS"].read_text())
                self.assertEqual(heartbeat["activation_id"],gate["activation_id"])
                self.assertEqual(heartbeat["recommended_w"],50)
                self.assertEqual(heartbeat["state"],"active_monitor_ok")

    def test_explicit_idle_ev_allows_normal_dispatch_and_resets_heartbeat(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)
            paths,_,_,_=self.fixture(folder)
            ev={**EVCC,"state":"B2","charging_detected":False,
                "channel_currents_a":[0,0,0],"measured_current_a":0}
            paths["EVCC"].write_text(json.dumps(ev))
            with self.setup_watchdog(paths,folder),patch.object(
                    guard,"_trusted_file",side_effect=lambda p:p.is_file()),patch.object(
                    w,"run"),patch.object(w,"rollback") as rollback:
                self.assertEqual(w.monitor(NOW),"idle_ev_not_charging")
                self.assertEqual(json.loads(paths["STATUS"].read_text())["state"],
                                 "idle_ev_not_charging")
                rollback.assert_not_called()

    def test_c2_with_zero_current_is_a_safe_noop_not_permanent_rollback(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)
            paths,_,_,_=self.fixture(folder)
            ev={**EVCC,"channel_currents_a":[0,0,0],"measured_current_a":0}
            paths["EVCC"].write_text(json.dumps(ev))
            with self.setup_watchdog(paths,folder),patch.object(
                    guard,"_trusted_file",side_effect=lambda p:p.is_file()),patch.object(
                    w,"run"),patch.object(w,"rollback") as rollback:
                self.assertEqual(w.monitor(NOW),"idle_ev_not_charging")
                rollback.assert_not_called()

    def test_pv_or_low_soc_standby_does_not_force_battery(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)
            paths,_,_,_=self.fixture(folder)
            with self.setup_watchdog(paths,folder),patch.object(
                    guard,"_trusted_file",side_effect=lambda p:p.is_file()),patch.object(
                    w,"run"),patch.object(w,"rollback") as rollback:
                for changed in ({"pv_w":350},{"soc_pct":20}):
                    with self.subTest(changed=changed):
                        paths["LIVE"].write_text(json.dumps({**LIVE,**changed}))
                        self.assertEqual(w.monitor(NOW),"standby_pv_or_soc")
                        rollback.assert_not_called()

    def test_unsafe_sensors_and_communication_disarm(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)
            paths,_,_,_=self.fixture(folder)
            with self.setup_watchdog(paths,folder),patch.object(
                    guard,"_trusted_file",side_effect=lambda p:p.is_file()),patch.object(
                    w,"run"),patch.object(w,"rollback") as rollback:
                for field,data in (("evcc", {**EVCC,"timestamp":
                                      (NOW-timedelta(seconds=120)).isoformat()}),
                                   ("bms", {**LIVE,"battery_temp_c":50}),
                                   ("phase", {**LIVE,"grid_unbalanced_output_raw":1})):
                    with self.subTest(field=field):
                        paths["EVCC"].write_text(json.dumps(EVCC if field!="evcc" else data))
                        paths["LIVE"].write_text(json.dumps(LIVE if field=="evcc" else data))
                        self.assertEqual(w.monitor(NOW),"rolled_back")
                self.assertEqual(rollback.call_count,3)

    def test_excess_discharge_rollback_after_session_grace(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)
            paths,gate,_,_=self.fixture(folder)
            paths["LIVE"].write_text(json.dumps({**LIVE,"battery_w":2500}))
            paths["STATUS"].write_text(json.dumps({
                "state":"active_monitor_ok","activation_id":gate["activation_id"],
                "eligible_since_utc":(NOW-timedelta(seconds=130)).isoformat()}))
            with self.setup_watchdog(paths,folder),patch.object(
                    guard,"_trusted_file",side_effect=lambda p:p.is_file()),patch.object(
                    w,"run"),patch.object(w,"rollback") as rollback:
                self.assertEqual(w.monitor(NOW),"rolled_back")
                self.assertIn("discharge",rollback.call_args[0][0])

    def test_real_rollback_disarms_and_restores_byte_exact_code(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)
            paths,gate,original,_=self.fixture(folder)
            with self.setup_watchdog(paths,folder),patch.object(
                    guard,"_trusted_file",side_effect=lambda p:p.is_file()),patch.object(
                    w,"run") as run:
                w.rollback("test deadline",NOW,gate)
                self.assertFalse(paths["GATE"].exists())
                self.assertTrue((folder/"gate.disabled.json").exists())
                self.assertEqual(paths["DISPATCH"].read_text(),original)
                self.assertEqual(json.loads(paths["STATUS"].read_text())["state"],
                                 "restored_previous_dispatch")
                self.assertEqual(run.call_count,3)
                self.assertEqual(run.call_args_list[0][0][0][-2:],
                                 ["stop",w.SERVICE])

    def test_changed_deployed_code_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)
            paths,gate,_,_=self.fixture(folder)
            changed="def main():\n    print('new deploy')\n"
            paths["DISPATCH"].write_text(changed)
            with self.setup_watchdog(paths,folder),patch.object(
                    guard,"_trusted_file",side_effect=lambda p:p.is_file()),patch.object(
                    w,"run") as run:
                w.rollback("hash mismatch",NOW,gate)
                self.assertFalse(paths["GATE"].exists())
                self.assertEqual(paths["DISPATCH"].read_text(),changed)
                self.assertEqual(json.loads(paths["STATUS"].read_text())["state"],
                                 "rollback_failed_manual_intervention")
                run.assert_not_called()

    def test_mismatched_root_policy_forces_rollback(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)
            paths,_,_,_=self.fixture(folder)
            paths["POLICY_DEPLOYED"].write_text("unreviewed code",encoding="utf-8")
            with self.setup_watchdog(paths,folder),patch.object(
                    guard,"_trusted_file",side_effect=lambda p:p.is_file()),patch.object(
                    w,"run"),patch.object(w,"rollback") as rollback:
                self.assertEqual(w.monitor(NOW),"rolled_back")
                self.assertIn("policy",rollback.call_args[0][0])

    def test_watchdog_never_has_direct_device_client(self):
        source=Path(w.__file__).read_text().lower()
        for forbidden in ("import pymodbus", "socket.connect", "requests.post",
                          "write_register", "evcc_ascii", "fc10"):
            self.assertNotIn(forbidden,source)


if __name__ == "__main__":
    unittest.main()
