"""Read-only pymodbus transport for a separately approved broker poller cutover.

Construction imports no pymodbus and opens no socket. connect() is explicit.
Even a mistakenly armed broker cannot use this transport to perform FC06:
the only callable protocol operation forwards fixed, allowlisted FC03 blocks.
"""
from __future__ import annotations

import math

# Exact blocks decoded by modbus_broker.Broker._snapshot (and nothing else).
READ_BLOCKS = frozenset({
    (50000, 1), (10994, 36), (11000, 30),
    (30258, 2), (33000, 4), (50006, 1),
})
DEVICE_ID = 255


class ReadOnlyModbusTransport:
    def __init__(self, host: str, *, port: int = 502, timeout: float = 10.0,
                 client_factory=None):
        if not isinstance(host, str) or not host.strip():
            raise ValueError("inverter host must be configured")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid inverter port")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError("invalid Modbus timeout")
        if client_factory is None:
            from pymodbus.client import ModbusTcpClient
            client_factory = ModbusTcpClient
        self._client = client_factory(host, port=port, timeout=timeout)
        self._connected = False

    @property
    def connected(self) -> bool:
        # pymodbus 3.13+ has a 'connected' property; fake clients expose it too.
        return self._connected and bool(self._client.connected)

    def connect(self) -> bool:
        if self.connected:
            return True
        try:
            self._connected = bool(self._client.connect())
        except Exception as exc:
            self.close()
            raise ConnectionError("read-only Modbus connection failed") from exc
        return self.connected

    def read_holding_registers(self, addr: int, count: int = 1,
                               device_id: int = DEVICE_ID):
        if (type(addr) is not int or type(count) is not int
                or (addr, count) not in READ_BLOCKS or device_id != DEVICE_ID):
            raise PermissionError("FC03 block or slave ID not approved for read-only broker")
        if not self.connected:
            raise ConnectionError("read-only Modbus transport disconnected")
        try:
            return self._client.read_holding_registers(addr, count=count, device_id=DEVICE_ID)
        except Exception as exc:
            self.close()
            raise ConnectionError("read-only Modbus FC03 request failed") from exc

    def write_register(self, *args, **kwargs):
        raise PermissionError("FC06 is unavailable in read-only transport")

    def close(self) -> None:
        self._connected = False
        self._client.close()
