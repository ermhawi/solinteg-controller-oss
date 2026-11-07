"""Prove planner hold cannot implicitly enable an executor hardware hold.

Offline-only, all inverter calls are mocked. Separate hardware-experiment
flags are intentionally not controlled by the planning hold option.
"""
from __future__ import annotations

from pathlib import Path
import sys
import ast
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "services"))
from fakes import install_pymodbus_stub

install_pymodbus_stub()
import dispatch_loop as dispatch  # noqa: E402


class HoldIsolationTests(unittest.TestCase):
    def test_legacy_planner_hold_never_invokes_freeze_or_force_hold(self):
        inv = object()
        with mock.patch.object(dispatch, "return_to_auto") as auto, \
             mock.patch.object(dispatch, "force_hold") as soft, \
             mock.patch.object(dispatch, "freeze_battery") as freeze:
            dispatch.apply_target(inv, "hold", 0)
        auto.assert_called_once_with(inv)
        soft.assert_not_called()
        freeze.assert_not_called()

    def test_physical_freeze_and_reserve_hold_flags_are_disabled_by_default(self):
        source = ast.parse((HERE.parent / "services" / "dispatch_loop.py").read_text())
        values = {}
        for statement in source.body:
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name) and target.id in (
                        "FREEZE_HOLDS", "RESERVE_HOLD_VERIFIED"
                    ):
                        values[target.id] = ast.unparse(statement.value)
        self.assertEqual(set(values), {"FREEZE_HOLDS", "RESERVE_HOLD_VERIFIED"})
        self.assertIn("'DISPATCH_FREEZE_HOLDS', '0'", values["FREEZE_HOLDS"])
        self.assertIn("'DISPATCH_RESERVE_HOLD_VERIFIED', '0'",
                      values["RESERVE_HOLD_VERIFIED"])

    def test_unverified_reserve_never_invents_open_cap_hold(self):
        from datetime import datetime, timezone

        with mock.patch.object(dispatch, "RESERVE_HOLD_VERIFIED", False), \
             mock.patch.object(dispatch, "read_live_soc_kwh",
                               return_value=dispatch.BATTERY_KWH * 0.16), \
             mock.patch.object(dispatch, "read_live_solar_surplus_w", return_value=0), \
             mock.patch.object(dispatch, "SOC_RECOVERY_ENTRY_PCT", 13.0), \
             mock.patch.object(dispatch, "SOC_RECOVERY_EXIT_PCT", 16.0), \
             mock.patch.object(dispatch, "_soc_recovery_active", False):
            action, _, _, _, _, flags = dispatch.prioritize_soc_recovery(
                "idle", 0, None, False, "", {},
                datetime.now(timezone.utc)
            )
        self.assertNotEqual(action, "reserve_hold")
        self.assertNotIn("reserve_hold", flags)


if __name__ == "__main__":
    unittest.main()
