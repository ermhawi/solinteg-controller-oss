"""Tests for scripts/services/backup.py and backup_offsite.py.

Both were changed 2026-09-03 after the offsite mirror hit 100% of Backblaze's free 10 GB storage
cap while the bucket listed only 21 files. B2 is versioned and rclone's B2 backend HIDES a
deleted file rather than removing it, so the nightly rotation was propagating deletes that B2
kept billing for — one whole snapshot per night, forever.

Two things are pinned here, because both are the kind of quiet mistake that only shows up as a
bill or a missing backup months later:

  1. The rotation glob. Compression renamed snapshots from `.db` to `.db.gz`, and the old glob
     (`telemetry-*.db`) would not have matched the new names — rotation would have silently
     stopped, growing the directory without bound while reporting success. The pair of naming
     schemes must rotate as one list across the changeover.

  2. is_b2_dest(). It decides whether `--b2-hard-delete` is passed. Wrong in one direction the
     accumulation bug comes back; wrong in the other, rclone gets a B2 flag for a non-B2 remote.

Run: py -m unittest scripts.tests.test_backup -v   (from the repo root)
"""
import gzip
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

import backup  # noqa: E402
import backup_offsite  # noqa: E402


class PruneTests(unittest.TestCase):
    """The rotation glob, across the .db -> .db.gz changeover."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(backup, "BACKUP_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def touch(self, *names):
        for n in names:
            (self.dir / n).write_bytes(b"x")

    def names(self):
        return sorted(p.name for p in self.dir.iterdir())

    def test_keeps_newest_across_both_naming_schemes(self):
        # Filenames lead with the timestamp, so a lexical sort is chronological even though the
        # extensions differ. Keeping 2 must keep the two NEWEST, not the two gzipped ones.
        self.touch(
            "telemetry-20260901-031500.db",
            "telemetry-20260902-031500.db",
            "telemetry-20260903-031500.db.gz",
            "telemetry-20260904-031500.db.gz",
        )
        removed = backup.prune("telemetry-*.db*", 2)
        self.assertEqual(removed, 2)
        self.assertEqual(
            self.names(),
            ["telemetry-20260903-031500.db.gz", "telemetry-20260904-031500.db.gz"],
        )

    def test_old_uncompressed_snapshots_are_rotated_out_not_stranded(self):
        # The regression the glob change exists to prevent: with `telemetry-*.db` these four .gz
        # files would never have matched, and nothing would ever have been deleted.
        self.touch(*[f"telemetry-2026090{i}-031500.db.gz" for i in range(1, 5)])
        self.touch("telemetry-20260801-031500.db")
        removed = backup.prune("telemetry-*.db*", 2)
        self.assertEqual(removed, 3)
        self.assertNotIn("telemetry-20260801-031500.db", self.names())

    def test_partials_are_never_treated_as_snapshots(self):
        self.touch("telemetry-20260903-031500.db.gz", "partial-20260904-031500.db")
        backup.prune("telemetry-*.db*", 1)
        self.assertIn("partial-20260904-031500.db", self.names())

    def test_clear_partials_removes_crash_leftovers(self):
        self.touch("partial-1.db", "partial-2.db", "telemetry-20260903-031500.db.gz")
        self.assertEqual(backup.clear_partials(), 2)
        self.assertEqual(self.names(), ["telemetry-20260903-031500.db.gz"])

    def test_keep_zero_removes_everything(self):
        self.touch("telemetry-20260901-031500.db.gz", "telemetry-20260902-031500.db.gz")
        self.assertEqual(backup.prune("telemetry-*.db*", 0), 2)
        self.assertEqual(self.names(), [])


class BackupDatabaseTests(unittest.TestCase):
    """The snapshot is a real gzip containing a real, readable database."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

        self.db = self.dir / "source.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY, note TEXT)")
        con.executemany("INSERT INTO readings (note) VALUES (?)", [(f"row {i}",) for i in range(500)])
        con.commit()
        con.close()

        for target, value in (("BACKUP_DIR", self.dir), ("DB_PATH", str(self.db))):
            patcher = mock.patch.object(backup, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_produces_a_gzipped_restorable_database(self):
        dest, raw_bytes = backup.backup_database("20260903-031500")
        self.assertEqual(dest.name, "telemetry-20260903-031500.db.gz")
        self.assertGreater(raw_bytes, 0)

        restored = self.dir / "restored.db"
        with gzip.open(dest, "rb") as f_in:
            restored.write_bytes(f_in.read())
        con = sqlite3.connect(restored)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 500)
        finally:
            con.close()

    def test_leaves_no_partial_behind(self):
        backup.backup_database("20260903-031500")
        self.assertEqual(list(self.dir.glob("partial-*")), [])

    def test_compression_actually_saves_space(self):
        # If this ever fails the feature is pointless — the whole justification for the extra
        # step is that telemetry rows are repetitive enough to compress well.
        dest, raw_bytes = backup.backup_database("20260903-031500")
        self.assertLess(dest.stat().st_size, raw_bytes / 2)


class IsB2DestTests(unittest.TestCase):
    """Whether --b2-hard-delete gets passed."""

    def check(self, dest, env):
        with mock.patch.dict(os.environ, env, clear=False):
            return backup_offsite.is_b2_dest(dest)

    def test_true_for_a_configured_b2_remote(self):
        self.assertTrue(self.check("b2:my-bucket", {"RCLONE_CONFIG_B2_TYPE": "b2"}))

    def test_remote_name_need_not_be_b2(self):
        # The remote can be called anything; its TYPE is what matters.
        self.assertTrue(self.check("offsite:bucket/path", {"RCLONE_CONFIG_OFFSITE_TYPE": "b2"}))

    def test_false_for_a_non_b2_backend(self):
        self.assertFalse(self.check("gdrive:backups", {"RCLONE_CONFIG_GDRIVE_TYPE": "drive"}))

    def test_false_for_a_local_path(self):
        self.assertFalse(self.check("/mnt/nas/backups", {}))

    def test_false_when_the_remote_type_is_not_configured(self):
        # Missing config is not an assumption of B2 — passing the flag blindly is what we avoid.
        self.assertFalse(self.check("b2:my-bucket", {}))

    def test_tolerates_case_and_whitespace_in_the_type(self):
        self.assertTrue(self.check("b2:bucket", {"RCLONE_CONFIG_B2_TYPE": " B2 "}))


class SyncDeletesGuardTests(unittest.TestCase):
    """The empty-source refusal.

    `rclone sync` makes the destination match the source. A source with no snapshots in it is
    therefore an instruction to delete every offsite copy — and it would fire in exactly the
    situation where the offsite copy is the last one standing (a wiped or unmounted BACKUP_DIR,
    or a backup.py that has been failing). The guard has to run BEFORE rclone, which is what
    these assert: not just the return code, but that rclone was never invoked at all.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def snapshot(self, name):
        Path(self.dir, name).write_bytes(b"x")

    def run_main(self, dest="b2:my-bucket", *, returncode=0):
        run = mock.Mock(return_value=mock.Mock(returncode=returncode, stderr="boom"))
        # Explicitly clear RCLONE_MAX_DELETE_CAP so a caller's environment cannot affect the
        # default-cap behaviour of these tests (a developer shell might have it set).
        with mock.patch.object(backup_offsite, "BACKUP_DIR", self.dir), \
             mock.patch.object(backup_offsite, "RCLONE_OFFSITE_DEST", dest), \
             mock.patch.dict(os.environ,
                             {"RCLONE_CONFIG_B2_TYPE": "b2", "RCLONE_MAX_DELETE_CAP": ""},
                             clear=False), \
             mock.patch.object(backup_offsite.subprocess, "run", run), \
             mock.patch.object(backup_offsite.notify, "send") as send:
            return backup_offsite.main(), run, send

    def test_empty_directory_is_refused_before_rclone_runs(self):
        code, run, send = self.run_main()
        self.assertEqual(code, 1)
        run.assert_not_called()
        send.assert_called_once()

    def test_a_directory_of_only_env_backups_is_still_refused(self):
        # The env copies are not snapshots. If rotation has taken every telemetry-* away, the
        # remaining .bak files must not make this look like a healthy source.
        Path(self.dir, "solinteg.env-20260904-031503.bak").write_bytes(b"x")
        code, run, _ = self.run_main()
        self.assertEqual(code, 1)
        run.assert_not_called()

    def test_one_snapshot_is_enough_to_proceed(self):
        self.snapshot("telemetry-20260904-031503.db.gz")
        code, run, send = self.run_main()
        self.assertEqual(code, 0)
        run.assert_called_once()
        argv = run.call_args[0][0]
        self.assertIn("--max-delete", argv)
        self.assertEqual(argv[argv.index("--max-delete") + 1], "2")
        send.assert_not_called()

    def test_an_uncompressed_legacy_snapshot_also_counts(self):
        self.snapshot("telemetry-20260817-031509.db")
        code, run, _ = self.run_main()
        self.assertEqual(code, 0)
        run.assert_called_once()

    def test_delete_before_is_off_by_default(self):
        # It opens a window where a file exists at the destination in neither its old nor its new
        # form. That is a deliberate recovery action, never a standing setting.
        self.snapshot("telemetry-20260904-031503.db.gz")
        code, run, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertNotIn("--delete-before", run.call_args[0][0])

    def test_delete_before_is_passed_when_the_recovery_lever_is_set(self):
        # The escape hatch from the over-cap deadlock: rclone refuses to delete at the destination
        # when a run had transfer errors, and over the cap every upload IS an error — so the
        # deletions that would free space are exactly the ones it will not do.
        self.snapshot("telemetry-20260904-031503.db.gz")
        with mock.patch.object(backup_offsite, "DELETE_BEFORE", True):
            code, run, _ = self.run_main()
        self.assertEqual(code, 0)
        argv = run.call_args[0][0]
        self.assertIn("--delete-before", argv)
        self.assertIn("--b2-hard-delete", argv)

    def test_bucket_root_is_warned_about_but_not_refused(self):
        # A warning rather than a refusal so that upgrading cannot break a deployment that is
        # already syncing to a bucket root. New setups should sync into a prefix.
        self.assertTrue(backup_offsite.warn_if_bucket_root("b2:my-bucket"))
        self.assertTrue(backup_offsite.warn_if_bucket_root("b2:my-bucket/"))
        self.assertFalse(backup_offsite.warn_if_bucket_root("b2:my-bucket/solinteg"))
        self.assertFalse(backup_offsite.warn_if_bucket_root("/opt/solinteg/backups"))
        self.snapshot("telemetry-20260904-031503.db.gz")
        code, run, _ = self.run_main()
        self.assertEqual(code, 0)
        run.assert_called_once()

    # ------------------------------------------------------------------
    # Safety fix regression tests (2026-10-02)
    # ------------------------------------------------------------------

    def test_temp_partial_file_alone_is_refused_before_rclone_runs(self):
        # REGRESSION: has_snapshots() used SNAPSHOT_GLOB = "telemetry-*.db*" which matches
        # any suffix after ".db", including ".db.gz.tmp" left by an interrupted gzip write or
        # a previous rclone run. A directory that contains only such a temp file is NOT a safe
        # source for rclone sync: syncing it would delete the real remote copies.
        # The guard must refuse and must not invoke rclone.
        self.snapshot("telemetry-20260904-031503.db.gz.tmp")
        code, run, send = self.run_main()
        self.assertEqual(code, 1, "temp-only dir must be refused")
        run.assert_not_called()
        send.assert_called_once()

    def test_temp_file_alongside_real_snapshot_does_not_block_sync(self):
        # A temp file in the same directory as a real snapshot must not affect the outcome:
        # the real snapshot is enough to proceed (this is the normal-case regression check
        # ensuring the fix does not over-tighten for the common scenario).
        self.snapshot("telemetry-20260904-031503.db.gz")
        self.snapshot("telemetry-20260904-031503.db.gz.tmp")
        code, run, _ = self.run_main()
        self.assertEqual(code, 0)
        run.assert_called_once()

    def test_max_delete_cap_is_passed_to_b2_rclone(self):
        # SAFETY DEPTH: default cap 2 allows one ordinary retention step (up to one expired DB
        # snapshot plus one env backup); it leaves no backlog margin. A larger backlog needs an
        # explicit operator override based on a dry run.
        self.snapshot("telemetry-20260904-031503.db.gz")
        code, run, _ = self.run_main()
        self.assertEqual(code, 0)
        argv = run.call_args[0][0]
        self.assertIn("--max-delete", argv,
                      "rclone argv must contain --max-delete to cap destructive deletes")
        max_delete_idx = argv.index("--max-delete")
        cap_value = argv[max_delete_idx + 1]
        self.assertEqual(cap_value, "2", "the safety cap is two deletes per sync")

    def test_max_delete_cap_applies_to_non_b2_remotes_too(self):
        # rclone sync deletes at any destination, even when deletes are not B2 hard deletes.
        # Keep the same two-file ceiling for every remote so a mistaken source cannot remove
        # the entire offsite history on other providers either.
        self.snapshot("telemetry-20260904-031503.db.gz")
        with mock.patch.dict(os.environ, {"RCLONE_CONFIG_OFFSITE_TYPE": "drive"}, clear=False):
            code, run, _ = self.run_main(dest="offsite:backups")
        self.assertEqual(code, 0)
        argv = run.call_args[0][0]
        self.assertIn("--max-delete", argv)
        self.assertEqual(argv[argv.index("--max-delete") + 1], "2")

    # ------------------------------------------------------------------
    # RCLONE_MAX_DELETE_CAP env var: validation and pass-through (2026-10-02)
    # ------------------------------------------------------------------

    def _run_main_with_cap(self, cap_value, dest="b2:my-bucket", delete_before=False):
        """Helper: run main() with RCLONE_MAX_DELETE_CAP set to cap_value (a string)."""
        self.snapshot("telemetry-20260904-031503.db.gz")
        run = mock.Mock(return_value=mock.Mock(returncode=0, stderr=""))
        env_patch = {"RCLONE_CONFIG_B2_TYPE": "b2", "RCLONE_MAX_DELETE_CAP": cap_value}
        with mock.patch.object(backup_offsite, "BACKUP_DIR", self.dir), \
             mock.patch.object(backup_offsite, "RCLONE_OFFSITE_DEST", dest), \
             mock.patch.dict(os.environ, env_patch, clear=False), \
             mock.patch.object(backup_offsite.subprocess, "run", run), \
             mock.patch.object(backup_offsite, "DELETE_BEFORE", delete_before), \
             mock.patch.object(backup_offsite.notify, "send"):
            code = backup_offsite.main()
        return code, run

    def test_invalid_cap_env_var_fails_closed_before_rclone(self):
        # GUARD: a non-integer RCLONE_MAX_DELETE_CAP must be rejected before rclone runs.
        # Wrong values must fail closed (exit 1, rclone never invoked) rather than silently
        # using a default — the operator needs to know the config is broken.
        run = mock.Mock(return_value=mock.Mock(returncode=0, stderr=""))
        self.snapshot("telemetry-20260904-031503.db.gz")
        env_patch = {"RCLONE_CONFIG_B2_TYPE": "b2", "RCLONE_MAX_DELETE_CAP": "abc"}
        with mock.patch.object(backup_offsite, "BACKUP_DIR", self.dir), \
             mock.patch.object(backup_offsite, "RCLONE_OFFSITE_DEST", "b2:my-bucket"), \
             mock.patch.dict(os.environ, env_patch, clear=False), \
             mock.patch.object(backup_offsite.subprocess, "run", run), \
             mock.patch.object(backup_offsite.notify, "send") as send:
            code = backup_offsite.main()
        self.assertEqual(code, 1, "invalid RCLONE_MAX_DELETE_CAP must exit 1")
        run.assert_not_called()
        send.assert_called_once()

    def test_negative_cap_env_var_fails_closed_before_rclone(self):
        # A negative cap would mean unlimited deletes — that is not conservative, it is dangerous.
        code, run = self._run_main_with_cap("-1")
        self.assertEqual(code, 1, "negative RCLONE_MAX_DELETE_CAP must exit 1")
        run.assert_not_called()

    def test_zero_cap_env_var_fails_closed_before_rclone(self):
        # Zero would block every sync that deletes anything, including normal nightly rotation.
        # Refuse rather than silently breaking every run.
        code, run = self._run_main_with_cap("0")
        self.assertEqual(code, 1, "zero RCLONE_MAX_DELETE_CAP must exit 1")
        run.assert_not_called()

    def test_custom_cap_env_var_is_passed_to_rclone(self):
        # A valid custom cap (e.g. 10 for one-off catch-up) must replace the default 2
        # and appear in the rclone argv without weakening the default guard.
        code, run = self._run_main_with_cap("10")
        self.assertEqual(code, 0)
        run.assert_called_once()
        argv = run.call_args[0][0]
        self.assertIn("--max-delete", argv)
        self.assertEqual(argv[argv.index("--max-delete") + 1], "10",
                         "RCLONE_MAX_DELETE_CAP=10 must be forwarded to rclone as-is")

    def test_custom_cap_is_passed_during_recovery_override(self):
        # When the operator raises the cap for a recovery run (--delete-before), the cap
        # they set must actually be forwarded — not silently overridden back to 2.
        code, run = self._run_main_with_cap("21", delete_before=True)
        self.assertEqual(code, 0)
        argv = run.call_args[0][0]
        self.assertIn("--delete-before", argv)
        self.assertIn("--max-delete", argv)
        self.assertEqual(argv[argv.index("--max-delete") + 1], "21",
                         "RCLONE_MAX_DELETE_CAP=21 must survive the recovery-override path")

    def test_non_b2_sync_does_not_log_hard_delete(self):
        # LOG WORDING REGRESSION: the success log used `" (hard-delete)" if flags else ""`,
        # but `flags` is non-empty whenever --max-delete is present — i.e. always — so
        # non-B2 syncs were incorrectly logging "(hard-delete)" even though --b2-hard-delete
        # was never passed. The annotation should appear only for real B2 hard-delete runs.
        self.snapshot("telemetry-20260904-031503.db.gz")
        run = mock.Mock(return_value=mock.Mock(returncode=0, stderr=""))
        with mock.patch.object(backup_offsite, "BACKUP_DIR", self.dir), \
             mock.patch.object(backup_offsite, "RCLONE_OFFSITE_DEST", "offsite:backups"), \
             mock.patch.dict(os.environ,
                             {"RCLONE_CONFIG_OFFSITE_TYPE": "drive",
                              "RCLONE_MAX_DELETE_CAP": ""},
                             clear=False), \
             mock.patch.object(backup_offsite.subprocess, "run", run), \
             mock.patch.object(backup_offsite.notify, "send"), \
             self.assertLogs("solinteg.backup_offsite", level="INFO") as log_cm:
            code = backup_offsite.main()
        self.assertEqual(code, 0)
        success_lines = [l for l in log_cm.output if "offsite sync ok" in l]
        self.assertEqual(len(success_lines), 1)
        self.assertNotIn("hard-delete", success_lines[0],
                         "non-B2 success log must not say '(hard-delete)'")

    # ------------------------------------------------------------------
    # Canonical-digit parser: 1_0 and +5 must not be accepted (2026-10-02 review)
    # ------------------------------------------------------------------

    def test_underscore_digit_cap_rejected(self):
        # Python's int() accepts '1_0' as a numeric literal (evaluates to 10), but an operator
        # typing RCLONE_MAX_DELETE_CAP=1_0 into an env file almost certainly made a typo.
        # The parser must accept ONLY canonical positive ASCII decimal digits — no underscores,
        # no sign prefix, no leading zeros that could hide intent.  Fail closed: exit 1, no rclone.
        code, run = self._run_main_with_cap("1_0")
        self.assertEqual(code, 1,
                         "RCLONE_MAX_DELETE_CAP='1_0' must be rejected (non-canonical digits)")
        run.assert_not_called()

    def test_plus_prefix_cap_rejected(self):
        # Python's int() accepts '+5' but a leading '+' is not a canonical positive decimal and
        # must be refused for the same reason — the intent is ambiguous and the operator should
        # use the unambiguous form '5'.  Fail closed before rclone.
        code, run = self._run_main_with_cap("+5")
        self.assertEqual(code, 1,
                         "RCLONE_MAX_DELETE_CAP='+5' must be rejected (non-canonical sign prefix)")
        run.assert_not_called()

    def test_whitespace_padded_cap_rejected(self):
        # The docs say canonical ASCII digits only. Whitespace-padded values like ' 5 ' look
        # correct at a glance but are a sign of an ill-formed env file; the parser must reject
        # them before passing to rclone, not silently strip and accept them.
        # REGRESSION: the old code called .strip() before the regex, which meant ' 5 ' was
        # accepted as if the operator had written '5'. After the fix, the raw env value is
        # matched directly, so leading/trailing whitespace fails the regex and the run is refused.
        code, run = self._run_main_with_cap(" 5 ")
        self.assertEqual(code, 1,
                         "RCLONE_MAX_DELETE_CAP=' 5 ' must be rejected (whitespace not canonical)")
        run.assert_not_called()

    # ------------------------------------------------------------------
    # Default-cap tests must not inherit RCLONE_MAX_DELETE_CAP from the caller (2026-10-02)
    # ------------------------------------------------------------------

    def test_default_cap_is_two_when_env_var_is_absent(self):
        # Explicitly unset RCLONE_MAX_DELETE_CAP so a caller's environment cannot make this test
        # flaky (e.g. a developer who runs with RCLONE_MAX_DELETE_CAP=5 in their shell).
        # When the variable is absent the default (2) must be passed to rclone.
        self.snapshot("telemetry-20260904-031503.db.gz")
        run = mock.Mock(return_value=mock.Mock(returncode=0, stderr=""))
        env_without_cap = {k: v for k, v in os.environ.items()
                           if k != "RCLONE_MAX_DELETE_CAP"}
        with mock.patch.object(backup_offsite, "BACKUP_DIR", self.dir), \
             mock.patch.object(backup_offsite, "RCLONE_OFFSITE_DEST", "b2:my-bucket"), \
             mock.patch.dict(os.environ, env_without_cap, clear=True), \
             mock.patch.dict(os.environ, {"RCLONE_CONFIG_B2_TYPE": "b2"}, clear=False), \
             mock.patch.object(backup_offsite.subprocess, "run", run), \
             mock.patch.object(backup_offsite.notify, "send"):
            code = backup_offsite.main()
        self.assertEqual(code, 0)
        argv = run.call_args[0][0]
        self.assertIn("--max-delete", argv)
        self.assertEqual(argv[argv.index("--max-delete") + 1], "2",
                         "default cap must be 2 when RCLONE_MAX_DELETE_CAP is unset")

    # ------------------------------------------------------------------
    # Directory-name safety: a directory whose name matches the snapshot glob must not
    # pass the destructive-sync guard (2026-10-02 review)
    # ------------------------------------------------------------------

    def test_directory_matching_db_glob_is_refused(self):
        # REGRESSION: has_snapshots() loops Path.glob(), which yields both files AND
        # directories.  A directory named telemetry-anything.db or telemetry-anything.db.gz
        # must NOT satisfy the guard — only regular files count.
        # The guard must refuse and must not invoke rclone.
        db_dir = Path(self.dir, "telemetry-20260904-031503.db")
        db_dir.mkdir()
        code, run, send = self.run_main()
        self.assertEqual(code, 1, "directory-only source must be refused")
        run.assert_not_called()
        send.assert_called_once()

    def test_directory_matching_dbgz_glob_is_refused(self):
        # Same as above but with the .db.gz glob — both exact extensions must require is_file().
        dbgz_dir = Path(self.dir, "telemetry-20260904-031503.db.gz")
        dbgz_dir.mkdir()
        code, run, send = self.run_main()
        self.assertEqual(code, 1, "directory-only source (.db.gz dir) must be refused")
        run.assert_not_called()
        send.assert_called_once()

    def test_real_file_alongside_matching_directory_is_accepted(self):
        # A real .db.gz file and a same-name-but-directory sibling: the file alone should
        # be enough to proceed; the directory must not confuse the outcome either way.
        Path(self.dir, "telemetry-20260903-031503.db.gz").write_bytes(b"x")
        db_dir = Path(self.dir, "telemetry-20260904-031503.db")
        db_dir.mkdir()
        code, run, _ = self.run_main()
        self.assertEqual(code, 0, "a real snapshot file present must allow sync to proceed")
        run.assert_called_once()

    # ------------------------------------------------------------------
    # Shell-syntax check: documented dry-run commands have single-backslash continuations
    # ------------------------------------------------------------------

    def test_dry_run_shell_commands_have_single_backslash_continuations(self):
        # Each line that continues a multi-line shell command must end with exactly one backslash
        # (the POSIX line-continuation character).  Doubled backslashes (\\) would appear as
        # literal backslash-backslash in a terminal, breaking the command.
        # Check both sources: the module docstring in backup_offsite.py and deploy/README.md.
        from pathlib import Path as _Path

        # Extract the raw module docstring (the string between the first triple-quote pair)
        py_src = (_Path(__file__).resolve().parent.parent /
                  "services" / "backup_offsite.py").read_text()
        # Find lines in the docstring that are shell continuation lines.
        # A valid continuation line ends with exactly one backslash (the last non-newline char).
        bad_docstring_lines = []
        in_docstring = False
        for line in py_src.splitlines():
            stripped = line.rstrip()
            if stripped.endswith('\\\\'):
                bad_docstring_lines.append(line)
        self.assertEqual(bad_docstring_lines, [],
                         "backup_offsite.py docstring must not contain \\\\ continuation lines: "
                         + repr(bad_docstring_lines))

        # Check deploy/README.md — only the code blocks (between ```bash fences) matter.
        readme = (_Path(__file__).resolve().parent.parent.parent /
                  "deploy" / "README.md").read_text()
        bad_readme_lines = []
        in_bash_block = False
        for line in readme.splitlines():
            if line.strip().startswith("```bash"):
                in_bash_block = True
                continue
            if line.strip() == "```" and in_bash_block:
                in_bash_block = False
                continue
            if in_bash_block and line.rstrip().endswith('\\\\'):
                bad_readme_lines.append(line)
        self.assertEqual(bad_readme_lines, [],
                         "README.md bash code blocks must not contain \\\\ continuation lines: "
                         + repr(bad_readme_lines))

        # Positive check: the dry-run systemd-run command must contain at least one continuation.
        dry_run_section = py_src[py_src.find("set -o pipefail"):py_src.find("| grep '^- '") + 20]
        self.assertIn("\\", dry_run_section,
                      "dry-run command in docstring must have at least one \\ continuation")



    def test_dry_run_shell_command_is_syntactically_valid(self):
        # Extract the dry-run shell command block from backup_offsite.py's module docstring
        # and from deploy/README.md's bash code block, write each to a temp file, and run
        # `bash -n` on it to check syntax.  This catches structural breakage (unclosed
        # subshells, missing quotes, wrong quoting) that a backslash-count check would miss.
        import subprocess
        import tempfile
        from pathlib import Path as _Path

        py_src = (_Path(__file__).resolve().parent.parent /
                  "services" / "backup_offsite.py").read_text()
        readme = (_Path(__file__).resolve().parent.parent.parent /
                  "deploy" / "README.md").read_text()

        # --- docstring dry-run block ---
        # The block starts at "( set -o pipefail" (anywhere in the docstring) and ends at
        # the closing ")" line that immediately follows the subshell body.
        OPEN_MARKER = "( set -o pipefail"
        CLOSE_MARKER = "\n    )"
        start = py_src.find(OPEN_MARKER)
        self.assertGreater(start, 0, "dry-run subshell not found in backup_offsite.py docstring")
        close_idx = py_src.find(CLOSE_MARKER, start)
        self.assertGreater(close_idx, start,
                           "closing ) not found after dry-run subshell in docstring")
        # Include the closing ) and its line
        end_of_close = py_src.find("\n", close_idx + 1)
        docstring_block = py_src[start : end_of_close + 1].strip()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as tf:
            tf.write("#!/bin/sh\n" + docstring_block + "\n")
            tf_path = tf.name
        result = subprocess.run(["bash", "-n", tf_path], capture_output=True, text=True)
        _Path(tf_path).unlink(missing_ok=True)
        self.assertEqual(result.returncode, 0,
                         f"bash -n failed on docstring dry-run block:\n{result.stderr}")

        # --- README bash code blocks containing dry-run commands ---
        # Walk bash code blocks; collect ALL that contain "( set -o pipefail" and
        # run bash -n on each one.  This covers both the cap-2 recovery block and the
        # B3 missed-run recovery block added in the 2026-10-02 reviewer request.
        in_block = False
        current_block_lines = []
        all_dry_run_blocks = []
        for line in readme.splitlines():
            if line.strip().startswith("```bash"):
                in_block = True
                current_block_lines = []
                continue
            if in_block and line.strip() == "```":
                in_block = False
                block_text = "\n".join(current_block_lines)
                if "( set -o pipefail" in block_text or "set -o pipefail" in block_text:
                    all_dry_run_blocks.append(current_block_lines[:])
                continue
            if in_block:
                current_block_lines.append(line)
        self.assertTrue(all_dry_run_blocks,
                        "no dry-run bash blocks found in deploy/README.md")

        for block_lines in all_dry_run_blocks:
            readme_block = "\n".join(block_lines)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as tf:
                tf.write("#!/bin/sh\n" + readme_block + "\n")
                tf_path = tf.name
            result = subprocess.run(["bash", "-n", tf_path], capture_output=True, text=True)
            _Path(tf_path).unlink(missing_ok=True)
            self.assertEqual(result.returncode, 0,
                             f"bash -n failed on README.md dry-run bash block:\n{result.stderr}")

    # ------------------------------------------------------------------
    # Symlink and zero-byte file rejection (2026-10-02 reviewer request)
    # ------------------------------------------------------------------

    def test_symlink_to_snapshot_is_refused(self):
        # GUARD: Path.is_file() follows symlinks, so a symlink whose TARGET is a real .db.gz
        # file would pass the old guard. A symlink is not a genuine local backup copy:
        # it may be dangling (target deleted) or may point outside BACKUP_DIR entirely.
        # has_snapshots() must reject it and refuse the sync.
        # Arrange: a file outside the snapshot namespace, and a snapshot-named symlink to it.
        target = Path(self.dir, "real-backup.db.gz")
        target.write_bytes(b"x")
        link = Path(self.dir, "telemetry-20260904-031503.db.gz")
        link.symlink_to(target)
        # BACKUP_DIR contains: telemetry-20260904-031503.db.gz -> real-backup.db.gz
        # "real-backup.db.gz" does NOT match the snapshot glob, only the symlink does.
        # The guard must refuse because the only glob-matching entry is a symlink.
        code, run, send = self.run_main()
        self.assertEqual(code, 1, "symlink-only snapshot dir must be refused")
        run.assert_not_called()
        send.assert_called_once()

    def test_dangling_symlink_to_snapshot_name_is_refused(self):
        # A dangling symlink (target no longer exists) must also be refused.
        # Path.is_file() returns False for dangling symlinks, but this test documents and
        # pins the expectation explicitly so any future change to the check is deliberate.
        link = Path(self.dir, "telemetry-20260904-031503.db.gz")
        link.symlink_to("/nonexistent/path/telemetry.db.gz")
        code, run, send = self.run_main()
        self.assertEqual(code, 1, "dangling-symlink snapshot dir must be refused")
        run.assert_not_called()
        send.assert_called_once()

    def test_real_snapshot_alongside_symlink_is_accepted(self):
        # A genuine regular file AND a symlink: the real file is enough; the symlink must not
        # prevent the sync from proceeding (no over-tightening for the normal case).
        real = Path(self.dir, "telemetry-20260903-031503.db.gz")
        real.write_bytes(b"x")
        link = Path(self.dir, "telemetry-20260904-031503.db.gz")
        link.symlink_to(real)
        code, run, _ = self.run_main()
        self.assertEqual(code, 0, "a real snapshot file alongside a symlink must allow sync")
        run.assert_called_once()

    def test_zero_byte_snapshot_alone_is_refused(self):
        # GUARD: a zero-byte file with a valid snapshot name is not a usable backup.
        # It could have been left by a failed write (e.g. gzip crashed before writing bytes).
        # has_snapshots() must require size > 0; an empty file must not satisfy the guard.
        Path(self.dir, "telemetry-20260904-031503.db.gz").write_bytes(b"")
        code, run, send = self.run_main()
        self.assertEqual(code, 1, "zero-byte snapshot must be refused")
        run.assert_not_called()
        send.assert_called_once()

    def test_zero_byte_db_snapshot_alone_is_refused(self):
        # Same for the legacy .db extension.
        Path(self.dir, "telemetry-20260904-031503.db").write_bytes(b"")
        code, run, send = self.run_main()
        self.assertEqual(code, 1, "zero-byte .db snapshot must be refused")
        run.assert_not_called()
        send.assert_called_once()

    def test_nonempty_snapshot_alongside_zero_byte_proceeds(self):
        # A zero-byte file must not prevent the sync when a real non-empty snapshot is present.
        Path(self.dir, "telemetry-20260903-031503.db.gz").write_bytes(b"x")
        Path(self.dir, "telemetry-20260904-031503.db.gz").write_bytes(b"")
        code, run, _ = self.run_main()
        self.assertEqual(code, 0, "non-empty snapshot alongside zero-byte file must allow sync")
        run.assert_called_once()


class DocCorrectnessTests(unittest.TestCase):
    """Checks that documentation text is accurate and safe.

    Finding 1 (independent review 2026-10-02): systemd EnvironmentFile= values take precedence
    over transient -E values, so docs must warn operators to check solinteg.env for
    RCLONE_MAX_DELETE_CAP and RCLONE_DELETE_BEFORE before using -E overrides.

    Finding 3 (independent review 2026-10-02): avoid asserting exact rclone cap-overflow
    behaviour ('will reject it') and do not promise nightly alerts definitely continue;
    B3 must not assume the destination is not over capacity.
    """

    @staticmethod
    def _py_src() -> str:
        from pathlib import Path as _P
        return (_P(__file__).resolve().parent.parent / "services" / "backup_offsite.py").read_text()

    @staticmethod
    def _readme() -> str:
        from pathlib import Path as _P
        return (_P(__file__).resolve().parent.parent.parent / "deploy" / "README.md").read_text()

    # ------------------------------------------------------------------
    # Finding 1 (updated): /usr/bin/env VAR=val approach — EnvironmentFile caveat explained
    # ------------------------------------------------------------------

    def test_readme_warns_to_check_env_file_before_E_override(self):
        # The README must explain how /usr/bin/env VAR=val interacts with EnvironmentFile.
        # With the new approach, /usr/bin/env RCLONE_DELETE_BEFORE=1 RCLONE_MAX_DELETE_CAP=NNN
        # always wins over EnvironmentFile for the child process, so the operator does NOT need
        # to check or comment out those keys in solinteg.env before running the recovery.
        # The README must: (a) mention RCLONE_MAX_DELETE_CAP; (b) reference both solinteg.env
        # and EnvironmentFile so context is clear; (c) explain the /usr/bin/env override wins.
        readme = self._readme()
        self.assertIn(
            "RCLONE_MAX_DELETE_CAP", readme,
            "README must mention RCLONE_MAX_DELETE_CAP (sanity check — should already be there)",
        )
        # Both solinteg.env and EnvironmentFile must be referenced for context.
        self.assertTrue(
            "solinteg.env" in readme and "EnvironmentFile" in readme,
            "README must reference both solinteg.env and EnvironmentFile for context",
        )
        # The /usr/bin/env override mechanism must be explained: it wins over EnvironmentFile.
        self.assertRegex(
            readme,
            r"(?s)/usr/bin/env.*overrid|overrid.*/usr/bin/env",
            "README must explain that /usr/bin/env VAR=val overrides EnvironmentFile for the "
            "child process, so the operator does not need to edit solinteg.env for the override.",
        )

    def test_docstring_warns_to_check_env_file_before_E_override(self):
        # backup_offsite.py's docstring recovery command uses /usr/bin/env VAR=val; the same
        # explanation must appear there so the docstring is self-contained for an operator
        # reading just the script.
        py_src = self._py_src()
        # The /usr/bin/env approach and its EnvironmentFile override semantics must be explained.
        self.assertRegex(
            py_src,
            r"(?s)/usr/bin/env.*overrid|overrid.*/usr/bin/env",
            "backup_offsite.py docstring must explain that /usr/bin/env VAR=val overrides "
            "EnvironmentFile for the child process (so the operator does not need to edit "
            "solinteg.env for the one-shot recovery override).",
        )
        # RCLONE_MAX_DELETE_CAP must still be mentioned with context about its role.
        self.assertIn(
            "RCLONE_MAX_DELETE_CAP",
            py_src,
            "backup_offsite.py docstring must mention RCLONE_MAX_DELETE_CAP",
        )

    # ------------------------------------------------------------------
    # Finding 3: no unverified cap-behavior claim; hedged alert language;
    # B3 must not assume destination is not over capacity
    # ------------------------------------------------------------------

    def test_docstring_does_not_claim_cap_will_reject(self):
        # "the default cap (2) will reject it" is an unverified assertion about rclone's
        # exact cap-exceeded behaviour; replace with hedged language.
        py_src = self._py_src()
        self.assertNotIn(
            "will reject it",
            py_src,
            "backup_offsite.py must not assert 'will reject it' — cap-exceeded rclone behaviour "
            "is not guaranteed and the claim is unverified",
        )

    def test_readme_does_not_claim_cap_will_reject(self):
        # Same check in deploy/README.md.
        readme = self._readme()
        self.assertNotIn(
            "will reject it",
            readme,
            "README must not assert 'will reject it' — cap-exceeded rclone behaviour "
            "is not guaranteed and the claim is unverified",
        )

    def test_readme_b3_does_not_assume_destination_is_not_full(self):
        # B3 guidance must not assert "not over-capacity": a missed run or a slow reclamation
        # cycle can leave the destination near or over capacity.  The doc should instead point
        # back to the delete-before procedure if capacity is uncertain or the destination is full.
        readme = self._readme()
        self.assertNotIn(
            "not over-capacity",
            readme,
            "README B3 must not assume the destination is not over capacity — "
            "if capacity is uncertain, the operator should use the delete-before procedure",
        )
        # B3 section must reference the delete-before procedure as an option when in doubt.
        b3_idx = readme.find("B3. Missed-run recovery")
        self.assertGreater(b3_idx, 0, "B3 section must exist in README")
        b3_section = readme[b3_idx:b3_idx + 1500]
        self.assertRegex(
            b3_section,
            r"delete.before|--delete-before|capacity",
            "B3 must reference delete-before or capacity uncertainty so the operator "
            "knows what to do if the destination is full",
        )

    def test_readme_nightly_alert_language_is_hedged(self):
        # "The nightly alert will continue" is too strong — it is not guaranteed.
        # The text must use hedged language (e.g. "can continue", "may continue").
        readme = self._readme()
        # Check the exact present-tense phrasing is not there:
        self.assertNotIn(
            "nightly alert will continue",
            readme,
            "README must not assert 'nightly alert will continue' — use hedged language "
            "('can continue', 'may continue') since this is not guaranteed",
        )

    # ------------------------------------------------------------------
    # Reviewer findings 2026-10-02: angle-bracket placeholders, /usr/bin/env
    # recovery approach, explicit dry-run count, no comment-out instruction
    # ------------------------------------------------------------------

    def _bash_blocks(self, text: str) -> list:
        """Extract content lines from ```bash ... ``` fences in text."""
        blocks = []
        current = []
        in_block = False
        for line in text.splitlines():
            if line.strip().startswith("```bash"):
                in_block = True
                current = []
                continue
            if in_block and line.strip() == "```":
                in_block = False
                blocks.append("\n".join(current))
                continue
            if in_block:
                current.append(line)
        return blocks

    def test_no_angle_bracket_placeholders_in_shell_examples(self):
        # REVIEWER FINDING: shell treats '<' and '>' as redirection operators, so a literal
        # '<count-from-dry-run-plus-2>' in a command would try to open a file named
        # 'count-from-dry-run-plus-2' for input rather than substituting a number.
        # Specifically, the recovery commands in README bash blocks and in the backup_offsite.py
        # docstring must not use the '<count-from-dry-run-plus-2>' form (or any variant).
        # Use a safe placeholder such as 'NNN' with a note instead.
        # Note: angle brackets in comments (# ...) and in URL strings are intentional and safe.
        import re
        # Match <word-with-hyphens> as a shell argument — skip lines that are pure comments or
        # that contain a URL scheme (these are placeholder labels in docs, not shell tokens).
        # We target the specific problematic pattern used in the recovery commands.
        bad_placeholder_re = re.compile(r'<count-[a-zA-Z0-9_-]*>')

        readme = self._readme()
        for block in self._bash_blocks(readme):
            for line in block.splitlines():
                stripped = line.lstrip()
                if stripped.startswith('#'):
                    continue  # comment line — angle brackets here are documentation
                bad = bad_placeholder_re.findall(line)
                self.assertEqual(
                    bad, [],
                    f"README bash block non-comment line contains angle-bracket placeholder(s) "
                    f"{bad!r} that shell treats as redirections — use 'NNN' with a note instead.\n"
                    f"  Line: {line!r}",
                )

        py_src = self._py_src()
        # Check the shell command lines in the docstring (lines inside the triple-quote block
        # before the first 'import').
        docstring_end = py_src.find('\nimport ')
        docstring = py_src[:docstring_end] if docstring_end > 0 else py_src
        for line in docstring.splitlines():
            stripped = line.lstrip()
            if stripped.startswith('#'):
                continue  # comment line — safe
            bad = bad_placeholder_re.findall(line)
            self.assertEqual(
                bad, [],
                f"backup_offsite.py docstring non-comment line contains angle-bracket "
                f"placeholder(s) {bad!r} that shell treats as redirections — use 'NNN'.\n"
                f"  Line: {line!r}",
            )

    def test_recovery_command_uses_env_prefix_not_dash_E_for_transient_overrides(self):
        # REVIEWER FINDING: the recovery command previously used systemd-run -E VAR=val for
        # RCLONE_DELETE_BEFORE and RCLONE_MAX_DELETE_CAP. Because systemd loads EnvironmentFile=
        # AFTER applying -E overrides (EnvironmentFile wins), a value in solinteg.env would
        # silently override the operator's one-shot flag. The fix: use
        #   systemd-run ... /usr/bin/env RCLONE_DELETE_BEFORE=1 RCLONE_MAX_DELETE_CAP=NNN python ...
        # so /usr/bin/env applies the overrides as the child's argv, which always takes precedence
        # over inherited environment — the EnvironmentFile is still used for credentials, and
        # the scheduled service is unaffected because /usr/bin/env only modifies the child.
        readme = self._readme()
        py_src = self._py_src()

        # README: the delete-before recovery command must use /usr/bin/env as the executable
        # immediately before the python invocation, not -E for the transient vars.
        self.assertIn(
            "/usr/bin/env",
            readme,
            "README recovery command must use '/usr/bin/env VAR=val python ...' to pass "
            "transient overrides directly to the child (EnvironmentFile= is still used for "
            "credentials; /usr/bin/env VAR= always wins over inherited env for the child).",
        )
        # The transient RCLONE_DELETE_BEFORE must appear as a /usr/bin/env argument, not -E.
        # Check that 'RCLONE_DELETE_BEFORE=1' appears adjacent to '/usr/bin/env' in the README.
        import re
        self.assertRegex(
            readme,
            r'/usr/bin/env\s+RCLONE_DELETE_BEFORE=1',
            "README recovery command must set RCLONE_DELETE_BEFORE=1 as a /usr/bin/env "
            "argument, not as a systemd-run -E flag.",
        )
        self.assertRegex(
            readme,
            r'/usr/bin/env\s+RCLONE_DELETE_BEFORE=1\s+RCLONE_MAX_DELETE_CAP=',
            "README recovery command must set RCLONE_MAX_DELETE_CAP= as part of the "
            "/usr/bin/env argument list alongside RCLONE_DELETE_BEFORE.",
        )
        # The same pattern must appear in the docstring recovery command.
        self.assertRegex(
            py_src,
            r'/usr/bin/env\s+RCLONE_DELETE_BEFORE=1\s+RCLONE_MAX_DELETE_CAP=',
            "backup_offsite.py docstring recovery command must use /usr/bin/env VAR=val "
            "so transient overrides always win over EnvironmentFile for the child.",
        )

    def test_recovery_explains_NNN_placeholder_meaning(self):
        # The /usr/bin/env recovery command uses NNN as a placeholder for the dry-run count+2.
        # The README must tell the operator what NNN means and how to derive it.
        readme = self._readme()
        py_src = self._py_src()
        # 'NNN' must appear with an explanation in both places.
        self.assertIn(
            "NNN",
            readme,
            "README must use 'NNN' as the placeholder in the recovery command and explain "
            "that NNN must be replaced with the dry-run count plus 2.",
        )
        self.assertIn(
            "NNN",
            py_src,
            "backup_offsite.py docstring must use 'NNN' as the placeholder in the recovery "
            "command and explain that NNN must be replaced with the dry-run count plus 2.",
        )

    def test_dry_run_pipeline_includes_explicit_count(self):
        # REVIEWER FINDING: the dry-run command should optionally print matched
        # destination-only lines AND an explicit count, so the operator can directly read off
        # the number without counting lines manually. The pipeline must be shell-safe (no
        # exposure of env values). 'wc -l' or equivalent count output must appear somewhere
        # in the dry-run command block.
        readme = self._readme()
        py_src = self._py_src()
        # Look for wc -l in a bash block in the README that also contains the dry-run subshell.
        found_in_readme = False
        for block in self._bash_blocks(readme):
            if "set -o pipefail" in block and "grep '^- '" in block:
                if "wc -l" in block or "wc -l" in block:
                    found_in_readme = True
                    break
        self.assertTrue(
            found_in_readme,
            "README dry-run bash block (set -o pipefail + grep '^- ') must include 'wc -l' "
            "so the operator gets an explicit count of destination-only files.",
        )
        # Same check for the docstring.
        self.assertIn(
            "wc -l",
            py_src[py_src.find("set -o pipefail"):py_src.find("set -o pipefail") + 500]
            if "set -o pipefail" in py_src else "",
            "backup_offsite.py docstring dry-run command must include 'wc -l' for an "
            "explicit count of destination-only files.",
        )

    def test_readme_does_not_require_commenting_out_env_file_for_recovery(self):
        # REVIEWER FINDING: the old recovery instruction said "temporarily comment it out
        # (prefix the line with #)" to handle the EnvironmentFile-vs--E conflict. The new
        # /usr/bin/env approach eliminates that need: /usr/bin/env VAR=val always wins for
        # the child process, so the operator never has to edit solinteg.env.
        # The README must not contain a "temporarily comment it out" instruction in the
        # context of the recovery commands.
        readme = self._readme()
        self.assertNotIn(
            "temporarily comment it out",
            readme,
            "README must not instruct the operator to temporarily comment out solinteg.env "
            "entries — the /usr/bin/env VAR=val approach makes that unnecessary; the override "
            "always wins for the child process regardless of EnvironmentFile.",
        )


class RestoreInstructionTests(unittest.TestCase):
    """Safety checks for the **Restore** section in deploy/README.md (added 2026-10-03).

    The original restore procedure had two hazards:

    1. Shell-active placeholder ``<stamp>`` — ``<`` and ``>`` are I/O-redirection
       operators in POSIX sh, so typing the example literally would try to open a
       file called "stamp>.db.gz" for input rather than substitute a value.
       Safe form: a plain ALL-CAPS variable name such as ``STAMP`` with no angle
       brackets.

    2. Direct decompression/copy to the live DB path — writing the gunzip stdout
       (or cp) straight to ``/opt/solinteg/telemetry.db`` means a failed or
       incomplete decompression silently corrupts or truncates the live database
       with no opportunity to check the result first.  The procedure must:
         a. Decompress / copy to a separate *candidate* path (e.g. ``telemetry.db.candidate``).
         b. Run ``PRAGMA integrity_check`` on the candidate and assert it returns exactly ``ok``.
         c. Only then (after stopping services) atomically move the candidate into place.
    """

    @staticmethod
    def _readme() -> str:
        from pathlib import Path as _P
        return (_P(__file__).resolve().parent.parent.parent / "deploy" / "README.md").read_text()

    @staticmethod
    def _restore_bash_blocks(readme: str) -> list:
        """Return the content lines of every ```bash block that appears after the
        '**Restore**' heading and before '**12e.**' (so we stay inside the restore
        sub-section and avoid false positives elsewhere in the file)."""
        # Locate the restore section boundaries
        restore_start = readme.find("**Restore**")
        next_section = readme.find("**12e.", restore_start)
        if restore_start < 0:
            return []
        section = readme[restore_start:next_section] if next_section > restore_start else readme[restore_start:]
        blocks = []
        current = []
        in_block = False
        for line in section.splitlines():
            if line.strip().startswith("```bash"):
                in_block = True
                current = []
                continue
            if in_block and line.strip() == "```":
                in_block = False
                blocks.append("\n".join(current))
                continue
            if in_block:
                current.append(line)
        return blocks

    def test_restore_no_shell_active_stamp_placeholder(self):
        # SAFETY: angle-bracket tokens like <stamp> are shell I/O-redirection
        # operators.  A literal ``gunzip -c telemetry-<stamp>.db.gz`` would have
        # the shell interpret ``<stamp>`` as "open file named 'stamp>' for input",
        # silently mangling the command rather than substituting a date.  The
        # restore procedure must use a safe non-metacharacter variable/placeholder
        # (e.g. the variable STAMP or a literal example date) with no ``<…>`` form.
        import re
        readme = self._readme()
        restore_start = readme.find("**Restore**")
        next_section = readme.find("**12e.", restore_start)
        restore_text = (
            readme[restore_start:next_section]
            if restore_start >= 0 and next_section > restore_start
            else readme[restore_start:]
        )
        # Match any <word> token on non-comment lines of bash blocks.
        # Skip comment lines (stripped starts with #) which are doc-only prose.
        angle_re = re.compile(r"<[A-Za-z][A-Za-z0-9_-]*>")
        for block in self._restore_bash_blocks(readme):
            for line in block.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                found = angle_re.findall(line)
                self.assertEqual(
                    found, [],
                    f"Restore bash block non-comment line contains shell-active angle-bracket "
                    f"placeholder(s) {found!r} — use a safe variable name (e.g. STAMP) instead.\n"
                    f"  Line: {line!r}",
                )

    def test_restore_decompresses_to_candidate_not_live_db(self):
        # SAFETY: the restore procedure must NOT decompress/redirect directly to
        # ``/opt/solinteg/telemetry.db``.  An interrupted or corrupt decompression
        # would truncate or corrupt the live DB before the operator can verify the
        # result.  Instead the procedure must write to a distinct candidate path
        # (e.g. ``telemetry.db.candidate``) and only move it into place after
        # integrity_check passes.
        readme = self._readme()
        for block in self._restore_bash_blocks(readme):
            for line in block.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                # Any shell redirect ``> /opt/solinteg/telemetry.db`` on a line that
                # also contains ``gunzip`` is the dangerous direct pattern.
                # Use a regex to avoid matching a redirect to telemetry.db.candidate
                # (which is the correct staged path).
                import re as _re
                if "gunzip" in line and _re.search(
                    r">\s*/opt/solinteg/telemetry\.db\s*$", line.rstrip()
                ):
                    self.fail(
                        f"Restore bash block writes gunzip output directly to the live DB "
                        f"path '/opt/solinteg/telemetry.db'.  Decompress to a candidate "
                        f"path first (e.g. telemetry.db.candidate), run PRAGMA integrity_check, "
                        f"then move into place.\n  Line: {line!r}"
                    )

    def test_restore_legacy_copy_goes_to_candidate_not_live_db(self):
        # SAFETY: the legacy uncompressed ``cp`` path must also stage to a candidate
        # rather than copying directly to ``/opt/solinteg/telemetry.db``.
        readme = self._readme()
        for block in self._restore_bash_blocks(readme):
            for line in block.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                # A plain ``cp … /opt/solinteg/telemetry.db`` is the dangerous pattern.
                if "cp " in line and "/opt/solinteg/telemetry.db" in line:
                    # It's fine if the line is a ``mv`` of the candidate (move into place)
                    # or if the destination is the candidate path itself.
                    # Flag only if the destination is the LIVE path without .candidate suffix.
                    import re as _re
                    # If it ends at telemetry.db (not telemetry.db.candidate or similar)
                    if _re.search(r"\bcp\b.*\btelemetry\.db\s*$", line.rstrip()):
                        self.fail(
                            f"Restore bash block copies directly to the live DB path "
                            f"'/opt/solinteg/telemetry.db' (cp).  Copy to a candidate path "
                            f"first, verify integrity_check, then move into place.\n"
                            f"  Line: {line!r}"
                        )

    def test_restore_verifies_integrity_check_before_move(self):
        # SAFETY: the procedure must run ``PRAGMA integrity_check`` (or
        # ``integrity_check``) on the candidate and assert the result is exactly
        # ``'ok'`` before the final move.  Without this step an operator following
        # the guide would not catch a corrupt or truncated snapshot before
        # overwriting the live database.
        readme = self._readme()
        restore_start = readme.find("**Restore**")
        next_section = readme.find("**12e.", restore_start)
        restore_text = (
            readme[restore_start:next_section]
            if restore_start >= 0 and next_section > restore_start
            else readme[restore_start:]
        )
        self.assertIn(
            "integrity_check",
            restore_text,
            "Restore section must run PRAGMA integrity_check on the candidate database "
            "and verify it returns exactly 'ok' before moving the candidate into place.",
        )

    def test_restore_moves_candidate_into_place_atomically(self):
        # SAFETY: once integrity_check passes the procedure must use ``mv`` (not cp)
        # to atomically replace the live DB with the verified candidate on the same
        # filesystem (a same-filesystem mv is an atomic rename at the kernel level).
        # Using ``cp`` here risks a read of a half-written file.
        readme = self._readme()
        restore_start = readme.find("**Restore**")
        next_section = readme.find("**12e.", restore_start)
        restore_text = (
            readme[restore_start:next_section]
            if restore_start >= 0 and next_section > restore_start
            else readme[restore_start:]
        )
        # mv of the candidate to the live path must appear somewhere in the section.
        import re
        self.assertRegex(
            restore_text,
            r"\bmv\b.*telemetry\.db",
            "Restore section must use 'mv' to atomically replace the live DB with the "
            "verified candidate (same-filesystem atomic rename); 'cp' is not atomic.",
        )

    # ------------------------------------------------------------------
    # Safety gap: stop+mv must be gated by decompression/copy AND sqlite3
    # exit success AND exact 'ok' output (2026-10-03)
    # ------------------------------------------------------------------

    def _gzip_restore_block(self) -> str:
        """Return the restore bash block that covers the gzip (.db.gz) path.

        Identified by containing both 'gunzip' and 'sqlite3' — that combination
        is unique to the gzip restore procedure.
        """
        blocks = self._restore_bash_blocks(self._readme())
        self.assertTrue(blocks, "no bash blocks found in restore section")
        for b in blocks:
            if 'gunzip' in b and 'sqlite3' in b:
                return b
        self.fail(
            "no restore bash block found that contains both 'gunzip' and 'sqlite3' "
            "(expected gzip restore procedure block)"
        )

    def _legacy_restore_block(self) -> str:
        """Return the restore bash block that covers the legacy uncompressed .db path.

        Identified by containing 'sqlite3' but NOT 'gunzip' — the legacy path uses
        cp instead of gunzip.
        """
        blocks = self._restore_bash_blocks(self._readme())
        self.assertGreaterEqual(len(blocks), 2,
                                "expected at least 2 restore bash blocks (gzip + legacy)")
        for b in blocks:
            if 'sqlite3' in b and 'gunzip' not in b and 'cp ' in b:
                return b
        self.fail(
            "no restore bash block found that contains 'sqlite3' and 'cp' but not 'gunzip' "
            "(expected legacy uncompressed-snapshot restore procedure block)"
        )

    def test_gzip_stop_and_mv_gated_against_decompression_failure(self):
        # SAFETY GAP: in the current interactive bash block, ``gunzip ... > .candidate``
        # and ``sqlite3 ... | grep -x ok`` are separate lines.  Without ``set -e`` or an
        # explicit ``&&``/``if`` chain, the shell continues to ``systemctl stop`` and ``mv``
        # even when gunzip exits non-zero (corrupt archive, disk full, etc.).
        #
        # The restore block must use a robust control-flow construct so that a failure of
        # the gunzip/copy step prevents the stop+mv from running.  Accepted forms:
        #   - ``set -e`` (or ``set -euo pipefail``) at the top of the block, OR
        #   - an explicit ``&&``/``||`` chain that reaches systemctl stop only on success, OR
        #   - an ``if ... then ... fi`` block that gates stop+mv.
        #
        # The test inspects the block text for one of these patterns.
        block = self._gzip_restore_block()
        import re
        has_set_e = bool(re.search(r'\bset\s+.*-[a-zA-Z]*e', block))
        # gunzip line chained with && to a subsequent command (not just echo):
        has_gunzip_and_chain = bool(re.search(
            r'gunzip\b.*&&\s*\n?\s*(?!echo\b)[^\n]+', block
        ))
        # if/then block that contains both gunzip and systemctl:
        has_if_block = bool(re.search(r'\bif\b', block) and
                            'gunzip' in block and 'systemctl stop' in block)
        self.assertTrue(
            has_set_e or has_gunzip_and_chain or has_if_block,
            "gzip restore block must use 'set -e', an '&&' chain, or an 'if/then' block "
            "to prevent systemctl stop+mv from running when gunzip fails.  "
            "Currently the commands are bare sequential lines with no error gate.\n"
            "Block content:\n" + block,
        )

    def test_gzip_stop_and_mv_gated_against_sqlite3_failure(self):
        # SAFETY GAP: ``sqlite3 ... | grep -x ok`` is a pipeline.  When grep finds no
        # 'ok' line (corrupt candidate), grep exits non-zero.  Without ``set -e`` or an
        # explicit chain, the shell still proceeds to ``systemctl stop`` and ``mv``.
        # The block must gate stop+mv against the sqlite3/grep result.
        block = self._gzip_restore_block()
        import re
        has_set_e = bool(re.search(r'\bset\s+.*-[a-zA-Z]*e', block))
        # sqlite3/grep line chained with && to a subsequent non-echo, non-comment command:
        has_sqlite_and_chain = bool(re.search(
            r'sqlite3\b.*&&\s*\n?\s*(?!echo\b|#)[^\n]+', block
        ) or re.search(
            r'grep\s+-x\s+ok\b.*&&', block
        ))
        has_if_block = bool(re.search(r'\bif\b', block) and
                            'sqlite3' in block and 'systemctl stop' in block)
        self.assertTrue(
            has_set_e or has_sqlite_and_chain or has_if_block,
            "gzip restore block must use 'set -e', an '&&' chain, or an 'if/then' block "
            "to prevent systemctl stop+mv from running when sqlite3/grep fails.  "
            "Currently the commands are bare sequential lines with no error gate.\n"
            "Block content:\n" + block,
        )

    def test_gzip_stop_and_mv_exact_ok_output_required(self):
        # SAFETY: the integrity check pipeline must assert that the output is EXACTLY 'ok'
        # (grep -x ok or equivalent).  A missing or wrong grep flag means corruption can
        # pass undetected.  The block must contain ``grep -x ok`` (or ``grep -xF ok``) so
        # that only the exact single-word string 'ok' satisfies the check.
        block = self._gzip_restore_block()
        import re
        self.assertTrue(
            bool(re.search(r'grep\s+(-\w*x\w*\s+ok|-x\s+ok|--line-regexp\s+ok)', block)),
            "gzip restore block must use 'grep -x ok' (exact-line match) on the "
            "sqlite3 integrity_check output — any other output must fail the check.\n"
            "Block content:\n" + block,
        )

    def test_legacy_stop_and_mv_gated_against_copy_failure(self):
        # SAFETY GAP: in the legacy (uncompressed .db) restore block, ``cp ... .candidate``
        # is a bare line.  If cp fails, the sqlite3 and stop+mv lines still run.
        # The block must gate stop+mv against the cp result.
        block = self._legacy_restore_block()
        import re
        has_set_e = bool(re.search(r'\bset\s+.*-[a-zA-Z]*e', block))
        has_cp_and_chain = bool(re.search(
            r'\bcp\b.*&&\s*\n?\s*(?!echo\b)[^\n]+', block
        ))
        has_if_block = bool(re.search(r'\bif\b', block) and
                            'cp ' in block and 'systemctl stop' in block)
        self.assertTrue(
            has_set_e or has_cp_and_chain or has_if_block,
            "legacy restore block must use 'set -e', an '&&' chain, or an 'if/then' block "
            "to prevent systemctl stop+mv from running when cp fails.  "
            "Currently the commands are bare sequential lines with no error gate.\n"
            "Block content:\n" + block,
        )

    def test_legacy_stop_and_mv_gated_against_sqlite3_failure(self):
        # SAFETY GAP: same as the gzip path — the legacy block's sqlite3/grep result must
        # gate stop+mv.
        block = self._legacy_restore_block()
        import re
        has_set_e = bool(re.search(r'\bset\s+.*-[a-zA-Z]*e', block))
        has_sqlite_and_chain = bool(re.search(
            r'sqlite3\b.*&&\s*\n?\s*(?!echo\b|#)[^\n]+', block
        ) or re.search(
            r'grep\s+-x\s+ok\b.*&&', block
        ))
        has_if_block = bool(re.search(r'\bif\b', block) and
                            'sqlite3' in block and 'systemctl stop' in block)
        self.assertTrue(
            has_set_e or has_sqlite_and_chain or has_if_block,
            "legacy restore block must use 'set -e', an '&&' chain, or an 'if/then' block "
            "to prevent systemctl stop+mv from running when sqlite3/grep fails.  "
            "Currently the commands are bare sequential lines with no error gate.\n"
            "Block content:\n" + block,
        )

    def test_legacy_stop_and_mv_exact_ok_output_required(self):
        # SAFETY: same as the gzip path — the legacy block must use ``grep -x ok``.
        block = self._legacy_restore_block()
        import re
        self.assertTrue(
            bool(re.search(r'grep\s+(-\w*x\w*\s+ok|-x\s+ok|--line-regexp\s+ok)', block)),
            "legacy restore block must use 'grep -x ok' (exact-line match) on the "
            "sqlite3 integrity_check output.\n"
            "Block content:\n" + block,
        )

    def test_restore_bash_blocks_are_syntactically_valid(self):
        # Verify each restore bash block passes ``bash -n`` (syntax check only, no execution).
        import subprocess
        import tempfile
        readme = self._readme()
        blocks = self._restore_bash_blocks(readme)
        self.assertTrue(blocks, "no bash blocks found in restore section")
        for i, block in enumerate(blocks):
            with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as tf:
                tf.write("#!/bin/bash\n" + block + "\n")
                tf_path = tf.name
            result = subprocess.run(["bash", "-n", tf_path], capture_output=True, text=True)
            Path(tf_path).unlink(missing_ok=True)
            self.assertEqual(
                result.returncode, 0,
                f"bash -n failed on restore bash block {i + 1}:\n{result.stderr}",
            )

    # ------------------------------------------------------------------
    # Three hardenings approved 2026-10-03
    # ------------------------------------------------------------------

    def test_gzip_validation_is_standalone_not_and_echo(self):
        # HARDENING 1: gzip -t must be a standalone command on its own line, not
        # combined with '&& echo "..."'.  Under set -e the compound form works, but
        # it obscures the validation step and couples success output to the check.
        # The hardened form: put 'gzip -t ...' alone on one line; if a confirmation
        # echo is wanted, put it on a SEPARATE subsequent line or drop it.
        #
        # The test rejects any line in the gzip restore block that contains both
        # 'gzip' and '&&' (i.e. 'gzip -t ... && echo ...' or similar).
        import re
        block = self._gzip_restore_block()
        bad_lines = [
            ln for ln in block.splitlines()
            if not ln.lstrip().startswith('#') and 'gzip' in ln and '&&' in ln
        ]
        self.assertEqual(
            bad_lines, [],
            "gzip restore block must NOT combine gzip validation with '&&' on a single line "
            "(e.g. 'gzip -t ... && echo').  Put gzip -t on its own line so set -e halts "
            "the block on a non-zero exit without any && coupling.  Offending lines:\n"
            + "\n".join(repr(l) for l in bad_lines),
        )

    def test_restore_checks_candidate_has_nonempty_nointernal_tables(self):
        # HARDENING 2: PRAGMA integrity_check passes for an empty SQLite database
        # (one with no tables at all).  The restore procedure must also assert that
        # the candidate contains at least one non-internal (non-sqlite_%) table so
        # that an empty database cannot silently replace the live DB.
        #
        # The restore section must contain a query that retrieves table names from
        # sqlite_master (or sqlite_schema) and filters out internal tables, e.g.:
        #   SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';
        # This query must appear (or an equivalent) in both restore bash blocks so
        # the empty-DB guard is applied before stop+mv runs.
        readme = self._readme()
        restore_start = readme.find("**Restore**")
        next_section = readme.find("**12e.", restore_start)
        restore_text = (
            readme[restore_start:next_section]
            if restore_start >= 0 and next_section > restore_start
            else readme[restore_start:]
        )
        import re
        # Accept queries referencing sqlite_master or sqlite_schema with a NOT LIKE 'sqlite_%' guard
        has_nonempty_guard = bool(re.search(
            r"sqlite_master|sqlite_schema", restore_text, re.IGNORECASE
        ) and re.search(
            r"NOT\s+LIKE\s+['\"]sqlite_%['\"]", restore_text, re.IGNORECASE
        ))
        self.assertTrue(
            has_nonempty_guard,
            "Restore section must query sqlite_master/sqlite_schema with "
            "NOT LIKE 'sqlite_%' to ensure the candidate contains at least one "
            "non-internal table.  An empty database passes integrity_check but would "
            "silently wipe the live DB — this guard prevents that.",
        )
        # Both the gzip block and the legacy block must include the guard.
        gzip_block = self._gzip_restore_block()
        legacy_block = self._legacy_restore_block()
        for label, block in (("gzip", gzip_block), ("legacy", legacy_block)):
            self.assertTrue(
                bool(re.search(r"sqlite_master|sqlite_schema", block, re.IGNORECASE))
                and bool(re.search(r"NOT\s+LIKE\s+['\"]sqlite_%['\"]", block, re.IGNORECASE)),
                f"{label} restore block must include a sqlite_master/sqlite_schema query "
                f"with NOT LIKE 'sqlite_%' to reject an empty candidate database.\n"
                f"Block content:\n{block}",
            )

    def test_restore_blocks_wrapped_in_heredoc_bash_block(self):
        # HARDENING 3: each complete restore example must be presented as a child
        # 'bash -euo pipefail' heredoc block so that the operator can paste/run the
        # whole unit at once.  Pasting individual lines into an interactive shell
        # loses the 'set -e' protection (the shell continues on errors) and risks
        # partial execution.  The heredoc form keeps parent interactive shell state
        # unaffected: all commands run inside the child bash process.
        #
        # Required form in the README (inside a ```bash fence):
        #   bash -euo pipefail << 'EOF'
        #   ... commands ...
        #   EOF
        #
        # The test checks that:
        #   (a) At least two bash blocks in the restore section use the heredoc form.
        #   (b) Each such block opens with 'bash -euo pipefail <<' and closes with
        #       a matching EOF marker.
        #   (c) The README prose instructs the operator to paste/run the whole block.
        import re
        readme = self._readme()
        restore_start = readme.find("**Restore**")
        next_section = readme.find("**12e.", restore_start)
        restore_text = (
            readme[restore_start:next_section]
            if restore_start >= 0 and next_section > restore_start
            else readme[restore_start:]
        )
        # (c) Operator instruction to paste/run the whole block must appear.
        has_paste_instruction = bool(re.search(
            r"paste.{0,40}(whole|entire|block|run)|run.{0,40}(whole|entire|block)",
            restore_text, re.IGNORECASE
        ))
        self.assertTrue(
            has_paste_instruction,
            "Restore section must instruct the operator to paste/run the WHOLE heredoc "
            "block, not line-by-line (e.g. 'Paste and run the whole block below').  "
            "This preserves bash -euo pipefail protection for all steps.",
        )
        # (a) and (b) At least two restore bash blocks must use the heredoc form.
        blocks = self._restore_bash_blocks(readme)
        heredoc_blocks = [
            b for b in blocks
            if re.search(r'bash\s+-euo\s+pipefail\s+<<', b)
        ]
        self.assertGreaterEqual(
            len(heredoc_blocks), 2,
            f"At least 2 restore bash blocks must wrap commands in "
            f"'bash -euo pipefail << ...' heredoc form (found {len(heredoc_blocks)}).  "
            f"This ensures operators can paste/run the whole block at once, keeping "
            f"parent shell state unaffected.  "
            f"Blocks found:\n" + "\n---\n".join(blocks),
        )
        # Each heredoc block must have a matching EOF closer.
        for b in heredoc_blocks:
            lines = b.splitlines()
            # Find the heredoc opener and its label
            opener_re = re.compile(r"bash\s+-euo\s+pipefail\s+<<\s*'?(\w+)'?")
            for ln in lines:
                m = opener_re.search(ln)
                if m:
                    label = m.group(1)
                    self.assertIn(
                        label, b,
                        f"Heredoc block opened with label '{label}' but the matching "
                        f"'{label}' closer was not found in the block.  "
                        f"Block:\n{b}",
                    )
                    # The closer must appear on a line by itself (at column 0, no leading spaces)
                    closer_re = re.compile(r'^' + re.escape(label) + r'\s*$', re.MULTILINE)
                    self.assertTrue(
                        bool(closer_re.search(b)),
                        f"Heredoc closer '{label}' must appear on its own line (column 0) "
                        f"to be recognised by bash.  "
                        f"Block:\n{b}",
                    )
                    break

    # ------------------------------------------------------------------
    # Behavioral test: stub sudo/systemctl prove corrupt/missing/empty inputs
    # never invoke stop or mv (2026-10-03)
    # ------------------------------------------------------------------

    def test_restore_stubs_stop_mv_never_called_on_bad_inputs(self):
        # BEHAVIORAL HARDENING: prove that the three bad-input cases (corrupt archive,
        # missing file, empty database) never cause 'systemctl stop' or 'mv' to run.
        # Uses stub 'sudo' and 'systemctl' binaries written to a temp PATH dir so no
        # real system service is ever touched.  The live-DB marker is a regular file
        # at a temp path — /opt or any production path is never referenced.
        #
        # Test cases (each in a clean scratch dir):
        #   A. Missing snapshot file  -> exit before decompression
        #   B. Corrupt gzip archive   -> gzip -t exits non-zero
        #   C. Empty SQLite candidate -> non-internal table count == 0
        import subprocess
        import stat

        def make_stubs(stub_dir: Path) -> None:
            """Write stub 'sudo', 'systemctl', and 'mv' that log invocations."""
            # stub sudo: log the full argv, then exec the remaining args as a command
            # BUT intercept 'systemctl' to record and exit 0 without executing.
            # Also intercept 'mv' to record and exit 0 without executing.
            sudo_stub = stub_dir / "sudo"
            sudo_stub.write_text(
                "#!/bin/bash\n"
                "set -e\n"
                "echo \"SUDO: $*\" >> \"$STUB_LOG\"\n"
                # Skip 'sudo -u USER' prefix when the next arg is a real binary
                "args=(\"$@\")\n"
                "# strip -u USER  if present\n"
                "if [[ \"${args[0]}\" == '-u' ]]; then\n"
                "  args=(\"${args[@]:2}\")\n"
                "fi\n"
                "cmd=\"${args[0]}\"\n"
                "if [[ \"$cmd\" == 'systemctl' ]]; then\n"
                "  echo \"SYSTEMCTL: ${args[*]}\" >> \"$STUB_LOG\"\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$cmd\" == 'mv' ]]; then\n"
                "  echo \"MV: ${args[*]}\" >> \"$STUB_LOG\"\n"
                "  exit 0\n"
                "fi\n"
                # For 'sh -c ...', exec it so gunzip/sqlite3/cp commands work
                "exec \"${args[@]}\"\n"
            )
            sudo_stub.chmod(sudo_stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

            # stub systemctl: record and succeed
            sc_stub = stub_dir / "systemctl"
            sc_stub.write_text(
                "#!/bin/bash\n"
                "echo \"SYSTEMCTL: $*\" >> \"$STUB_LOG\"\n"
                "exit 0\n"
            )
            sc_stub.chmod(sc_stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

            # stub mv: record invocation but DO NOT actually move anything
            mv_stub = stub_dir / "mv"
            mv_stub.write_text(
                "#!/bin/bash\n"
                "echo \"MV: $*\" >> \"$STUB_LOG\"\n"
                "exit 0\n"
            )
            mv_stub.chmod(mv_stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        def _run(case_name, setup_fn):
            # Inline the with-block to avoid nesting
            import tempfile as _tf2
            td = Path(_tf2.mkdtemp())
            try:
                backup_dir = td / "backups"
                backup_dir.mkdir()
                live_db = td / "telemetry.db"
                live_db_marker = b"LIVE-DB-UNCHANGED-MARKER"
                live_db.write_bytes(live_db_marker)

                stub_dir = td / "stubs"
                stub_dir.mkdir()
                make_stubs(stub_dir)
                log_file = td / "stub.log"
                log_file.write_text("")

                stamp = setup_fn(td, backup_dir, live_db)

                snap_gz  = backup_dir / f"telemetry-{stamp}.db.gz"
                snap_db  = backup_dir / f"telemetry-{stamp}.db"
                candidate = td / "telemetry.db.candidate"

                if case_name == "missing_gzip":
                    snap_path = str(snap_gz)
                    is_gzip = True
                elif case_name == "corrupt_gzip":
                    snap_path = str(snap_gz)
                    is_gzip = True
                elif case_name == "empty_db":
                    snap_path = str(snap_gz)
                    is_gzip = True
                else:
                    snap_path = str(snap_gz)
                    is_gzip = True

                script = (
                    f"export STUB_LOG={str(log_file)!r}\n"
                    f"export PATH={str(stub_dir)!r}:$PATH\n"
                    f"CANDIDATE={str(candidate)!r}\n"
                    f"LIVE_DB={str(live_db)!r}\n"
                )
                if is_gzip:
                    script += (
                        f"test -f {snap_path!r}\n"
                        f"gzip -t {snap_path!r}\n"
                        f"gunzip -c {snap_path!r} > \"$CANDIDATE\"\n"
                    )
                else:
                    script += (
                        f"test -f {snap_path!r}\n"
                        f"cp {snap_path!r} \"$CANDIDATE\"\n"
                    )
                script += (
                    f"sqlite3 \"$CANDIDATE\" 'PRAGMA integrity_check;' | grep -x ok\n"
                    f"count=$(sqlite3 \"$CANDIDATE\" "
                    f"\"SELECT COUNT(*) FROM sqlite_master "
                    f"WHERE type='table' AND name NOT LIKE 'sqlite_%';\")\n"
                    f"[ \"$count\" -gt 0 ]\n"
                    f"systemctl stop solinteg-web\n"
                    f"mv \"$CANDIDATE\" \"$LIVE_DB\"\n"
                )
                full_script = "bash -euo pipefail << 'TESTEOF'\n" + script + "TESTEOF\n"
                result = subprocess.run(
                    ["bash"],
                    input=full_script,
                    capture_output=True,
                    text=True,
                )
                log_content = log_file.read_text()
                live_db_changed = live_db.read_bytes() != live_db_marker
                return {
                    "exit_code": result.returncode,
                    "log": log_content,
                    "live_db_changed": live_db_changed,
                    "stderr": result.stderr,
                }
            finally:
                import shutil
                shutil.rmtree(str(td), ignore_errors=True)

        # Case A: missing snapshot file
        def setup_missing(td, backup_dir, live_db):
            # Do NOT create the snapshot — the test-f check must fail
            return "20260904-031503"

        r = _run("missing_gzip", setup_missing)
        self.assertNotEqual(r["exit_code"], 0, "missing snapshot must cause non-zero exit")
        self.assertNotIn("SYSTEMCTL: stop", r["log"],
                         "systemctl stop must not run when snapshot file is missing")
        self.assertNotIn("MV:", r["log"],
                         "mv must not run when snapshot file is missing")
        self.assertFalse(r["live_db_changed"],
                         "live DB must be unchanged when snapshot file is missing")

        # Case B: corrupt gzip archive
        import gzip as _gzip
        def setup_corrupt(td, backup_dir, live_db):
            snap = backup_dir / "telemetry-20260904-031503.db.gz"
            snap.write_bytes(b"\x1f\x8b\x00\x00CORRUPTED_NOT_A_REAL_GZIP_STREAM")
            return "20260904-031503"

        r = _run("corrupt_gzip", setup_corrupt)
        self.assertNotEqual(r["exit_code"], 0, "corrupt gzip must cause non-zero exit")
        self.assertNotIn("SYSTEMCTL: stop", r["log"],
                         "systemctl stop must not run when gzip is corrupt")
        self.assertNotIn("MV:", r["log"],
                         "mv must not run when gzip is corrupt")
        self.assertFalse(r["live_db_changed"],
                         "live DB must be unchanged when gzip is corrupt")

        # Case C: empty SQLite database (integrity_check passes but no user tables)
        def setup_empty_db(td, backup_dir, live_db):
            import sqlite3 as _sq
            db_path = td / "empty.db"
            con = _sq.connect(str(db_path))
            con.close()
            snap = backup_dir / "telemetry-20260904-031503.db.gz"
            with _gzip.open(str(snap), "wb") as gz:
                gz.write(db_path.read_bytes())
            return "20260904-031503"

        r = _run("empty_db", setup_empty_db)
        self.assertNotEqual(r["exit_code"], 0, "empty SQLite DB must cause non-zero exit")
        self.assertNotIn("SYSTEMCTL: stop", r["log"],
                         "systemctl stop must not run when candidate has no user tables")
        self.assertNotIn("MV:", r["log"],
                         "mv must not run when candidate has no user tables")
        self.assertFalse(r["live_db_changed"],
                         "live DB must be unchanged when candidate has no user tables")


if __name__ == "__main__":
    unittest.main()
