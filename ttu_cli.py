#!/usr/bin/env python3
"""
ttu  —  CLI client for the awto serial daemon.

Usage:
    python ttu_cli.py "status"
    python ttu_cli.py "out 3 on" --timeout 1000
    python ttu_cli.py ping

Requires the free-threaded (no-GIL) CPython build: python3.13t
"""

import argparse
import logging
import logging.handlers
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from protocol import DEFAULT_SOCKET_PATH, DEFAULT_TIMEOUT_MS, send_request

# ---------------------------------------------------------------------------
# Logging  (syslog + stderr)
# ---------------------------------------------------------------------------

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    root = logging.getLogger()
    root.setLevel(level)

    try:
        syslog = logging.handlers.SysLogHandler(
            address="/dev/log",
            facility=logging.handlers.SysLogHandler.LOG_USER,
        )
        syslog.ident = "awto-ttu: "
        syslog.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(syslog)
    except OSError:
        pass

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(
        logging.Formatter("ttu[%(process)d]: %(levelname)-8s %(message)s")
    )
    root.addHandler(stderr)


log = logging.getLogger("cli")

# ---------------------------------------------------------------------------
# Daemon communication
# ---------------------------------------------------------------------------

def _call(req: dict) -> dict:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(DEFAULT_SOCKET_PATH)
            return send_request(sock, req)
    except FileNotFoundError:
        print(
            f"error: daemon socket not found at {DEFAULT_SOCKET_PATH}\n"
            "       Start the daemon first:  python serial_daemon.py",
            file=sys.stderr,
        )
        sys.exit(1)
    except ConnectionRefusedError:
        print(
            "error: daemon is not running\n"
            "       Start it with:  python serial_daemon.py",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Send ASCII commands to the awto serial daemon.",
        epilog="Examples:\n"
               "  ttu_cli.py status\n"
               "  ttu_cli.py 'out 3 on'\n"
               "  ttu_cli.py ping",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "command",
        help="ASCII command to send, or 'ping' to health-check the daemon.",
    )
    ap.add_argument(
        "--timeout", "-t",
        type=int,
        default=DEFAULT_TIMEOUT_MS,
        metavar="MS",
        help=f"response timeout in milliseconds (default {DEFAULT_TIMEOUT_MS})",
    )
    ap.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    args = ap.parse_args()

    _setup_logging(args.verbose)

    if args.command == "ping":
        resp = _call({"cmd": "ping"})
    else:
        log.debug("query: %r timeout=%dms", args.command, args.timeout)
        resp = _call({"cmd": "query", "line": args.command, "timeout_ms": args.timeout})

    if resp.get("ok"):
        print(resp.get("response", ""))
    else:
        print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
