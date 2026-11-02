"""Full offline fallback integration: root callbacks + OS observer + kernel flock.

All systemctl/ss replies, approval files, and the Auto readback are synthetic.
The only real cross-process operation is flock on a TemporaryDirectory file.
NEVER touches the installed broker, physical Modbus TCP or the live watchdog.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from modbus_broker_host_observer import observe_host_release
from modbus_broker_lease import LeaseBusy, broker_ownership_lease
from modbus_broker_root_adapter import BROKER_UNIT, BROKER_UNIT_FILE, RootBrokerStopAdapter
from modbus_broker_supervisor import coordinated_emergency_auto

INVERTER = "192.0.2.25"  # RFC 5737 example, NEVER a physical device


class FakeHost:
    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.socket_path.touch()  # Simulated IPC pathname, not a real socket
        self.properties = {
            "LoadState": "loaded", "ActiveState": "active",
            "MainPID": "4321", "ControlPID": "0",
            "KillMode": "control-group", "PrivateNetwork": "no",
            "NetworkNamespacePath": "", "User": "solinteg", "Group": "solinteg",
            "FragmentPath": BROKER_UNIT_FILE,
        }
        self.connected = True
        self.other_peer_state = None
        self.keep_socket = False
        self.keep_running = False
        self.stop_error = False
        self.ss_error = False
        self.restore_marker_on_ss = None
        self.spawn_peer_on_ss = None
        self.marker = None
        self.calls = []
        self.ss_calls = 0

    def __call__(self, command, **opts):
        self.calls.append(list(command))
        if opts != {"capture_output": True, "text": True,
                    "timeout": opts["timeout"], "check": False}:
            raise AssertionError("unexpected subprocess options")
        if command[0] == "systemctl":
            if command[-1] != BROKER_UNIT or not 0 < opts["timeout"] <= 8:
                raise AssertionError("invalid or unauthorized systemd command")
            if "show" in command:
                return SimpleNamespace(
                    returncode=0,
                    stdout="\n".join(f"{k}={v}" for k, v in self.properties.items()),
                )
            if "stop" in command:
                if self.stop_error:
                    return SimpleNamespace(returncode=1, stdout="")
                if not self.keep_running:
                    self.properties.update(ActiveState="inactive", MainPID="0",
                                           ControlPID="0")
                    self.connected = False
                    if not self.keep_socket:
                        self.socket_path.unlink()
                return SimpleNamespace(returncode=0, stdout="")
            raise AssertionError("only show and stop of fixed broker allowed")
        if command != ["ss", "--no-header", "--numeric", "--tcp", "--all"]:
            raise AssertionError("only read-only numeric socket enumeration allowed")
        self.ss_calls += 1
        if self.spawn_peer_on_ss == self.ss_calls:
            self.other_peer_state = "SYN-SENT"
        if self.ss_error:
            return SimpleNamespace(returncode=1, stdout="")
        rows = []
        if self.connected:
            rows.append(f"ESTAB 0 0 192.0.2.10:43000 {INVERTER}:502")
        if self.other_peer_state:
            rows.append(
                f"{self.other_peer_state} 0 0 192.0.2.10:44000 {INVERTER}:502"
            )
        if self.restore_marker_on_ss == self.ss_calls:
            self.marker.touch(mode=0o600)
            self.marker.chmod(0o600)
        return SimpleNamespace(returncode=0, stdout="\n".join(rows))


class CompleteHostTakeoverTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.marker = self.base / "approved"
        self.marker.touch(mode=0o600)
        self.marker.chmod(0o600)
        self.ipc = self.base / "broker.sock"
        self.lock_path = self.base / "ownership.lock"
        self.lock_path.touch()
        self.lock_path.chmod(0o440)
        self.host = FakeHost(self.ipc)
        self.host.marker = self.marker
        self.root = RootBrokerStopAdapter(
            marker_path=self.marker, run=self.host,
            geteuid=lambda: 0, owner_uid=os.geteuid(),
        )
        self.events = []
        self.writes = []
        self.alerts = []

    def tearDown(self):
        self.temp.cleanup()

    def lock(self):
        return broker_ownership_lease(
            self.lock_path, owner_uid=os.geteuid(), owner_gid=os.getegid()
        )

    def observe(self):
        self.events.append("observe")
        with self.assertRaises(LeaseBusy):
            with self.lock():
                self.fail("host observer did not hold the exclusive lease")
        return observe_host_release(
            unit=BROKER_UNIT, inverter_ip=INVERTER, socket_path=self.ipc,
            port=502, run=self.host, geteuid=lambda: 0,
        )

    def fake_auto(self):
        self.events.append("auto")
        self.assertFalse(self.marker.exists())
        with self.assertRaises(LeaseBusy):
            with self.lock():
                self.fail("concurrent broker acquired the lease during Auto")
        self.writes.append("fake_auto_readback")
        return True

    def takeover(self, **overrides):
        callbacks = {
            "revoke_approval": self.root.revoke_approval,
            "approval_absent": self.root.approval_absent,
            "stop_broker": self.root.stop_broker,
            "ownership_lease": self.lock,
            "observe": self.observe,
            "emergency_auto": self.fake_auto,
            "alert": self.alerts.append,
            "monotonic": lambda: 0.0,
        }
        callbacks.update(overrides)
        return coordinated_emergency_auto(**callbacks)

    def test_full_takeover_sequencing_and_no_reapproval(self):
        result = self.takeover()
        self.assertTrue(result.auto_readback_verified)
        self.assertEqual(self.writes, ["fake_auto_readback"])
        self.assertEqual(self.events, ["observe", "observe", "auto"])
        self.assertEqual(self.host.ss_calls, 2)
        commands = self.host.calls
        stop_index = next(i for i, cmd in enumerate(commands) if "stop" in cmd)
        first_ss = next(i for i, cmd in enumerate(commands) if cmd[0] == "ss")
        self.assertLess(stop_index, first_ss)
        self.assertEqual(sum("stop" in cmd for cmd in commands), 1)
        self.assertTrue(self.root.approval_absent())
        self.assertFalse(self.ipc.exists())
        self.assertEqual(self.host.properties["ActiveState"], "inactive")
        with self.lock():  # Lock is released only after the fake readback
            pass

    def test_other_process_restart_blocked_during_fake_auto(self):
        probe = (
            "import fcntl,os,sys;"
            "fd=os.open(sys.argv[1],os.O_RDONLY);"
            "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)"
        )
        def emergency():
            blocked = subprocess.run(
                [sys.executable, "-c", probe, str(self.lock_path)],
                capture_output=True, timeout=5, check=False,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn(b"BlockingIOError", blocked.stderr)
            return self.fake_auto()
        self.assertTrue(self.takeover(emergency_auto=emergency).auto_readback_verified)

    def test_competing_external_tcp_owner_blocks_auto(self):
        self.host.other_peer_state = "ESTAB"
        result = self.takeover()
        self.assertFalse(result.emergency_attempted)
        self.assertEqual(result.error, "FenceFailure")
        self.assertEqual(self.writes, [])
        self.assertTrue(self.root.approval_absent())

    def test_competing_tcp_owner_appears_between_two_os_checks(self):
        self.host.spawn_peer_on_ss = 2
        result = self.takeover()
        self.assertEqual(result.error, "FenceFailure")
        self.assertEqual(self.host.ss_calls, 2)
        self.assertEqual(self.writes, [])

    def test_time_wait_to_inverter_also_blocks_auto(self):
        self.host.other_peer_state = "TIME-WAIT"
        self.assertFalse(self.takeover().emergency_attempted)
        self.assertEqual(self.writes, [])

    def test_leftover_ipc_path_blocks_auto(self):
        self.host.keep_socket = True
        result = self.takeover()
        self.assertEqual(result.error, "FenceFailure")
        self.assertTrue(self.ipc.exists())
        self.assertEqual(self.writes, [])

    def test_ss_unavailable_blocks_auto(self):
        self.host.ss_error = True
        result = self.takeover()
        self.assertEqual(result.error, "FenceFailure")
        self.assertEqual(self.writes, [])

    def test_stop_failure_never_acquires_ownership_or_sends_auto(self):
        self.host.stop_error = True
        result = self.takeover()
        self.assertEqual(result.error, "broker_stop_unconfirmed")
        self.assertEqual(self.host.ss_calls, 0)
        self.assertEqual(self.writes, [])
        self.assertTrue(self.root.approval_absent())

    def test_stop_reports_success_but_pid_active_blocks_auto(self):
        self.host.keep_running = True
        result = self.takeover()
        self.assertEqual(result.error, "broker_stop_unconfirmed")
        self.assertEqual(self.writes, [])
        self.assertEqual(self.host.ss_calls, 0)

    def test_untrusted_service_identity_prevents_stop(self):
        self.host.properties["FragmentPath"] = "/etc/systemd/system/other.service"
        result = self.takeover()
        self.assertEqual(result.error, "broker_stop_unconfirmed")
        self.assertEqual(self.writes, [])
        self.assertFalse(any("stop" in cmd for cmd in self.host.calls))

    def test_approval_wrong_mode_blocks_stop_and_preserves_file(self):
        self.marker.chmod(0o644)
        result = self.takeover()
        self.assertEqual(result.error, "approval_not_revoked")
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.writes, [])

    def test_broker_still_holds_kernel_lock_blocks_auto(self):
        with self.lock():
            result = self.takeover()
        self.assertEqual(result.error, "LeaseBusy")
        self.assertFalse(result.emergency_attempted)
        self.assertEqual(self.writes, [])
        self.assertTrue(self.root.approval_absent())

    def test_marker_restored_after_second_socket_check_blocks_auto(self):
        # The OS observer completed a clean read, but a privileged actor
        # improperly recreated approval right before the Auto callback.
        self.host.restore_marker_on_ss = 2
        result = self.takeover()
        self.assertEqual(result.error, "write_guard_denied")
        self.assertEqual(self.host.ss_calls, 2)
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.writes, [])

    def test_unverified_auto_readback_is_not_retried(self):
        def no_readback():
            self.writes.append("attempted_fake_auto")
            return False
        result = self.takeover(emergency_auto=no_readback)
        self.assertEqual(result.error, "auto_unverified")
        self.assertTrue(result.emergency_attempted)
        self.assertFalse(result.auto_readback_verified)
        self.assertEqual(self.writes, ["attempted_fake_auto"])

    def test_uncertain_root_or_host_identity_denies_takeover(self):
        denied = RootBrokerStopAdapter(
            marker_path=self.marker, run=self.host,
            geteuid=lambda: 1000, owner_uid=os.geteuid(),
        )
        result = self.takeover(revoke_approval=denied.revoke_approval)
        self.assertEqual(result.error, "approval_not_revoked")
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.writes, [])


if __name__ == "__main__":
    unittest.main()
