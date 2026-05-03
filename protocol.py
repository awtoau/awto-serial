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

# ---------------------------------------------------------------------------
# Known USB-serial devices
# (VID, PID) → dict with chip name, typical baud, and free-text notes.
# Used by consumers (e.g. awto-mcp-riden) to score / prefer ports.
# ---------------------------------------------------------------------------
KNOWN_DEVICES: dict[tuple[int, int], dict] = {
    # QinHeng Electronics — CH340/341/343 family (Riden RD60xx / RK60xx use these)
    (0x1A86, 0x7523): dict(chip="CH340",  typical_baud=115_200,
                           notes="Riden RD60xx/RK60xx USB-serial adapter"),
    (0x1A86, 0x5523): dict(chip="CH341",  typical_baud=115_200,
                           notes="CH341 — common on Riden clones and Arduino nano"),
    (0x1A86, 0x55D4): dict(chip="CH343",  typical_baud=115_200,
                           notes="CH343 — higher-speed variant, some Riden BT dongles"),
    (0x1A86, 0xE018): dict(chip="CH9102", typical_baud=115_200,
                           notes="CH9102F — seen on some Riden OEM cables"),
    # FTDI
    (0x0403, 0x6001): dict(chip="FT232R", typical_baud=115_200,
                           notes="FTDI FT232R — generic USB-serial"),
    (0x0403, 0x6010): dict(chip="FT2232", typical_baud=115_200,
                           notes="FTDI FT2232 — dual-channel"),
    (0x0403, 0x6014): dict(chip="FT232H", typical_baud=115_200,
                           notes="FTDI FT232H — high-speed"),
    # Silicon Labs CP210x
    (0x10C4, 0xEA60): dict(chip="CP2102", typical_baud=115_200,
                           notes="CP210x — common USB-serial bridge"),
    # Prolific
    (0x067B, 0x2303): dict(chip="PL2303", typical_baud=115_200,
                           notes="Prolific PL2303 — legacy USB-serial"),
    # WCH CH32/CH32V RISC-V dev boards (CDC-ACM)
    (0x1A86, 0x8010): dict(chip="CH32V-CDC", typical_baud=2_000_000,
                           notes="WCH CH32V CDC-ACM (awto firmware default)"),
    (0x1A86, 0x8012): dict(chip="CH32V-CDC", typical_baud=2_000_000,
                           notes="WCH CH32V CDC-ACM variant"),
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
