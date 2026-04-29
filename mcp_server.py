#!/usr/bin/env python3
"""
awto-serial MCP server  —  exposes the serial daemon as MCP tools for Copilot.

Runs as a stdio MCP server (VS Code launches it automatically via mcp.json).
Connects to the serial daemon over the Unix socket; the daemon keeps the
serial port open between calls so there is no per-call startup cost.

Tools exposed to Copilot:
  serial_query(command, timeout_ms?)  — send command, return response
  serial_ping()                        — check daemon + serial port are up
"""

import logging
import logging.handlers
import socket
import sys
from pathlib import Path

# allow running from repo root without install
sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP

import os

from protocol import DEFAULT_SOCKET_PATH as _DEFAULT_SOCKET_PATH, DEFAULT_TIMEOUT_MS, make_ok, send_request

# Allow test harness (and systemd overrides) to redirect the socket path
DEFAULT_SOCKET_PATH = os.environ.get("AWTO_SOCKET", _DEFAULT_SOCKET_PATH)


def _sock_path() -> str:
    """Return socket path, honouring AWTO_SOCKET env var at call time."""
    return os.environ.get("AWTO_SOCKET", _DEFAULT_SOCKET_PATH)

# ---------------------------------------------------------------------------
# Logging  (syslog via /dev/log + stderr fallback)
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    try:
        syslog = logging.handlers.SysLogHandler(
            address="/dev/log",
            facility=logging.handlers.SysLogHandler.LOG_USER,
        )
        syslog.ident = "awto-mcp-server: "
        syslog.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(syslog)
    except OSError:
        pass

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(
        logging.Formatter("awto-mcp-server[%(process)d]: %(levelname)-8s %(name)s: %(message)s")
    )
    root.addHandler(stderr)


_setup_logging()
log = logging.getLogger("mcp")

mcp = FastMCP(
    "awto-serial",
    instructions="Persistent ASCII serial interface for embedded devices.",
)

# ---------------------------------------------------------------------------
# Daemon connection helper
# ---------------------------------------------------------------------------

def _daemon_query(req: dict) -> str:
    """Open a connection to the daemon, send *req*, return the response text.

    Raises RuntimeError on daemon / serial errors so Copilot gets a clear
    error message rather than a raw exception traceback.
    """
    # Read at call time so test/override via os.environ["AWTO_SOCKET"] is honoured
    sock_path = os.environ.get("AWTO_SOCKET", _DEFAULT_SOCKET_PATH)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(sock_path)
            resp = send_request(sock, req)
    except FileNotFoundError:
        raise RuntimeError(
            f"daemon socket not found at {sock_path}. "
            "Start the daemon first:  python serial_daemon.py"
        )
    except ConnectionRefusedError:
        raise RuntimeError(
            "daemon is not running. "
            "Start it with:  python serial_daemon.py"
        )
    except OSError as exc:
        raise RuntimeError(f"socket error: {exc}") from exc

    if not resp.get("ok"):
        raise RuntimeError(resp.get("error", "unknown daemon error"))

    return resp.get("response", "")


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
def serial_query(command: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> str:
    """Send an ASCII command to the serial device and return its response.

    Args:
        command:    The ASCII command line to send (newline appended automatically).
        timeout_ms: How long to wait for the response in milliseconds (default 500).

    Returns:
        The device's ASCII response, stripped of leading/trailing whitespace.
    """
    log.debug("serial_query: %r timeout=%dms", command, timeout_ms)
    result = _daemon_query({"cmd": "query", "line": command, "timeout_ms": timeout_ms})
    log.debug("serial_query response: %r", result[:120])
    return result


@mcp.tool()
def serial_ping() -> str:
    """Check that the serial daemon is running and the port is open.

    Returns:
        'ok' if the daemon responds, or an error message.
    """
    try:
        result = _daemon_query({"cmd": "ping"})
        log.info("ping ok: %s", result)
        return f"ok ({result})"
    except RuntimeError as exc:
        log.warning("ping failed: %s", exc)
        return f"error: {exc}"


@mcp.tool()
def serial_set_baud(baud: int) -> str:
    """Set the serial port baud rate live (e.g. 2480000).

    The change applies to all subsequent queries until changed again or
    the daemon restarts. Use this when you already know the device's baud
    rate; otherwise prefer ``serial_detect_baud``.

    Returns:
        'ok (baud=N)' on success, or an error message.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(_sock_path())
            resp = send_request(sock, {"cmd": "set_baud", "baud": int(baud)})
        if not resp.get("ok"):
            return f"error: {resp.get('error', 'unknown')}"
        return f"ok (baud={resp.get('baud')})"
    except OSError as exc:
        return f"error: {exc}"


@mcp.tool()
def serial_set_eol(eol: str) -> str:
    """Set the line ending used for outgoing commands.

    Args:
        eol: One of 'lf', 'cr', 'crlf' (matches tio --map convention).
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(_sock_path())
            resp = send_request(sock, {"cmd": "set_eol", "eol": eol})
        if not resp.get("ok"):
            return f"error: {resp.get('error', 'unknown')}"
        return f"ok (eol={resp.get('eol')})"
    except OSError as exc:
        return f"error: {exc}"


@mcp.tool()
def serial_detect_baud(probe: str = "?", timeout_ms: int = 200) -> str:
    """Auto-detect the device's baud rate by probing fastest-first.

    The daemon sends ``probe`` at each candidate rate (2_480_000 → 9600)
    and selects the first rate that returns valid printable ASCII.
    On success the daemon's active baud rate is updated.

    Returns:
        'detected baud=N' or an error message.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(_sock_path())
            resp = send_request(
                sock,
                {"cmd": "detect_baud", "probe": probe, "timeout_ms": int(timeout_ms)},
            )
        if not resp.get("ok"):
            return f"error: {resp.get('error', 'unknown')}"
        return f"detected baud={resp.get('baud')}"
    except OSError as exc:
        return f"error: {exc}"


@mcp.tool()
def serial_detect_eol(probe: str = "?", timeout_ms: int = 500) -> str:
    """Auto-detect the device's line ending (LF / CR / CRLF).

    On success the daemon's active EOL is updated so subsequent
    ``serial_query`` calls use the correct terminator.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(_sock_path())
            resp = send_request(
                sock,
                {"cmd": "detect_eol", "probe": probe, "timeout_ms": int(timeout_ms)},
            )
        if not resp.get("ok"):
            return f"error: {resp.get('error', 'unknown')}"
        return f"detected eol={resp.get('eol')}"
    except OSError as exc:
        return f"error: {exc}"


@mcp.tool()
def serial_info() -> dict:
    """Return current daemon state: port, baud, eol, is_open."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(_sock_path())
            resp = send_request(sock, {"cmd": "info"})
        if not resp.get("ok"):
            return {"error": resp.get("error", "unknown")}
        return resp.get("info", {})
    except OSError as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
