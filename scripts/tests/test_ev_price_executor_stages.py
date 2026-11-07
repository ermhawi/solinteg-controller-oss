"""E02: four-phase EV price executor contracts, fully offline FC10 fakes.

Only private TemporaryDirectory snapshots and existing FakeEvcc, never real
EVCC Modbus. Golden master comparisons are source-compatibility tests.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "services")]

import ev_price_controller as controller
import evcc_cable_tracker as cables
import ev_price_pause_owner as owners
import test_ev_price_pause_owner as fixtures


class PriceExecutorStagesTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OwnershipTests(methodName="runTest")
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_locked_cable_preflight_is_pure_and_conservative(self):
        original = cables.read(self.fixture.cable)
        evcc = json.loads(self.fixture.evcc.read_text())
        self.assertTrue(controller.cable_matches_locked_preflight(original, original, evcc))
        for modified in (
            None, {**original, "connection_epoch": original["connection_epoch"] + 1},
            {**original, "coverage_epoch": original["coverage_epoch"] + 1},
            {**original, "tracking_id": "other"}, {**original, "observed_state": "B2"},
        ):
            with self.subTest(changed=modified):
                self.assertFalse(controller.cable_matches_locked_preflight(
                    original, modified, evcc))
        self.assertEqual(cables.read(self.fixture.cable), original)

    def test_locked_settings_preflight_checks_exact_intent(self):
        session = {
            "settings_revision": "revision-123", "charge_mode": "economy",
            "departure_local_time": "07:00", "max_buy_ore_per_kwh": None,
        }
        current = {
            "revision": "revision-123", "charge_mode": "economy",
            "departure_local_time": "07:00", "max_buy_ore_per_kwh": None,
        }
        self.assertTrue(controller.intent_matches_locked_preflight(current, session))
        for key, value in (
            ("revision", "other"), ("charge_mode", "deadline"),
            ("departure_local_time", "10:00"), ("max_buy_ore_per_kwh", 80.0),
        ):
            with self.subTest(key=key):
                self.assertFalse(controller.intent_matches_locked_preflight(
                    {**current, key: value}, session))

    def test_existing_pause_resume_golden_is_exactly_one_fc10_per_iteration(self):
        first = self.fixture.invoke(price=fixtures.EXPENSIVE)
        self.assertEqual((first["state"], first["writes"], first["configured_raw"]),
                         ("pause", 1, 1000))
        self.assertEqual(len(self.fixture.driver.calls), 1)
        owner = owners.read_owner(self.fixture.owner)
        self.assertEqual(owner["state"], "owned")
        self.fixture.set_paused()
        next_step = self.fixture.invoke(price=fixtures.CHEAP)
        self.assertEqual((next_step["state"], next_step["writes"], next_step["configured_raw"]),
                         ("resume", 1, 100))
        self.assertEqual(len(self.fixture.driver.calls), 2)
        self.assertEqual(owners.read_owner(self.fixture.owner)["state"], "released")

    def test_orchestrator_never_calls_raw_fc10_outside_explicit_stage(self):
        import ast
        path = HERE.parent / "services" / "ev_price_controller.py"
        tree = ast.parse(path.read_text())
        functions = {
            fn.name: fn for fn in tree.body
            if isinstance(fn, ast.FunctionDef)
        }
        self.assertIn("run_once", functions)
        self.assertIn("choose_action", functions)
        self.assertIn("execute_locked_fc10", functions)
        self.assertIn("publish_controller_status", functions)
        calls = [fn.func for fn in ast.walk(functions["run_once"])
                 if isinstance(fn, ast.Call)]
        self.assertTrue(any(isinstance(fn, ast.Name)
                            and fn.id == "execute_locked_fc10" for fn in calls))
        self.assertFalse(any(isinstance(fn, ast.Attribute)
                             and fn.attr == "set_limit_raw" for fn in calls))
        physical_calls = [fn.func for fn in ast.walk(functions["execute_locked_fc10"])
                          if isinstance(fn, ast.Call)]
        self.assertEqual(sum(isinstance(fn, ast.Attribute)
                             and fn.attr == "set_limit_raw" for fn in physical_calls), 1)
        self.assertFalse(any(isinstance(fn, ast.Attribute)
                             and fn.attr == "set_limit_raw"
                             for fn in ast.walk(functions["choose_action"])
                             if isinstance(fn, ast.Call)))

    def test_unowned_manual_pause_never_reaches_fc10_executor(self):
        self.fixture.set_paused()
        got = self.fixture.invoke(price=fixtures.CHEAP)
        self.assertEqual((got["state"], got["writes"]), ("inhibited", 0))
        self.assertEqual(self.fixture.driver.calls, [])
        self.assertFalse(self.fixture.owner.exists())

    def test_uncertain_write_keeps_pre_fc10_durable_owner(self):
        from evcc_ascii import EvccUncertainWrite
        self.fixture.driver.fail = True
        with self.assertRaises(EvccUncertainWrite):
            self.fixture.invoke(price=fixtures.EXPENSIVE)
        self.assertEqual(len(self.fixture.driver.calls), 1)
        self.assertEqual(owners.read_owner(self.fixture.owner)["state"], "pending")


if __name__ == "__main__":
    unittest.main()
