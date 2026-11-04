"""Optional ModbusTcpClient-compatible shim backed by the single-owner IPC broker.

Used only when the dispatch service explicitly selects a broker mode.
The optional transaction_mode sends whole actions to the broker worker;
the existing inverter_control algorithms execute there unchanged.
The old watchdog remains unchanged. This object never creates an IP socket.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from modbus_broker_ipc import BrokerIpcClient

DEFAULT_SOCKET = "/run/solinteg-broker/broker.sock"


class _Reply:
    def __init__(self, registers=()):
        self.registers = list(registers)

    def isError(self):
        return False


class BrokerModbusClient:
    def __init__(self, socket_path: str = DEFAULT_SOCKET, *, ipc_client=None,
                 transaction_mode: bool = False):
        if socket_path != DEFAULT_SOCKET and ipc_client is None:
            raise ValueError("unapproved broker socket path")
        self._ipc = ipc_client or BrokerIpcClient(Path(socket_path))
        self.transaction_mode = transaction_mode
        self.connected = False

    def connect(self):
        info = self._ipc.request("status", timeout_s=5)
        if info.get("control_runtime") is not True or info.get("armed") is not True:
            raise PermissionError("broker is not armed for dispatcher control")
        if self.transaction_mode and info.get("transactions_enabled") is not True:
            raise PermissionError("broker does not support complete control transactions")
        self.connected = True
        return True

    def is_socket_open(self):
        return self.connected

    def read_holding_registers(self, address, *, count=1, device_id=255):
        if device_id != 255 or not self.connected:
            raise ConnectionError("broker control connection not ready")
        result = self._ipc.request(
            "read_registers", address=address, count=count, timeout_s=30,
        )
        registers = result.get("registers")
        if (not isinstance(registers, list) or len(registers) != count
                or any(type(v) is not int or not 0 <= v <= 65535
                       for v in registers)):
            raise RuntimeError("broker returned incomplete/invalid FC03 reply")
        return _Reply(registers)

    def write_register(self, address, value, *, device_id=255):
        if device_id != 255 or not self.connected:
            raise ConnectionError("broker control connection not ready")
        result = self._ipc.request(
            "write_register", address=address, value=value,
            request_id=uuid.uuid4().hex, timeout_s=30,
        )
        if result.get("status") != "sent_unverified":
            raise RuntimeError("broker FC06 outcome is not acknowledged")
        return _Reply()

    def run_transaction(self, action: str, *, watts: int | None = None,
                        min_soc_pct: float | None = None) -> dict:
        if not self.transaction_mode:
            raise PermissionError("complete transactions require broker_transaction mode")
        if not self.connected:
            self.connect()
        return self._ipc.request(
            "transaction_" + action, watts=watts, min_soc_pct=min_soc_pct,
            request_id=uuid.uuid4().hex, timeout_s=90,
        )

    def close(self):
        self.connected = False
