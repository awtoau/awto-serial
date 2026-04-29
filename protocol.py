"""
Shared protocol helpers for awto-mcp-serial.

The daemon and all clients speak JSON-lines over a Unix domain socket.

Request  (client → daemon):
    {"cmd": "query", "line": "<ascii command>", "timeout_ms": 500}\n

Response (daemon → client):
    {"ok": true,  "response": "<ascii response>"}\n
    {"ok": false, "error":    "<reason>"}\n

Special commands:
    {"cmd": "ping"}\n  →  {"ok": true, "response": "pong"}\n
"""

import json
import socket
from typing import Any

DEFAULT_SOCKET_PATH = "/tmp/awto-serial.sock"
DEFAULT_PORT        = "/dev/ttyACM0"
DEFAULT_BAUD        = 2_000_000
DEFAULT_TIMEOUT_MS  = 500


def send_request(sock: socket.socket, req: dict[str, Any]) -> dict[str, Any]:
    """Send a JSON-lines request and return the parsed response."""
    sock.sendall((json.dumps(req) + "\n").encode())
    return recv_response(sock)


def recv_response(sock: socket.socket) -> dict[str, Any]:
    """Read one newline-terminated JSON line from *sock*."""
    buf = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("daemon closed connection")
        buf.extend(chunk)
        if b"\n" in buf:
            line, _, _ = buf.partition(b"\n")
            return json.loads(line.decode())


def make_ok(response: str) -> dict[str, Any]:
    return {"ok": True, "response": response}


def make_err(error: str) -> dict[str, Any]:
    return {"ok": False, "error": error}
