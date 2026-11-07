"""Pure alarm classification and dedup tests, no notifier or hardware."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from safety_alert_contract import SIGNALS, classify, SCHEMA  # noqa: E402


class SafetyAlertTests(unittest.TestCase):
    def test_all_known_critical_cases_have_stable_identity_and_operator_action(self):
        self.assertEqual(len(SIGNALS), 7)
        for code, spec in SIGNALS.items():
            with self.subTest(code=code):
                report = classify(code, True, source=spec.producer)
                self.assertEqual(report["schema"], SCHEMA)
                self.assertEqual(report["dedup_key"], "solinteg:" + code)
                self.assertTrue(report["emit_new_alert"])
                self.assertTrue(report["owner"])
                self.assertTrue(report["response"])
                self.assertEqual(report["severity"], "critical")

    def test_repeated_fault_does_not_create_another_alert(self):
        code = "ems_state_uncertain"
        src = SIGNALS[code].producer
        initial = classify(code, True, source=src, last_state="unknown")
        repeated = classify(code, True, source=src, last_state="active")
        self.assertEqual(initial["dedup_key"], repeated["dedup_key"])
        self.assertTrue(initial["emit_new_alert"])
        self.assertFalse(repeated["emit_new_alert"])

    def test_unknown_never_clears_and_clear_is_explicit(self):
        code = "soc_under_reserve"
        src = SIGNALS[code].producer
        uncertain = classify(code, None, source=src, last_state="active")
        self.assertEqual(uncertain["signal_state"], "unknown")
        self.assertFalse(uncertain["clear_active"])
        self.assertFalse(uncertain["emit_new_alert"])
        recovered = classify(code, False, source=src, last_state="active")
        self.assertTrue(recovered["clear_active"])
        self.assertFalse(recovered["emit_new_alert"])

    def test_unsupported_provenance_and_previous_state_rejected(self):
        code = "broker_owner_unknown"
        with self.assertRaises(ValueError):
            classify(code, True, source="dispatcher")
        with self.assertRaises(ValueError):
            classify(code, True, source=SIGNALS[code].producer,
                     last_state="unverified")
        with self.assertRaises(ValueError):
            classify("arbitrary", True, source="anything")

    def test_false_is_not_an_active_alarm(self):
        code = "ev_pause_owner_uncertain"
        report = classify(code, False, source=SIGNALS[code].producer)
        self.assertEqual(report["signal_state"], "clear")
        self.assertFalse(report["emit_new_alert"])


if __name__ == "__main__":
    unittest.main()
