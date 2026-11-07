"""S07: synthetic FC06 failure matrix; never opens a real inverter socket.

Reuse the existing complete transaction fixture with a fake dongle and local
Unix IPC. The source broker is not the installed one and physical safety is
never inferred from these tests.
"""
from __future__ import annotations

import threading
import time
import unittest

import test_modbus_broker_transactions as transactions


class BrokerFaultMatrixTest(unittest.TestCase):
    def setUp(self):
        self.fixture = transactions.CompleteTransactionsTest(methodName="runTest")
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_all_five_charge_register_failures_do_not_retry_ambiguous_fc06(self):
        # Construct a new fixture for each failure to avoid contaminating the
        # persistent broker connection and its cached idempotency result.
        for index in range(1, 6):
            with self.subTest(failed_write=index):
                if index != 1:
                    self.fixture.tearDown()
                    self.fixture = transactions.CompleteTransactionsTest(methodName="runTest")
                    self.fixture.setUp()
                self.fixture.dongle.fail_at_write = index
                with self.assertRaisesRegex(RuntimeError, "OSError"):
                    self.fixture.ipc().request(
                        "transaction_charge", watts=300,
                        request_id=f"fault_{index}",
                    )
                written = list(self.fixture.dongle.writes)
                self.assertEqual(len(written), index)
                with self.assertRaisesRegex(RuntimeError, "OSError"):
                    self.fixture.ipc().request(
                        "transaction_charge", watts=300,
                        request_id=f"fault_{index}",
                    )
                self.assertEqual(self.fixture.dongle.writes, written)
                self.assertEqual(self.fixture.dongle.regs[50000], 0x101)
                self.assertLessEqual(self.fixture.dongle.peak, 1)

    def test_timeout_fences_queued_write_and_stops_before_mode_change(self):
        self.fixture.dongle.block_write_addr = 50207
        results = {}

        def write(key, operation, timeout):
            try:
                self.fixture.broker.submit(
                    operation, role="control", watts=300,
                    request_id=key, timeout_s=timeout,
                )
                results[key] = "unexpected-success"
            except Exception as exc:
                results[key] = type(exc).__name__

        first = threading.Thread(
            target=write, args=("first", "transaction_charge", 0.25))
        first.start()
        self.assertTrue(self.fixture.dongle.entered.wait(3))
        second = threading.Thread(
            target=write, args=("second", "transaction_discharge", 1))
        second.start()
        try:
            # The second request is pending behind the first in the single worker.
            deadline = time.monotonic() + 1
            while self.fixture.broker.status()["queue_depth"] == 0 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertGreater(self.fixture.broker.status()["queue_depth"], 0)
            first.join(1)
            self.assertEqual(results.get("first"), "TimeoutError")
        finally:
            self.fixture.dongle.release.set()
            first.join(4)
            second.join(4)
        self.fixture.broker._tasks.join()
        self.assertNotEqual(results.get("second"), "unexpected-success")
        self.assertNotIn(50000, [address for address, _ in self.fixture.dongle.writes])
        self.assertEqual(self.fixture.dongle.regs[50000], 0x101)
        self.assertEqual(self.fixture.dongle.peak, 1)
        self.assertEqual(self.fixture.dongle.clients, 1)

    def test_reconnect_reuses_single_owner_without_replaying_prior_write(self):
        self.fixture.ipc().request(
            "transaction_charge", watts=300, request_id="applied"
        )
        original = list(self.fixture.dongle.writes)
        transport = self.fixture.broker._transport
        self.assertIsNotNone(transport)
        transport.close()  # Synthetic disconnect, no host network access.
        self.assertEqual(self.fixture.dongle.active, 0)
        self.fixture.ipc().request("read_registers", address=33000, count=4)
        self.assertEqual(self.fixture.dongle.clients, 2)
        self.assertEqual(self.fixture.dongle.peak, 1)
        self.assertEqual(self.fixture.dongle.writes, original)
        self.fixture.ipc().request(
            "transaction_charge", watts=300, request_id="applied"
        )
        self.assertEqual(self.fixture.dongle.writes, original)


if __name__ == "__main__":
    unittest.main()
