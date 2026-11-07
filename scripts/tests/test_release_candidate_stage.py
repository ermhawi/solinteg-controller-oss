"""Offline release staging controls: no /opt or hardware access."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/tools"))
spec = importlib.util.spec_from_file_location(
    "stage_release_candidate", ROOT / "scripts/tools/stage_release_candidate.py")
stage = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(stage)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


class StagingTests(unittest.TestCase):
    def test_rejects_parent_escape_absolute_and_symlink_archive(self):
        for path in ("../etc/passwd", "/opt/solinteg/app/dispatch.py", "x/../../evil"):
            self.assertFalse(stage.safe_member(path))
        with tempfile.TemporaryDirectory() as d:
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w") as tar:
                info = tarfile.TarInfo("normal.py")
                info.type = tarfile.SYMTYPE
                info.linkname = "/opt/solinteg/app"
                tar.addfile(info)
            with self.assertRaisesRegex(ValueError, "unsupported Git archive object"):
                stage.extract_regular_source(archive.getvalue(), Path(d))

    def test_rejects_system_installation_and_uncontrolled_root(self):
        for path in (Path("/opt"), Path("/opt/solinteg/app/rollback"),
                     Path("/etc/solinteg/release"), Path.home(), Path("/tmp")):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    stage.assert_private_stage_root(path)

    def test_committed_tree_not_dirty_working_tree_is_staged_private(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.name", "Release Test")
            git(repo, "config", "user.email", "release-test@localhost")
            (repo / "control.py").write_text("SAFE_VERSION = 1\n")
            git(repo, "add", "control.py")
            git(repo, "commit", "-qm", "base")
            (repo / "control.py").write_text("DONT_DEPLOY = 1\n")
            output = root / "staging"
            with mock.patch.object(stage, "validate_source_manifest",
                                   return_value={"status": "pass", "errors": []}), \
                 mock.patch.object(stage, "MODULES", {}), \
                 mock.patch.object(stage, "UNITS", ()), \
                 mock.patch.object(stage, "DEPENDENCIES", ()):
                first = stage.create_candidate(repo, output)
                second = stage.create_candidate(repo, output)
            self.assertNotEqual(first["stage"], second["stage"])
            self.assertEqual(first["content_sha256"], second["content_sha256"])
            manifest = json.loads((first["stage"] / "candidate-manifest.json").read_text())
            self.assertFalse(manifest["release_go"])
            self.assertIn("site-parity-unverified", manifest["stop_reasons"])
            self.assertEqual((first["stage"] / "control.py").read_text(),
                             "SAFE_VERSION = 1\n")
            self.assertEqual((repo / "control.py").read_text(), "DONT_DEPLOY = 1\n")
            self.assertEqual(first["stage"].stat().st_mode & 0o777, 0o700)
            self.assertEqual((first["stage"] / "control.py").stat().st_mode & 0o777, 0o600)
            self.assertEqual((first["stage"] / "candidate-manifest.json").stat().st_mode & 0o777,
                             0o600)

    def test_dependency_inventory_and_sql_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "deploy").mkdir()
            (root / "deploy/schema.sql").write_text(
                "-- CREATE TABLE IF NOT EXISTS is a comment, never a table\n"
                "CREATE TABLE IF NOT EXISTS readings (id INT);\nCREATE TABLE actions(id INT);\n")
            report = stage.template_snapshot(root, {
                "deploy/schema.sql": "a" * 64,
                "deploy/test.service": "b" * 64,
            })
            self.assertEqual(report["schema_tables"], ["actions", "readings"])
            self.assertIn("deploy/test.service", report["unit_templates"])
            self.assertIn("package-lock.json", report["missing_dependencies"])

    def test_public_unit_hashes_only_not_exec_arguments(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "deploy"
            source.mkdir()
            (source / "my.service").write_text("[Unit]\nDescription=Public unit\n")
            live = root / "installed.service"
            live.write_bytes((source / "my.service").read_bytes())
            class Result:
                returncode = 0
                stdout = ("LoadState=loaded\nActiveState=active\n"
                          "FragmentPath=" + str(live) + "\n"
                          "ExecStart=SECRET_TOKEN_UNSAFE\n")
            with mock.patch.object(stage, "UNITS", ("my.service",)):
                report = stage.read_unit_parity(root, runner=lambda *_args, **_kwargs: Result())
            self.assertEqual(report["my.service"]["parity"], "equal")
            self.assertNotIn("SECRET_TOKEN_UNSAFE", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
