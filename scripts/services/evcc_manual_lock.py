"""Offline-safe exclusive lock for supervised manual EVCC FC10 commands.

The price controller owns the *same* two locks: price-owner first, EVCC
reader second. Manual actions are refused while a price pause is unresolved,
never silently revoke the price controller's persisted ownership.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
from typing import Iterator

import ev_price_pause_owner as ownership


OWNER_LOCK = Path("/opt/solinteg/ev-shadow-runtime/var/ev-price-pause-owner.lock")
READER_LOCK = Path("/opt/solinteg/ev-shadow-runtime/var/evcc-reader.lock")
OWNER = Path("/opt/solinteg/ev-shadow-runtime/var/ev-price-pause-owner.json")


class ManualWriterBlocked(PermissionError):
    """Manual action must not reach FC10 while ownership is uncertain."""


@contextmanager
def exclusive(path: Path) -> Iterator[None]:
    """Acquire owner-only regular lock; reject symlinks and competing writers."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise ManualWriterBlocked("EVCC writer lock unavailable") from exc

    with os.fdopen(fd, "a+b") as lock:
        info = os.fstat(lock.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.geteuid()):
            raise ManualWriterBlocked("EVCC writer lock not exclusively owned")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ManualWriterBlocked("another EVCC writer owns the lock") from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _assert_no_active_price_pause(path: Path) -> None:
    try:
        record = ownership.read_owner(path)
    except ownership.OwnershipError as exc:
        raise ManualWriterBlocked("EVCC price pause owner unknown") from exc
    if record is not None and record["state"] not in ("released", "revoked"):
        raise ManualWriterBlocked("EVCC price pause is still owned or unresolved")


def guarded_manual_write(client, raw: int, *, expected_raw: int):
    """One manual FC10 at most; perform readback under both shared locks.

    The caller still needs its explicit manual action approval/expected
    register value. This guard does NOT authorize physical testing.
    """
    owner_path = Path(os.environ.get("EV_PRICE_PAUSE_OWNER_PATH", str(OWNER)))
    lock_path = Path(os.environ.get("EV_PRICE_PAUSE_OWNER_LOCK", str(OWNER_LOCK)))
    reader_path = Path(os.environ.get("EVCC_READER_LOCK", str(READER_LOCK)))

    with exclusive(lock_path):
        _assert_no_active_price_pause(owner_path)
        with exclusive(reader_path):
            _assert_no_active_price_pause(owner_path)
            return client.set_limit_raw(raw, expected_raw=expected_raw)
