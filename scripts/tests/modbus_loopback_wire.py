"""Minimal synthetic Modbus TCP FC03/FC06 listener for 127.0.0.1 tests only.

Not a production Modbus client/server. It supports one unit (255), bounded
register requests and full MBAP framing; never accepts a non-loopback bind.
"""
from __future__ import annotations

import socket
import socketserver
import struct
import threading

from test_modbus_broker import Response


def exact_read(sock, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        chunk = sock.recv(count - len(result))
        if not chunk:
            raise ConnectionError("synthetic Modbus TCP connection ended")
        result.extend(chunk)
    return bytes(result)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        parent = self.server.device
        parent.clients += 1
        while True:
            try:
                head = exact_read(self.request, 7)
            except (ConnectionError, OSError, socket.timeout):
                break
            trans, proto, length, unit = struct.unpack(">HHHB", head)
            if proto != 0 or unit != 255 or length < 2 or length > 260:
                break
            payload = exact_read(self.request, length - 1)
            if payload[0] == 3 and len(payload) == 5:
                addr, count = struct.unpack(">HH", payload[1:])
                if not 1 <= count <= 125:
                    break
                parent.reads.append((addr, count))
                registers = [parent.regs.get(addr + i, 0) for i in range(count)]
                response = bytes([3, count * 2]) + struct.pack(
                    ">" + "H" * count, *registers)
            elif payload[0] == 6 and len(payload) == 5:
                addr, raw = struct.unpack(">HH", payload[1:])
                parent.writes.append((addr, raw))
                parent.regs[addr] = raw
                response = payload
            else:
                break
            self.request.sendall(struct.pack(
                ">HHHB", trans, 0, len(response) + 1, unit) + response)


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = False
    block_on_close = True


class FakeModbusTcp:
    def __init__(self, regs):
        self.regs = regs
        self.clients = 0
        self.reads = []
        self.writes = []
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.device = self
        assert self.server.server_address[0] == "127.0.0.1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_address[1]

    def connection(self):
        return FakeModbusClient("127.0.0.1", self.port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class FakeModbusClient:
    def __init__(self, host: str, port: int):
        if host != "127.0.0.1" or not 1 <= port <= 65535:
            raise ValueError("synthetic Modbus transport must be bound to loopback")
        self.host = host
        self.port = port
        self.connected = False
        self.socket = None
        self.transaction = 0

    def connect(self):
        self.socket = socket.create_connection((self.host, self.port), timeout=3)
        self.connected = True
        return True

    def close(self):
        if self.socket is not None:
            self.socket.close()
        self.socket = None
        self.connected = False

    def _exchange(self, function: int, address: int, count_or_value: int, *, device_id: int):
        if not self.connected or device_id != 255:
            raise ValueError("bad synthetic Modbus connection or unit")
        self.transaction = (self.transaction + 1) % 65536
        pdu = struct.pack(">BHH", function, address, count_or_value)
        self.socket.sendall(
            struct.pack(">HHHB", self.transaction, 0, len(pdu) + 1, device_id) + pdu)
        head = exact_read(self.socket, 7)
        trans, proto, length, unit = struct.unpack(">HHHB", head)
        if trans != self.transaction or proto != 0 or unit != device_id or length < 2:
            raise IOError("bad Modbus TCP response header")
        body = exact_read(self.socket, length - 1)
        if body[0] != function:
            raise IOError("unexpected Modbus response function")
        return body

    def read_holding_registers(self, addr, count=1, device_id=255):
        body = self._exchange(3, addr, count, device_id=device_id)
        if len(body) != 2 + 2 * count or body[1] != count * 2:
            raise IOError("bad Modbus FC03 response size")
        registers = struct.unpack(">" + "H" * count, body[2:])
        return Response(registers=registers)

    def write_register(self, addr, value, device_id=255):
        body = self._exchange(6, addr, value & 0xFFFF, device_id=device_id)
        if body != struct.pack(">BHH", 6, addr, value & 0xFFFF):
            raise IOError("bad Modbus FC06 echo")
        return Response()
