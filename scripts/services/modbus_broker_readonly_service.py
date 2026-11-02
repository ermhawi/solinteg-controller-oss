#!/usr/bin/env python3
"""Opt-in, strictly read-only broker service for a future poller cutover.

Not installed or started by this repository. Startup requires BOTH
--read-only and SOLINTEG_BROKER_READONLY_ENABLED=1. There is no write-capable
transport in this process, regardless of SOLINTEG_CONTROL_ARMED.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import stat
import threading
from pathlib import Path

from modbus_broker import Broker
from modbus_broker_ipc import BrokerIpcServer
from modbus_broker_lease import broker_ownership_lease
from modbus_broker_readonly_transport import DEVICE_ID, ReadOnlyModbusTransport

log = logging.getLogger(__name__)
DEFAULT_SOCKET = "/run/solinteg-broker/broker.sock"
APPROVAL_MARKER = Path("/run/solinteg-broker-readonly-approved")


def approved_cutover_marker() -> bool:
    """Only an explicit root-created protected marker permits opening TCP."""
    if APPROVAL_MARKER.is_symlink() or not APPROVAL_MARKER.is_file():
        return False
    info = APPROVAL_MARKER.stat()
    return info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600


def build_readonly_broker(environ, *, client_factory=None) -> Broker:
    """Validate configuration before even constructing a physical transport."""
    if environ.get("SOLINTEG_BROKER_READONLY_ENABLED") != "1":
        raise PermissionError("read-only broker activation not explicitly enabled")
    if int(environ.get("SOLINTEG_SLAVE_ID", "0")) != DEVICE_ID:
        raise ValueError("broker requires explicitly configured inverter slave 255")
    host = environ.get("SOLINTEG_HOST", "")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("inverter host must be configured")
    port = int(environ.get("SOLINTEG_PORT", "502"))
    if not 1 <= port <= 65535:
        raise ValueError("invalid inverter port")
    timeout = float(environ.get("SOLINTEG_BROKER_TIMEOUT_S", "10"))
    if not 0 < timeout <= 30:
        raise ValueError("invalid Modbus timeout")
    max_span = float(environ.get("SOLINTEG_BROKER_MAX_SAMPLE_S", "20"))
    capacity = float(environ.get("SOLINTEG_BATTERY_KWH", "25.6"))
    if not 0 < max_span <= 60:
        raise ValueError("invalid maximum sample duration")
    transport_factory = lambda: ReadOnlyModbusTransport(
        host, port=port, timeout=timeout, client_factory=client_factory,
    )
    # No arm, no phase guard, no confirmed control polarity, no FC06 transport.
    def on_rejection(event):
        log.warning("Broker rejected sample %s: %s (%.2fs)",
                    event["sample_id"], event["quality"], event["duration_s"])

    return Broker(transport_factory, runtime_mode="readonly",
                  battery_kwh=capacity, max_sample_s=max_span,
                  rejection_sink=on_rejection)


def main(argv=None, environ=None) -> None:
    parser = argparse.ArgumentParser(description="Solinteg read-only Modbus broker")
    parser.add_argument("--read-only", action="store_true",
                        help="Mandatory: permit only read-only broker service mode")
    args = parser.parse_args(argv)
    if not args.read_only:
        parser.error("explicit --read-only is required")
    env = os.environ if environ is None else environ
    if env.get("SOLINTEG_BROKER_READONLY_ENABLED") != "1":
        parser.error("SOLINTEG_BROKER_READONLY_ENABLED=1 is required")
    if not approved_cutover_marker():
        parser.error("protected root-created broker cutover approval is missing")

    socket_path = Path(env.get("SOLINTEG_BROKER_SOCKET", DEFAULT_SOCKET))
    if str(socket_path) != DEFAULT_SOCKET:
        parser.error("only the dedicated protected /run/solinteg-broker/broker.sock is allowed")
    # Fail before publishing the socket if production dependency is missing.
    from pymodbus.client import ModbusTcpClient  # noqa: F401

    # The same root-owned /run inode must be locked for the whole Modbus
    # session. A privileged emergency fallback will hold it across its Auto
    # attempt; any broker restart then fails BEFORE opening the TCP transport.
    with broker_ownership_lease():
        if not approved_cutover_marker():
            raise PermissionError("cutover approval revoked during ownership handover")
        broker = build_readonly_broker(env)
        server = BrokerIpcServer(socket_path, broker)
        stop = threading.Event()

        def on_signal(_signum, _frame):
            stop.set()

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        try:
            server.start()
            log.info("Read-only broker IPC ready; no FC06 transport available")
            stop.wait()
        finally:
            server.close()
            broker.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
