"""Offline-only EV writer ownership inventory; documents a remaining lock gap."""
from __future__ import annotations

import ast
from pathlib import Path
import unittest


SERVICES = Path(__file__).resolve().parents[1] / "services"


class EvWriterOwnershipTests(unittest.TestCase):
    def test_scheduled_price_writer_uses_owner_and_serial_locks_before_fc10(self):
        tree = ast.parse((SERVICES / "ev_price_controller.py").read_text())
        run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_once")
        calls = [n for n in ast.walk(run) if isinstance(n, ast.Call)]
        lock_lines = [n.lineno for n in calls if isinstance(n.func, ast.Attribute)
                      and n.func.attr == "flock"]
        write_lines = [n.lineno for n in calls if isinstance(n.func, ast.Attribute)
                       and n.func.attr == "set_limit_raw"]
        self.assertGreaterEqual(len(lock_lines), 2)
        self.assertEqual(len(write_lines), 1)
        self.assertLess(sorted(lock_lines)[1], write_lines[0])

    def test_manual_cli_is_separately_gated_but_does_not_take_shared_flock(self):
        tree = ast.parse((SERVICES / "evcc_service.py").read_text())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        source = ast.unparse(main)
        self.assertIn("'EV_ENABLED'", source)
        self.assertIn('args.confirm', source)
        self.assertIn('args.expected_raw', source)
        calls = [n for n in ast.walk(main) if isinstance(n, ast.Call)]
        self.assertTrue(any(isinstance(n.func, ast.Attribute)
                            and n.func.attr == "set_limit_raw" for n in calls))
        self.assertFalse(any(isinstance(n.func, ast.Attribute)
                             and n.func.attr == "flock" for n in calls),
                         "Manual CLI bypass is a known risk, not proven safe")


if __name__ == "__main__":
    unittest.main()
