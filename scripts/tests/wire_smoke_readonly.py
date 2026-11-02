#!/usr/bin/env python3
"""Real pymodbus wire smoke against a synthetic LOOPBACK-ONLY Modbus TCP server.

Run explicitly with a Python environment containing pymodbus>=3.13:
  python3 scripts/tests/wire_smoke_readonly.py

This does NOT use SOLINTEG_HOST, cannot connect to an inverter, and never
writes hardware registers. Unlike unittest fakes it tests real TCP/MBAP/FC03.
"""
from __future__ import annotations

import socket
import struct
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from modbus_broker import Broker
from modbus_broker_readonly_transport import READ_BLOCKS, ReadOnlyModbusTransport


def recv_exact(conn, n):
    data = b""
    while len(data) < n:
        part = conn.recv(n - len(data))
        if not part:
            raise ConnectionError("peer closed")
        data += part
    return data


class FakeModbusTcp:
    def __init__(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))  # kernel-assigned loopback-only port
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.errors = []
        self.requests = []
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.listener.accept()
            with conn:
                conn.settimeout(4)
                while True:
                    try:
                        head = recv_exact(conn, 7)
                    except (ConnectionError, OSError):
                        break
                    tid, protocol, length, unit = struct.unpack("!HHHB", head)
                    pdu = recv_exact(conn, length - 1)
                    if protocol != 0 or unit != 255 or len(pdu) != 5 or pdu[0] != 3:
                        raise AssertionError("unexpected Modbus TCP protocol/FC06 request")
                    addr, count = struct.unpack("!HH", pdu[1:])
                    if (addr, count) not in READ_BLOCKS:
                        raise AssertionError("unapproved FC03 read reached loopback device")
                    self.requests.append((addr, count))
                    registers = [
                        5000 if addr + i == 33000 else
                        9900 if addr + i == 33001 else
                        250 if addr + i == 33003 else
                        0x101 if addr + i == 50000 else 0
                        for i in range(count)
                    ]
                    result = bytes([3, count * 2]) + struct.pack("!" + "H"*count, *registers)
                    reply = struct.pack("!HHHB", tid, 0, len(result)+1, unit) + result
                    conn.sendall(reply)
        except Exception as exc:
            self.errors.append(repr(exc))
        finally:
            self.listener.close()

    def close(self):
        self.listener.close()
        self.thread.join(timeout=6)
        if self.thread.is_alive():
            raise RuntimeError("loopback fake did not exit")


def main():
    server = FakeModbusTcp()
    server.start()
    broker = Broker(lambda: ReadOnlyModbusTransport(
        "127.0.0.1", port=server.port, timeout=3,
    ), runtime_mode="readonly")
    try:
        for _ in range(2):
            result = broker.submit("read_snapshot", timeout_s=10)
            assert result["status"] == "valid", result.get("reason")
            sample = result["sample"]
            assert sample["soc_pct"] == 50 and sample["work_mode_raw"] == 0x101
        assert broker.status()["read_only"]
        assert len(server.requests) == 12, server.requests
        assert set(server.requests) == READ_BLOCKS - {(11000, 30)}  # fallback not needed
        try:
            broker.submit("charge", role="control", request_id="deny", watts=1000)
        except PermissionError:
            pass
        else:
            raise AssertionError("read-only broker accepted FC06 control")
    finally:
        broker.close()
        server.close()
    if server.errors:
        raise RuntimeError("loopback server failure: " + repr(server.errors))
    print("WIRE_SMOKE_OK: real pymodbus FC03 over 127.0.0.1, 12 reads, 0 writes")


if __name__ == "__main__":
    main()
