"""Verify V01 qualification matrix is explicit and incapable of granting GO."""
from __future__ import annotations

import json
from pathlib import Path
import unittest


MATRIX = Path(__file__).resolve().parents[2] / (
    "docs/operations/physical-qualification-matrix-2026-11-07.json"
)


class PhysicalPlanContractTest(unittest.TestCase):
    def setUp(self):
        self.plan = json.loads(MATRIX.read_text(encoding="utf-8"))

    def test_no_implicit_release_authorization(self):
        guard = self.plan["safety_constraints"]
        self.assertEqual(self.plan["status"], "PREPARED-NO-GO")
        self.assertTrue(guard["approval_required_for_each_release"])
        self.assertFalse(guard["approval_granted"])
        self.assertFalse(guard["physical_test_authorized"])
        self.assertFalse(guard["automatic_rollback_authorized"])
        self.assertTrue(guard["parallel_releases_forbidden"])
        self.assertTrue(guard["effective_native_endsoc_unverified"])

    def test_each_release_has_distinct_owner_stop_rollback_and_observation(self):
        rows = {group["id"]: group for group in self.plan["release_groups"]}
        self.assertEqual(set(rows), {
            "broker-failsafe", "dispatch-policy", "optimizer-and-web", "evcc"
        })
        for name, group in rows.items():
            with self.subTest(release=name):
                self.assertTrue(group["owner_role"])
                self.assertTrue(group["rollback_role"])
                self.assertGreaterEqual(len(group["preconditions"]), 3)
                self.assertGreaterEqual(len(group["observable"]), 3)
                self.assertGreaterEqual(len(group["stop_conditions"]), 3)
                self.assertGreaterEqual(len(group["rollback"]), 3)

    def test_readonly_preflight_requires_both_physical_and_version_evidence(self):
        required = set(self.plan["evidence_fields_required_before_go"])
        for field in (
            "explicit_go_approval_reference",
            "full_installation_backup_hash", "sqlite_consistent_backup_hash",
            "unit_dropin_hashes", "installed_source_hashes",
            "fresh_broker_ownership", "observed_soc", "observed_battery_w",
            "observed_phase_l1_l2_l3", "evcc_manual_pause_state",
            "native_soc_verification",
        ):
            self.assertIn(field, required)


if __name__ == "__main__":
    unittest.main()
