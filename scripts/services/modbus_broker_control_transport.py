"""Allowlisted FC03/FC06 transport owned exclusively by a broker worker.

Never construct or use this directly from the poller/dispatcher. The control
service's root-approved marker and systemd identity gate this transport.
"""
from __future__ import annotations

from modbus_broker import CONTROL_READS, CONTROL_WRITES
from modbus_broker_readonly_transport import (
    DEVICE_ID, READ_BLOCKS, ReadOnlyModbusTransport,
)


class ControlModbusTransport(ReadOnlyModbusTransport):
    def read_holding_registers(self, addr: int, count: int = 1,
                               device_id: int = DEVICE_ID):
        if (type(addr) is not int or type(count) is not int
                or (addr, count) not in READ_BLOCKS | CONTROL_READS
                or device_id != DEVICE_ID):
            raise PermissionError("FC03 not allowlisted for control broker")
        if not self.connected:
            raise ConnectionError("control broker transport disconnected")
        try:
            return self._client.read_holding_registers(
                addr, count=count, device_id=DEVICE_ID,
            )
        except Exception as exc:
            self.close()
            raise ConnectionError("broker FC03 failed") from exc

    def write_register(self, addr: int, value: int, device_id: int = DEVICE_ID):
        if (type(addr) is not int or addr not in CONTROL_WRITES
                or type(value) is not int or not 0 <= value <= 65535
                or device_id != DEVICE_ID):
            raise PermissionError("FC06 not allowlisted for control broker")
        if not self.connected:
            raise ConnectionError("control broker transport disconnected")
        try:
            return self._client.write_register(addr, value, device_id=DEVICE_ID)
        except Exception as exc:
            self.close()
            raise ConnectionError("broker FC06 result unknown") from exc
