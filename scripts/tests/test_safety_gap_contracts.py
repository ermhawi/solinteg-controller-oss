"""Source-only safety gap reproduction. NEVER contacts an inverter or live IPC."""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
SERVICES = HERE.parent / "services"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SERVICES))

from fakes import install_pymodbus_stub  # noqa: E402

install_pymodbus_stub()
import watchdog as watchdog  # noqa: E402
from modbus_broker import Broker  # noqa: E402
from modbus_broker_control_service import role_for_systemd_peer  # noqa: E402


class WatchdogContractTests(unittest.TestCase):
    def test_missing_or_unreadable_heartbeat_fails_open_without_revert(self):
        with tempfile.TemporaryDirectory() as d:
            heartbeat = Path(d) / "nonexistent.json"
            with mock.patch.object(watchdog, "HEARTBEAT_PATH", str(heartbeat)), \
                 mock.patch.object(watchdog, "STATE_PATH", str(Path(d) / "state.json")), \
                 mock.patch.object(watchdog, "ARMED", True), \
                 mock.patch.object(watchdog, "attempt_revert") as revert, \
                 mock.patch.object(watchdog.notify, "send") as alert:
                self.assertEqual(watchdog.main(), 0)
                heartbeat.write_text("{malformed", encoding="utf-8")
                self.assertEqual(watchdog.main(), 0)
                revert.assert_not_called()
                alert.assert_not_called()

    def test_fresh_heartbeat_masks_explicit_failed_apply(self):
        with tempfile.TemporaryDirectory() as d:
            heartbeat = Path(d) / "heartbeat.json"
            heartbeat.write_text(json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "result": "error_revert_failed",
                "broker_policy": "inhibit",
            }), encoding="utf-8")
            with mock.patch.object(watchdog, "HEARTBEAT_PATH", str(heartbeat)), \
                 mock.patch.object(watchdog, "STATE_PATH", str(Path(d) / "state.json")), \
                 mock.patch.object(watchdog, "ARMED", True), \
                 mock.patch.object(watchdog, "attempt_revert") as revert, \
                 mock.patch.object(watchdog.notify, "send") as alert:
                self.assertEqual(watchdog.main(), 0)
                revert.assert_not_called()
                alert.assert_not_called()

    def test_dispatch_main_writes_heartbeat_after_iteration_exception_handler(self):
        """AST structural guard; does not claim physical or end-to-end coverage."""
        tree = ast.parse((SERVICES / "dispatch_loop.py").read_text(encoding="utf-8"))
        main = next(n for n in tree.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "main")
        candidates = []
        for node in ast.walk(main):
            if not isinstance(node, (ast.While, ast.For)):
                continue
            for i, stmt in enumerate(node.body):
                if not isinstance(stmt, ast.Try) or not any(
                    isinstance(h.type, ast.Name) and h.type.id == "Exception"
                    for h in stmt.handlers if h.type is not None
                ):
                    continue
                for after in node.body[i + 1:]:
                    if any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                           and call.func.id == "write_heartbeat" for call in ast.walk(after)):
                        candidates.append(True)
        self.assertTrue(candidates, "heartbeat must be recorded after the caught failed iteration")


class SafetyBrokerContractTests(unittest.TestCase):
    def test_control_runtime_denies_safety_auto_before_any_transport(self):
        constructed = mock.Mock(side_effect=AssertionError("no transport allowed"))
        broker = Broker(constructed, runtime_mode="control", armed=True,
                        transactions_enabled=True)
        try:
            with self.assertRaisesRegex(PermissionError, "control runtime operation is not enabled"):
                broker.submit("safety_auto", role="safety", request_id="reproduce")
            constructed.assert_not_called()
        finally:
            broker.close()

    def test_live_systemd_peer_mapping_does_not_authorize_safety_role(self):
        with mock.patch("modbus_broker_control_service.Path.read_text",
                        return_value="0::/system.slice/solinteg-safety-watchdog.service\n"):
            self.assertIsNone(role_for_systemd_peer(101, os.getuid(), os.getgid()))
        with mock.patch("modbus_broker_control_service.Path.read_text",
                        return_value="0::/system.slice/solinteg-watchdog.service\n"):
            self.assertIsNone(role_for_systemd_peer(101, os.getuid(), os.getgid()))


if __name__ == "__main__":
    unittest.main()
