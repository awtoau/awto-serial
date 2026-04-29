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

from protocol import DEFAULT_SOCKET_PATH, DEFAULT_TIMEOUT_MS, make_ok, send_request

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
    description="Persistent ASCII serial interface for embedded devices.",
)

# ---------------------------------------------------------------------------
# Daemon connection helper
# ---------------------------------------------------------------------------

def _daemon_query(req: dict) -> str:
    """Open a connection to the daemon, send *req*, return the response text.

    Raises RuntimeError on daemon / serial errors so Copilot gets a clear
    error message rather than a raw exception traceback.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(DEFAULT_SOCKET_PATH)
            resp = send_request(sock, req)
    except FileNotFoundError:
        raise RuntimeError(
            f"daemon socket not found at {DEFAULT_SOCKET_PATH}. "
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
