"""D04: decision evidence keeps arbitration and logging separate from apply."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from dispatch_decision_contract import DispatchDecision  # noqa: E402
from energy_priority_policy import SolarState, resolve  # noqa: E402


NOW = datetime(2026, 11, 7, 14, tzinfo=timezone.utc)


class DispatchDecisionContractTests(unittest.TestCase):
    def proposal(self):
        return resolve(
            "charge", 1500, now=NOW, readings=[], state=SolarState(),
            soc_pct=20, reserve_pct=20, save_w=50, recovery=False,
            ev_charging=False)

    def test_compatibility_adapter_preserves_arbitration_outputs(self):
        baseline = self.proposal()
        decision = DispatchDecision.from_priority(
            baseline, origin="energy_priority_policy", data_quality="fresh",
            guard_results=("soc-recovery-checked", "ev-status-observed"),
            requires_register_readback=True, expected_soc_kwh=7.0,
        )
        self.assertEqual((decision.action, decision.power_w, decision.winner, decision.reason),
                         (baseline.action, baseline.power_w, baseline.winner, baseline.reason))
        self.assertEqual(decision.guard_results, ("soc-recovery-checked", "ev-status-observed"))

    def test_log_represents_decision_not_physical_execution(self):
        decision = DispatchDecision.from_priority(
            self.proposal(), origin="energy_priority_policy", data_quality="stale",
            guard_results=("broker-unknown",), requires_register_readback=True,
        )
        fields = decision.log_fields(observed_at=NOW)
        self.assertEqual(fields["schema"], "solinteg-dispatch-decision-v1")
        self.assertEqual(fields["decision_state"], "proposed_unverified")
        self.assertEqual(fields["physical_confirmation"], "unknown")
        self.assertEqual(fields["data_quality"], "stale")
        self.assertTrue(fields["requires_register_readback"])
        self.assertIn("broker-unknown", fields["guard_results"])
        self.assertIsNone(fields["expected_soc_kwh"])
        self.assertEqual(json.loads(json.dumps(fields))["action"], "charge")

    def test_immutable_payload_and_time_requirement(self):
        decision = DispatchDecision.from_priority(
            self.proposal(), origin="optimizer", data_quality="unknown",
            guard_results=(), requires_register_readback=False,
        )
        with self.assertRaises(FrozenInstanceError):
            decision.action = "discharge"
        with self.assertRaises(ValueError):
            decision.log_fields(observed_at=NOW.replace(tzinfo=None))

    def test_invalid_power_provenance_and_nonfinite_soc_fail_closed(self):
        original = self.proposal()
        base = dict(action=original.action, power_w=original.power_w,
                    origin="optimizer", winner=original.winner,
                    reason=original.reason, data_quality="fresh",
                    guard_results=(), requires_register_readback=True)
        for patch in (
            {"power_w": -1}, {"power_w": True}, {"power_w": 100_001},
            {"origin": "/private/key"}, {"origin": "http://sensitive"},
            {"winner": ""}, {"guard_results": ["not-immutable"]},
            {"guard_results": ("../../unknown",)},
            {"expected_soc_kwh": float("nan")}, {"expected_soc_kwh": -1.0},
            {"data_quality": "safe"}, {"requires_register_readback": "yes"},
            {"physical_confirmation": "verified"},
        ):
            with self.subTest(patch=patch):
                with self.assertRaises(ValueError):
                    DispatchDecision(**(base | patch))

    def test_same_evidence_emits_deterministic_log_without_mutating_pure_state(self):
        proposal = self.proposal()
        before = (proposal.state, proposal.action, proposal.power_w)
        decision = DispatchDecision.from_priority(
            proposal, origin="energy_priority_policy", data_quality="unknown",
            guard_results=("soc-not-verified",), requires_register_readback=True,
            solar_skip=True,
        )
        first = decision.log_fields(observed_at=NOW)
        second = decision.log_fields(observed_at=NOW)
        self.assertEqual(first, second)
        self.assertEqual(before, (proposal.state, proposal.action, proposal.power_w))
        self.assertEqual(first["solar_skip"], True)
        self.assertEqual(first["physical_confirmation"], "unknown")


if __name__ == "__main__":
    unittest.main()
