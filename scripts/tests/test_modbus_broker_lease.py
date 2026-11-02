"""Kernel flock tests for broker restarts; all lease paths are temporary."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "services"))
from fakes import install_pymodbus_stub
install_pymodbus_stub()

from modbus_broker_lease import LEASE_PATH, LeaseBusy, LeaseUntrusted, broker_ownership_lease
from modbus_broker_readonly_service import main


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "ownership.lock"
        self.path.write_bytes(b"")
        self.path.chmod(0o440)
        self.owner = os.geteuid()
        self.group = os.getegid()

    def tearDown(self):
        self.tempdir.cleanup()

    def lease(self):
        return broker_ownership_lease(self.path, owner_uid=self.owner,
                                      owner_gid=self.group)

    def test_only_one_independent_open_file_description_can_hold_lock(self):
        with self.lease():
            with self.assertRaises(LeaseBusy), self.lease():
                self.fail("second exclusive lease was acquired")
        with self.lease():
            pass

    def test_other_process_is_blocked_until_original_releases(self):
        code = (
            "import fcntl,os,sys;"
            "fd=os.open(sys.argv[1],os.O_RDONLY);"
            "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)"
        )
        with self.lease():
            blocked = subprocess.run([sys.executable, "-c", code, str(self.path)],
                                     capture_output=True, timeout=5, check=False)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn(b"BlockingIOError", blocked.stderr)
        released = subprocess.run([sys.executable, "-c", code, str(self.path)],
                                  capture_output=True, timeout=5, check=False)
        self.assertEqual(released.returncode, 0, released.stderr)

    def test_exception_releases_lease(self):
        with self.assertRaisesRegex(RuntimeError, "fake"):
            with self.lease():
                raise RuntimeError("fake")
        with self.lease():
            pass

    def test_missing_lock_file_fails_not_creates(self):
        self.path.unlink()
        with self.assertRaises(FileNotFoundError), self.lease():
            pass
        self.assertFalse(self.path.exists())

    def test_untrusted_lock_file_or_directory_fails_closed(self):
        self.path.chmod(0o666)
        with self.assertRaises(LeaseUntrusted), self.lease():
            pass
        self.path.chmod(0o440)
        with self.assertRaises(LeaseUntrusted):
            with broker_ownership_lease(self.path, owner_uid=self.owner,
                                        owner_gid=self.group + 1):
                pass
        self.path.parent.chmod(0o777)
        try:
            with self.assertRaises(LeaseUntrusted), self.lease():
                pass
        finally:
            self.path.parent.chmod(0o700)

    def test_symlink_and_nonregular_path_are_rejected(self):
        target = self.path.parent / "other"
        target.write_bytes(b"")
        self.path.unlink()
        self.path.symlink_to(target)
        with self.assertRaises(OSError), self.lease():
            pass
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises((LeaseUntrusted, OSError)), self.lease():
            pass

    def test_staged_systemd_and_tmpfiles_require_same_stable_inode(self):
        deploy = HERE.parents[1] / "deploy"
        unit = (deploy / "solinteg-broker-readonly.service").read_text()
        tmpfiles = (deploy / "solinteg-broker-ownership.conf").read_text()
        self.assertIn(f"ConditionPathExists={LEASE_PATH}", unit)
        self.assertIn("Group=solinteg", unit)
        self.assertIn(f"f {LEASE_PATH} 0440 root solinteg -", tmpfiles)
        self.assertNotIn("RuntimeDirectory=solinteg-broker-ownership", unit)

    def test_relative_path_not_supported(self):
        with self.assertRaises(ValueError):
            with broker_ownership_lease(Path("ownership.lock"), owner_uid=self.owner,
                                        owner_gid=self.group):
                pass

    def test_readonly_service_exits_before_startup_when_fallback_owns_lease(self):
        env = {
            "SOLINTEG_HOST": "localhost-fake",
            "SOLINTEG_SLAVE_ID": "255",
            "SOLINTEG_BROKER_READONLY_ENABLED": "1",
        }
        with self.lease(), \
             mock.patch("modbus_broker_readonly_service.approved_cutover_marker",
                        return_value=True), \
             mock.patch("modbus_broker_readonly_service.broker_ownership_lease",
                        side_effect=self.lease), \
             mock.patch("modbus_broker_readonly_service.build_readonly_broker") as build:
            with self.assertRaises(LeaseBusy):
                main(["--read-only"], environ=env)
            build.assert_not_called()

    def test_revoked_approval_inside_lease_never_starts_broker(self):
        env = {
            "SOLINTEG_HOST": "localhost-fake",
            "SOLINTEG_SLAVE_ID": "255",
            "SOLINTEG_BROKER_READONLY_ENABLED": "1",
        }
        with mock.patch("modbus_broker_readonly_service.approved_cutover_marker",
                        side_effect=[True, False]), \
             mock.patch("modbus_broker_readonly_service.broker_ownership_lease",
                        side_effect=self.lease), \
             mock.patch("modbus_broker_readonly_service.build_readonly_broker") as build:
            with self.assertRaisesRegex(PermissionError, "revoked"):
                main(["--read-only"], environ=env)
            build.assert_not_called()
        with self.lease():
            pass

    def test_readonly_service_holds_lock_through_ipc_and_close(self):
        env = {
            "SOLINTEG_HOST": "localhost-fake",
            "SOLINTEG_SLAVE_ID": "255",
            "SOLINTEG_BROKER_READONLY_ENABLED": "1",
        }
        observed = []
        def assert_lock(stage):
            with self.assertRaises(LeaseBusy), self.lease():
                pass
            observed.append(stage)

        class FakeBroker:
            def close(self):
                assert_lock("broker.close")

        class FakeServer:
            def __init__(self, socket_path, broker):
                self.broker = broker

            def start(self):
                assert_lock("server.start")

            def close(self):
                assert_lock("server.close")

        class ImmediateEvent:
            def wait(self):
                assert_lock("wait")

            def set(self):
                pass

        with mock.patch("modbus_broker_readonly_service.approved_cutover_marker",
                        return_value=True), \
             mock.patch("modbus_broker_readonly_service.broker_ownership_lease",
                        side_effect=self.lease), \
             mock.patch("modbus_broker_readonly_service.build_readonly_broker",
                        return_value=FakeBroker()), \
             mock.patch("modbus_broker_readonly_service.BrokerIpcServer", FakeServer), \
             mock.patch("modbus_broker_readonly_service.threading.Event",
                        return_value=ImmediateEvent()), \
             mock.patch("modbus_broker_readonly_service.signal.signal"):
            main(["--read-only"], environ=env)
        self.assertEqual(observed,
                         ["server.start", "wait", "server.close", "broker.close"])
        with self.lease():
            pass


if __name__ == "__main__":
    unittest.main()
