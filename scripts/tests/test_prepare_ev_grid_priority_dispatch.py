"""Offline-only release patch tests for the known deployed Solinteg dispatcher."""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import prepare_ev_grid_priority_dispatch as prepare

SOURCE = (
    "from __future__ import annotations\n"
    "from zoneinfo import ZoneInfo\n"
    "from l3_charge_guard import observe_l3_house_w, cap_grid_charge_w\n"
    "def main() -> None:\n"
    "    if True:\n"
    "            power_w, detail, numbers, l3_limited = guard_l3_charge(\n"
    "                action, power_w, detail, numbers, iteration_now)\n"
)


class PrepareGridPriorityDispatchTests(unittest.TestCase):
    def test_minimal_patch_keeps_original_dispatch_and_compiles(self):
        source=SOURCE.encode()
        digest=hashlib.sha256(source).hexdigest()
        patched=prepare.patch_source(source,digest).decode()
        self.assertIn("from pathlib import Path\n",patched)
        self.assertIn(prepare.IMPORT_HOOK,patched)
        self.assertIn(prepare.CALL_HOOK,patched)
        self.assertEqual(patched.count("guard_ev_grid_priority("),1)
        self.assertLess(patched.index("guard_ev_grid_priority("),
                        patched.index("power_w, detail, numbers, l3_limited = guard_l3_charge("))
        self.assertEqual(source,SOURCE.encode())

    def test_unknown_baseline_refuses_patch(self):
        with self.assertRaisesRegex(ValueError,"SHA256 changed"):
            prepare.patch_source(SOURCE.encode(),"a"*64)

    def test_repatch_and_anchor_drift_are_rejected(self):
        source=SOURCE.encode()
        digest=hashlib.sha256(source).hexdigest()
        patched=prepare.patch_source(source,digest)
        with self.assertRaisesRegex(ValueError,"already contains"):
            prepare.patch_source(patched,hashlib.sha256(patched).hexdigest())
        no_anchor=source.replace(b"guard_l3_charge(",b"unknown_guard(")
        with self.assertRaisesRegex(ValueError,"anchors changed"):
            prepare.patch_source(no_anchor,hashlib.sha256(no_anchor).hexdigest())

    def test_prepare_file_is_readonly_without_output(self):
        with tempfile.TemporaryDirectory() as t:
            src=Path(t)/"dispatcher.py"
            src.write_text(SOURCE)
            digest=hashlib.sha256(src.read_bytes()).hexdigest()
            result=prepare.prepare_file(src,None,digest)
            self.assertFalse(result["written"])
            self.assertEqual(src.read_text(),SOURCE)
            self.assertNotEqual(result["candidate_sha256"],result["original_sha256"])


if __name__=="__main__":
    unittest.main()
