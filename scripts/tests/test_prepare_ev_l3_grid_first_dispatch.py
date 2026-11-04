"""Exact, source-preserving live dispatcher gate patch regression tests."""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from prepare_ev_l3_grid_first_dispatch import (  # noqa: E402
    BASELINE_SHA256, PATCHES, patch_text, prepare,
)


class GridFirstDispatchPatchTests(unittest.TestCase):
    baseline = (
        "def needs_apply(target, last_target, effective_action: str, effective_power: int,\n"
        "                due_for_reassert: bool, *, soc_recovery: bool = False) -> bool:\n"
        "    mid_slot_drift_to_charge = True\n"
        "    if mid_slot_drift_to_charge and not soc_recovery:\n"
        "        return False\n"
        "    small_power_move = (\n"
        "        last_target is not None\n"
        "        and abs(effective_power - last_target[2]) < LIVE_LOAD_DEADBAND_W\n"
        "    )\n"
        "    return not small_power_move\n"
        "\n"
        "def main():\n"
        "    while True:\n"
        "        try:\n"
        "            if needs_apply(target, last_target, effective_action, effective_power, due_for_reassert,\n"
        "                           soc_recovery=bool(numbers.get(\"soc_recovery\"))):\n"
        "                pass\n"
        "        except Exception:\n"
        "            break\n"
    )

    def test_source_reconstructs_exactly_after_reverse_patching(self):
        candidate = patch_text(self.baseline)
        compile(candidate, "<test>", "exec")
        for before, after in reversed(PATCHES):
            self.assertEqual(candidate.count(after), 1)
            candidate = candidate.replace(after, before, 1)
        self.assertEqual(candidate, self.baseline)

    def test_fails_closed_on_drift_and_previously_patched_source(self):
        self.assertEqual(len(BASELINE_SHA256), 64)
        self.assertNotEqual(hashlib.sha256(self.baseline.encode()).hexdigest(), BASELINE_SHA256)
        with self.assertRaisesRegex(ValueError, "dispatcher drift"):
            prepare(self.baseline.encode())
        with self.assertRaisesRegex(ValueError, "already modified"):
            patch_text(patch_text(self.baseline))
        with self.assertRaisesRegex(ValueError, "unexpected dispatcher"):
            patch_text(self.baseline.replace("if mid_slot_drift_to_charge", "if old_guard"))
        with self.assertRaisesRegex(ValueError, "unexpected dispatcher"):
            patch_text(self.baseline + self.baseline)


if __name__ == "__main__":
    unittest.main()
