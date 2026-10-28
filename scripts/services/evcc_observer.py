#!/usr/bin/env python3
"""Read-only ABL/DEFA EVCC2 diagnostics through a Waveshare TCP/RS485 bridge.

The ONLY Modbus function constructed here is FC03 (read holding registers).
There is intentionally no raw-write API, FC06, FC10, and no EV_ENABLED switch
in this module. It is not a fuse guard, power meter or charge controller.
"""
from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
import socket
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import common

_CONFIG_START = 0x000F
_STATUS_START = 0x002E
_READ_FUNCTION = 0x03
_PAUSE_SENTINEL = 0x03E8
_CURRENT_NOISE_A = 1.0
_MAX_REPLY = 520


class EvccReadError(RuntimeError):
    pass


def _read_frame(start: int, count: int, *, slave: int = 1) -> bytes:
    if start not in (_CONFIG_START, _STATUS_START) or count != 5 or slave != 1:
        raise ValueError("observer accepts only the two known 5-register read windows")
    data = bytes([slave, _READ_FUNCTION]) + start.to_bytes(2, "big") + count.to_bytes(2, "big")
    lrc = (-sum(data)) & 0xFF
    return b":" + (data + bytes([lrc])).hex().upper().encode("ascii") + b"\r\n"


def _parse_read_reply(raw: bytes, *, start: int, count: int) -> tuple[int, ...]:
    if not raw.startswith(b">") or not raw.endswith(b"\r\n"):
        raise EvccReadError("invalid EVCC ASCII reply frame")
    encoded = raw[1:-2]
    if len(encoded) % 2 or len(encoded) > 512:
        raise EvccReadError("invalid EVCC reply size")
    try:
        data = bytes.fromhex(encoded.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EvccReadError("invalid EVCC ASCII reply encoding") from exc
    if len(data) < 4 or sum(data) & 0xFF:
        raise EvccReadError("invalid EVCC LRC")
    if data[0] != 1 or data[1] != _READ_FUNCTION:
        raise EvccReadError("unexpected EVCC response/slave/function")
    if len(data) != 4 + count * 2 or data[2] != 2 * count:
        raise EvccReadError("unexpected EVCC register count")
    values = data[3:-1]
    registers = tuple(int.from_bytes(values[i:i + 2], "big") for i in range(0, len(values), 2))
    if registers[0] >> 8 != start & 0xFF:
        raise EvccReadError("EVCC register echo mismatch")
    return registers


def _private_ipv4(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.version == 4 and any(
        address in ipaddress.ip_network(net)
        for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    )


@contextmanager
def _local_serial_owner(lock_file: str):
    # All our reader processes must share this lock. It does not guard
    # against non-cooperating applications connected to the Waveshare.
    with open(lock_file, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_window(host: str, start: int, count: int, *, port: int, timeout: float) -> tuple[int, ...]:
    request = _read_frame(start, count)
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request)
            reply = bytearray()
            while not reply.endswith(b"\r\n"):
                next_chunk = sock.recv(128)
                if not next_chunk:
                    raise EvccReadError("EVCC closed TCP connection")
                reply.extend(next_chunk)
                if len(reply) > _MAX_REPLY:
                    raise EvccReadError("oversized EVCC reply")
    except OSError as exc:
        raise EvccReadError("EVCC network read failed") from exc
    return _parse_read_reply(bytes(reply), start=start, count=count)


def observe(*, host: str, port: int = 4196, timeout: float = 2.5,
            lock_file: str = "/tmp/solinteg-evcc-readonly.lock",
            now: datetime | None = None) -> dict:
    if not _private_ipv4(host) or not 1 <= port <= 65535 or not 0.1 <= timeout <= 10:
        raise ValueError("explicit private Waveshare IP, port and bounded timeout required")
    # One pair of FC03 reads, no retrying or acting on ambiguous device state.
    with _local_serial_owner(lock_file):
        config = _read_window(host, _CONFIG_START, 5, port=port, timeout=timeout)
        status = _read_window(host, _STATUS_START, 5, port=port, timeout=timeout)
    config_raw, pilot_raw = config[3] & 0x0FFF, status[1] & 0x0FFF
    channel_currents = tuple(
        None if raw == _PAUSE_SENTINEL else round(raw / 10, 2)
        for raw in status[2:5]
    )
    known = [x for x in channel_currents if x is not None]
    current_a = round(sum(known), 2) if known else None
    active = current_a is not None and current_a >= _CURRENT_NOISE_A
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise ValueError("timezone required for observer timestamp")
    return {
        "timestamp": stamp.astimezone(timezone.utc).isoformat(),
        "source": "waveshare_evcc_fc03_readonly",
        "read_ok": True,
        "state": f"{status[0] & 0xFF:02X}",
        "configured_pwm_raw": config_raw,
        "pilot_pwm_raw": pilot_raw,
        "pilot_matches_config": config_raw == pilot_raw,
        "paused_by_pwm": config_raw == _PAUSE_SENTINEL or pilot_raw == _PAUSE_SENTINEL,
        "configured_current_a": (
            None if config_raw == _PAUSE_SENTINEL else round(config_raw * 0.06, 2)),
        "channel_currents_a": channel_currents,
        "measured_current_a": current_a,
        "charging_detected": active,
        "l3_rms_current_verified": False,
        "metered_ev_energy_available": False,
        "physical_control_eligible": False,
        "modbus_function": "FC03",
        "writes": 0,
    }


def once(*, now: datetime | None = None) -> dict:
    stamp = now or datetime.now(timezone.utc)
    try:
        return observe(
            host=os.environ.get("EVCC_READ_HOST", ""),
            port=int(os.environ.get("EVCC_READ_PORT", "4196")),
            timeout=float(os.environ.get("EVCC_READ_TIMEOUT_S", "2.5")),
            lock_file=os.environ.get("EVCC_READ_LOCK_PATH",
                                     "/tmp/solinteg-evcc-readonly.lock"),
            now=stamp,
        )
    except (EvccReadError, OSError, ValueError, BlockingIOError, TypeError) as exc:
        return {
            "timestamp": stamp.isoformat(),
            "source": "waveshare_evcc_fc03_readonly",
            "read_ok": False,
            "state": "unknown",
            "reason": type(exc).__name__,
            "charging_detected": None,
            "l3_rms_current_verified": False,
            "physical_control_eligible": False,
            "writes": 0,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="repeat read-only FC03 snapshots")
    args = parser.parse_args()
    if os.environ.get("EVCC_READ_ONLY_ENABLED") != "1":
        print(json.dumps({"mode": "disabled", "reason": "read-only gate off", "writes": 0}))
        return
    period = max(20, int(os.environ.get("EVCC_READ_POLL_SECONDS", "30")))
    path = os.environ.get("EVCC_READONLY_PATH",
                          "/opt/solinteg/evcc-readonly.json")
    while True:
        record = once()
        if not args.watch:
            print(json.dumps(record, ensure_ascii=False))
            return
        common.write_json_atomic(path, record, indent=2)
        time.sleep(period)


if __name__ == "__main__":
    main()
