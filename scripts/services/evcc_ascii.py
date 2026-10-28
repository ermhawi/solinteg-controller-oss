"""ABL eMH1 EVCC2 over Waveshare transparent Modbus ASCII (38400/8E1).

FC03 is read-only. FC10 to register 0x0014 is deliberately manual-only and
disabled unless three independent opt-ins are present; no automatic control.
The charger has NOT been proven to stop on communication loss.
See docs/superpowers/specs/2026-10-19-smart-ev-charging-design.md.
"""
from __future__ import annotations

import math
import os
import socket
from dataclasses import dataclass


REG_STATUS = 0x002E
REG_CONFIG = 0x000F
REG_CURRENT_LIMIT = 0x0014
RAW_PAUSE = 0x03E8  # 100% PWM: no permitted charging current, NOT 0 A.
MAX_TEST_CURRENT_A = 16
MAX_TEST_RAW = math.floor(MAX_TEST_CURRENT_A / 0.06)


class EvccError(RuntimeError):
    pass


class EvccUncertainWrite(EvccError):
    """Write may have taken effect. Never automatically retry or resume."""


def frame(payload: bytes, *, reply: bool = False) -> bytes:
    lrc = (-sum(payload)) & 0xFF
    marker = b">" if reply else b":"
    return marker + (payload + bytes([lrc])).hex().upper().encode("ascii") + b"\r\n"


def parse_reply(raw: bytes, address: int, function: int) -> bytes:
    if not raw.startswith(b">") or not raw.endswith(b"\r\n"):
        raise EvccError("invalid EVCC response framing")
    try:
        encoded = raw[1:-2]
        if len(encoded) % 2 or len(encoded) > 512:
            raise ValueError("invalid response length")
        data = bytes.fromhex(encoded.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EvccError("invalid EVCC response encoding") from exc
    if len(data) < 4 or sum(data) & 0xFF:
        raise EvccError("invalid EVCC response LRC")
    if data[0] != address:
        raise EvccError("EVCC response from unexpected device")
    if data[1] == function | 0x80:
        raise EvccError(f"EVCC exception 0x{data[2]:02X}")
    if data[1] != function:
        raise EvccError("unexpected EVCC function")
    return data[2:-1]


@dataclass(frozen=True)
class EvccStatus:
    state: str
    configured_raw: int
    pilot_raw: int
    currents_a: tuple[float | None, float | None, float | None]

    @property
    def measured_current_a(self) -> float | None:
        valid = [current for current in self.currents_a if current is not None]
        return sum(valid) if valid else None


class EvccAscii:
    def __init__(self, host: str, port: int = 4196, address: int = 1,
                 timeout_s: float = 2.5, *, manual_test: bool = False):
        if not host or not (1 <= port <= 65535) or not (1 <= address <= 16):
            raise ValueError("explicit EVCC host, TCP port and valid device address required")
        self.host, self.port, self.address = host, port, address
        self.timeout_s = timeout_s
        self.manual_test = manual_test

    def _exchange(self, request: bytes, function: int) -> bytes:
        # One request per TCP connection: a delayed reply cannot acknowledge a
        # subsequent write. The caller must serialize all EVCC access.
        with socket.create_connection((self.host, self.port), self.timeout_s) as sock:
            sock.settimeout(self.timeout_s)
            sock.sendall(frame(request))
            answer = bytearray()
            while not answer.endswith(b"\r\n"):
                chunk = sock.recv(256)
                if not chunk:
                    raise EvccError("EVCC closed connection before response")
                answer.extend(chunk)
                if len(answer) > 520:
                    raise EvccError("oversized EVCC response")
        return parse_reply(bytes(answer), self.address, function)

    def read(self, start: int, count: int, *, retries: int = 2) -> tuple[int, ...]:
        if not (0 <= start <= 0xFFFF) or not (1 <= count <= 16):
            raise ValueError("invalid read range")
        request = bytes([self.address, 0x03]) + start.to_bytes(2, "big") + count.to_bytes(2, "big")
        for attempt in range(retries):
            try:
                data = self._exchange(request, 0x03)
                if not data or data[0] != count * 2 or len(data) != count * 2 + 1:
                    raise EvccError("EVCC read length mismatch")
                values = data[1:]
                registers = tuple(int.from_bytes(values[i:i + 2], "big")
                                  for i in range(0, len(values), 2))
                if registers[0] >> 8 != start & 0xFF:
                    raise EvccError("EVCC read register echo mismatch")
                return registers
            except (OSError, EvccError):
                if attempt + 1 == retries:
                    raise
        raise AssertionError("unreachable")

    def status(self) -> EvccStatus:
        # FC03 directly at 0x0014 times out on this EVCC2 firmware.
        # The fourth register in the 0x000F block echoes the configured PWM.
        config = self.read(REG_CONFIG, 5)
        live = self.read(REG_STATUS, 5)
        amps = tuple(None if v == 0x03E8 else v / 10 for v in live[2:5])
        return EvccStatus(
            state=f"{live[0] & 0xFF:02X}",
            configured_raw=config[3] & 0x0FFF,
            pilot_raw=live[1] & 0x0FFF,
            currents_a=amps,
        )

    def set_limit_raw(self, raw: int, *, expected_raw: int) -> EvccStatus:
        """One physical FC10 attempt, readback afterward; never auto-retry."""
        gates = (
            self.manual_test,
            os.environ.get("EV_ENABLED") == "1",
            os.environ.get("EVCC_CONTROL_ARMED") == "1",
            os.environ.get("EVCC_MANUAL_TEST") == "1",
        )
        if not all(gates):
            raise PermissionError("EVCC writing disabled: manual test and three opt-ins required")
        if raw != RAW_PAUSE and not (100 <= raw <= MAX_TEST_RAW):
            raise ValueError("current outside 6-16 A or pause sentinel")
        if not (0 <= expected_raw <= 0x0FFF):
            raise ValueError("invalid expected PWM")
        if raw != RAW_PAUSE and os.environ.get("EVCC_START_APPROVED") != "1":
            raise PermissionError("starting charging requires separate start opt-in")
        before = self.status()
        if before.configured_raw != expected_raw:
            raise EvccError("EVCC changed since preflight; no write")
        if before.state.startswith("F") or before.state.startswith("E"):
            raise EvccError("EVCC fault/disabled state; no write")
        request = (bytes([self.address, 0x10]) + REG_CURRENT_LIMIT.to_bytes(2, "big") +
                   b"\x00\x01\x02" + raw.to_bytes(2, "big"))
        try:
            ack = self._exchange(request, 0x10)
        except (OSError, EvccError) as exc:
            raise EvccUncertainWrite("write outcome unknown; read state before any new action") from exc
        if ack != REG_CURRENT_LIMIT.to_bytes(2, "big") + b"\x00\x01":
            raise EvccUncertainWrite("unexpected EVCC write acknowledgement")
        try:
            after = self.status()
        except (OSError, EvccError) as exc:
            raise EvccUncertainWrite("write ACK received but readback unavailable") from exc
        if after.configured_raw != raw or after.pilot_raw != raw:
            raise EvccUncertainWrite("readback differs from requested PWM; do not retry")
        return after


def amps_to_raw(current_a: float) -> int:
    if not math.isfinite(current_a) or not 6 <= current_a <= MAX_TEST_CURRENT_A:
        raise ValueError("current must be within 6..16 A")
    return math.floor(current_a / 0.06)
