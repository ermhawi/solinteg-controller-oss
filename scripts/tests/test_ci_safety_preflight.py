"""Ensure CI never silently opts into armed hardware access."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "ci_safety_preflight", ROOT / "scripts/tools/ci_safety_preflight.py"
)
guard = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(guard)


class CiSafetyTests(unittest.TestCase):
    def clean_env(self):
        return {**guard.EXPECTED, "GITHUB_ACTIONS": "true"}

    def test_default_hosted_ci_is_sanitized(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(guard.check(self.clean_env(), Path(d),
                                         runtime=(Path(d) / "no-runtime",)), [])

    def test_any_write_gate_or_nonloopback_host_blocks(self):
        for key in ("SOLINTEG_CONTROL_ARMED", "EVCC_CONTROL_ARMED",
                    "EVCC_PRICE_CONTROL_ARMED", "EVCC_MANUAL_TEST",
                    "EVCC_START_APPROVED", "DISPATCH_FREEZE_HOLDS",
                    "DISPATCH_RESERVE_HOLD_VERIFIED", "SOLINTEG_HOLD_ENABLED"):
            with self.subTest(key=key):
                env = {**self.clean_env(), key: "1"}
                with tempfile.TemporaryDirectory() as d:
                    self.assertIn("unsafe-or-missing:" + key,
                                  guard.check(env, Path(d), runtime=()))
        with tempfile.TemporaryDirectory() as d:
            env = {**self.clean_env(), "SOLINTEG_HOST": "private.example"}
            self.assertIn("unsafe-or-missing:SOLINTEG_HOST",
                          guard.check(env, Path(d), runtime=()))

    def test_secrets_and_private_runtime_block(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".env.local").write_text("DO_NOT_PRINT=secret\n")
            missing = guard.check(self.clean_env(), root, runtime=())
            self.assertIn("private-repo-environment-present", missing)
            self.assertNotIn("secret", str(missing))
            (root / ".env.local").unlink()
            private_runtime = root / "private_runtime"
            private_runtime.mkdir()
            self.assertEqual(guard.check(self.clean_env(), root,
                                         runtime=(private_runtime,)),
                             ["private-runtime-mounted"])

    def test_non_ci_execution_and_missing_flag_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            env = self.clean_env()
            del env["EVCC_START_APPROVED"]
            del env["GITHUB_ACTIONS"]
            failures = guard.check(env, Path(d), runtime=())
            self.assertIn("unsafe-or-missing:EVCC_START_APPROVED", failures)
            self.assertIn("not-github-actions", failures)


if __name__ == "__main__":
    unittest.main()
