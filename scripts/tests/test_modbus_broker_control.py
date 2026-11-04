"""Single-owner broker control integration tests: only fake Modbus I/O."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SERVICES = HERE.parent / "services"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SERVICES))

from fakes import install_pymodbus_stub
install_pymodbus_stub()

import inverter_control
from modbus_broker import Broker
from modbus_broker_control_client import BrokerModbusClient
from modbus_broker_control_service import (
    approved_control_marker, build_control_broker, main, role_for_systemd_peer,
)
from modbus_broker_control_transport import ControlModbusTransport
from modbus_broker_ipc import BrokerIpcClient, BrokerIpcServer
from test_modbus_broker import FakeClock, FakeDongle


class BrokerControlIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.dongle = FakeDongle(self.clock)
        self.fake_roles = {"role": "control"}
        self.env = {
            "SOLINTEG_BROKER_CONTROL_ENABLED": "1",
            "SOLINTEG_CONTROL_ARMED": "1",
            "SOLINTEG_50207_SIGN": "neg_charge",
            "SOLINTEG_HOST": "fake-inverter",
            "SOLINTEG_SLAVE_ID": "255",
        }
        self.clients = []

        def client_factory(host, port, timeout):
            self.assertEqual(host, "fake-inverter")
            self.assertEqual(port, 502)
            self.assertEqual(timeout, 10)
            client = self.dongle.connection()
            self.clients.append(client)
            return client

        self.client_factory = client_factory
        self.temp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.temp.name) / "broker.sock"
        self.broker = None
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.close()
        if self.broker:
            self.broker.close()
        self.temp.cleanup()

    def start(self):
        self.broker = Broker(
            lambda: ControlModbusTransport(
                "fake-inverter", client_factory=self.client_factory,
            ),
            armed=True, runtime_mode="control", charge_sign="neg_charge",
            min_write_interval_s=0, clock=self.clock, sleep=self.clock.sleep,
            battery_kwh=15.055,
        )
        self.server = BrokerIpcServer(
            self.socket_path, self.broker,
            role_for_peer=lambda _pid, _uid, _gid: self.fake_roles["role"],
        )
        self.server.start()
        ipc = BrokerIpcClient(self.socket_path)
        return BrokerModbusClient(ipc_client=ipc)

    def test_control_runtime_rejects_high_level_untested_control(self):
        proxy = self.start()
        self.assertTrue(proxy.connect())
        with self.assertRaises(PermissionError):
            self.broker.submit("charge", role="control", request_id="legacy", watts=300)
        self.assertEqual(self.dongle.writes, [])

    def test_broker_is_single_socket_owner_for_dispatch_and_poller(self):
        proxy = self.start()
        self.assertTrue(proxy.connect())
        self.assertEqual(proxy.read_holding_registers(33000).registers, [5000])
        self.assertFalse(proxy.write_register(50207, 65506).isError())
        self.assertEqual(proxy.read_holding_registers(50207).registers, [65506])

        self.fake_roles["role"] = "read"
        poller = BrokerIpcClient(self.socket_path)
        self.assertEqual(poller.request("read_snapshot")["status"], "valid")
        with self.assertRaisesRegex(RuntimeError, "PermissionError"):
            poller.request("write_register", address=50207, value=0,
                           request_id="untrusted")
        with self.assertRaisesRegex(RuntimeError, "PermissionError"):
            poller.request("read_registers", address=33000, count=1)
        self.assertEqual(self.dongle.writes, [(50207, 65506)])
        self.assertEqual(self.dongle.clients, 1)
        self.assertEqual(self.dongle.peak, 1)
        self.assertEqual(self.dongle.active, 1)
        proxy.close()

    def test_unknown_or_protection_registers_never_reach_fc06(self):
        proxy = self.start()
        self.assertTrue(proxy.connect())
        for addr, value in (
            (52502, 0), (52503, 100), (10008, 42), (50207, -1),
            (50207, 65536), (50000, True),
        ):
            with self.subTest(addr=addr, value=value):
                with self.assertRaisesRegex(RuntimeError, "PermissionError"):
                    BrokerIpcClient(self.socket_path).request(
                        "write_register", address=addr, value=value,
                        request_id="bad_" + str(addr),
                    )
        for addr, count in ((10008, 1), (33000, 3), (30258, 4), (52503, 1)):
            with self.subTest(addr=addr, count=count):
                with self.assertRaisesRegex(RuntimeError, "PermissionError"):
                    BrokerIpcClient(self.socket_path).request(
                        "read_registers", address=addr, count=count,
                    )
        self.assertEqual(self.dongle.writes, [])

    def test_legacy_force_charge_and_auto_preserve_register_sequence(self):
        proxy = self.start()
        with mock.patch.dict(os.environ, {"SOLINTEG_MODBUS_SOURCE": "broker"}), \
             mock.patch("modbus_broker_control_client.BrokerIpcClient",
                        return_value=BrokerIpcClient(self.socket_path)), \
             mock.patch.object(inverter_control, "ARMED", True), \
             mock.patch.object(inverter_control, "SIGN", "neg_charge"), \
             mock.patch.object(inverter_control.RateLimiter, "wait"), \
             mock.patch.object(inverter_control.time, "sleep"), \
             mock.patch.object(inverter_control, "SOC_CEILING_PCT", 98):
            inv = inverter_control.Inverter(unit=255)
            try:
                self.assertIsInstance(inv.client, BrokerModbusClient)
                inverter_control.force_charge(inv, 300)
                self.assertEqual(
                    [a for a, _ in self.dongle.writes],
                    [50209, 50208, 50210, 50207, 50000],
                )
                self.assertEqual(self.dongle.regs[50207], 65506)
                self.assertEqual(self.dongle.regs[50000], 0x303)
                inverter_control.return_to_auto(inv)
                self.assertEqual(
                    [a for a, _ in self.dongle.writes[-4:]],
                    [50208, 50209, 50000, 50207],
                )
                self.assertEqual(self.dongle.regs[50000], 0x101)
                self.assertEqual(self.dongle.regs[50207], 0)
                self.assertEqual(self.dongle.clients, 1)
                self.assertEqual(self.dongle.peak, 1)
            finally:
                inv.close()
                inverter_control._forced_active = False

    def test_legacy_poller_decoding_via_broker_preserves_live_schema(self):
        import json
        import sqlite3
        import modbus_poller as poller

        self.start()
        self.fake_roles["role"] = "read"
        out = Path(self.temp.name) / "live.json"
        db = Path(self.temp.name) / "telemetry.db"
        with mock.patch.object(poller, "POLL_SOURCE", "broker_legacy"), \
             mock.patch.object(poller, "SLAVE_ID", 255), \
             mock.patch.object(poller, "DATA_PATH", out), \
             mock.patch.object(poller, "DB_PATH", db), \
             mock.patch.dict(os.environ, {
                 "SOLINTEG_BROKER_SOCKET": "/run/solinteg-broker/broker.sock",
             }), \
             mock.patch("modbus_broker_control_client.BrokerIpcClient",
                        return_value=BrokerIpcClient(self.socket_path)), \
             mock.patch.object(poller.time, "sleep", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                poller.main()
        data = json.loads(out.read_text())
        self.assertEqual((data["soc_pct"], data["work_mode_raw"]), (50, 0x101))
        self.assertEqual((data["grid_w"], data["battery_w"]), (-800, 200))
        self.assertEqual(
            sqlite3.connect(db).execute("SELECT COUNT(*) FROM readings").fetchone()[0], 1,
        )
        self.assertEqual(self.dongle.clients, 1)
        self.assertEqual(self.dongle.peak, 1)

    def test_legacy_low_soc_protection_still_selects_charge(self):
        self.dongle.load(
            grid=-800, battery=200, ac=200, pv=0, mode=0x101, soc=10,
        )
        self.start()
        with mock.patch.dict(os.environ, {"SOLINTEG_MODBUS_SOURCE": "broker"}), \
             mock.patch("modbus_broker_control_client.BrokerIpcClient",
                        return_value=BrokerIpcClient(self.socket_path)), \
             mock.patch.object(inverter_control, "ARMED", True), \
             mock.patch.object(inverter_control, "SIGN", "neg_charge"), \
             mock.patch.object(inverter_control, "SOC_FLOOR_PCT", 12), \
             mock.patch.object(inverter_control, "SOC_CEILING_PCT", 98), \
             mock.patch.object(inverter_control.RateLimiter, "wait"), \
             mock.patch.object(inverter_control.time, "sleep"):
            inv = inverter_control.Inverter(unit=255)
            try:
                inverter_control.force_discharge(inv, 2000)
                self.assertEqual(self.dongle.regs[50000], 0x303)
                self.assertEqual(self.dongle.regs[50207], 65506)
                self.assertEqual(self.dongle.peak, 1)
            finally:
                inv.close()
                inverter_control._forced_active = False

    def test_legacy_fast_path_preserves_single_target_write(self):
        self.dongle.load(
            grid=-800, battery=200, ac=200, pv=0, mode=0x303, soc=50,
        )
        self.dongle.regs.update({
            50208: 1100, 50209: (-1100) & 0xFFFF, 50210: 1,
        })
        self.start()
        with mock.patch.dict(os.environ, {"SOLINTEG_MODBUS_SOURCE": "broker"}), \
             mock.patch("modbus_broker_control_client.BrokerIpcClient",
                        return_value=BrokerIpcClient(self.socket_path)), \
             mock.patch.object(inverter_control, "ARMED", True), \
             mock.patch.object(inverter_control, "SIGN", "neg_charge"), \
             mock.patch.object(inverter_control, "SOC_FLOOR_PCT", 12), \
             mock.patch.object(inverter_control.RateLimiter, "wait"), \
             mock.patch.object(inverter_control.time, "sleep"):
            inv = inverter_control.Inverter(unit=255)
            try:
                inverter_control.force_discharge(inv, 50)
                self.assertEqual(self.dongle.writes, [(50207, 5)])
                self.assertEqual(self.dongle.peak, 1)
            finally:
                inv.close()
                inverter_control._forced_active = False

    def test_existing_site_import_limit_register_sequence_preserved(self):
        self.start()
        with mock.patch.dict(os.environ, {"SOLINTEG_MODBUS_SOURCE": "broker"}), \
             mock.patch("modbus_broker_control_client.BrokerIpcClient",
                        return_value=BrokerIpcClient(self.socket_path)), \
             mock.patch.object(inverter_control, "ARMED", True), \
             mock.patch.object(inverter_control.RateLimiter, "wait"), \
             mock.patch.object(inverter_control.time, "sleep"):
            inv = inverter_control.Inverter(unit=255)
            try:
                inverter_control.set_import_limit(inv, 8000)
                self.assertEqual(
                    self.dongle.writes, [(50009, 80), (50007, 1)],
                )
                self.assertEqual(self.dongle.peak, 1)
            finally:
                inv.close()

    def test_wrong_device_or_disconnected_proxy_fails_closed(self):
        proxy = self.start()
        with self.assertRaises(ConnectionError):
            proxy.read_holding_registers(33000)
        self.assertTrue(proxy.connect())
        with self.assertRaises(ConnectionError):
            proxy.write_register(50207, 30, device_id=1)
        with self.assertRaises(ConnectionError):
            proxy.read_holding_registers(33000, device_id=1)
        proxy.close()
        with self.assertRaises(ConnectionError):
            proxy.write_register(50207, 30)
        self.assertEqual(self.dongle.writes, [])

    def test_bad_ipc_or_unarmed_broker_never_falls_back_to_direct(self):
        class Denied:
            def request(self, *_args, **_kwargs):
                raise ConnectionError("broker unavailable")
        proxy = BrokerModbusClient(ipc_client=Denied())
        with self.assertRaisesRegex(ConnectionError, "broker unavailable"):
            proxy.connect()
        self.assertEqual(self.dongle.clients, 0)


class ControlServiceGateTest(unittest.TestCase):
    def test_build_requires_explicit_arms_before_transport(self):
        base = {
            "SOLINTEG_BROKER_CONTROL_ENABLED": "1",
            "SOLINTEG_CONTROL_ARMED": "1",
            "SOLINTEG_50207_SIGN": "neg_charge",
            "SOLINTEG_SLAVE_ID": "255",
            "SOLINTEG_HOST": "fake-inverter",
        }
        for diff in (
            {"SOLINTEG_BROKER_CONTROL_ENABLED": "0"},
            {"SOLINTEG_CONTROL_ARMED": "0"},
            {"SOLINTEG_50207_SIGN": ""},
            {"SOLINTEG_SLAVE_ID": "1"},
            {"SOLINTEG_HOST": ""},
            {"SOLINTEG_BROKER_TIMEOUT_S": "nan"},
        ):
            with self.subTest(diff=diff), self.assertRaises((PermissionError, ValueError)):
                build_control_broker(base | diff)
        with mock.patch("modbus_broker_control_service.APPROVAL_MARKER") as marker:
            marker.is_symlink.return_value = False
            marker.is_file.return_value = False
            self.assertFalse(approved_control_marker())

    def test_missing_root_marker_prevents_service_start(self):
        with mock.patch("modbus_broker_control_service.approved_control_marker",
                        return_value=False):
            with self.assertRaises(SystemExit):
                main(["--control"], environ={
                    "SOLINTEG_BROKER_CONTROL_ENABLED": "1",
                })

    def test_role_is_derived_from_os_cgroup_not_request(self):
        uid = os.getuid()
        with mock.patch("pathlib.Path.read_text",
                        return_value="0::/system.slice/solinteg-dispatch.service\n"):
            self.assertEqual(role_for_systemd_peer(1234, uid, uid), "control")
            self.assertIsNone(role_for_systemd_peer(1234, uid + 1, uid))
        with mock.patch("pathlib.Path.read_text",
                        return_value="0::/system.slice/solinteg-poller.service\n"):
            self.assertEqual(role_for_systemd_peer(1234, uid, uid), "read")
        with mock.patch("pathlib.Path.read_text",
                        return_value="0::/system.slice/solinteg-watchdog.service\n"):
            self.assertIsNone(role_for_systemd_peer(1234, uid, uid))
        self.assertIsNone(role_for_systemd_peer(-1, uid, uid))

    def test_transport_refuses_unknown_registers_without_io(self):
        from test_modbus_broker import FakeDongle
        device = FakeDongle()
        transport = ControlModbusTransport(
            "fake", client_factory=lambda *a, **k: device.connection(),
        )
        self.assertTrue(transport.connect())
        try:
            with self.assertRaises(PermissionError):
                transport.write_register(52503, 100)
            with self.assertRaises(PermissionError):
                transport.read_holding_registers(10008, count=1)
            with self.assertRaises(PermissionError):
                transport.write_register(50207, 1, device_id=1)
            self.assertEqual(device.writes, [])
            self.assertEqual(device.reads, [])
        finally:
            transport.close()


if __name__ == "__main__":
    unittest.main()
