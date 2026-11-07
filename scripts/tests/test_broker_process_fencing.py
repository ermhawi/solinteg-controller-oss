"""S08 bounded OS-level synthetic broker takeover / loopback qualification.

Temporary 0440 lease, local AF_UNIX and 127.0.0.1 Modbus only. SIGKILL
targets exactly the child spawned by the test; no systemd or /opt involved.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "services")]

from fakes import install_pymodbus_stub
install_pymodbus_stub()
from modbus_broker import Broker  # noqa: E402
from modbus_broker_ipc import BrokerIpcClient, BrokerIpcServer  # noqa: E402
from modbus_broker_lease import LeaseBusy, broker_ownership_lease  # noqa: E402
from modbus_broker_supervisor import coordinated_emergency_auto  # noqa: E402


class BrokerProcessFencingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.lock = self.home / "ownership.lock"
        self.lock.touch(mode=0o440)
        self.lock.chmod(0o440)

    def acquire(self):
        return broker_ownership_lease(
            self.lock, owner_uid=os.geteuid(), owner_gid=os.getegid())

    def test_crashed_broker_releases_kernel_flock_but_never_implies_safe_auto(self):
        """The lease can be reacquired, but unknown device state is still NO-GO."""
        code = (
            "import os,sys,time;"
            "from pathlib import Path;"
            "from modbus_broker_lease import broker_ownership_lease;"
            "p=Path(sys.argv[1]);"
            "lease=broker_ownership_lease(p,owner_uid=os.geteuid(),owner_gid=os.getegid());"
            "lease.__enter__();print('LEASE_HELD',flush=True);time.sleep(45)"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(HERE.parent / "services")
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", code, str(self.lock)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "LEASE_HELD",
                             child.stderr.read() if child.poll() is not None else "")
            inode = self.lock.stat().st_ino
            with self.assertRaises(LeaseBusy):
                with self.acquire():
                    self.fail("second concurrent broker acquired original lease")
            child.kill()  # SIGKILL only the synthetic child spawned above
            child.wait(timeout=5)
            with self.acquire():
                self.assertEqual(self.lock.stat().st_ino, inode)
                result = coordinated_emergency_auto(
                    revoke_approval=lambda: True,
                    approval_absent=lambda: True,
                    stop_broker=lambda: True,
                    ownership_lease=self.acquire,
                    observe=lambda: self.fail("should not read physical device"),
                    emergency_auto=lambda: self.fail("unknown physical state may not write"),
                    alert=lambda text: None,
                )
                self.assertEqual(result.error, "independent_evidence_source_missing")
                self.assertFalse(result.emergency_attempted)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()

    def test_ipc_inode_and_role_prevent_competing_socket_owner(self):
        path = self.home / "broker.sock"
        broker = Broker(lambda: self.fail("no physical transport"),
                        armed=False, runtime_mode="offline")
        self.addCleanup(broker.close)
        server = BrokerIpcServer(
            path, broker,
            role_for_peer=lambda _pid, uid, _gid:
                "read" if uid == os.geteuid() else None,
        )
        server.start()
        self.addCleanup(server.close)
        self.assertTrue(path.is_socket())
        self.assertEqual(BrokerIpcClient(path).request("status")["armed"], False)
        with self.assertRaisesRegex(RuntimeError, "socket path already owned"):
            BrokerIpcServer(path, broker).start()
        initial_inode = path.stat().st_ino
        shifted = self.home / "previous-owner.sock"
        path.rename(shifted)
        foreign = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            foreign.bind(str(path))  # a new socket cannot inherit old ownership
            self.assertNotEqual(path.stat().st_ino, initial_inode)
            server.close()
            self.assertTrue(path.is_socket())  # broker may not unlink another owner's socket
            self.assertNotEqual(path.stat().st_ino, initial_inode)
        finally:
            foreign.close()
            path.unlink(missing_ok=True)
            shifted.unlink(missing_ok=True)

    def test_real_modbus_protocol_against_loopback_only(self):
        # No optional pymodbus dependency: the test fixture itself speaks
        # bounded Modbus TCP MBAP/FC03/FC06 on an ephemeral 127.0.0.1 port.
        from unittest import mock
        from modbus_loopback_wire import FakeModbusTcp
        from test_modbus_broker import FakeDongle
        import inverter_control as control

        dongle = FakeDongle()
        wire = FakeModbusTcp(dongle.regs)
        self.assertNotEqual(wire.port, 502)  # ephemeral loopback only
        broker = None
        server = None
        role = {"value": "control"}
        try:
            with mock.patch.object(control, "ARMED", True), \
                 mock.patch.object(control, "SIGN", "neg_charge"), \
                 mock.patch.object(control, "SLAVE_ID", 255), \
                 mock.patch.object(control.RateLimiter, "wait"), \
                 mock.patch.object(control, "SETTLE_S", 0):
                control._forced_active = False
                broker = Broker(
                    wire.connection, armed=True, runtime_mode="control",
                    transactions_enabled=True, charge_sign="neg_charge",
                    min_write_interval_s=0,
                )
                server = BrokerIpcServer(
                    self.home / "broker.sock", broker,
                    role_for_peer=lambda *_: role["value"],
                )
                server.start()
                ipc = BrokerIpcClient(self.home / "broker.sock")
                self.assertEqual(ipc.request("transaction_charge", watts=300,
                                             request_id="charge")["status"], "applied")
                self.assertEqual(dongle.regs[50000], 0x303)
                self.assertEqual(ipc.request("transaction_auto",
                                             request_id="auto")["status"], "applied")
                self.assertEqual(dongle.regs[50000], 0x101)
                self.assertEqual(dongle.regs[50207], 0)
                role["value"] = "read"
                self.assertEqual(ipc.request("read_registers", address=33000,
                                             count=4)["registers"][0], 5000)
                with self.assertRaisesRegex(RuntimeError, "PermissionError"):
                    ipc.request("transaction_charge", watts=300, request_id="denied")
                self.assertEqual(wire.clients, 1)
                self.assertEqual([a for a, _ in wire.writes],
                                 [50209, 50208, 50210, 50207, 50000,
                                  50208, 50209, 50000, 50207])
        finally:
            if server is not None:
                server.close()
            if broker is not None:
                broker.close()
            wire.close()
            control._forced_active = False


if __name__ == "__main__":
    unittest.main()
