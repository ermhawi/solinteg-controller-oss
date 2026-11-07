"""S07 synthetic ambiguity latch: no new FC06 until independent recovery.

Exercises only FakeDongle through existing local Unix broker IPC. Absolutely
no physical Modbus TCP and no direct EVCC access.
"""
from __future__ import annotations

import threading
import time
import unittest

import test_modbus_broker_transactions as fixtures


class StrictTransactionLatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CompleteTransactionsTest(methodName="runTest")
        self.fixture.setUp()
        # Use an opt-in fixture flag; normal installed legacy mode remains unchanged.
        self.fixture.broker._strict_transactions_timeout_latch = True

    def tearDown(self):
        self.fixture.tearDown()

    def test_ambiguous_timeout_fences_pending_and_new_request_ids(self):
        self.fixture.dongle.block_write_addr = 50207
        results = {}

        def submit(key, operation, timeout):
            try:
                self.fixture.ipc().request(operation, watts=300, request_id=key,
                                           timeout_s=timeout)
                results[key] = "unexpected_success"
            except Exception as error:
                results[key] = type(error).__name__ + ":" + str(error)

        first = threading.Thread(target=submit, args=("first", "transaction_charge", 0.25))
        second = threading.Thread(target=submit, args=("queued", "transaction_discharge", 4))
        first.start()
        self.assertTrue(self.fixture.dongle.entered.wait(3))
        second.start()
        try:
            deadline = time.monotonic() + 1.0
            while (self.fixture.broker.status()["queue_depth"] == 0
                   and time.monotonic() < deadline):
                time.sleep(0.01)
            self.assertGreater(self.fixture.broker.status()["queue_depth"], 0)
            first.join(2)
            self.assertIn("TimeoutError", results.get("first", ""))
            self.assertTrue(self.fixture.broker.status()["safety_latched"])
            with self.assertRaisesRegex(RuntimeError, "Fenced"):
                self.fixture.ipc().request("transaction_charge", watts=300,
                                           request_id="new-generation")
        finally:
            self.fixture.dongle.release.set()
            first.join(4)
            second.join(4)
        self.fixture.broker._tasks.join()
        self.assertNotIn("unexpected_success", results.values())
        self.assertIn("Fenced", results.get("queued", ""))
        self.assertNotIn(50000, [address for address, _ in self.fixture.dongle.writes])
        self.assertEqual(self.fixture.dongle.peak, 1)

    def test_each_partial_fc06_exception_latches_without_blind_retry(self):
        for index in (1, 3, 5):
            with self.subTest(fault_step=index):
                if index != 1:
                    self.fixture.tearDown()
                    self.fixture = fixtures.CompleteTransactionsTest(methodName="runTest")
                    self.fixture.setUp()
                    self.fixture.broker._strict_transactions_timeout_latch = True
                self.fixture.dongle.fail_at_write = index
                with self.assertRaisesRegex(RuntimeError, "OSError"):
                    self.fixture.ipc().request(
                        "transaction_charge", watts=300, request_id="partial")
                snapshot = list(self.fixture.dongle.writes)
                self.assertEqual(len(snapshot), index)
                self.assertTrue(self.fixture.broker.status()["safety_latched"])
                with self.assertRaisesRegex(RuntimeError, "Fenced"):
                    self.fixture.ipc().request(
                        "transaction_charge", watts=300, request_id="different")
                with self.assertRaisesRegex(RuntimeError, "OSError"):
                    self.fixture.ipc().request(
                        "transaction_charge", watts=300, request_id="partial")
                self.assertEqual(self.fixture.dongle.writes, snapshot)
                self.assertEqual(self.fixture.dongle.peak, 1)

    def test_read_only_diagnostics_still_work_after_ambiguous_failure(self):
        self.fixture.dongle.fail_at_write = 1
        with self.assertRaisesRegex(RuntimeError, "OSError"):
            self.fixture.ipc().request("transaction_charge", watts=300,
                                       request_id="unverified")
        result = self.fixture.ipc().request("read_registers", address=50000, count=1)
        self.assertEqual(result["registers"], [0x101])
        self.assertTrue(self.fixture.broker.status()["safety_latched"])

    def test_service_configuration_requires_explicit_zero_or_one(self):
        from modbus_broker_control_service import build_control_broker
        base = {
            "SOLINTEG_BROKER_CONTROL_ENABLED": "1",
            "SOLINTEG_CONTROL_ARMED": "1",
            "SOLINTEG_50207_SIGN": "neg_charge",
            "SOLINTEG_SLAVE_ID": "255",
            "SOLINTEG_HOST": "offline.example.invalid",
            "SOLINTEG_BROKER_TRANSACTIONS_ENABLED": "1",
        }
        for value in ("true", "yes", "-1", "2", "unset"):
            with self.subTest(value=value), self.assertRaisesRegex(
                    ValueError, "strict transaction timeout latch"):
                build_control_broker(base | {
                    "SOLINTEG_BROKER_STRICT_TRANSACTION_TIMEOUT_LATCH": value,
                })
        broker = build_control_broker(base | {
            "SOLINTEG_BROKER_STRICT_TRANSACTION_TIMEOUT_LATCH": "1",
        })
        try:
            self.assertTrue(broker.status()["strict_transaction_fencing"])
        finally:
            broker.close()

    def test_default_off_and_invalid_configuration_are_never_implicit(self):
        from modbus_broker import Broker
        from test_modbus_broker import FakeDongle
        dongle = FakeDongle()
        normal = Broker(dongle.connection, armed=True, runtime_mode="control",
                        transactions_enabled=True, min_write_interval_s=0)
        try:
            self.assertFalse(normal._strict_transactions_timeout_latch)
        finally:
            normal.close()
        for config in (
            {"armed": False, "runtime_mode": "control", "transactions_enabled": True},
            {"armed": True, "runtime_mode": "offline", "transactions_enabled": False},
            {"armed": True, "runtime_mode": "control", "transactions_enabled": False},
        ):
            with self.subTest(config=config), self.assertRaises(PermissionError):
                Broker(lambda: None, strict_transactions_timeout_latch=True, **config)


if __name__ == "__main__":
    unittest.main()
