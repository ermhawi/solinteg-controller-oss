"""Offline supervisor handover tests: real temporary flock, fake host/Modbus.

No live systemctl, network operations, approval marker or real Modbus access.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from modbus_broker_fencing import FenceObservation
from modbus_broker_lease import LeaseBusy, broker_ownership_lease
from modbus_broker_supervisor import coordinated_emergency_auto


def released() -> FenceObservation:
    return FenceObservation(False, 0, 0, False, 1.0)


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ownership.lock"
        self.path.touch()
        self.path.chmod(0o440)
        self.events = []
        self.alerts = []
        self.approved = True
        self.writes = []

    def tearDown(self):
        self.temp.cleanup()

    @contextmanager
    def lease(self):
        self.events.append("lease_acquire")
        with broker_ownership_lease(self.path, owner_uid=os.geteuid(),
                                    owner_gid=os.getegid()):
            self.events.append("lease_held")
            try:
                yield
            finally:
                self.events.append("lease_releasing")
        self.events.append("lease_released")

    def assert_held(self):
        with self.assertRaises(LeaseBusy):
            with self.lease():
                self.fail("other broker acquired fallback's live lease")

    def revoke(self):
        self.events.append("revoke")
        self.approved = False
        return True

    def absent(self):
        self.events.append("approval_absent")
        return not self.approved

    def stop(self):
        self.events.append("stop")
        return True

    def observe(self):
        self.events.append("observe")
        self.assert_held()
        return released()

    def emergency(self):
        self.events.append("emergency_auto")
        self.assert_held()
        self.writes.append("fake_auto")
        return True

    def run_takeover(self, **overrides):
        funcs = dict(
            revoke_approval=self.revoke,
            approval_absent=self.absent,
            stop_broker=self.stop,
            ownership_lease=self.lease,
            observe=self.observe,
            emergency_auto=self.emergency,
            alert=self.alerts.append,
            monotonic=lambda: 0.0,
        )
        funcs.update(overrides)
        return coordinated_emergency_auto(**funcs)

    def test_order_lock_held_through_physical_readback_and_released(self):
        result = self.run_takeover()
        self.assertTrue(result.auto_readback_verified)
        self.assertEqual(self.writes, ["fake_auto"])
        self.assertFalse(self.approved)  # NEVER automatically re-approve broker
        self.assertLess(self.events.index("revoke"), self.events.index("stop"))
        self.assertLess(self.events.index("stop"), self.events.index("lease_held"))
        self.assertLess(self.events.index("lease_held"), self.events.index("emergency_auto"))
        self.assertLess(self.events.index("emergency_auto"), self.events.index("lease_released"))
        self.assertEqual(self.events.count("observe"), 2)
        with self.lease():  # lease freed only after fake Auto/readback
            pass

    def test_failed_revoke_blocks_stopping_or_writing(self):
        result = self.run_takeover(revoke_approval=lambda: False)
        self.assertEqual(result.error, "approval_not_revoked")
        self.assertEqual(self.writes, [])
        self.assertNotIn("stop", self.events)
        self.assertNotIn("lease_acquire", self.events)
        self.assertTrue(self.alerts)

    def test_false_revoke_success_without_absence_never_stops(self):
        result = self.run_takeover(revoke_approval=lambda: True)
        self.assertEqual(result.error, "approval_still_present")
        self.assertEqual(self.writes, [])
        self.assertNotIn("stop", self.events)

    def test_revoke_exception_blocks_stop_and_write(self):
        def broken_revoke():
            raise PermissionError("root required")
        result = self.run_takeover(revoke_approval=broken_revoke)
        self.assertEqual(result.error, "PermissionError")
        self.assertFalse(result.emergency_attempted)

    def test_stop_failure_keeps_approval_revoked_and_no_lease(self):
        result = self.run_takeover(stop_broker=lambda: False)
        self.assertEqual(result.error, "broker_stop_unconfirmed")
        self.assertFalse(self.approved)
        self.assertNotIn("lease_acquire", self.events)
        self.assertEqual(self.writes, [])

    def test_stop_exception_fail_closed(self):
        def broken_stop():
            raise TimeoutError("supervisor stop timeout")
        result = self.run_takeover(stop_broker=broken_stop)
        self.assertEqual(result.error, "TimeoutError")
        self.assertFalse(result.emergency_attempted)
        self.assertEqual(self.writes, [])

    def test_fallback_refuses_when_broker_still_holds_same_lease(self):
        # Mimic a broker that hasn't released its TCP ownership lock.
        with self.lease():
            result = self.run_takeover()
        self.assertEqual(result.error, "LeaseBusy")
        self.assertFalse(result.emergency_attempted)
        self.assertEqual(self.writes, [])
        self.assertFalse(self.approved)

    def test_marker_restored_after_stop_before_lease_blocks(self):
        def restore_after_stop():
            self.events.append("stop")
            self.approved = True
            return True
        result = self.run_takeover(stop_broker=restore_after_stop)
        self.assertEqual(result.error, "approval_restored_before_observation")
        self.assertEqual(self.writes, [])
        self.assertEqual(self.events.count("observe"), 0)

    def test_marker_restored_between_two_independent_observations_blocks(self):
        def observer():
            self.assert_held()
            self.events.append("observe")
            if self.events.count("observe") == 1:
                self.approved = True
            return released()
        result = self.run_takeover(observe=observer)
        self.assertFalse(result.emergency_attempted)
        self.assertEqual(result.error, "FenceFailure")
        self.assertEqual(self.writes, [])

    def test_marker_restored_after_second_observation_before_write_blocks(self):
        def absent():
            self.events.append("approval_absent")
            if self.events.count("approval_absent") == 5:
                self.approved = True
            return not self.approved
        result = self.run_takeover(approval_absent=absent)
        self.assertEqual(result.error, "write_guard_denied")
        self.assertFalse(result.emergency_attempted)
        self.assertEqual(self.writes, [])

    def test_competing_modbus_connection_reported_by_os_blocks_write(self):
        result = self.run_takeover(
            observe=lambda: FenceObservation(False, 0, 1, False, 1.0))
        self.assertFalse(result.emergency_attempted)
        self.assertFalse(result.released)
        self.assertEqual(self.writes, [])

    def test_unknown_observer_exception_fails_closed(self):
        def fail_observe():
            raise RuntimeError("ss unavailable")
        result = self.run_takeover(observe=fail_observe)
        self.assertFalse(result.emergency_attempted)
        self.assertEqual(result.error, "RuntimeError")
        self.assertEqual(self.writes, [])

    def test_real_other_process_cannot_restart_under_fallback_lock(self):
        code = (
            "import fcntl,os,sys;"
            "fd=os.open(sys.argv[1],os.O_RDONLY);"
            "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)"
        )

        def emergency():
            other = subprocess.run(
                [sys.executable, "-c", code, str(self.path)],
                capture_output=True, timeout=5, check=False,
            )
            self.assertNotEqual(other.returncode, 0)
            self.assertIn(b"BlockingIOError", other.stderr)
            return self.emergency()

        result = self.run_takeover(emergency_auto=emergency)
        self.assertTrue(result.auto_readback_verified)
        self.assertEqual(self.writes, ["fake_auto"])

    def test_auto_without_readback_is_failure_never_retried(self):
        def unverified():
            self.writes.append("fake_unverified")
            return False
        result = self.run_takeover(emergency_auto=unverified)
        self.assertTrue(result.emergency_attempted)
        self.assertFalse(result.auto_readback_verified)
        self.assertEqual(result.error, "auto_unverified")
        self.assertEqual(self.writes, ["fake_unverified"])

    def test_deadline_after_slow_stop_blocks_lease_and_writes(self):
        times = iter([0, 0, 11])
        result = self.run_takeover(
            monotonic=lambda: next(times), deadline_s=10)
        self.assertEqual(result.error, "deadline_exceeded")
        self.assertFalse(result.emergency_attempted)
        self.assertEqual(self.writes, [])
        self.assertFalse(self.approved)

    def test_alert_failure_never_authorizes_a_modbus_write(self):
        def failed_alert(_):
            raise RuntimeError("no alert transport")
        result = self.run_takeover(
            revoke_approval=lambda: False, alert=failed_alert)
        self.assertFalse(result.emergency_attempted)
        self.assertEqual(self.writes, [])

    def test_releasing_lease_with_error_does_not_hide_attempt(self):
        @contextmanager
        def broken_exit():
            with self.lease():
                yield
            raise RuntimeError("lease release error")
        result = self.run_takeover(ownership_lease=broken_exit)
        self.assertEqual(result.error, "RuntimeError")
        self.assertTrue(result.emergency_attempted)
        self.assertTrue(result.auto_readback_verified)

    def test_never_installed_as_an_executable_fallback(self):
        source = (Path(__file__).resolve().parents[1] /
                  "services/modbus_broker_supervisor.py").read_text()
        self.assertNotIn("subprocess.run", source)
        self.assertNotIn("ModbusTcpClient(", source)
        self.assertNotIn("os.unlink(", source)
        self.assertNotIn("systemctl stop", source)


if __name__ == "__main__":
    unittest.main()
