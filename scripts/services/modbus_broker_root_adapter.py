"""Privileged stop/approval callbacks for a FUTURE Modbus emergency supervisor.

Not installed, invoked, or wired to the live watchdog. Constructing this
adapter has no side effects. The callbacks require root and are intended
ONLY for a separately approved emergency supervisor; tests inject a fake
systemctl runner and a private temporary marker directory.

This module never opens TCP, sends Modbus commands, re-approves the broker,
or operates on any service except solinteg-broker-readonly.service.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from modbus_broker_host_observer import _stopped_by_systemd

BROKER_UNIT = "solinteg-broker-readonly.service"
BROKER_UNIT_FILE = "/etc/systemd/system/solinteg-broker-readonly.service"
APPROVAL_MARKER = Path("/run/solinteg-broker-readonly-approved")
_SYSTEMD_FIELDS = (
    "LoadState", "ActiveState", "MainPID", "ControlPID", "KillMode",
    "PrivateNetwork", "NetworkNamespacePath", "User", "Group", "FragmentPath",
)


def _systemd_properties(output: str) -> dict[str, str] | None:
    properties: dict[str, str] = {}
    for line in output.splitlines():
        name, separator, value = line.partition("=")
        if not separator or name in properties:
            return None
        properties[name] = value
    if not set(_SYSTEMD_FIELDS).issubset(properties):
        return None
    if (properties["LoadState"] != "loaded"
            or properties["User"] != "solinteg"
            or properties["Group"] != "solinteg"
            or properties["FragmentPath"] != BROKER_UNIT_FILE
            or properties["KillMode"] != "control-group"
            or properties["PrivateNetwork"] != "no"
            or properties["NetworkNamespacePath"]):
        return None
    if properties["ActiveState"] not in {
        "active", "activating", "deactivating", "reloading", "inactive", "failed"
    }:
        return None
    try:
        if int(properties["MainPID"]) < 0 or int(properties["ControlPID"]) < 0:
            return None
    except ValueError:
        return None
    return properties


class RootBrokerStopAdapter:
    """Three separately callable, fail-closed callbacks for future injection.

    The unit/fragment path is fixed by the module, not supplied by the caller.
    owner_uid/geteuid/marker_path/run injection is for offline tests only;
    production must use the defaults and a separately privileged service.
    """

    def __init__(
        self, *, marker_path: Path = APPROVAL_MARKER,
        run=subprocess.run, geteuid=os.geteuid, owner_uid: int = 0,
    ):
        self._marker = Path(marker_path)
        self._run = run
        self._geteuid = geteuid
        self._owner_uid = owner_uid

    def _protected_directory(self) -> bool:
        if not self._marker.is_absolute():
            return False
        parent = self._marker.parent.lstat()
        return (
            stat.S_ISDIR(parent.st_mode)
            and parent.st_uid == self._owner_uid
            and not parent.st_mode & 0o022
        )

    def _privileged(self) -> bool:
        return self._geteuid() == 0 and self._protected_directory()

    def approval_absent(self) -> bool:
        """Only a verified absence in a protected directory is sufficient."""
        try:
            if not self._privileged():
                return False
        except OSError:
            return False
        try:
            self._marker.lstat()
            return False
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def revoke_approval(self) -> bool:
        """Remove ONLY the exact, root-owned 0600 marker; never create one."""
        try:
            if not self._privileged():
                return False
            try:
                before = self._marker.lstat()
            except FileNotFoundError:
                return self.approval_absent()
            if (not stat.S_ISREG(before.st_mode)
                    or before.st_uid != self._owner_uid
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_nlink != 1):
                return False
            # Re-check inode before unlink; root-owned /run prevents ordinary
            # users from swapping the path between checks. A *privileged*
            # concurrent replacement is outside this local guarantee.
            current = self._marker.lstat()
            if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                return False
            self._marker.unlink()
            return self.approval_absent()
        except OSError:
            return False

    def _show(self) -> dict[str, str] | None:
        result = self._run(
            ["systemctl", "--system", "show", "--no-pager",
             "--property=" + ",".join(_SYSTEMD_FIELDS), BROKER_UNIT],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode != 0 or not isinstance(result.stdout, str):
            return None
        return _systemd_properties(result.stdout)

    def stop_broker(self) -> bool:
        """Stop ONLY the approved broker and verify that no service PID remains.

        A successful 'systemctl stop' alone is NOT proof of termination.
        A separate OS observer + exclusive flock are still required before
        the emergency procedure is authorized to open a TCP connection.
        """
        try:
            if not self._privileged() or not self.approval_absent():
                return False
            if self._show() is None:
                return False
            if not self.approval_absent():
                return False
            stopped = self._run(
                ["systemctl", "--system", "--no-ask-password", "stop", BROKER_UNIT],
                capture_output=True, text=True, timeout=8, check=False,
            )
            if stopped.returncode != 0:
                return False
            properties = self._show()
            return (
                properties is not None
                and _stopped_by_systemd(
                    "\n".join(f"{key}={value}" for key, value in properties.items())
                ) is False
                and self.approval_absent()
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
