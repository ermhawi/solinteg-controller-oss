"""D06 contract for latch state versus independent physical observation.

In-memory poller snapshots only. No inverter, SQLite, systemd or EVCC.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from dispatch_solar_feedback import (  # noqa: E402
    observe_post_apply_general, reconcile_solar_state,
)
from energy_priority_policy import SolarState  # noqa: E402

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
ACK = NOW - timedelta(seconds=90)
PREVIOUS = SolarState(True, NOW - timedelta(minutes=4),
                      NOW - timedelta(seconds=60), 8.0)
PROPOSED = SolarState(True, NOW, NOW - timedelta(seconds=25), 0.0)


def samples(*, mode=0x101, battery=-400, pv=2200, house=900, grid=900):
    return [
        {"timestamp": (NOW - timedelta(seconds=age)).isoformat(),
         "work_mode_raw": mode, "battery_w": battery, "pv_w": pv,
         "house_load_w": house, "grid_w": grid}
        for age in (60, 25)
    ]


class SolarFeedbackContracts(unittest.TestCase):
    def assert_unknown(self, outcome, *, old=PREVIOUS):
        result = reconcile_solar_state(
            old, PROPOSED, requested_action="idle", outcome=outcome,
            acknowledged_idle=True, observation="observed_charging")
        self.assertFalse(result.state.capturing)
        self.assertEqual(result.physical_confirmation, "unknown")

    def test_unapplied_and_pending_outcomes_clear_prior_physical_claim(self):
        for status in (
            "accepted_pending_physical_confirmation", "skipped_divergence",
            "skipped_solar_shortfall", "inhibited_broker_telemetry",
            "error_reverted", "error_revert_failed", "unknown",
        ):
            with self.subTest(status=status):
                self.assert_unknown(status)

    def test_accepted_register_ack_without_post_apply_power_is_unknown(self):
        for acknowledged, observed in (
            (False, False), (False, True), (True, False),
        ):
            with self.subTest(acknowledged=acknowledged, observed=observed):
                result = reconcile_solar_state(
                    PREVIOUS, PROPOSED, requested_action="idle",
                    outcome="applied", acknowledged_idle=acknowledged,
                    observation="observed_charging" if observed else "unknown")
                self.assertFalse(result.state.capturing)
                self.assertEqual(result.physical_confirmation, "unknown")

    def test_needs_apply_false_keeps_only_previously_acknowledged_and_observed(self):
        result = reconcile_solar_state(
            PREVIOUS, PROPOSED, requested_action="idle", outcome=None,
            acknowledged_idle=True, observation="observed_charging")
        self.assertEqual(result.state, PROPOSED)
        self.assertEqual(result.physical_confirmation, "observed_charging")
        missing = reconcile_solar_state(
            PREVIOUS, PROPOSED, requested_action="idle", outcome=None,
            acknowledged_idle=False, observation="unknown")
        self.assertFalse(missing.state.capturing)

    def test_non_idle_or_non_capture_does_not_publish_physical_capture(self):
        for action, proposed in (("discharge", PROPOSED),
                                 ("charge", PROPOSED),
                                 ("idle", SolarState())):
            result = reconcile_solar_state(
                PREVIOUS, proposed, requested_action=action, outcome="applied",
                acknowledged_idle=True, observation="observed_charging")
            self.assertEqual(result.physical_confirmation, "unknown")
            self.assertFalse(result.state.capturing)

    def test_two_fresh_independent_post_ack_general_charging_samples(self):
        self.assertEqual(observe_post_apply_general(samples(), applied_at=ACK, now=NOW),
                         "observed_charging")
        self.assertEqual(observe_post_apply_general(samples(), applied_at=None, now=NOW),
                         "unknown")
        self.assertEqual(observe_post_apply_general(samples(), applied_at=NOW, now=NOW),
                         "unknown")

    def test_observed_general_under_cloud_does_not_claim_charging(self):
        # Valid balance: 2200 W PV + 200 W battery = 900 W house + 1500 W grid.
        evidence = observe_post_apply_general(
            samples(battery=200, grid=1500), applied_at=ACK, now=NOW)
        self.assertEqual(evidence, "observed_general")
        result = reconcile_solar_state(
            PREVIOUS, PROPOSED, requested_action="idle", outcome=None,
            acknowledged_idle=True, observation=evidence)
        self.assertTrue(result.state.capturing)  # retain policy hysteresis
        self.assertEqual(result.physical_confirmation, "observed_general")

    def test_stale_duplicate_mode_and_power_mismatch_are_unknown(self):
        base = samples()
        cases = [
            [base[0]],
            [base[0], base[0]],
            samples(mode=0x303),
            samples(battery=200, grid=300),
            samples(pv=900, grid=900),
            [{**s, "timestamp": (NOW - timedelta(seconds=180)).isoformat()}
             for s in base],
            [base[0], {**base[1], "work_mode_raw": 0x303}],
            [base[0], {**base[1], "battery_w": 200}],
            [base[0], {**base[1], "timestamp": (NOW + timedelta(seconds=20)).isoformat()}],
            base + [{**base[1], "timestamp": (NOW - timedelta(seconds=1)).isoformat(),
                     "work_mode_raw": 0x303}],
            base + [{**base[1], "timestamp": (NOW - timedelta(seconds=1)).isoformat(),
                     "battery_w": 150}],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                self.assertEqual(observe_post_apply_general(rows, applied_at=ACK, now=NOW),
                             "unknown")

    def test_dispatcher_wires_observation_before_log_and_reconciles_after_apply(self):
        path = Path(__file__).resolve().parents[1] / "services" / "dispatch_loop.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        main = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "main")
        calls = {
            node.func.id: node.lineno
            for node in ast.walk(main) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in (
                "observe_post_apply_general", "apply_decision", "reconcile_solar_state",
            )
        }
        self.assertEqual(set(calls), {
            "observe_post_apply_general", "apply_decision", "reconcile_solar_state",
        })
        self.assertLess(calls["observe_post_apply_general"], calls["apply_decision"])
        self.assertLess(calls["apply_decision"], calls["reconcile_solar_state"])
        source = ast.get_source_segment(path.read_text(encoding="utf-8"), main)
        self.assertIn("and not was_in_auto and ARMED", source)
        self.assertIn('numbers["energy_priority_solar_state_quality"]', path.read_text())

    def test_cooldown_state_is_not_physical_capture(self):
        blocked = SolarState(blocked_until=NOW + timedelta(seconds=90))
        result = reconcile_solar_state(
            PREVIOUS, blocked, requested_action="discharge", outcome="applied",
            acknowledged_idle=False, observation="unknown")
        self.assertEqual(result.state, blocked)
        self.assertEqual(result.physical_confirmation, "unknown")


if __name__ == "__main__":
    unittest.main()
