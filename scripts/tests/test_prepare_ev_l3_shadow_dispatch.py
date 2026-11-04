"""SHA-pinned offline EV/L3 shadow candidate does not alter control decisions."""
from __future__ import annotations

import ast
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from prepare_ev_l3_shadow_dispatch import CALL_ANCHOR, HOOK, patch_source  # noqa: E402


def baseline() -> bytes:
    return (
        "import os\nimport common\n\n"
        "def sample():\n"
        "    while True:\n"
        "        try:\n"
        "            action, power_w = 'discharge', 2300\n"
        "            detail, numbers = '', {}\n"
        "            iteration_now = None\n"
        "            power_w, detail, numbers, l3_limited = guard_l3_charge(\n"
        "                action, power_w, detail, numbers, iteration_now)\n"
        "        except Exception:\n"
        "            pass\n"
    ).encode("utf-8")


class EvL3ShadowCandidateTests(unittest.TestCase):
    def test_exact_hash_source_gets_a_compileable_log_only_hook(self):
        original = baseline()
        patched = patch_source(original, hashlib.sha256(original).hexdigest())
        ast.parse(patched)
        self.assertEqual(patched.count(HOOK.encode()), 1)
        self.assertEqual(patched.replace(HOOK.encode(), b"", 1), original)
        self.assertLess(patched.index(HOOK.encode()), patched.index(CALL_ANCHOR.encode()))
        self.assertNotIn("guard_dispatch(", HOOK)
        self.assertNotIn("write_register", HOOK)
        self.assertIn('from ev_l3_metered_policy import assess', HOOK)
        self.assertNotIn('gate=', HOOK)
        self.assertIn('DISPATCH_EV_L3_SHADOW_LOG', HOOK)

    def test_unknown_sha_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "baseline SHA256"):
            patch_source(baseline(), "0" * 64)

    def test_changed_or_duplicate_anchor_is_rejected(self):
        for content in (baseline().replace(CALL_ANCHOR.encode(), b""),
                        baseline() + CALL_ANCHOR.encode()):
            with self.subTest(content_size=len(content)):
                with self.assertRaisesRegex(ValueError, "hook anchor"):
                    patch_source(content, hashlib.sha256(content).hexdigest())

    def test_missing_imports_are_rejected(self):
        content = baseline().replace(b"import common\n", b"")
        with self.assertRaisesRegex(ValueError, "required imports"):
            patch_source(content, hashlib.sha256(content).hexdigest())


if __name__ == "__main__":
    unittest.main()
