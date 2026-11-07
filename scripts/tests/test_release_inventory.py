"""Offline-only tests for the sanitized release inventory (B03, R01, R02)."""
from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "release_inventory", ROOT / "scripts/tools/release_inventory.py"
)
inventory = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(inventory)


class InventoryTests(unittest.TestCase):
    def test_source_manifest_validation_is_safe_and_complete(self):
        report = inventory.validate_source_manifest(ROOT)
        self.assertEqual(report["status"], "pass", report)
        self.assertEqual(report["module_count"], 24)
        self.assertEqual(report["errors"], [])

    def test_manifest_validation_rejects_missing_unsafe_and_duplicate_paths(self):
        bad = {
            "inverter": ("scripts/services/missing_control.py", "../private/file.env",
                         "scripts/services/watchdog.py"),
            "evcc": ("scripts/services/watchdog.py",),
        }
        report = inventory.validate_source_manifest(ROOT, modules=bad)
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(
            {"missing-source", "unsafe-source-path", "duplicate-source-path"}
            <= {e["code"] for e in report["errors"]}
        )
        self.assertNotIn("../private/file.env", json.dumps(report))

    def test_manifest_validation_fails_closed_on_new_local_ev_import(self):
        with tempfile.TemporaryDirectory() as d:
            site = Path(d)
            services = site / "scripts/services"
            services.mkdir(parents=True)
            (services / "ev.py").write_text("import helper\n")
            (services / "helper.py").write_text("pass\n")
            result = inventory.validate_source_manifest(
                site, modules={"evcc": ("scripts/services/ev.py",)}, units=())
        self.assertEqual(result["status"], "blocked")
        self.assertIn({"code": "unlisted-ev-import", "path": "scripts/services/helper.py"},
                      result["errors"])

    def test_ev_manifest_covers_active_entrypoints_and_all_local_imports(self):
        service_dir = ROOT / "scripts" / "services"
        included = {Path(path).name for path in inventory.MODULES["evcc"]}
        entrypoints = {"ev_price_controller.py", "evcc_observer.py",
                       "ev_history.py", "ha_ev_session.py"}
        self.assertTrue(entrypoints <= included)
        for name in included:
            tree = ast.parse((service_dir / name).read_text(encoding="utf-8"))
            module_names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    module_names.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    module_names.add(node.module.split(".")[0])
            local = {n + ".py" for n in module_names if (service_dir / (n + ".py")).is_file()}
            self.assertTrue(local <= included, f"{name}: missing imports {local - included}")

    def test_hash_parity_states(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source, installed = root / "source", root / "installed"
            source.write_text("baseline")
            installed.write_text("baseline")
            self.assertEqual(inventory.parity(source, installed)["parity"], "equal")
            installed.write_text("patched")
            self.assertEqual(inventory.parity(source, installed)["parity"], "different")
            installed.unlink()
            self.assertEqual(inventory.parity(source, installed)["parity"], "missing")
            source.unlink()
            self.assertEqual(inventory.parity(source, installed)["parity"], "source-missing")

    def test_systemd_exec_and_paths_are_sanitized(self):
        raw = (
            "LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=42\n"
            "FragmentPath=/private/secret/solinteg-dispatch.service\n"
            "DropInPaths=/private/secret/40-energy-priority.conf\n"
            "ExecStart={ path=/opt/private/bin/python ; argv[]=/opt/private/bin/python "
            "/opt/solinteg/app/scripts/services/dispatch_loop.py "
            "--token=VERY_SECRET --url=http://10.0.4.3 ; ignore_errors=no ; }\n"
        )
        def fake_run(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 0, stdout=raw)
        public, pid, _ = inventory.read_unit("solinteg-dispatch.service", runner=fake_run)
        output = json.dumps(public)
        self.assertEqual(pid, 42)
        self.assertEqual(public["exec_binary"], "python")
        self.assertEqual(public["exec_entry"], "dispatch_loop.py")
        self.assertEqual(public["drop_ins"], ["40-energy-priority.conf"])
        for secret in ("VERY_SECRET", "10.0.4.3", "/private/", "/opt/"):
            self.assertNotIn(secret, output)

    def test_flag_provenance_ignores_other_environment_entries(self):
        with tempfile.TemporaryDirectory() as d:
            unit = Path(d) / "unit.service"
            env = Path(d) / "secrets.env"
            env.write_text("API_TOKEN=keep_me_secret\nDISPATCH_ENERGY_PRIORITY=1\n")
            unit.write_text(
                "Environment=DISPATCH_ENERGY_PRIORITY=0 API_TOKEN=secret_two\n"
                + "EnvironmentFile=" + str(env) + "\n"
            )
            result = inventory.extract_assignments([("unit", unit)], {"DISPATCH_ENERGY_PRIORITY"})
            self.assertEqual(result["DISPATCH_ENERGY_PRIORITY"][:2], ("1", "environment-file"))
            self.assertNotIn("API_TOKEN", json.dumps(result))
            self.assertNotIn("keep_me_secret", json.dumps(result))

    def test_process_flags_accept_only_named_switches(self):
        with patch.object(inventory.Path, "read_bytes", return_value=(
            b"SECRET_TOKEN=sensitive\0SOLINTEG_CONTROL_ARMED=1\0PRIVATE_IP=10.0.0.1\0"
        )):
            status, values = inventory.process_flags(52, {"SOLINTEG_CONTROL_ARMED"})
        self.assertEqual(status, "ok")
        self.assertEqual(values, {"SOLINTEG_CONTROL_ARMED": "1"})

    def test_source_default_from_code(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.py").write_text('x = os.environ.get("MODE", "direct")\n')
            self.assertEqual(inventory.source_default(root, "MODE", "a.py",
                                                     ("direct", "broker")), "direct")
            self.assertIsNone(inventory.source_default(root, "MISSING", "a.py", ("0", "1")))

    def test_unknown_flag_value_cannot_leave_host(self):
        raw = (
            "LoadState=loaded\nActiveState=active\nSubState=running\n"
            "MainPID=52\nFragmentPath=\nDropInPaths=\nExecStart=\n"
        )
        def fake_run(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 0, stdout=raw)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(inventory, "UNITS", ("solinteg-dispatch.service",)), \
                 patch.object(inventory, "MODULES", {}), \
                 patch.object(inventory, "process_flags", return_value=(
                     "ok", {"SOLINTEG_CONTROL_ARMED": "secret-as-value",
                            "SOLINTEG_MODBUS_SOURCE": "broker_transaction"}
                 )), patch.object(inventory, "git_head", return_value="f" * 40):
                result = inventory.snapshot(root, root, root, runner=fake_run)
        out = json.dumps(result)
        self.assertNotIn("secret-as-value", out)
        self.assertEqual(result["flags"]["solinteg-dispatch.service:SOLINTEG_CONTROL_ARMED"]
                         ["effective"], "unrecognized")
        self.assertEqual(result["flags"]["solinteg-dispatch.service:SOLINTEG_MODBUS_SOURCE"]
                         ["effective"], "broker_transaction")
        self.assertEqual(result["repo_head"], "f" * 40)

    def test_test_summary_whitelists_counters_and_rejects_bool(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "results.json"
            p.write_text(json.dumps({"python": {"passed": 1617, "failed": False,
                                                "secret": "private"}, "unknown": "secret"}))
            self.assertEqual(inventory.read_test_summary(p), {"python": {"passed": 1617}})

    def test_two_snapshots_diff_without_timestamps_or_secret_values(self):
        first = {
            "repo_head": "a" * 40,
            "modules": [{"chain": "inverter", "path": "scripts/services/dispatch_loop.py",
                         "source_sha256": "a", "installed_sha256": "a", "parity": "equal"}],
            "units": {}, "flags": {}, "captured_at_utc": "2026-01-01",
        }
        second = {**first, "captured_at_utc": "2026-11-07"}
        with patch.object(inventory, "UNITS", ()), patch.object(inventory, "FLAGS", {}):
            same = inventory.compare_snapshots(first, second)
            self.assertEqual(same["status"], "equal")
            second = {**first, "modules": [{**first["modules"][0],
                                            "installed_sha256": "SECRET_PRIVATE_VALUE",
                                            "parity": "different"}]}
            diff = inventory.compare_snapshots(first, second)
        self.assertEqual(diff["status"], "different")
        self.assertEqual(diff["changes"], [
            {"kind": "module", "id": "scripts/services/dispatch_loop.py"}])
        self.assertNotIn("SECRET_PRIVATE_VALUE", json.dumps(diff))

    def test_manifest_gate_blocks_even_stable_site_divergence(self):
        current = {
            "repo_head": "a" * 40, "repo_dirty": False,
            "modules": [{"path": "scripts/services/dispatch_loop.py",
                         "source_sha256": "a", "installed_sha256": "b",
                         "parity": "different"}], "units": {}, "flags": {},
        }
        with patch.object(inventory, "UNITS", ()), patch.object(inventory, "FLAGS", {}):
            outcome = inventory.compare_snapshots(current, current)
            self.assertEqual(outcome["status"], "equal")
            self.assertEqual(outcome["manifest_gate"], "blocked")
            self.assertEqual(outcome["unreconciled_modules"], [
                "scripts/services/dispatch_loop.py"])
            clean = {**current, "modules": [{**current["modules"][0],
                                              "installed_sha256": "a", "parity": "equal"}]}
            self.assertEqual(inventory.compare_snapshots(clean, clean)["manifest_gate"],
                             "pass")

    def test_inaccessible_installation_is_not_claimed_equal(self):
        current = {
            "repo_head": "a" * 40,
            "modules": [{"path": "scripts/services/dispatch_loop.py",
                         "source_sha256": "a", "installed_sha256": None, "parity": "not-readable"}],
            "units": {}, "flags": {},
        }
        with patch.object(inventory, "UNITS", ()), patch.object(inventory, "FLAGS", {}):
            result = inventory.compare_snapshots(current, current)
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["runtime_complete"])

    def test_rollback_reference_rejects_paths(self):
        self.assertIsNone(inventory.git_head(Path("/path/that/does/not/exist")))
        self.assertFalse(inventory.SAFE_NAME.fullmatch("/private/archive"))
        self.assertFalse(inventory.SAFE_SHA.fullmatch("not-a-sha"))


if __name__ == "__main__":
    unittest.main()
