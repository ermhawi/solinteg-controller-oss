"""Local Unix-domain socket IPC for Solinteg's injected Modbus broker.

Phases 0-2 used in-memory fake transport. An opt-in READ-ONLY transport and
service entry point are staged for a future cutover, NOT installed here.
Roles are derived from peer OS credentials, NEVER from request JSON.
A future control/safety migration needs distinct UIDs and host fencing.
"""
from __future__ import annotations

import json
import os
import socket
import socketserver
import struct
import threading
from pathlib import Path

from modbus_broker import Broker

MAX_MESSAGE_BYTES = 16 * 1024


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = True

    def __init__(self, path: str, broker: Broker, role_for_peer):
        self.broker = broker
        self.role_for_peer = role_for_peer
        super().__init__(path, _Handler)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        response = None
        try:
            # Linux SO_PEERCRED: 3 native int32s (pid, uid, gid).
            peer = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                            struct.calcsize("3i"))
            pid, uid, gid = struct.unpack("3i", peer)
            role = self.server.role_for_peer(pid, uid, gid)
            if role not in {"read", "control", "safety"}:
                raise PermissionError("unauthorized IPC peer")
            raw = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
            if not raw or len(raw) > MAX_MESSAGE_BYTES or not raw.endswith(b"\n"):
                raise ValueError("missing/oversize IPC request")
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError("expected request object")
            if set(message) - {"operation", "request_id", "watts", "timeout_s",
                               "address", "count", "value"}:
                raise ValueError("unsupported IPC request field")
            response = self.server.broker.submit(
                message.get("operation"), role=role, request_id=message.get("request_id"),
                watts=message.get("watts"), address=message.get("address"),
                count=message.get("count"), value=message.get("value"),
                timeout_s=message.get("timeout_s", 30),
            )
            envelope = {"ok": True, "result": response}
        except (ValueError, TypeError, PermissionError, RuntimeError, TimeoutError) as exc:
            envelope = {"ok": False, "error": type(exc).__name__}
        self.wfile.write((json.dumps(envelope, separators=(",", ":")) + "\n").encode())


class BrokerIpcServer:
    """Single local socket; no default address, start must be explicit."""

    def __init__(self, socket_path: Path, broker: Broker, *, role_for_peer=None):
        self.path = Path(socket_path)
        self.broker = broker
        self._resolver = role_for_peer or (
            lambda pid, uid, gid: "read" if uid == os.getuid() else None
        )
        self._server = None
        self._thread = None
        self._inode = None

    def start(self):
        if self._server is not None:
            raise RuntimeError("IPC server already started")
        if not self.path.parent.is_dir() or self.path.exists() or self.path.is_symlink():
            raise RuntimeError("IPC directory missing or socket path already owned")
        if self.path.parent.stat().st_mode & 0o022:
            raise PermissionError("IPC directory must not be group/world writable")
        self._server = _UnixServer(str(self.path), self.broker, self._resolver)
        os.chmod(self.path, 0o600)
        self._inode = self.path.stat().st_ino
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="offline-broker-ipc", daemon=True)
        self._thread.start()

    def close(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=10)
            self._server = None
        if self.path.is_socket() and self.path.stat().st_ino == self._inode:
            self.path.unlink()
        self._inode = None


class BrokerIpcClient:
    def __init__(self, socket_path: Path):
        self.path = str(socket_path)

    def request(self, operation: str, *, timeout_s: float = 30, **kwargs) -> dict:
        request = json.dumps({"operation": operation, "timeout_s": timeout_s, **kwargs})
        if len(request.encode()) >= MAX_MESSAGE_BYTES:
            raise ValueError("request too large")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(timeout_s + 2)
            peer.connect(self.path)
            peer.sendall((request + "\n").encode())
            incoming = b""
            while not incoming.endswith(b"\n"):
                packet = peer.recv(4096)
                if not packet or len(incoming) + len(packet) > MAX_MESSAGE_BYTES * 4:
                    raise RuntimeError("incomplete/oversize IPC response")
                incoming += packet
        result = json.loads(incoming)
        if not result.get("ok"):
            raise RuntimeError("broker IPC rejected request: " + str(result.get("error")))
        return result["result"]
