#!/usr/bin/env python3
"""Opt-in single TCP owner for poller FC03 and legacy dispatcher FC03/FC06.

The existing Dell service remains in raw legacy IPC mode by default.
Whole transactions require an explicit additional environment flag;
no watchdog/recovery or systemd service is altered by importing this code.
See docs/modbus-broker-transactions-release-2026-11-04.md.
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import signal
import stat
import threading
from pathlib import Path

from modbus_broker import Broker
from modbus_broker_control_transport import ControlModbusTransport
from modbus_broker_ipc import BrokerIpcServer
from modbus_broker_lease import broker_ownership_lease
from modbus_broker_safety_policy import (SAFETY_ENABLED_ENV, SUPERVISOR_UNIT,
                                         approve_supervisor_role)

log = logging.getLogger(__name__)
DEFAULT_SOCKET = "/run/solinteg-broker/broker.sock"
APPROVAL_MARKER = Path("/run/solinteg-broker-control-approved")


def approved_control_marker() -> bool:
    """A separate root-owned, protected cutover marker is mandatory."""
    if APPROVAL_MARKER.is_symlink() or not APPROVAL_MARKER.is_file():
        return False
    info = APPROVAL_MARKER.stat()
    return info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600


def role_for_systemd_peer(pid: int, uid: int, _gid: int) -> str | None:
    """Use kernel SO_PEERCRED PID and its real systemd cgroup, not JSON roles."""
    if uid != os.getuid() or type(pid) is not int or pid <= 0:
        return None
    try:
        cgroups = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    units = {
        "solinteg-poller.service": "read",
        "solinteg-dispatch.service": "control",
        "solinteg-broker-observer.service": "read",
    }
    for line in cgroups.splitlines():
        path = line.rsplit(":", 1)[-1]
        unit = path.rsplit("/", 1)[-1]
        if unit == SUPERVISOR_UNIT:
            return "safety" if approve_supervisor_role(unit, os.environ) else None
        if unit in units:
            return units[unit]
    return None


def build_control_broker(environ, *, client_factory=None) -> Broker:
    """Validate explicit opt-in and legacy register prerequisites before TCP."""
    if environ.get("SOLINTEG_BROKER_CONTROL_ENABLED") != "1":
        raise PermissionError("control broker requires explicit approval flag")
    if environ.get("SOLINTEG_CONTROL_ARMED") != "1":
        raise PermissionError("control broker requires existing physical arm")
    if environ.get("SOLINTEG_50207_SIGN") not in ("neg_charge", "pos_charge"):
        raise PermissionError("unverified 50207 charge polarity")
    if int(environ.get("SOLINTEG_SLAVE_ID", "0")) != 255:
        raise ValueError("broker requires inverter slave 255")
    host = environ.get("SOLINTEG_HOST", "")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("missing inverter host")
    port = int(environ.get("SOLINTEG_PORT", "502"))
    if not 1 <= port <= 65535:
        raise ValueError("invalid inverter port")
    timeout = float(environ.get("SOLINTEG_BROKER_TIMEOUT_S", "10"))
    capacity = float(environ.get("SOLINTEG_BATTERY_KWH", "25.6"))
    span = float(environ.get("SOLINTEG_BROKER_MAX_SAMPLE_S", "20"))
    if (not all(math.isfinite(x) for x in (timeout, capacity, span))
            or not 0 < timeout <= 30 or not 0 < capacity <= 100
            or not 0 < span <= 60):
        raise ValueError("invalid broker timing or battery capacity")

    transaction_opt_in = environ.get("SOLINTEG_BROKER_TRANSACTIONS_ENABLED", "0")
    if transaction_opt_in not in ("0", "1"):
        raise ValueError("broker transaction mode must be explicitly 0 or 1")
    supervisor_opt_in = environ.get(SAFETY_ENABLED_ENV, "0")
    if supervisor_opt_in not in ("0", "1"):
        raise ValueError("broker safety supervisor must be explicitly 0 or 1")
    strict_timeout_opt_in = environ.get("SOLINTEG_BROKER_STRICT_TRANSACTION_TIMEOUT_LATCH", "0")
    if strict_timeout_opt_in not in ("0", "1"):
        raise ValueError("strict transaction timeout latch must be explicitly 0 or 1")

    return Broker(
        lambda: ControlModbusTransport(
            host, port=port, timeout=timeout, client_factory=client_factory,
        ),
        armed=True, runtime_mode="control",
        transactions_enabled=transaction_opt_in == "1",
        safety_supervisor_enabled=supervisor_opt_in == "1",
        strict_transactions_timeout_latch=strict_timeout_opt_in == "1",
        battery_kwh=capacity, max_sample_s=span,
        charge_sign=environ["SOLINTEG_50207_SIGN"],
        rejection_sink=lambda event: log.warning(
            "Broker rejected sample %s: %s (%.2fs)",
            event["sample_id"], event["quality"], event["duration_s"],
        ),
    )


def main(argv=None, environ=None) -> None:
    parser = argparse.ArgumentParser(description="Solinteg approved single-owner Modbus broker")
    parser.add_argument("--control", action="store_true")
    args = parser.parse_args(argv)
    if not args.control:
        parser.error("explicit --control is required")
    env = os.environ if environ is None else environ
    if env.get("SOLINTEG_BROKER_CONTROL_ENABLED") != "1":
        parser.error("SOLINTEG_BROKER_CONTROL_ENABLED=1 is required")
    if not approved_control_marker():
        parser.error("root-approved control cutover marker missing")
    socket_path = Path(env.get("SOLINTEG_BROKER_SOCKET", DEFAULT_SOCKET))
    if str(socket_path) != DEFAULT_SOCKET:
        parser.error("only dedicated protected broker socket is allowed")
    from pymodbus.client import ModbusTcpClient  # noqa: F401 - fail before socket publication

    with broker_ownership_lease():
        if not approved_control_marker():
            raise PermissionError("control approval revoked during handover")
        broker = build_control_broker(env)
        server = BrokerIpcServer(
            socket_path, broker, role_for_peer=role_for_systemd_peer,
        )
        stop = threading.Event()

        def on_signal(_signum, _frame):
            stop.set()

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        try:
            server.start()
            log.info("Single-owner broker IPC ready; poller reads and dispatcher writes serialized")
            stop.wait()
        finally:
            server.close()
            broker.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
