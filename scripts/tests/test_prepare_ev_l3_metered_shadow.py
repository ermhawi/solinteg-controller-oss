"""The read-only sidecar patch must be SHA-locked, additive and never arm EVCC."""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from prepare_ev_l3_metered_shadow import (  # noqa: E402
    BASELINE_SHA256, IMPORT_ADD, RESULT_ADD, patch_text, prepare,
)


class MeteredSidecarPatchTests(unittest.TestCase):
    def setUp(self):
        self.baseline = (
            "from ev_grid_priority_idle_strategy import evaluate as evaluate_idle\n"
            "def inspect(live_path, evcc_path, controls_path, gate_path, now):\n"
            "    result = {\n"
            "        'device_writes': 0,\n"
            "    }\n"
            "    try:\n"
            "        try:\n"
            "            gate = json.loads(gate_path.read_text(encoding=\"utf-8\"))\n"
            "        except FileNotFoundError:\n"
            "            result.update(state='disabled')\n"
            "            return result\n"
            "    except Exception:\n"
            "        return result\n"
        )

    def test_patch_adds_only_import_and_independent_observation(self):
        candidate = patch_text(self.baseline)
        self.assertIn(IMPORT_ADD, candidate)
        self.assertIn(RESULT_ADD, candidate)
        self.assertEqual(
            candidate.replace(IMPORT_ADD, "").replace(RESULT_ADD, ""),
            self.baseline,
        )
        self.assertLess(candidate.index('result["metered_policy"]'),
                        candidate.index("gate = json.loads"))
        compile(candidate, "<sidecar-patch-test>", "exec")

    def test_unrecognized_baseline_sha_never_prepared(self):
        self.assertEqual(len(BASELINE_SHA256), 64)
        with self.assertRaisesRegex(ValueError, "observer drift"):
            prepare(self.baseline.encode())
        self.assertNotEqual(hashlib.sha256(self.baseline.encode()).hexdigest(),
                            BASELINE_SHA256)

    def test_patch_fails_closed_on_repeated_or_unknown_anchor(self):
        for source in (self.baseline.replace("gate = json.loads", "gate = dict"),
                       self.baseline + self.baseline,
                       self.baseline + IMPORT_ADD,
                       self.baseline + RESULT_ADD):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    patch_text(source)


if __name__ == "__main__":
    unittest.main()
