"""Host-local, kernel-enforced broker ownership lease (not a deployment).

Both the future broker and a separately privileged emergency supervisor MUST
hold this SAME exclusive flock while they might open Modbus TCP. The supervisor
must revoke the root-owned approval marker and stop the broker BEFORE taking
the lease; a competing restart must fail closed. This cannot fence unrelated
direct Modbus clients or other network hosts.

The inode lives directly under root-owned /run, never in the broker's
RuntimeDirectory (which systemd removes on service stop).
"""
from __future__ import annotations

import errno
import fcntl
import grp
import os
import stat
from contextlib import contextmanager
from pathlib import Path

LEASE_PATH = Path("/run/solinteg-broker-ownership.lock")


class LeaseBusy(RuntimeError):
    """Another process currently owns the inverter connection lease."""


class LeaseUntrusted(PermissionError):
    """The shared lock inode or its parent is not protected."""


@contextmanager
def broker_ownership_lease(path: Path = LEASE_PATH, *, owner_uid: int = 0,
                           owner_gid: int | None = None):
    """Hold a nonblocking, exclusive BSD flock for an entire broker lifecycle.

    This does not create/chmod/chown/unlink a file; a root-only deployment
    action must first create a 0440 root:solinteg file whose inode is
    never replaced while broker/fallback may own it.
    Releasing the lease does not re-enable the independently required marker.
    owner_uid is injectable solely for unprivileged temporary-file tests.
    """
    if owner_gid is None:
        owner_gid = grp.getgrnam("solinteg").gr_gid
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("lease path must be absolute")
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != owner_uid
            or parent.st_mode & 0o022):
        raise LeaseUntrusted("lease directory is not exclusively owned")
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        held = os.fstat(fd)
        named = path.lstat()
        if (not stat.S_ISREG(held.st_mode) or held.st_uid != owner_uid
                or held.st_gid != owner_gid or stat.S_IMODE(held.st_mode) != 0o440
                or held.st_nlink != 1 or
                (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino)):
            raise LeaseUntrusted("lock file identity, ownership or mode invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise LeaseBusy("Modbus owner lease already held") from exc
            raise
        # A root-owned /run pathname is stable for the lifetime of the lease
        # unless a privileged operator incorrectly replaces its inode.
        # Do not delete/recreate it while either side is running.
        yield
    finally:
        if fd is not None:
            os.close(fd)
