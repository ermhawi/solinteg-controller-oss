"""Stage 3 readiness tests: real-transport API, only fake I/O (NEVER live TCP)."""
from __future__ import annotations

import copy
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
SERVICES = HERE.parent / "services"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SERVICES))

from fakes import FakeModbusClient, FakeModbusResult, install_pymodbus_stub
install_pymodbus_stub()

import modbus_poller as poller
from modbus_broker import Broker
from modbus_broker_ipc import BrokerIpcClient, BrokerIpcServer
from modbus_broker_readonly_service import approved_cutover_marker, build_readonly_broker, main
from modbus_broker_readonly_transport import ReadOnlyModbusTransport
from test_modbus_broker import FakeDongle, FakeClock, s32_regs


class TransportReadOnlyTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.device = FakeDongle(self.clock)
        self.sessions = []
        self.parameters = []

        def factory(host, *, port, timeout):
            self.parameters.append((host, port, timeout))
            conn = self.device.connection()
            self.sessions.append(conn)
            return conn

        self.factory = factory

    def make(self, **kwargs):
        return ReadOnlyModbusTransport(
            "example-inverter", port=502, timeout=10, client_factory=self.factory,
            **kwargs,
        )

    def test_construct_does_not_connect(self):
        transport = self.make()
        self.assertEqual(len(self.sessions), 1)
        self.assertFalse(transport.connected)
        self.assertEqual(self.device.active, 0)
        self.assertEqual(self.parameters, [("example-inverter", 502, 10)])
        transport.close()

    def test_only_allowlisted_fc03_to_slave_255(self):
        transport = self.make()
        try:
            self.assertTrue(transport.connect())
            expected = {(50000, 1), (10994, 36), (11000, 30),
                        (30258, 2), (33000, 4), (50006, 1)}
            for addr, count in expected:
                registers = transport.read_holding_registers(
                    addr, count=count, device_id=255).registers
                self.assertEqual(len(registers), count)
            self.assertEqual(set(self.device.reads), expected)
            with self.assertRaises(PermissionError):
                transport.read_holding_registers(50207, count=1, device_id=255)
            with self.assertRaises(PermissionError):
                transport.read_holding_registers(50000, count=2, device_id=255)
            with self.assertRaises(PermissionError):
                transport.read_holding_registers(50000, count=1, device_id=1)
            with self.assertRaises(PermissionError):
                transport.read_holding_registers(50000, count=True, device_id=255)
            self.assertEqual(len(self.device.reads), len(expected))
        finally:
            transport.close()
        self.assertEqual(self.device.active, 0)

    def test_fc06_hard_refused_even_for_armed_parent(self):
        transport = self.make()
        try:
            self.assertTrue(transport.connect())
            with self.assertRaises(PermissionError):
                transport.write_register(50000, 0x303, device_id=255)
            self.assertEqual(self.device.writes, [])
        finally:
            transport.close()

    def test_disconnect_closes_transport_and_next_factory_can_recover(self):
        transport = self.make()
        self.assertTrue(transport.connect())
        self.sessions[-1].close()
        self.assertFalse(transport.connected)
        self.assertTrue(transport.connect())
        transport.close()
        self.assertEqual(self.device.active, 0)

    def test_failed_read_converts_exception_and_closes_session(self):
        class BrokenFake:
            connected = True

            def connect(self):
                return True

            def read_holding_registers(self, *args, **kwargs):
                raise RuntimeError("simulated pymodbus connection error")

            def close(self):
                self.connected = False

        transport = ReadOnlyModbusTransport("fake", client_factory=lambda *a, **k: BrokenFake())
        try:
            self.assertTrue(transport.connect())
            with self.assertRaises(ConnectionError):
                transport.read_holding_registers(50000, 1, device_id=255)
            self.assertFalse(transport.connected)
        finally:
            transport.close()

    def test_validation_fails_before_factory(self):
        for opts in ({"host": ""}, {"host": "  "}, {"port": 0},
                     {"port": True}, {"timeout": float("inf")},
                     {"timeout": 0}, {"timeout": 31}):
            with self.subTest(opts=opts), self.assertRaises((ValueError, TypeError)):
                cfg = {"host": "fake", "port": 502, "timeout": 10}
                cfg.update(opts)
                ReadOnlyModbusTransport(client_factory=self.factory, **cfg)
        self.assertEqual(self.sessions, [])


class ReadOnlyBrokerRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.device = FakeDongle(self.clock)
        self.env = {
            "SOLINTEG_HOST": "synthetic-modbus-device",
            "SOLINTEG_SLAVE_ID": "255",
            "SOLINTEG_BROKER_READONLY_ENABLED": "1",
            "SOLINTEG_CONTROL_ARMED": "1",  # deliberately ignored
        }
        self.factory = lambda host, port, timeout: self.device.connection()

    def build(self):
        return build_readonly_broker(self.env, client_factory=self.factory)

    def test_explicit_activation_and_slave_id_required(self):
        for change in ({"SOLINTEG_BROKER_READONLY_ENABLED": "0"},
                       {"SOLINTEG_SLAVE_ID": "1"},
                       {"SOLINTEG_HOST": ""},
                       {"SOLINTEG_BROKER_MAX_SAMPLE_S": "0"}):
            env = self.env | change
            with self.subTest(change=change), self.assertRaises((ValueError, PermissionError)):
                build_readonly_broker(env, client_factory=self.factory)
        self.assertEqual(self.device.clients, 0)

    def test_root_owned_approval_marker_is_explicit_and_mode_0600(self):
        fake = mock.Mock()
        fake.is_symlink.return_value = False
        fake.is_file.return_value = True
        fake.stat.return_value = SimpleNamespace(st_uid=0, st_mode=0o100600)
        with mock.patch("modbus_broker_readonly_service.APPROVAL_MARKER", fake):
            self.assertTrue(approved_cutover_marker())
            fake.stat.return_value = SimpleNamespace(st_uid=1000, st_mode=0o100600)
            self.assertFalse(approved_cutover_marker())
            fake.stat.return_value = SimpleNamespace(st_uid=0, st_mode=0o100644)
            self.assertFalse(approved_cutover_marker())
            fake.is_symlink.return_value = True
            self.assertFalse(approved_cutover_marker())

    def test_service_entrypoint_never_starts_without_root_approved_cutover(self):
        with mock.patch("modbus_broker_readonly_service.approved_cutover_marker",
                        return_value=False), \
             mock.patch("modbus_broker_readonly_service.build_readonly_broker") as build:
            with self.assertRaises(SystemExit):
                main(["--read-only"], environ=self.env)
            build.assert_not_called()
        self.assertEqual(self.device.clients, 0)

    def test_readonly_mode_refuses_arm_even_with_phase_guard(self):
        with self.assertRaises(PermissionError):
            Broker(self.device.connection, runtime_mode="readonly", armed=True)
        with self.assertRaises(PermissionError):
            Broker(self.device.connection, runtime_mode="readonly",
                   phase_guard=lambda *_: True)
        self.assertEqual(self.device.clients, 0)

    def test_real_adapter_shape_and_broker_worker_one_client(self):
        broker = self.build()
        try:
            self.assertTrue(broker.status()["read_only"])
            self.assertFalse(broker.status()["armed"])
            self.assertFalse(broker.status()["offline_only"])
            self.assertEqual(broker.submit("read_snapshot")["status"], "valid")
            self.assertEqual(broker.submit("read_snapshot")["status"], "valid")
            self.assertEqual(self.device.clients, 1)
            self.assertEqual(self.device.peak, 1)
            with self.assertRaises(PermissionError):
                broker.submit("charge", role="control", request_id="attack", watts=1000)
            with self.assertRaises(PermissionError):
                broker.submit("safety_auto", role="safety", request_id="attack")
            self.assertEqual(self.device.writes, [])
        finally:
            broker.close()
        self.assertEqual(self.device.active, 0)

    def test_unix_socket_session_is_readonly_even_with_armed_env(self):
        broker = self.build()
        with tempfile.TemporaryDirectory() as tmp:
            server = BrokerIpcServer(Path(tmp) / "broker.sock", broker)
            server.start()
            try:
                client = BrokerIpcClient(Path(tmp) / "broker.sock")
                self.assertEqual(client.request("read_snapshot")["status"], "valid")
                with self.assertRaisesRegex(RuntimeError, "PermissionError"):
                    client.request("charge", request_id="forbidden", watts=1000)
                with self.assertRaisesRegex(RuntimeError, "PermissionError"):
                    client.request("safety_auto", request_id="forbidden")
                self.assertEqual(self.device.writes, [])
            finally:
                server.close()
        broker.close()
        self.assertEqual(self.device.active, 0)

    def test_main_requires_explicit_readonly_flag_without_importing_network(self):
        with mock.patch("modbus_broker_readonly_service.build_readonly_broker") as build:
            with self.assertRaises(SystemExit):
                main([], environ=self.env)
            build.assert_not_called()
            with self.assertRaises(SystemExit):
                main(["--read-only"], environ=self.env | {
                    "SOLINTEG_BROKER_READONLY_ENABLED": "0",
                })
            build.assert_not_called()


class PollerBrokerCutoverTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.device = FakeDongle(self.clock)
        self.broker = Broker(
            self.device.connection, armed=False,
            clock=self.clock, sleep=self.clock.sleep,
            min_write_interval_s=0, runtime_mode="readonly",
        )
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name)
        self.socket = self.path / "broker.sock"
        self.server = BrokerIpcServer(self.socket, self.broker)
        self.server.start()
        self.client = BrokerIpcClient(self.socket)

    def tearDown(self):
        self.server.close()
        self.broker.close()
        self.tempdir.cleanup()

    def test_new_snapshot_keeps_previous_live_json_and_sqlite_contract(self):
        value = poller.read_broker_snapshot(self.client)
        old = FakeModbusClient("fake")
        old.regs.update(self.device.regs)
        original = poller.read_inverter(old)
        for key in poller.BROKER_LIVE_FIELDS:
            if key == "timestamp":
                self.assertIsInstance(datetime.fromisoformat(value[key]), datetime)
            else:
                with self.subTest(key=key):
                    self.assertEqual(value[key], original[key])
        self.assertEqual(value["sample_quality"], "valid")
        self.assertIn("sample_id", value)
        self.assertEqual(old.write_calls(), [])
        db = poller.init_db(self.path / "telemetry.db")
        poller.write_atomic(self.path / "live.json", value)
        poller.log_reading(db, value)
        row = db.execute("SELECT soc_pct,grid_w,meter_l3_w FROM readings").fetchone()
        self.assertEqual(row, (50.0, -800, -268))
        self.assertEqual(self.path.joinpath("live.json").stat().st_size > 0, True)
        db.close()

    def test_invalid_energy_does_not_refresh_last_good_timestamp_or_db(self):
        db = poller.init_db(self.path / "telemetry.db")
        first = poller.read_broker_snapshot(self.client)
        poller.write_atomic(self.path / "live.json", first)
        poller.log_reading(db, first)
        self.device.load(grid=-11, battery=-1692, ac=978, pv=0, mode=0x303)
        with self.assertRaisesRegex(IOError, "rejected"):
            poller.read_broker_snapshot(self.client)
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 1)
        self.assertEqual(
            __import__("json").loads((self.path / "live.json").read_text())["timestamp"],
            first["timestamp"],
        )
        self.assertEqual(self.device.writes, [])
        db.close()

    def test_stale_broker_sample_never_republished(self):
        good = poller.read_broker_snapshot(self.client)
        old = copy.deepcopy(good)
        old["timestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        fake = mock.Mock()
        fake.request.return_value = {"status": "valid", "sample": {
            **old, "quality": "valid", "duration_s": 1.0, "sample_id": "replayed",
        }}
        with self.assertRaisesRegex(IOError, "coherent telemetry"):
            poller.read_broker_snapshot(fake)
        self.assertEqual(self.device.writes, [])

    def test_broker_ipc_failure_does_not_open_fallback_direct_connection(self):
        with mock.patch.object(poller, "ModbusTcpClient") as tcp:
            disconnected = BrokerIpcClient(self.path / "missing.sock")
            with self.assertRaises(OSError):
                poller.read_broker_snapshot(disconnected)
            tcp.assert_not_called()
        self.assertEqual(self.device.writes, [])

    def test_validity_checks_reject_malformed_sample(self):
        good = poller.read_broker_snapshot(self.client)
        for key, value in (("soc_pct", float("nan")), ("grid_w", None),
                           ("work_mode_raw", None), ("timestamp", "bad-timestamp")):
            with self.subTest(key=key):
                altered = dict(good)
                altered[key] = value
                fake = mock.Mock()
                fake.request.return_value = {"status": "valid", "sample": {
                    **altered, "quality": "valid", "duration_s": 1.0,
                    "sample_id": "fake",
                }}
                with self.assertRaises(IOError):
                    poller.read_broker_snapshot(fake)

    def test_poller_main_broker_mode_never_instantiates_direct_tcp(self):
        db = poller.init_db(self.path / "via-broker.db")
        with mock.patch.object(poller, "init_db", return_value=db), \
             mock.patch.object(poller, "POLL_SOURCE", "broker"), \
             mock.patch.object(poller, "DATA_PATH", self.path / "via-broker.json"), \
             mock.patch.object(poller, "DB_PATH", self.path / "via-broker.db"), \
             mock.patch.object(poller, "INVERTER_HOST", ""), \
             mock.patch.object(poller, "ModbusTcpClient", side_effect=AssertionError("direct TCP forbidden")) as tcp, \
             mock.patch("modbus_broker_ipc.BrokerIpcClient", return_value=self.client), \
             mock.patch.dict(os.environ, {"SOLINTEG_BROKER_SOCKET": "/run/solinteg-broker/broker.sock"}), \
             mock.patch.object(poller.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                poller.main()
            tcp.assert_not_called()
        self.assertTrue((self.path / "via-broker.json").is_file())
        self.assertEqual(db.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 1)
        db.close()
        self.assertEqual(self.device.clients, 1)

    def test_poller_main_rejection_keeps_last_file_and_never_opens_direct_tcp(self):
        self.device.load(grid=-11, battery=-1692, ac=978, pv=0, mode=0x303)
        db = poller.init_db(self.path / "rejected.db")
        with mock.patch.object(poller, "init_db", return_value=db), \
             mock.patch.object(poller, "POLL_SOURCE", "broker"), \
             mock.patch.object(poller, "DATA_PATH", self.path / "rejected.json"), \
             mock.patch.object(poller, "DB_PATH", self.path / "rejected.db"), \
             mock.patch.object(poller, "ModbusTcpClient", side_effect=AssertionError("direct TCP forbidden")) as tcp, \
             mock.patch("modbus_broker_ipc.BrokerIpcClient", return_value=self.client), \
             mock.patch.dict(os.environ, {"SOLINTEG_BROKER_SOCKET": "/run/solinteg-broker/broker.sock"}), \
             mock.patch.object(poller.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                poller.main()
            tcp.assert_not_called()
        self.assertFalse((self.path / "rejected.json").exists())
        self.assertEqual(db.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 0)
        db.close()

    def test_opt_in_default_remains_direct(self):
        self.assertEqual(poller.POLL_SOURCE, "direct")
        self.assertEqual(self.device.writes, [])


if __name__ == "__main__":
    unittest.main()
