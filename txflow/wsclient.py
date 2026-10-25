"""Minimal RFC 6455 WebSocket client (Python 3 stdlib only), enough for a ws:// JSON stream.

Text frames only (client frames masked), fragmented messages reassembled, pings answered,
close frames reported as ConnectionError. No TLS: it is meant for the node's loopback port.
"""

import base64
import hashlib
import os
import socket
import struct
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WsClient:
    def __init__(self, url, timeout=10):
        u = urlparse(url)
        if u.scheme != "ws":
            raise ValueError("only ws:// URLs are supported")
        self.host, self.port, self.path = u.hostname, u.port or 80, u.path or "/"
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self._buf = b""
        self._handshake()

    def _handshake(self):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {self.path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed during handshake")
            self._buf += chunk
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise ConnectionError(f"handshake refused: {lines[0]}")
        want = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        hdrs = {k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
        if hdrs.get("sec-websocket-accept") != want:
            raise ConnectionError("bad Sec-WebSocket-Accept")

    def _send_frame(self, opcode, payload):
        n = len(payload)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(hdr + mask + body)

    def send(self, text):
        self._send_frame(0x1, text.encode())

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self):
        """Return the next text message (str). Raises socket.timeout or ConnectionError."""
        parts = []
        while True:
            b0, b1 = self._read(2)
            fin, opcode, n = b0 & 0x80, b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise ConnectionError("server sent close")
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            parts.append(data)
            if fin:
                return b"".join(parts).decode()

    def settimeout(self, t):
        self.sock.settimeout(t)

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self.sock.close()
