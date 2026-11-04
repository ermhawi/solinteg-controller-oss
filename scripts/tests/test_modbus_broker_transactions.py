"""Complete broker transactions: offline fake only, never a physical inverter."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "services")]
from fakes import install_pymodbus_stub
install_pymodbus_stub()

import inverter_control as control
from modbus_broker import Broker
from modbus_broker_control_client import BrokerModbusClient
from modbus_broker_control_service import build_control_broker
from modbus_broker_ipc import BrokerIpcClient, BrokerIpcServer
from test_modbus_broker import FakeDongle, FakeClock


class CompleteTransactionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.socket = Path(self.temp.name) / "broker.sock"
        self.clock = FakeClock()
        self.dongle = FakeDongle(self.clock)
        self.role = {"value": "control"}
        self.patches = [
            mock.patch.object(control, "ARMED", True),
            mock.patch.object(control, "SIGN", "neg_charge"),
            mock.patch.object(control, "SLAVE_ID", 255),
            mock.patch.object(control, "SOC_FLOOR_PCT", 12),
            mock.patch.object(control, "SOC_CEILING_PCT", 98),
            mock.patch.object(control.RateLimiter, "wait"),
            mock.patch.object(control, "SETTLE_S", 0),
            mock.patch.object(control, "FREEZE_SETTLE_S", 0),
            mock.patch.dict(os.environ, {"SOLINTEG_MODBUS_SOURCE": "broker_transaction"}),
        ]
        for patch in self.patches:
            patch.start()
        control._forced_active = False
        self.broker = Broker(
            self.dongle.connection, armed=True, runtime_mode="control",
            transactions_enabled=True, charge_sign="neg_charge",
            min_write_interval_s=0, clock=self.clock, sleep=self.clock.sleep,
        )
        self.server = BrokerIpcServer(
            self.socket, self.broker,
            role_for_peer=lambda *_args: self.role["value"],
        )
        self.server.start()

    def tearDown(self):
        self.dongle.release.set()
        self.server.close()
        self.broker.close()
        control._forced_active = False
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def ipc(self):
        return BrokerIpcClient(self.socket)

    def client(self):
        return BrokerModbusClient(
            ipc_client=self.ipc(), transaction_mode=True,
        )

    def inverter(self):
        with mock.patch("modbus_broker_control_client.BrokerIpcClient",
                        return_value=self.ipc()):
            inv = control.Inverter(unit=255)
        self.assertTrue(inv.client.transaction_mode)
        return inv

    def test_charge_full_then_fast_retarget_is_one_physical_session(self):
        inv = self.inverter()
        control.force_charge(inv, 300)
        self.assertEqual(
            [addr for addr, value in self.dongle.writes],
            [50209, 50208, 50210, 50207, 50000],
        )
        self.assertEqual(self.dongle.regs[50207], 65506)
        self.assertEqual(self.dongle.regs[50000], 0x303)
        self.assertEqual(self.dongle.clients, 1)
        control.force_charge(inv, 500)
        self.assertEqual(self.dongle.writes[-1], (50207, 65486))
        self.assertEqual(len(self.dongle.writes), 6)
        self.assertEqual(self.dongle.peak, 1)

    def test_complete_sequences_match_original_direct_inverter(self):
        original_dongle = FakeDongle(self.clock)
        direct = original_dongle.connection()
        self.assertTrue(direct.connect())
        old = control.Inverter(unit=255, client=direct)
        control._forced_active = False
        control.force_charge(old, 300)
        control.force_charge(old, 500)
        control.force_discharge(old, 50)
        control.return_to_auto(old)
        expected = list(original_dongle.writes)
        direct.close()
        control._forced_active = False

        migrated = self.inverter()
        control.force_charge(migrated, 300)
        control.force_charge(migrated, 500)
        control.force_discharge(migrated, 50)
        control.return_to_auto(migrated)
        self.assertEqual(self.dongle.writes, expected)
        self.assertEqual(self.dongle.clients, 1)
        self.assertEqual(self.dongle.peak, 1)

    def test_unchanged_dispatcher_apply_target_uses_complete_transactions(self):
        import dispatch_loop as dispatch

        inv = self.inverter()
        dispatch.apply_target(inv, "charge", 300)
        self.assertEqual(self.dongle.regs[50207], 65506)
        dispatch.apply_target(inv, "discharge", 50)
        self.assertEqual(self.dongle.regs[50207], 5)
        dispatch.apply_target(inv, "idle", 0)
        self.assertEqual(self.dongle.regs[50000], 0x101)
        self.assertEqual(self.dongle.clients, 1)
        self.assertEqual(self.dongle.peak, 1)

    def test_discharge_fast_path_and_hold_preserve_existing_sequence(self):
        self.dongle.load(grid=-800, battery=200, ac=200, pv=0, mode=0x303)
        self.dongle.regs.update({
            50208: control.GRID_CAP_RAW,
            50209: (-control.GRID_CAP_RAW) & 0xFFFF,
            50210: 1,
        })
        inv = self.inverter()
        control.force_discharge(inv, 50)
        self.assertEqual(self.dongle.writes, [(50207, 5)])
        self.assertTrue(control.force_hold(inv, 12))
        self.assertEqual(self.dongle.writes[-1], (50207, 0))
        self.assertEqual(self.dongle.peak, 1)

    def test_discharge_near_floor_still_protectively_charges(self):
        self.dongle.load(grid=-800, battery=200, ac=200, pv=0, soc=10)
        inv = self.inverter()
        control.force_discharge(inv, 2000)
        self.assertEqual(self.dongle.regs[50207], 65506)
        self.assertEqual(self.dongle.regs[50000], 0x303)
        self.assertEqual(self.dongle.peak, 1)

    def test_hold_rejection_returns_false_and_retains_legacy_recovery(self):
        self.dongle.load(grid=-800, battery=200, ac=200, pv=0, soc=10)
        inv = self.inverter()
        self.assertFalse(control.force_hold(inv, 12))
        self.assertEqual(self.dongle.regs[50000], 0x101)
        # The unchanged dispatcher applies its previous recovery charge next.
        control.force_charge(inv, control.SOC_FLOOR_RECOVERY_W)
        self.assertEqual(self.dongle.regs[50207], 65506)

    def test_return_to_auto_restores_caps_after_forced_state(self):
        inv = self.inverter()
        control.force_charge(inv, 300)
        start = len(self.dongle.writes)
        control.return_to_auto(inv)
        self.assertEqual(
            self.dongle.writes[start:],
            [(50208, control.GRID_CAP_RAW),
             (50209, (-control.GRID_CAP_RAW) & 0xFFFF),
             (50000, 0x101), (50207, 0)],
        )
        self.assertEqual(self.dongle.regs[50000], 0x101)
        self.assertEqual(self.dongle.clients, 1)

    def test_import_limit_and_disable_are_complete_transactions(self):
        inv = self.inverter()
        control.set_import_limit(inv, 10500)
        self.assertEqual(self.dongle.writes, [(50009, 105), (50007, 1)])
        control.clear_import_limit(inv)
        self.assertEqual(self.dongle.writes[-1], (50007, 0))
        self.assertEqual(self.dongle.clients, 1)

    def test_freeze_and_auto_use_original_mode_first_and_cap_restoration(self):
        self.dongle.load(grid=-800, battery=0, ac=200, pv=0, soc=50)
        inv = self.inverter()
        control.freeze_battery(inv, min_soc_pct=20)
        self.assertEqual(
            [addr for addr, _ in self.dongle.writes],
            [50000, 50210, 50208, 50209, 50207],
        )
        self.assertEqual(self.dongle.regs[50208], 0)
        self.assertEqual(self.dongle.regs[50209], 0)
        control.return_to_auto(inv)
        self.assertEqual(self.dongle.regs[50208], control.GRID_CAP_RAW)
        self.assertEqual(
            self.dongle.regs[50209], (-control.GRID_CAP_RAW) & 0xFFFF,
        )
        self.assertEqual(self.dongle.regs[50000], 0x101)

    def test_freeze_not_enforced_keeps_original_distinct_exception(self):
        self.dongle.load(grid=-800, battery=500, ac=200, pv=0, soc=50)
        inv = self.inverter()
        with self.assertRaises(control.FreezeNotEnforced):
            control.freeze_battery(inv, min_soc_pct=20)
        self.assertEqual(self.dongle.regs[50000], 0x101)

    def test_no_poller_read_can_interleave_a_control_transaction(self):
        self.dongle.block_write_addr = 50207
        ctrl = self.client()
        self.assertTrue(ctrl.connect())
        finished_control = threading.Event()
        finished_read = threading.Event()
        started_read = threading.Event()
        errors = []

        def charge():
            try:
                ctrl.run_transaction("charge", watts=300)
            except Exception as exc:
                errors.append(exc)
            finally:
                finished_control.set()

        def poll():
            try:
                self.role["value"] = "read"
                started_read.set()
                self.ipc().request("read_registers", address=33000, count=4)
            except Exception as exc:
                errors.append(exc)
            finally:
                finished_read.set()

        writer = threading.Thread(target=charge)
        writer.start()
        self.assertTrue(self.dongle.entered.wait(3), "transaction did not reach FC06")
        reader = threading.Thread(target=poll)
        reader.start()
        try:
            self.assertTrue(started_read.wait(2), "poller thread never started")
            self.assertFalse(finished_read.wait(.08), "poller interleaved with FC06 sequence")
        finally:
            self.dongle.release.set()
            writer.join(6)
            reader.join(6)
        self.assertFalse(errors, errors)
        self.assertTrue(finished_control.is_set())
        self.assertTrue(finished_read.is_set())
        self.assertEqual(self.dongle.clients, 1)
        self.assertEqual(self.dongle.peak, 1)

    def test_raw_fc06_and_other_roles_cannot_bypass_transactions(self):
        ctrl = self.client()
        self.assertTrue(ctrl.connect())
        with self.assertRaisesRegex(RuntimeError, "PermissionError"):
            self.ipc().request(
                "write_register", address=50207, value=100, request_id="bypass",
            )
        with self.assertRaisesRegex(RuntimeError, "PermissionError"):
            self.ipc().request(
                "charge", watts=300, request_id="old-prototype",
            )
        self.role["value"] = "read"
        with self.assertRaisesRegex(RuntimeError, "PermissionError"):
            self.ipc().request(
                "transaction_discharge", watts=500, request_id="poller-write",
            )
        self.assertEqual(self.dongle.writes, [])

    def test_invalid_power_floor_and_request_id_never_write(self):
        for operation, fields in (
            ("transaction_charge", {"watts": True}),
            ("transaction_charge", {"watts": -1}),
            ("transaction_charge", {"watts": 100001}),
            ("transaction_charge", {"watts": 1.5}),
            ("transaction_import_limit", {"watts": 0}),
            ("transaction_hold", {"min_soc_pct": float("nan")}),
            ("transaction_hold", {"min_soc_pct": -1}),
            ("transaction_freeze", {"min_soc_pct": None}),
            ("transaction_auto", {"min_soc_pct": 20}),
            ("transaction_auto", {"watts": 100}),
            ("transaction_charge", {"watts": 500, "address": 52503}),
        ):
            with self.subTest(operation=operation, fields=fields):
                with self.assertRaisesRegex(RuntimeError, "ValueError"):
                    self.ipc().request(operation, request_id="invalid", **fields)
        self.assertEqual(self.dongle.writes, [])

    def test_same_request_id_is_idempotent_for_a_complete_sequence(self):
        a = self.ipc().request("transaction_charge", watts=300, request_id="idempotent")
        before = list(self.dongle.writes)
        b = self.ipc().request("transaction_charge", watts=300, request_id="idempotent")
        self.assertEqual(a, b)
        self.assertEqual(self.dongle.writes, before)
        with self.assertRaisesRegex(RuntimeError, "BrokerError"):
            self.ipc().request("transaction_charge", watts=500, request_id="idempotent")

    def test_failed_mid_sequence_reports_error_without_new_fallback(self):
        self.dongle.fail_at_write = 2
        inv = self.inverter()
        with self.assertRaisesRegex(RuntimeError, "OSError"):
            control.force_charge(inv, 300)
        self.assertEqual(len(self.dongle.writes), 2)
        # Normal dispatcher catches this and uses its OLD revert_best_effort.
        self.dongle.fail_at_write = None
        control.return_to_auto(inv)
        self.assertEqual(self.dongle.regs[50000], 0x101)
        self.assertFalse(self.broker.status()["safety_latched"])

    def test_inverter_readback_mismatch_stops_transaction_before_mode_write(self):
        from test_modbus_broker import FakeConnection, Response

        old_read = FakeConnection.read_holding_registers

        def wrong_read(connection, address, count=1, device_id=255):
            result = old_read(connection, address, count=count, device_id=device_id)
            if address == 50207 and any(
                reg == 50207 for reg, _ in connection.device.writes
            ):
                return Response([0])
            return result

        inv = self.inverter()
        with mock.patch.object(FakeConnection, "read_holding_registers", wrong_read):
            with self.assertRaisesRegex(RuntimeError, "ValueError"):
                control.force_charge(inv, 300)
        self.assertEqual(self.dongle.regs[50000], 0x101)
        self.assertEqual(self.dongle.peak, 1)

    def test_timeout_stops_remaining_registers_without_new_safety_latch(self):
        self.dongle.block_write_addr = 50207
        result = []

        def run():
            try:
                self.broker.submit(
                    "transaction_charge", role="control", watts=300,
                    request_id="timed_out", timeout_s=.12,
                )
            except Exception as exc:
                result.append(type(exc).__name__)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(self.dongle.entered.wait(3))
        thread.join(2)
        self.assertEqual(result, ["TimeoutError"])
        self.dongle.release.set()
        # A timed-out multi-write request must never reach 50000/EMS mode.
        self.broker._tasks.join()
        self.assertNotIn(50000, [addr for addr, _ in self.dongle.writes])
        self.assertFalse(self.broker.status()["safety_latched"])

    def test_broker_worker_keeps_one_persistent_write_rate_limiter(self):
        inv = self.inverter()
        control.force_charge(inv, 300)
        first = self.broker._transaction_worker._inverter._rl
        control.force_charge(inv, 500)
        self.assertIs(first, self.broker._transaction_worker._inverter._rl)
        self.assertEqual(self.dongle.clients, 1)

    def test_disarmed_inverter_does_not_issue_transaction(self):
        inv = self.inverter()
        with mock.patch.object(control, "ARMED", False):
            control.force_charge(inv, 300)
            control.return_to_auto(inv)
        self.assertEqual(self.dongle.clients, 0)
        self.assertEqual(self.dongle.writes, [])

    def test_new_client_refuses_older_broker_without_direct_fallback(self):
        class OlderBroker:
            def request(self, operation, **_kwargs):
                return {"armed": True, "control_runtime": True} if operation == "status" else {}
        client = BrokerModbusClient(ipc_client=OlderBroker(), transaction_mode=True)
        with self.assertRaisesRegex(PermissionError, "does not support"):
            client.connect()
        self.assertEqual(self.dongle.clients, 0)

    def test_release_dropins_require_both_explicit_opt_ins(self):
        repo = HERE.parents[1]
        broker = (repo / "deploy/solinteg-broker-transactions.conf").read_text()
        dispatcher = (repo / "deploy/solinteg-dispatch-transactions.conf").read_text()
        self.assertIn("[Service]", broker)
        self.assertIn("Environment=SOLINTEG_BROKER_TRANSACTIONS_ENABLED=1", broker)
        self.assertIn("[Service]", dispatcher)
        self.assertIn("Environment=SOLINTEG_MODBUS_SOURCE=broker_transaction", dispatcher)
        self.assertNotIn("watchdog.service", broker + dispatcher)
        self.assertNotIn("ExecStart=", broker + dispatcher)

    def test_transaction_feature_explicit_opt_in_only(self):
        env = {
            "SOLINTEG_BROKER_CONTROL_ENABLED": "1",
            "SOLINTEG_CONTROL_ARMED": "1",
            "SOLINTEG_50207_SIGN": "neg_charge",
            "SOLINTEG_HOST": "fake",
            "SOLINTEG_SLAVE_ID": "255",
        }
        with mock.patch.object(control, "SLAVE_ID", 255):
            old = build_control_broker(env, client_factory=lambda *_: self.dongle.connection())
            try:
                self.assertFalse(old.status()["transactions_enabled"])
                with self.assertRaises(PermissionError):
                    old.submit("transaction_charge", role="control",
                               request_id="notenabled", watts=300)
            finally:
                old.close()
            enabled = build_control_broker(
                env | {"SOLINTEG_BROKER_TRANSACTIONS_ENABLED": "1"},
                client_factory=lambda *_: self.dongle.connection(),
            )
            try:
                self.assertTrue(enabled.status()["transactions_enabled"])
            finally:
                enabled.close()
            with self.assertRaises(ValueError):
                build_control_broker(env | {"SOLINTEG_BROKER_TRANSACTIONS_ENABLED": "yes"})
        self.assertEqual(self.dongle.clients, 0)


if __name__ == "__main__":
    unittest.main()
