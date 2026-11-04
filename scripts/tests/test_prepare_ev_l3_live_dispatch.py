"""Installed dispatcher patch is additive and locked to one exact Dell baseline."""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from prepare_ev_l3_live_dispatch import (  # noqa: E402
    BASELINE_SHA256, HOOK_ADD, IMPORT_ADD, PATH_IMPORT, patch_text, prepare,
)


class LiveDispatchPatchTests(unittest.TestCase):
    baseline = (
        "from datetime import datetime, timedelta\n"
        "from l3_charge_guard import observe_l3_house_w, cap_grid_charge_w\n"
        "def main():\n"
        "    while True:\n"
        "            power_w, detail, numbers, l3_limited = guard_l3_charge(\n"
        "                action, power_w, detail, numbers, iteration_now)\n"
    )

    def test_additive_hook_is_before_phase_charge_guard(self):
        patched = patch_text(self.baseline)
        self.assertIn(PATH_IMPORT, patched)
        self.assertIn(IMPORT_ADD, patched)
        self.assertIn(HOOK_ADD, patched)
        self.assertEqual(patched.replace(PATH_IMPORT, "").replace(IMPORT_ADD, "").replace(HOOK_ADD, ""),
                         self.baseline)
        self.assertLess(patched.index("coordinate_ev_grid_live("),
                        patched.index("power_w, detail, numbers, l3_limited"))
        compile(patched, "<test>", "exec")

    def test_baseline_sha_rejects_any_unreviewed_dispatcher(self):
        self.assertEqual(len(BASELINE_SHA256), 64)
        self.assertNotEqual(hashlib.sha256(self.baseline.encode()).hexdigest(),
                            BASELINE_SHA256)
        with self.assertRaisesRegex(ValueError, "dispatcher drift"):
            prepare(self.baseline.encode())

    def test_unknown_or_repeated_anchor_refused(self):
        for source in (self.baseline * 2,
                       self.baseline.replace("guard_l3_charge(", "other("),
                       self.baseline.replace("from l3_charge_guard", "from old_l3_guard"),
                       self.baseline + HOOK_ADD,
                       self.baseline + IMPORT_ADD,
                       self.baseline + PATH_IMPORT):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    patch_text(source)


if __name__ == "__main__":
    unittest.main()
