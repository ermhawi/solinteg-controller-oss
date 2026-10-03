#!/usr/bin/env python3
"""
Offsite mirror of the local nightly backup, via rclone to a cloud remote (e.g. Backblaze B2).

backup.py's own local rotation only protects against DB corruption or a bad deploy — not disk
or hardware failure, since it writes to the same disk. This script closes that gap by mirroring
BACKUP_DIR to a remote rclone destination on the same nightly cadence.

rclone needs no config file here: its remote credentials are supplied entirely via
RCLONE_CONFIG_<REMOTE>_* environment variables (rclone's own supported mechanism), read from
solinteg.env (600-permissioned, already the home for every other secret in this deployment) via
systemd's EnvironmentFile=. E.g. for a remote named "b2" referenced as RCLONE_OFFSITE_DEST=b2:my-bucket:
  RCLONE_CONFIG_B2_TYPE=b2
  RCLONE_CONFIG_B2_ACCOUNT=<application key id>
  RCLONE_CONFIG_B2_KEY=<application key>

Uses `rclone sync` (not `copy`), so the remote mirrors BACKUP_DIR's own rotation.

THAT CUTS BOTH WAYS: sync makes the destination match the source, deleting whatever else is
there. Two consequences, one guarded here and one that cannot be:

  * An EMPTY source directory is not "nothing to do" — it is an instruction to delete every
    offsite copy, and it would fire precisely when the offsite copy is the only one left (a
    wiped or unmounted BACKUP_DIR, or a backup.py that has been failing). So a source with no
    snapshots in it is refused. See has_snapshots.
  * If RCLONE_OFFSITE_DEST names a BUCKET ROOT, anything else stored in that bucket by any other
    tool is deleted as extraneous on the next run — permanently, given --b2-hard-delete. The
    bucket then belongs to this deployment alone. Point a second application at its own bucket
    (B2's free tier is per account, not per bucket) rather than at a prefix in this one; a prefix
    is only safe if THIS destination moves under a prefix too. That is warned about nightly
    rather than refused, so that upgrading cannot break a working backup — but syncing into a
    prefix is the better setup if you are configuring this from scratch.

ON BACKBLAZE B2 THAT IS NOT ENOUGH BY ITSELF, and this file used to claim otherwise. B2 buckets
are versioned, and rclone's B2 backend HIDES a deleted file rather than removing it — the bytes
stay, and stay billable, forever. So the nightly rotation looked like a steady 21 files while the
bucket actually grew by one whole snapshot every night. The reference deployment hit 100 % of the
free 10 GB storage cap on 2026-09-03 this way, with a bucket that listed 21 files.

Two things are needed, and only together:

  1. `--b2-hard-delete` below, so rclone deletes versions instead of hiding them. Stops the
     growth, but does nothing about versions already accumulated.
  2. A LIFECYCLE RULE on the bucket — "Keep only the last version of the file" in the B2
     console. This is what actually reclaims the backlog, and it cannot be set from here: the
     credentials live in solinteg.env and are root-only by design. See deploy/README.md §12d.

The flag is applied only when the destination really is a B2 remote, so this file stays honest
for the other backends rclone supports.

THE OVER-CAP DEADLOCK, and RCLONE_DELETE_BEFORE
-----------------------------------------------
Once the destination is over its storage cap, the normal sync may not be able to recover by itself. With the default `--delete-after` mode, rclone documents that destination files are not deleted if errors occur during the run. If uploads fail because the bucket is full, ordinary rotation and compression therefore may not free space remotely. The alert can repeat while the remote still has old filenames and new uploads are rejected.

`RCLONE_DELETE_BEFORE=1` enables `--delete-before` for a deliberate recovery attempt, so destination deletions are attempted before uploads. This is deliberately NOT the default: it opens a window in which a file exists in neither its old nor its new form at the destination, and if upload then fails the offsite copy may be thinner than before. Use only after verifying the local copies and reviewing the dry-run. Do not assume a cap-exceeded run leaves the remote unchanged; inspect the actual destination before retrying.

Before running the recovery, determine how many files rclone would delete (to set the cap):

    ( set -o pipefail
      sudo systemd-run --collect --wait --pipe --uid=solinteg \
        -p EnvironmentFile=/opt/solinteg/solinteg.env \
        /bin/sh -c 'exec "${RCLONE_BIN:-rclone}" sync --dry-run --combined - /opt/solinteg/backups "$RCLONE_OFFSITE_DEST"' \
        | tee /dev/stderr | grep '^- ' | wc -l
    )
    # Verify the same configured binary the service will use supports --combined:
    sudo systemd-run --collect --wait --pipe --uid=solinteg -p EnvironmentFile=/opt/solinteg/solinteg.env /bin/sh -c '"${RCLONE_BIN:-rclone}" sync --help' | grep -F -- '--combined'

The subshell prints each destination-only line (prefixed `- `) to stderr and the total count to
stdout. Use that count plus 2 for margin as NNN for the one-off recovery below.

Note: if you run compress-backup-backlog.sh before the recovery, or if more nightly backups have
rotated out since the dry run, the count of obsolete files at the destination can increase before
you run the recovery command — set the cap to the dry-run count plus 2 for margin, or re-run the
dry run immediately before the recovery if in doubt.

WARNING: the first sync after compression (or any backlog catch-up) may delete many files at the
destination — all the old, larger pre-compression filenames that the new compressed ones replaced.
This is expected and safe when the local directory is verified, but it is why the dry run is
important: the count can be large, and the default cap (2) may reject it if not raised first.

If the recovery does not complete as intended or exits non-zero:
  1. Inspect the command output and actual remote state; do not assume a particular number of deletes.
  2. Re-run the dry run to get the current destination-only count (it may have changed).
  3. Replace NNN with that new count plus 2 for margin.
  4. Re-run the recovery command and verify its result and the remote contents.
  5. RCLONE_DELETE_BEFORE and RCLONE_MAX_DELETE_CAP were passed as /usr/bin/env arguments, not
     written to solinteg.env; there is nothing to remove and the scheduled service is unaffected.

Then run the recovery with NNN set to the dry-run count plus 2 for margin. The default cap (2) may
be too low for a recovery with a significant backlog; check the actual command result and inspect
the remote state before any further retry.

Replace NNN below with the dry-run count plus 2. /usr/bin/env VAR=val sets those variables only
for the python child process, overriding any value from EnvironmentFile for that process only —
the scheduled service and solinteg.env are unchanged. The override is transient: it exists only
for this one systemd-run invocation.

    sudo systemd-run --collect --wait --pipe --uid=solinteg \
      -p EnvironmentFile=/opt/solinteg/solinteg.env \
      /usr/bin/env RCLONE_DELETE_BEFORE=1 RCLONE_MAX_DELETE_CAP=NNN \
      /opt/solinteg/app/.venv/bin/python /opt/solinteg/app/scripts/services/backup_offsite.py

(The credentials stay in the env file; only the overrides are on the command line.) Pair it with
scripts/tools/compress-backup-backlog.sh, which is usually the reason the remote has obsolete
files to delete in the first place.

Environment:
  BACKUP_DIR           source directory (default /opt/solinteg/backups)
  RCLONE_OFFSITE_DEST  rclone destination, e.g. "b2:your-bucket-name" (required; skips with a
                       warning, not a failure, if unset — mirrors NTFY_TOPIC/HEALTHCHECKS_PING_URL's
                       "optional layer" convention)
  RCLONE_BIN           path to the rclone binary (default "rclone")
  RCLONE_DELETE_BEFORE "1" to delete at the destination before uploading — recovery only, see
                       above. Unset by default and must not be left set.
  RCLONE_MAX_DELETE_CAP  maximum number of remote-side deletes rclone may perform in one run
                       (default 2; must be a positive integer written as plain decimal digits
                       only — no '+' sign, no '_' separators, no leading zeros). Raise
                       temporarily for catch-up or recovery, but determine the right value
                       with a --dry-run first and clear it afterwards. Non-canonical or
                       non-positive values are rejected before rclone runs.
"""
import logging
import os
import subprocess
from pathlib import Path

import notify  # sibling module (scripts/services/) — script dir is sys.path[0]

log = logging.getLogger("solinteg.backup_offsite")

BACKUP_DIR = os.environ.get("BACKUP_DIR", "/opt/solinteg/backups")
RCLONE_OFFSITE_DEST = os.environ.get("RCLONE_OFFSITE_DEST", "")
RCLONE_BIN = os.environ.get("RCLONE_BIN", "rclone")

# RECOVERY LEVER, off by default. See the module docstring's "the over-cap deadlock" section.
DELETE_BEFORE = os.environ.get("RCLONE_DELETE_BEFORE", "").strip().lower() in ("1", "true", "yes")

# What a snapshot looks like, for the "is there anything to mirror" check below.
# Two exact-extension globs rather than the wider "telemetry-*.db*" that backup.py uses for
# rotation: that broader form matches any suffix after ".db" (e.g. ".db.gz.tmp" left by an
# interrupted compress step or a previous rclone download), which would let a directory full of
# partials pass the deletion guard. The rotation glob is intentionally wider — it needs to sweep
# up both naming schemes in one sorted pass. The guard here must be stricter.
# Note: Path.glob() yields both files and directories; has_snapshots() requires is_file() so
# that a directory whose name matches the pattern (e.g. telemetry-anything.db/) is NOT accepted.
_SNAPSHOT_GLOBS = ("telemetry-*.db", "telemetry-*.db.gz")

# Maximum number of remote-side deletes rclone sync may perform in one run, for every backend.
# --max-delete defaults to -1 (unlimited); the configured threshold limits the number of deletes
# for a run. The exact result of a cap-exceeded run, especially with --delete-before, is not
# verified here; check the installed rclone documentation and inspect the remote after any error.
#
# The default (2) permits one ordinary nightly retention step (up to one old database snapshot
# and one env backup); it leaves no backlog buffer. A larger backlog requires explicit operator
# action: set RCLONE_MAX_DELETE_CAP to a higher value as a one-off, then clear it. This includes:
#   - The first sync after running compress-backup-backlog.sh (many old filenames to delete).
#   - Any run after a missed day: the remote may retain files local rotation has removed. The
#     next sync may request more deletions than the default cap allows. Check its result and inspect
#     the remote state; do not assume whether any partial deletions occurred. The nightly alert can
#     repeat until an operator reviews the backlog and deliberately raises the cap for a one-off
#     recovery. Use the dry-run count plus 2 for margin, then unset the override (or remove it from
#     solinteg.env if you configured it there).
# To determine a safe value, run rclone with --dry-run first:
#
# Follow the credential-safe, systemd-based dry-run command in deploy/README.md §12d.
#
# Use that count plus 2 for margin as your cap for the recovery run. Do not leave a raised cap
# in solinteg.env.
# Must be a positive integer written as plain decimal digits (no '+' prefix, no '_' separators);
# non-canonical or non-positive values are rejected before rclone runs, so a misconfigured env
# fails closed rather than silently permitting unlimited deletes.
_MAX_DELETE_CAP_DEFAULT = 2


def is_b2_dest(dest: str) -> bool:
    """True when `dest` names an rclone remote configured as type b2.

    The remote's type comes from the same RCLONE_CONFIG_<NAME>_TYPE variable rclone itself reads,
    so this cannot disagree with what rclone will actually do. A bare local path (no colon, or a
    remote with no configured type) is not B2.
    """
    remote, _, _ = dest.partition(":")
    if not remote or ":" not in dest:
        return False
    return os.environ.get(f"RCLONE_CONFIG_{remote.upper()}_TYPE", "").strip().lower() == "b2"


def has_snapshots(directory: str) -> bool:
    """True when the source holds at least one snapshot file to mirror.

    THIS IS A DATA-LOSS GUARD, not a tidiness check. `rclone sync` makes the destination match
    the source: an empty source directory is not "nothing to do", it is an instruction to delete
    every offsite copy. That would fire in exactly the situation where the offsite copy is the
    only one left — a wiped or unmounted BACKUP_DIR, or a backup.py that has been failing.

    Only regular FILES with the exact extensions ".db" (legacy uncompressed) or ".db.gz"
    (current compressed) are counted. The broader pattern used by backup.py for rotation
    intentionally matches both naming schemes in one sorted pass, but it also matches any
    trailing suffix — e.g. ".db.gz.tmp" from an interrupted compress step — which must not
    satisfy this guard. Directories whose names match the pattern are also excluded: Path.glob()
    yields directories too, and a directory named telemetry-anything.db must NOT pass a guard
    that exists to prevent destructive remote deletes.

    NOTE: checking the file name and is_file() reduces false positives from temp files and
    oddly-named directories, but does NOT validate gzip integrity or that the file is a
    readable SQLite database — it is a name-and-type check only. Those checks are the
    responsibility of the operator's restore procedure, not this deletion guard.

    Specifically, a match is accepted only when ALL of the following hold:
      - Path.is_file() returns True (the entry is a file, not a directory).
      - Path.is_symlink() returns False (symlinks are excluded: a symlink is not a genuine
        local copy — it may be dangling, or may point outside BACKUP_DIR entirely, and
        Path.is_file() follows symlinks so without this check a live symlink to any non-empty
        file would pass the guard).
      - stat().st_size > 0 (zero-byte files are excluded: an empty file with a valid snapshot
        name is not a usable backup; it could have been left by a crashed write step).
    """
    p = Path(directory)
    return any(
        match.is_file() and not match.is_symlink() and match.stat().st_size > 0
        for glob in _SNAPSHOT_GLOBS
        for match in p.glob(glob)
    )


def warn_if_bucket_root(dest: str) -> bool:
    """Warn when `dest` is a bucket root rather than a path inside one. Returns True if it is.

    A warning and not a refusal, deliberately: an existing deployment may already be syncing to
    a bucket root, and breaking a working backup on upgrade is worse than the hazard. New setups
    should sync into a prefix.

    The hazard is worth restating once a night: syncing to a bucket ROOT means anything else
    stored in that bucket, by any other tool, gets deleted as "extraneous" on the next run —
    permanently, given --b2-hard-delete. So the bucket belongs to this deployment alone. A second
    application wanting offsite backups needs its own bucket, or this one has to move under a
    prefix first.
    """
    remote, sep, path = dest.partition(":")
    if not sep:
        return False
    bucket, _, prefix = path.partition("/")
    if bucket and not prefix.strip("/"):
        log.warning(
            "RCLONE_OFFSITE_DEST '%s' is a bucket ROOT — this sync deletes anything else stored "
            "in that bucket. Do not let another tool write to it.", dest,
        )
        return True
    return False


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not RCLONE_OFFSITE_DEST:
        log.warning("RCLONE_OFFSITE_DEST not set — skipping offsite sync")
        return 0

    # Validate RCLONE_MAX_DELETE_CAP before anything else. Fail closed on a bad value so a
    # misconfigured env is loud rather than silently permitting unlimited or zero deletes.
    # Only canonical positive ASCII decimal digits are accepted — no underscore separators
    # (e.g. 1_0), no sign prefix (e.g. +5), no leading zeros, no whitespace of any kind.
    # Python's int() accepts all of those forms, so we check first with a
    # strict regex rather than relying on int() alone.
    _raw_cap = os.environ.get("RCLONE_MAX_DELETE_CAP", "")
    if _raw_cap:
        import re as _re
        _canonical = _re.fullmatch(r'[1-9][0-9]*', _raw_cap)
        if _canonical:
            max_delete_cap = int(_raw_cap)
        else:
            max_delete_cap = None
        if max_delete_cap is None:
            msg = (
                f"RCLONE_MAX_DELETE_CAP={_raw_cap!r} is invalid — must be a positive integer "
                f"(>= 1) written as plain decimal digits (e.g. '5', not '+5' or '1_0'). "
                f"Refusing to run rclone. Use --dry-run to determine a safe value."
            )
            log.error("%s", msg)
            notify.send("Solinteg: offsite backup FAILED", msg, priority=notify.PRIORITY_HIGH)
            return 1
    else:
        max_delete_cap = _MAX_DELETE_CAP_DEFAULT

    warn_if_bucket_root(RCLONE_OFFSITE_DEST)

    if not has_snapshots(BACKUP_DIR):
        _globs_desc = " or ".join(f'"{g}"' for g in _SNAPSHOT_GLOBS)
        msg = (
            f"no snapshots matching {_globs_desc} in {BACKUP_DIR} — refusing to sync, because "
            "that would delete the offsite copies too. Check solinteg-backup.service first."
        )
        log.error("%s", msg)
        notify.send("Solinteg: offsite backup REFUSED", msg, priority=notify.PRIORITY_HIGH)
        return 1

    # See the module docstring: without this, every rotated-out snapshot lives on as a hidden
    # version and the bucket grows without bound.
    b2 = is_b2_dest(RCLONE_OFFSITE_DEST)
    flags = ["--b2-hard-delete"] if b2 else []
    flags.extend(["--max-delete", str(max_delete_cap)])
    if DELETE_BEFORE:
        flags.append("--delete-before")
        log.warning(
            "RCLONE_DELETE_BEFORE is set — deleting at the destination BEFORE uploading. "
            "Recovery mode; do not leave this set in solinteg.env."
        )

    try:
        result = subprocess.run(
            [RCLONE_BIN, "sync", *flags, BACKUP_DIR, RCLONE_OFFSITE_DEST],
            capture_output=True, text=True, timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("offsite sync failed to run: %s", exc)
        notify.send("Solinteg: offsite backup FAILED", str(exc), priority=notify.PRIORITY_HIGH)
        return 1

    if result.returncode != 0:
        log.error("rclone sync failed (exit %d): %s", result.returncode, result.stderr.strip())
        notify.send(
            "Solinteg: offsite backup FAILED",
            f"rclone exit {result.returncode}: {result.stderr.strip()[:500]}",
            priority=notify.PRIORITY_HIGH,
        )
        return 1

    log.info(
        "offsite sync ok: %s -> %s%s",
        BACKUP_DIR, RCLONE_OFFSITE_DEST, " (hard-delete)" if b2 else "",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
