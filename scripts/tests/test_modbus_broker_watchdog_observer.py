"""Offline tests for Dell-first, Pi-portable observe-only broker watchdog.

No systemd mutation, TCP connection, inverter access or physical commands.
"""
import json
import socket
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import modbus_broker_watchdog_observer as observer  # noqa: E402

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def heartbeat(seconds=0, **changes):
    payload = {"timestamp": (NOW - timedelta(seconds=seconds)).isoformat()}
    payload.update(changes)
    return payload


def broker_healthy(**changes):
    payload = {
        "read_only": True, "armed": False, "safety_latched": False,
        "last_valid_timestamp": NOW.isoformat(),
    }
    payload.update(changes)
    return payload


class ObserverPolicyTests(unittest.TestCase):
    def assess(self, dispatch=None, live=None, *, broker_required=False, broker=None):
        return observer.assess(
            NOW, heartbeat() if dispatch is None else dispatch,
            heartbeat() if live is None else live,
            broker_required=broker_required, broker=broker,
        )

    def test_legacy_dispatch_and_poller_are_healthy_without_broker(self):
        self.assertEqual(self.assess(), ("healthy", ()))

    def test_dispatch_stale_missing_invalid_and_future_inhibit(self):
        for data in (
            {}, heartbeat(seconds=181), heartbeat(timestamp="nonsense"),
            heartbeat(timestamp="2026-11-03T12:00:00"),
            heartbeat(timestamp=(NOW + timedelta(seconds=2)).isoformat()),
        ):
            with self.subTest(data=data):
                status, reasons = self.assess(dispatch=data)
                self.assertEqual(status, "fault")
                self.assertIn("dispatch_heartbeat_missing_stale_or_invalid", reasons)

    def test_poller_stale_does_not_pass_just_because_dispatch_is_alive(self):
        status, reasons = self.assess(live=heartbeat(seconds=121))
        self.assertEqual(status, "fault")
        self.assertIn("poller_telemetry_missing_stale_or_invalid", reasons)

    def test_current_dispatch_inhibit_is_a_fault_despite_fresh_heartbeat(self):
        status, reasons = self.assess(
            dispatch=heartbeat(broker_policy="inhibit", supervisor_required=True))
        self.assertEqual(status, "fault")
        self.assertIn("dispatcher_requires_supervisor", reasons)

    def test_unknown_policy_fails_closed(self):
        status, reasons = self.assess(dispatch=heartbeat(broker_policy="unknown"))
        self.assertEqual(status, "fault")
        self.assertIn("dispatcher_broker_policy_unknown", reasons)

    def test_broker_required_rejects_missing_ipc(self):
        status, reasons = self.assess(
            live=heartbeat(sample_quality="valid"), broker_required=True)
        self.assertEqual(status, "fault")
        self.assertIn("broker_ipc_unavailable", reasons)

    def test_readonly_broker_requires_fresh_snapshot_and_accepted_poller_data(self):
        self.assertEqual(self.assess(
            live=heartbeat(sample_quality="valid"), broker_required=True,
            broker=broker_healthy()), ("healthy", ()))
        for changes in (
            {"armed": True}, {"read_only": False},
            {"safety_latched": True},
            {"last_valid_timestamp": None},
            {"last_valid_timestamp": (NOW - timedelta(seconds=121)).isoformat()},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.assess(
                    live=heartbeat(sample_quality="valid"),
                    broker_required=True,
                    broker=broker_healthy(**changes))[0], "fault")
        self.assertEqual(self.assess(
            live=heartbeat(), broker_required=True,
            broker=broker_healthy())[0], "fault")

    def test_missing_timestamp_and_bad_structure_are_unknown_not_fresh(self):
        for value in (None, [], {}, {"timestamp": True}, {"timestamp": 123}):
            with self.subTest(value=value):
                self.assertIsNone(observer.age_seconds(value, NOW, 120))

    def test_broker_required_off_never_attempts_ipc(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "dispatch.json").write_text(json.dumps(heartbeat()))
            (d / "live.json").write_text(json.dumps(heartbeat()))
            env = {
                "BROKER_OBSERVER_DISPATCH_PATH": str(d / "dispatch.json"),
                "BROKER_OBSERVER_POLLER_PATH": str(d / "live.json"),
                "BROKER_OBSERVER_SOCKET": str(d / "nonexistent.sock"),
                "BROKER_OBSERVER_REQUIRE_BROKER": "0",
            }
            with mock.patch.object(observer, "broker_status") as broker:
                self.assertEqual(observer.observe(NOW, env), ("healthy", ()))
            broker.assert_not_called()

    def test_non_observe_and_malformed_activation_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "only observe"):
            observer.observe(NOW, {"BROKER_OBSERVER_MODE": "control"})
        with self.assertRaisesRegex(ValueError, "must be 0 or 1"):
            observer.observe(NOW, {"BROKER_OBSERVER_REQUIRE_BROKER": "yes"})


class ObserverIpcTests(unittest.TestCase):
    def test_queries_only_status_over_local_unix_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ipc.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)
            recorded = []

            def respond():
                peer, _ = server.accept()
                with peer:
                    recorded.append(peer.recv(1024))
                    peer.sendall(json.dumps({
                        "ok": True, "result": broker_healthy(),
                    }).encode() + b"\n")

            worker = threading.Thread(target=respond, daemon=True)
            worker.start()
            try:
                self.assertEqual(observer.broker_status(path), broker_healthy())
            finally:
                server.close()
                worker.join(timeout=3)
            self.assertEqual(json.loads(recorded[0])["operation"], "status")
            self.assertEqual(set(json.loads(recorded[0])), {"operation", "timeout_s"})

    def test_missing_socket_is_a_soft_health_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(observer.broker_status(str(Path(tmp) / "missing.sock")))


class ObserverRuntimeTests(unittest.TestCase):
    def test_writes_only_observer_state_and_does_not_restart_or_write_modbus(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            inputs = d / "dispatch.json", d / "live.json"
            for path in inputs:
                path.write_text(json.dumps(heartbeat()))
            state = d / "state.json"
            env = {
                "BROKER_OBSERVER_DISPATCH_PATH": str(inputs[0]),
                "BROKER_OBSERVER_POLLER_PATH": str(inputs[1]),
                "BROKER_OBSERVER_STATE_PATH": str(state),
            }
            self.assertEqual(observer.run_once(NOW, env), 0)
            saved = json.loads(state.read_text())
            self.assertEqual(saved["status"], "healthy")
            self.assertFalse(saved["physical_writes"])
            self.assertEqual(saved["mode"], "observe")
            self.assertEqual(len(list(d.iterdir())), 3)

            # Fault is observational: still returns 0, never invokes a fallback.
            inputs[0].write_text(json.dumps(heartbeat(seconds=200)))
            with self.assertLogs("solinteg.broker_observer", level="WARNING"):
                self.assertEqual(observer.run_once(NOW + timedelta(seconds=1), env), 0)
            saved = json.loads(state.read_text())
            self.assertEqual(saved["status"], "fault")
            self.assertEqual(len(list(d.iterdir())), 3)

    def test_no_extra_disk_write_when_healthy_state_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "dispatch.json").write_text(json.dumps(heartbeat()))
            (d / "live.json").write_text(json.dumps(heartbeat()))
            env = {
                "BROKER_OBSERVER_DISPATCH_PATH": str(d / "dispatch.json"),
                "BROKER_OBSERVER_POLLER_PATH": str(d / "live.json"),
                "BROKER_OBSERVER_STATE_PATH": str(d / "state.json"),
            }
            self.assertEqual(observer.run_once(NOW, env), 0)
            with mock.patch.object(observer, "write_json_atomic") as writer:
                self.assertEqual(observer.run_once(NOW + timedelta(seconds=10), env), 0)
                writer.assert_not_called()

    def test_fault_reminder_and_malformed_previous_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "dispatch.json").write_text(json.dumps(heartbeat(seconds=200)))
            (d / "live.json").write_text(json.dumps(heartbeat()))
            env = {
                "BROKER_OBSERVER_DISPATCH_PATH": str(d / "dispatch.json"),
                "BROKER_OBSERVER_POLLER_PATH": str(d / "live.json"),
                "BROKER_OBSERVER_STATE_PATH": str(d / "state.json"),
            }
            (d / "state.json").write_text(json.dumps({
                "status": "fault", "reasons": None,
                "timestamp": NOW.isoformat(), "last_report_at": NOW.isoformat(),
            }))
            with self.assertLogs("solinteg.broker_observer", level="WARNING"):
                self.assertEqual(observer.run_once(NOW, env), 0)

            with mock.patch.object(observer.log, "warning") as warning:
                self.assertEqual(observer.run_once(NOW + timedelta(seconds=10), env), 0)
                warning.assert_not_called()

            with mock.patch.object(observer.log, "warning") as warning:
                self.assertEqual(observer.run_once(NOW + timedelta(seconds=901), env), 0)
                warning.assert_called_once()

    def test_malformed_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "broken.json"
            file.write_text("{broken")
            self.assertIsNone(observer.read_json(str(file)))
            self.assertIsNone(observer.read_json(str(Path(tmp) / "absent.json")))


if __name__ == "__main__":
    unittest.main()
