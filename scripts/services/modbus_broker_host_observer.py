"""Read-only Linux host observation for future Modbus-broker fencing.

No Modbus I/O, service stops, socket connections or host changes.
This is a prerequisite observation, NOT a complete exclusive fallback:
the supervisor must inhibit broker restarts and fence the TCP ownership
throughout the handover, including the check/use window.
"""
from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import time
from pathlib import Path

from modbus_broker_fencing import FenceObservation

_UNIT = re.compile(r"^solinteg-[a-zA-Z0-9_.@-]+\.service$")
_REQUIRED = {
    "LoadState", "ActiveState", "MainPID", "ControlPID",
    "KillMode", "PrivateNetwork", "NetworkNamespacePath",
}


def _stopped_by_systemd(output: str) -> bool | None:
    values = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in values:
            return None
        values[key] = value
    if not _REQUIRED.issubset(values) or values["LoadState"] != "loaded":
        return None
    if values["ActiveState"] not in {"inactive", "failed"}:
        return True if values["ActiveState"] in {
            "active", "activating", "deactivating", "reloading"
        } else None
    if (values["KillMode"] != "control-group"
            or values["PrivateNetwork"] != "no"
            or values["NetworkNamespacePath"]):
        return None
    try:
        return False if int(values["MainPID"]) == int(values["ControlPID"]) == 0 else None
    except ValueError:
        return None


def _normal_address(value: str):
    addr = ipaddress.ip_address(value.strip("[]").split("%", 1)[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return addr.ipv4_mapped
    return addr


def _tcp_peers(output: str, address, port: int) -> int:
    count = 0
    for row in output.splitlines():
        columns = row.split()
        if len(columns) < 5:
            raise ValueError("incomplete ss TCP entry")
        peer = columns[4]
        remote, separator, raw_port = peer.rpartition(":")
        if not separator or not remote:
            raise ValueError("missing peer endpoint")
        if raw_port == "*":  # LISTEN has no connected peer
            continue
        if not raw_port.isascii() or not raw_port.isdecimal():
            raise ValueError("non-numeric remote TCP port")
        parsed = _normal_address(remote)
        if int(raw_port) == port and parsed == address:
            count += 1  # ANY peer, including SYN-SENT/TIME-WAIT; fail closed
    return count


def observe_host_release(
    *, unit: str, inverter_ip: str, socket_path: Path, port: int = 502,
    run=subprocess.run, geteuid=os.geteuid, monotonic=time.monotonic,
) -> FenceObservation:
    """Return unknown fields on ANY incomplete OS observation.

    Requires host root + host network namespace, a loaded and stopped
    systemd unit with KillMode=control-group, and complete numeric ss TCP
    enumeration. Only checks THIS host, not connections from other hosts.
    The socket path must be absent, not merely unresponsive.
    """
    if not _UNIT.fullmatch(unit):
        raise ValueError("invalid service unit name")
    address = _normal_address(inverter_ip)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid TCP port")
    socket_path = Path(socket_path)
    checked_at = monotonic()
    unknown = FenceObservation(None, None, None, None, checked_at)
    try:
        if geteuid() != 0:
            return unknown
        result = run(
            ["systemctl", "show", "--no-pager",
             "--property=" + ",".join(sorted(_REQUIRED)), unit],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode or not isinstance(result.stdout, str):
            return unknown
        alive = _stopped_by_systemd(result.stdout)
        if alive is not False:
            return FenceObservation(alive, None, None, None, checked_at)
        result = run(
            ["ss", "--no-header", "--numeric", "--tcp", "--all"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode or not isinstance(result.stdout, str):
            return unknown
        peers = _tcp_peers(result.stdout, address, port)
        try:
            socket_path.lstat()
        except FileNotFoundError:
            listener_present = False
        else:
            listener_present = True  # socket, symlink or another path: NOT safe
        return FenceObservation(False, 0, peers, listener_present, checked_at)
    except (OSError, ValueError, subprocess.SubprocessError):
        return unknown
