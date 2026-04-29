#!/usr/bin/env python3
"""
awto-serial-daemon  —  owns the serial port, multiplexes it over a Unix socket.

Usage:
    python serial_daemon.py [--port /dev/ttyACM0] [--baud 2000000]
                             [--socket /tmp/awto-serial.sock]

Clients connect to the Unix socket and exchange JSON-lines (see protocol.py).
The daemon serialises all serial access through a threading.Lock so multiple
clients (CLI, MCP server, test scripts) can coexist safely.

Requires the free-threaded (no-GIL) CPython build: python3.13t
"""

import argparse
import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time

import serial

from protocol import (
    DEFAULT_BAUD,
    DEFAULT_PORT,
    DEFAULT_SOCKET_PATH,
    make_err,
    make_ok,
)

log = logging.getLogger("daemon")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging(ident: str, level_name: str) -> None:
    """Configure syslog + stderr logging.

    Syslog entries appear in journald / /var/log/syslog as:
        awto-serial-daemon[PID]: LEVEL daemon: message
    """
    level = getattr(logging, level_name.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    # --- syslog handler (journald / /dev/log) ---
    try:
        syslog = logging.handlers.SysLogHandler(
            address="/dev/log",
            facility=logging.handlers.SysLogHandler.LOG_DAEMON,
        )
        syslog.ident = f"{ident}: "          # prepended to every message
        # Use syslog priority mapping so journald assigns correct severity
        syslog.mapPriority = logging.handlers.SysLogHandler.mapPriority  # type: ignore[method-assign]
        syslog_fmt = logging.Formatter("%(levelname)s %(name)s: %(message)s")
        syslog.setFormatter(syslog_fmt)
        root.addHandler(syslog)
    except OSError:
        pass  # /dev/log absent (e.g. minimal container) — fall through to stderr only

    # --- stderr handler (interactive / systemd ExecStart journal fallback) ---
    stderr = logging.StreamHandler(sys.stderr)
    stderr_fmt = logging.Formatter(
        f"{ident}[%(process)d]: %(levelname)-8s %(name)s: %(message)s"
    )
    stderr.setFormatter(stderr_fmt)
    root.addHandler(stderr)


# ---------------------------------------------------------------------------
# Serial worker
# ---------------------------------------------------------------------------

class SerialWorker:
    """Owns the serial port and exposes a thread-safe query() method."""

    def __init__(self, port: str, baud: int) -> None:
        self._port = port
        self._baud = baud
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def open(self) -> None:
        self._ser = serial.Serial(
            self._port,
            baudrate=self._baud,
            timeout=0.01,       # non-blocking short reads
            write_timeout=0.2,
        )
        log.info("serial open: %s @ %d", self._port, self._baud)

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None

    # ------------------------------------------------------------------
    def query(self, line: str, timeout_ms: int) -> str:
        """Send *line* and collect the response within *timeout_ms*.

        Returns as soon as a newline is seen in the response, or when the
        deadline expires — whichever comes first.
        """
        with self._lock:
            if self._ser is None or not self._ser.is_open:
                raise IOError("serial port not open")

            self._ser.reset_input_buffer()
            self._ser.write((line + "\n").encode())
            self._ser.flush()

            deadline = time.monotonic() + timeout_ms / 1000.0
            buf = bytearray()

            while time.monotonic() < deadline:
                chunk = self._ser.read(4096)
                if chunk:
                    buf.extend(chunk)
                    if b"\n" in chunk:   # stop as soon as we have a complete line
                        break

            return buf.decode(errors="replace").strip()

    def ping(self) -> bool:
        if self._ser is None:
            return False
        return self._ser.is_open


# ---------------------------------------------------------------------------
# Client connection handler
# ---------------------------------------------------------------------------

def handle_client(conn: socket.socket, addr: str, worker: SerialWorker) -> None:
    log.debug("client connected: %s", addr)
    buf = bytearray()
    try:
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)

            # process all complete lines in the buffer
            while b"\n" in buf:
                raw, _, buf = buf.partition(b"\n")
                if not raw.strip():
                    continue
                try:
                    req = json.loads(raw.decode())
                except json.JSONDecodeError as exc:
                    _send(conn, make_err(f"bad JSON: {exc}"))
                    continue

                cmd = req.get("cmd", "")

                if cmd == "ping":
                    _send(conn, make_ok("pong"))

                elif cmd == "query":
                    line_str = req.get("line", "")
                    timeout_ms = int(req.get("timeout_ms", 500))
                    try:
                        resp = worker.query(line_str, timeout_ms)
                        _send(conn, make_ok(resp))
                    except IOError as exc:
                        _send(conn, make_err(str(exc)))

                else:
                    _send(conn, make_err(f"unknown cmd: {cmd!r}"))

    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        conn.close()
        log.debug("client disconnected: %s", addr)


def _send(conn: socket.socket, obj: dict) -> None:
    try:
        conn.sendall((json.dumps(obj) + "\n").encode())
    except (BrokenPipeError, OSError):
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="awto serial daemon")
    ap.add_argument("--port",   default=DEFAULT_PORT,        help="serial device")
    ap.add_argument("--baud",   default=DEFAULT_BAUD, type=int, help="baud rate")
    ap.add_argument("--socket", default=DEFAULT_SOCKET_PATH, help="Unix socket path")
    ap.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = ap.parse_args()

    _setup_logging("awto-serial-daemon", args.log_level)

    worker = SerialWorker(args.port, args.baud)
    try:
        worker.open()
    except serial.SerialException as exc:
        log.error("cannot open serial port: %s", exc)
        sys.exit(1)

    # remove stale socket
    if os.path.exists(args.socket):
        os.unlink(args.socket)

    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(args.socket)
    os.chmod(args.socket, 0o600)
    server_sock.listen(8)

    log.info("listening on %s  (ctrl-c to stop)", args.socket)

    try:
        while True:
            conn, _ = server_sock.accept()
            addr = conn.fileno()
            t = threading.Thread(
                target=handle_client,
                args=(conn, addr, worker),
                daemon=True,
            )
            t.start()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server_sock.close()
        if os.path.exists(args.socket):
            os.unlink(args.socket)
        worker.close()


if __name__ == "__main__":
    main()
