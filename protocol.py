"""
Shared protocol helpers for awto-mcp-serial.

The daemon and all clients speak JSON-lines over a Unix domain socket.

Requests  (client → daemon):
    {"cmd": "ping"}
    {"cmd": "query",        "line": "<ascii>", "timeout_ms": 500}
    {"cmd": "set_baud",     "baud": 2480000}
    {"cmd": "set_eol",      "eol": "lf"|"cr"|"crlf"}
    {"cmd": "detect_baud",  "probe": "?\\n", "timeout_ms": 200, "candidates": [...]?}
    {"cmd": "detect_eol",   "probe": "?\\n", "timeout_ms": 500}
    {"cmd": "info"}                          # report current port/baud/eol

Responses (daemon → client):
    {"ok": true,  "response": "<ascii>"}      # query / ping
    {"ok": true,  "baud": 2480000}            # set_baud / detect_baud
    {"ok": true,  "eol": "crlf"}              # set_eol / detect_eol
    {"ok": true,  "info": {...}}              # info
    {"ok": false, "error": "<reason>"}

Naming follows tio(1) where possible:
    eol "lf"   → tio --map=ODELBS / output mode lflf
    eol "cr"   → tio --map=OCRNL
    eol "crlf" → tio default for Windows-line devices
"""

import json
import socket
from typing import Any

DEFAULT_SOCKET_PATH = "/tmp/awto-serial.sock"
DEFAULT_PORT        = "/dev/ttyACM0"
DEFAULT_BAUD        = 2_000_000
DEFAULT_TIMEOUT_MS  = 500
DEFAULT_EOL         = "lf"

# Candidate baud rates for detect_baud(), FASTEST FIRST so that working
# high-speed devices (the common case) are detected immediately.
# 2_480_000 is included because it is the user's normal CDC-ACM rate.
# Standard termios Bxxx values for Linux + arbitrary integers that
# common USB-serial drivers (FTDI, CP210x, CH343, CDC-ACM) accept.
CANDIDATE_BAUDS: tuple[int, ...] = (
    2_480_000,   # user's preferred rate
    2_000_000,
    1_500_000,
    1_152_000,
    1_000_000,
    921_600,
    576_000,
    500_000,
    460_800,
    230_400,
    115_200,
    57_600,
    38_400,
    19_200,
    9_600,
)

EOL_BYTES = {
    "lf":   b"\n",
    "cr":   b"\r",
    "crlf": b"\r\n",
}


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
