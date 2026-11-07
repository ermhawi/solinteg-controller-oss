"""Offline A01/A02 import/entrypoint inventory tests, no real systemd or /opt."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import subprocess


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "python_reachability", ROOT / "scripts/tools/python_reachability.py")
audit = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(audit)


class ReachabilityTests(unittest.TestCase):
    def test_load_import_graph_and_classify_manual_without_deleting(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            svc = repo / "scripts/services"
            svc.mkdir(parents=True)
            (repo / "scripts/tests").mkdir()
            (repo / "scripts/tools").mkdir()
            (repo / "deploy").mkdir()
            (svc / "main.py").write_text("import worker\n")
            (svc / "worker.py").write_text("def run(): pass\n")
            (svc / "evcc_service.py").write_text("def main(): pass\n")
            (svc / "shadow_probe.py").write_text("def test(): pass\n")
            (svc / "dynamic.py").write_text("import importlib\nimportlib.import_module('worker')\n")
            (repo / "scripts/tests/test_shadow.py").write_text("import shadow_probe\n")
            (repo / "deploy/manual.service").write_text(
                "ExecStart=python3 /opt/test/scripts/services/evcc_service.py\n")
            empty = repo / "runtime-empty"
            result = audit.inventory(repo,
                [{"unit": "live.service", "active": "active", "entrypoints": ["main"]}],
                runtimes=(empty, empty))
            self.assertEqual(result["source_module_count"], 5)
            self.assertEqual(result["reachable_count"], 2)
            rows = {Path(row["path"]).stem: row for row in result["modules"]}
            self.assertTrue(rows["worker"]["reachable_from_loaded_units"])
            self.assertIn("main", rows["worker"]["imported_by"])
            self.assertEqual(rows["evcc_service"]["category"], "manual")
            self.assertEqual(rows["shadow_probe"]["category"], "historical")
            self.assertIn("test_shadow.py", rows["shadow_probe"]
                          ["manual_or_test_references"]["tests"])
            self.assertIn("worker", rows["dynamic"]["literal_dynamic_imports"])
            self.assertTrue(all(row["deletion_proposed"] is False for row in result["modules"]))
            self.assertEqual(result["deletion_candidates"], [])

    def test_full_source_inventory_covers_all_service_modules(self):
        result = audit.inventory(ROOT, [], runtimes=(ROOT / "no-site", ROOT / "no-ev"))
        self.assertGreaterEqual(result["source_module_count"], 79)
        self.assertEqual(result["source_module_count"], len(result["modules"]))
        self.assertEqual(result["candidate_count"], result["source_module_count"])
        self.assertTrue(all(row["category"] in audit.CATEGORIES for row in result["modules"]))
        self.assertFalse(any(row["owner_domain"] == "unassigned" for row in result["modules"]))

    def test_uses_only_sanitized_source_and_unit_names(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "scripts/services").mkdir(parents=True)
            (repo / "scripts/tests").mkdir()
            (repo / "scripts/tools").mkdir()
            (repo / "deploy").mkdir()
            (repo / "scripts/services/secret_free.py").write_text(
                "PASSWORD = 'super_secret'\nURL = 'https://private.example'\n")
            result = audit.inventory(repo, [], runtimes=(repo, repo))
            evidence = json.dumps(result)
            self.assertNotIn("super_secret", evidence)
            self.assertNotIn("private.example", evidence)
            self.assertNotIn(str(repo), evidence)
            self.assertEqual(result["modules"][0]["purpose_origin"], "module-name-fallback")

    def test_privileged_hash_read_is_opt_in_and_never_exposes_secret_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "protected.py"
            with mock.patch.object(Path, "read_bytes", side_effect=PermissionError), \
                 mock.patch.object(audit.subprocess, "run") as runner:
                runner.return_value = subprocess.CompletedProcess(
                    [], 0, stdout="a" * 64 + "  /private/secret.py\n", stderr="")
                self.assertEqual(audit.sha(path)["status"], "not-readable")
                runner.assert_not_called()
                expected = audit.sha(path, sudo_read=True)
                self.assertEqual(expected, {"status": "ok", "sha256": "a" * 64})
                self.assertEqual(runner.call_args.args[0][:3],
                                 ["sudo", "-n", "/usr/bin/sha256sum"])
                self.assertNotIn("private", str(expected))
                runner.return_value = subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="No such file or directory")
                self.assertEqual(audit.sha(path, sudo_read=True)["status"], "missing")

    def test_import_parser_handles_both_literal_and_computed_dynamic_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m.py"
            path.write_text(
                "import common\nfrom evcc_ascii import EvccAscii\n"
                "from importlib import import_module\nimport_module('common')\n"
                "import_module(variable)\n"
            )
            direct, dynamic = audit.imports(path, {"common", "evcc_ascii", "other"})
            self.assertEqual(direct, ["common", "evcc_ascii"])
            self.assertEqual(dynamic, ["<computed>", "common"])


if __name__ == "__main__":
    unittest.main()
