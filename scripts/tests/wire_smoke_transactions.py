#!/usr/bin/env python3
"""Real FC03/FC06 + UDS complete transaction smoke against 127.0.0.1 ONLY.

The synthetic server binds 127.0.0.1 on an OS-assigned ephemeral port.
No Solinteg host, Modbus env address, real inverter, or systemd service used.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path[:0] = [
    str(Path(__file__).resolve().parent),
    str(Path(__file__).resolve().parents[1] / "services"),
]

import inverter_control as control
from modbus_broker import Broker
from modbus_broker_control_client import BrokerModbusClient
from modbus_broker_control_transport import ControlModbusTransport
from modbus_broker_ipc import BrokerIpcClient, BrokerIpcServer
from wire_smoke_control import FakeInverterTcp


def main():
    device = FakeInverterTcp()
    device.start()
    broker = None
    server = None
    try:
        with tempfile.TemporaryDirectory() as path:
            sock = Path(path) / "broker.sock"
            role = {"value": "control"}
            with mock.patch.object(control, "ARMED", True), \
                 mock.patch.object(control, "SLAVE_ID", 255), \
                 mock.patch.object(control, "SIGN", "neg_charge"), \
                 mock.patch.object(control.RateLimiter, "wait"), \
                 mock.patch.object(control, "SETTLE_S", 0):
                control._forced_active = False
                broker = Broker(
                    lambda: ControlModbusTransport(
                        "127.0.0.1", port=device.port, timeout=5,
                    ),
                    armed=True, runtime_mode="control",
                    transactions_enabled=True, charge_sign="neg_charge",
                    min_write_interval_s=0,
                )
                server = BrokerIpcServer(
                    sock, broker, role_for_peer=lambda *_: role["value"],
                )
                server.start()
                client = BrokerModbusClient(
                    ipc_client=BrokerIpcClient(sock), transaction_mode=True,
                )
                assert client.connect()
                assert client.run_transaction("charge", watts=300)["status"] == "applied"
                assert device.registers[50207] == 65506
                assert device.registers[50000] == 0x303
                assert client.run_transaction("charge", watts=500)["status"] == "applied"
                assert device.registers[50207] == 65486
                assert client.run_transaction("auto")["status"] == "applied"
                assert device.registers[50000] == 0x101
                assert device.registers[50207] == 0

                role["value"] = "read"
                registers = BrokerIpcClient(sock).request(
                    "read_registers", address=33000, count=4,
                )["registers"]
                assert registers[0] == 5000
                try:
                    BrokerIpcClient(sock).request(
                        "transaction_charge", watts=900, request_id="blocked",
                    )
                except RuntimeError as exc:
                    assert "PermissionError" in str(exc)
                else:
                    raise AssertionError("poller incorrectly accepted control")

                assert device.connections == 1, device.connections
                fc06 = [r for r in device.requests if r[0] == 6]
                assert [addr for _, addr, _ in fc06] == [
                    50209, 50208, 50210, 50207, 50000,
                    50207,
                    50208, 50209, 50000, 50207,
                ], fc06
                print("TRANSACTION_WIRE_SMOKE_OK: one 127.0.0.1 TCP client, "
                      "charge, fast retarget, auto, poller FC03, 10 FC06")
    finally:
        if server is not None:
            server.close()
        if broker is not None:
            broker.close()
        device.close()
        control._forced_active = False
    if device.errors:
        raise RuntimeError("loopback fake failed: " + repr(device.errors))


if __name__ == "__main__":
    main()
