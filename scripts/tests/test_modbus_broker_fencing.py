"""Broker emergency fallback fencing is tested exclusively with injected fakes."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from modbus_broker_fencing import FenceObservation, fenced_emergency_auto


def observation(*, alive=False, broker=0, other=0, socket=False, checked=1.0):
    return FenceObservation(alive, broker, other, socket, checked)


class FenceTest(unittest.TestCase):
    def run_case(self, *, stop=lambda: True, states=None, emergency=lambda: True,
                 deadline=10, clock=lambda: 1):
        alerts, writes, stops = [], [], []
        observations = iter(states if states is not None else [observation(), observation()])

        def stop_once():
            stops.append(True)
            return stop()

        def observe():
            return next(observations)

        def write():
            writes.append("auto")
            return emergency()

        result = fenced_emergency_auto(stop_broker=stop_once, observe=observe,
                                       emergency_auto=write, alert=alerts.append,
                                       before_write=lambda: True,  # synthetic safety proof
                                       deadline_s=deadline, monotonic=clock)
        return result, alerts, writes, stops

    def test_verified_exclusive_release_allows_one_auto_attempt(self):
        result, alerts, writes, stops = self.run_case()
        self.assertTrue(result.auto_readback_verified)
        self.assertEqual((stops, writes, alerts), ([True], ["auto"], []))

    def test_stop_unconfirmed_never_touches_modbus(self):
        result, alerts, writes, _ = self.run_case(stop=lambda: False)
        self.assertFalse(result.emergency_attempted)
        self.assertEqual(writes, [])
        self.assertTrue(alerts)

    def test_supervisor_exception_fails_closed(self):
        def failed():
            raise PermissionError("cannot stop systemd")
        result, alerts, writes, _ = self.run_case(stop=failed)
        self.assertFalse(result.emergency_attempted)
        self.assertEqual(result.error, "PermissionError")
        self.assertEqual(writes, [])

    def test_observation_rejects_all_ambiguous_or_competing_owners(self):
        unsafe = [
            observation(alive=True),
            observation(alive=None),
            observation(broker=1),
            observation(broker=None),
            observation(other=1),
            observation(other=None),
            observation(socket=True),
            observation(socket=None),
        ]
        for value in unsafe:
            with self.subTest(value=value):
                result, alerts, writes, _ = self.run_case(states=[value])
                self.assertFalse(result.emergency_attempted)
                self.assertFalse(result.released)
                self.assertEqual(writes, [])
                self.assertTrue(alerts)

    def test_ownership_changed_between_checks_fails_closed(self):
        result, alerts, writes, _ = self.run_case(
            states=[observation(), observation(broker=1)]
        )
        self.assertFalse(result.emergency_attempted)
        self.assertEqual(writes, [])
        self.assertTrue(alerts)

    def test_deadline_expired_never_touches_modbus(self):
        times = iter([1.0, 20.0])
        result, alerts, writes, _ = self.run_case(clock=lambda: next(times), deadline=10)
        self.assertEqual(result.error, "FenceFailure")
        self.assertEqual(writes, [])
        self.assertTrue(alerts)

    def test_wrong_observer_contract_fails_closed(self):
        result, alerts, writes, _ = self.run_case(states=[{"broker_alive": False}])
        self.assertEqual(result.error, "TypeError")
        self.assertEqual(writes, [])

    def test_missing_write_guard_fails_closed_even_with_exclusive_release(self):
        writes = []
        result = fenced_emergency_auto(
            stop_broker=lambda: True,
            observe=observation,
            emergency_auto=lambda: writes.append("unsafe"),
            alert=lambda _: None,
        )
        self.assertEqual(result.error, "write_guard_missing")
        self.assertFalse(result.emergency_attempted)
        self.assertEqual(writes, [])

    def test_deadline_expiring_during_final_safety_check_blocks_write(self):
        writes = []
        elapsed = {"expired": False}
        def slow_authorization():
            elapsed["expired"] = True
            return True
        result = fenced_emergency_auto(
            stop_broker=lambda: True,
            observe=observation,
            emergency_auto=lambda: writes.append("unsafe"),
            before_write=slow_authorization, alert=lambda _: None,
            monotonic=lambda: 100.0 if elapsed["expired"] else 0.0,
            deadline_s=10,
        )
        self.assertEqual(result.error, "deadline_exceeded_before_write")
        self.assertFalse(result.emergency_attempted)
        self.assertEqual(writes, [])

    def test_write_guard_blocks_after_double_observation(self):
        writes = []
        result = fenced_emergency_auto(
            stop_broker=lambda: True,
            observe=observation,
            emergency_auto=lambda: writes.append("unsafe"),
            before_write=lambda: False,
            alert=lambda _: None,
        )
        self.assertEqual(result.error, "write_guard_denied")
        self.assertFalse(result.emergency_attempted)
        self.assertEqual(writes, [])

    def test_write_guard_exception_is_fail_closed(self):
        writes = []
        def unavailable():
            raise PermissionError("marker cannot be verified")
        result = fenced_emergency_auto(
            stop_broker=lambda: True,
            observe=observation,
            emergency_auto=lambda: writes.append("unsafe"),
            before_write=unavailable,
            alert=lambda _: None,
        )
        self.assertEqual(result.error, "PermissionError")
        self.assertFalse(result.emergency_attempted)
        self.assertEqual(writes, [])

    def test_auto_unverified_is_not_success(self):
        result, alerts, writes, _ = self.run_case(emergency=lambda: False)
        self.assertTrue(result.emergency_attempted)
        self.assertFalse(result.auto_readback_verified)
        self.assertEqual(writes, ["auto"])
        self.assertTrue(alerts)

    def test_emergency_exception_is_reported_no_retry(self):
        def crashed():
            raise TimeoutError("write response lost")
        result, alerts, writes, _ = self.run_case(emergency=crashed)
        self.assertEqual(result.error, "TimeoutError")
        self.assertEqual(writes, ["auto"])
        self.assertFalse(result.auto_readback_verified)
        self.assertTrue(alerts)

    def test_alert_failure_does_not_allow_unsafe_write(self):
        writes = []
        result = fenced_emergency_auto(
            stop_broker=lambda: False,
            observe=lambda: observation(),
            emergency_auto=lambda: writes.append("never"),
            alert=lambda text: 1 / 0,
        )
        self.assertFalse(result.emergency_attempted)
        self.assertFalse(writes)


if __name__ == "__main__":
    unittest.main()
