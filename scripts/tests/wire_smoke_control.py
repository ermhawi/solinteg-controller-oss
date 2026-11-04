#!/usr/bin/env python3
"""Real pymodbus FC03/FC06 wire smoke against a LOOPBACK-ONLY fake, never inverter.

The only TCP server binds 127.0.0.1 on an OS-selected ephemeral port.
Real broker worker, UDS IPC and the legacy Modbus client shim are exercised.
No physical Modbus endpoints or systemd services are touched.
"""
from __future__ import annotations

import socket
import struct
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from modbus_broker import Broker, CONTROL_READS, CONTROL_WRITES
from modbus_broker_control_client import BrokerModbusClient
from modbus_broker_control_transport import ControlModbusTransport
from modbus_broker_ipc import BrokerIpcClient, BrokerIpcServer
from modbus_broker_readonly_transport import READ_BLOCKS


def receive(conn, size):
    result = b""
    while len(result) < size:
        next_data = conn.recv(size - len(result))
        if not next_data:
            raise EOFError("peer closed")
        result += next_data
    return result


class FakeInverterTcp:
    def __init__(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.connections = 0
        self.requests = []
        self.errors = []
        self.registers = {33000: 5000, 33001: 9900, 33003: 250,
                          50000: 0x101, 50006: 0}
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def start(self):
        self.thread.start()

    def serve(self):
        try:
            conn, _ = self.listener.accept()
            self.connections += 1
            with conn:
                conn.settimeout(5)
                while True:
                    try:
                        header = receive(conn, 7)
                    except (EOFError, OSError):
                        break
                    transaction, protocol, size, unit = struct.unpack("!HHHB", header)
                    pdu = receive(conn, size - 1)
                    if protocol != 0 or unit != 255:
                        raise AssertionError("invalid MBAP header or unit")
                    function = pdu[0]
                    address, operand = struct.unpack("!HH", pdu[1:])
                    if function == 3 and (address, operand) in READ_BLOCKS | CONTROL_READS:
                        data = [self.registers.get(address + i, 0) for i in range(operand)]
                        reply_pdu = bytes((3, len(data) * 2)) + struct.pack(
                            "!" + "H" * len(data), *data)
                        self.requests.append((3, address, operand))
                    elif function == 6 and address in CONTROL_WRITES:
                        self.registers[address] = operand
                        reply_pdu = pdu
                        self.requests.append((6, address, operand))
                    else:
                        raise AssertionError(f"unexpected function/register: {function}/{address}")
                    reply = struct.pack("!HHHB", transaction, 0, len(reply_pdu) + 1, unit)
                    conn.sendall(reply + reply_pdu)
        except Exception as exc:
            self.errors.append(repr(exc))
        finally:
            self.listener.close()

    def close(self):
        self.listener.close()
        self.thread.join(timeout=7)
        if self.thread.is_alive():
            raise RuntimeError("loopback server did not finish")


def main():
    device = FakeInverterTcp()
    device.start()
    broker = Broker(
        lambda: ControlModbusTransport(
            "127.0.0.1", port=device.port, timeout=4,
        ),
        armed=True, runtime_mode="control", charge_sign="neg_charge",
        min_write_interval_s=0,
    )
    server = None
    try:
        with tempfile.TemporaryDirectory() as directory:
            role = {"value": "control"}
            path = Path(directory) / "broker.sock"
            server = BrokerIpcServer(
                path, broker, role_for_peer=lambda *_: role["value"],
            )
            server.start()
            client = BrokerModbusClient(ipc_client=BrokerIpcClient(path))
            assert client.connect()
            assert client.read_holding_registers(33000).registers == [5000]
            assert not client.write_register(50207, 65506).isError()
            assert client.read_holding_registers(50207).registers == [65506]
            assert not client.write_register(50000, 0x303).isError()
            client.close()

            role["value"] = "read"
            sample = BrokerIpcClient(path).request("read_snapshot")
            assert sample["status"] == "valid", sample.get("reason")
            assert sample["sample"]["work_mode_raw"] == 0x303
            try:
                BrokerIpcClient(path).request(
                    "write_register", address=50207, value=0, request_id="blocked",
                )
                raise AssertionError("poller role unexpectedly wrote FC06")
            except RuntimeError as exc:
                assert "PermissionError" in str(exc)

            assert device.connections == 1, device.connections
            assert [r for r in device.requests if r[0] == 6] == [
                (6, 50207, 65506), (6, 50000, 0x303),
            ]
            assert [r for r in device.requests if r[0] == 3]
    finally:
        if server is not None:
            server.close()
        broker.close()
        device.close()
    if device.errors:
        raise RuntimeError("loopback fake error: " + repr(device.errors))
    print("CONTROL_WIRE_SMOKE_OK: real FC03 + 2 FC06 over one 127.0.0.1 socket")


if __name__ == "__main__":
    main()
