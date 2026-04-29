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
    CANDIDATE_BAUDS,
    DEFAULT_BAUD,
    DEFAULT_EOL,
    DEFAULT_PORT,
    DEFAULT_SOCKET_PATH,
    EOL_BYTES,
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

    def __init__(self, port: str, baud: int, eol: str = DEFAULT_EOL) -> None:
        self._port = port
        self._baud = baud
        self._eol = eol
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    @property
    def baud(self) -> int:
        return self._baud

    @property
    def eol(self) -> str:
        return self._eol

    @property
    def port(self) -> str:
        return self._port

    # ------------------------------------------------------------------
    def open(self) -> None:
        self._ser = serial.Serial(
            self._port,
            baudrate=self._baud,
            timeout=0.01,       # non-blocking short reads
            write_timeout=0.2,
        )
        log.info("serial open: %s @ %d (eol=%s)", self._port, self._baud, self._eol)

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None

    # ------------------------------------------------------------------
    def set_baud(self, baud: int) -> None:
        """Change baud rate live. Raises SerialException if driver rejects it."""
        with self._lock:
            if self._ser is None or not self._ser.is_open:
                raise IOError("serial port not open")
            # pyserial setter does the platform-specific reconfigure
            self._ser.baudrate = baud
            self._baud = baud
            log.info("baud changed: %d", baud)

    def set_eol(self, eol: str) -> None:
        if eol not in EOL_BYTES:
            raise ValueError(f"eol must be one of {list(EOL_BYTES)}, got {eol!r}")
        with self._lock:
            self._eol = eol
            log.info("eol changed: %s", eol)

    def info(self) -> dict:
        is_open = bool(self._ser and self._ser.is_open)
        return {
            "port": self._port,
            "baud": self._baud,
            "eol": self._eol,
            "is_open": is_open,
        }

    # ------------------------------------------------------------------
    def query(self, line: str, timeout_ms: int) -> str:
        """Send *line* terminated by current EOL and collect the response.

        Returns as soon as a newline (\\n or \\r) is seen in the response,
        or when the deadline expires — whichever comes first.
        """
        with self._lock:
            return self._query_locked(line, timeout_ms)

    def _query_locked(self, line: str, timeout_ms: int) -> str:
        if self._ser is None or not self._ser.is_open:
            raise IOError("serial port not open")

        terminator = EOL_BYTES[self._eol]
        self._ser.reset_input_buffer()
        self._ser.write(line.encode() + terminator)
        self._ser.flush()

        deadline = time.monotonic() + timeout_ms / 1000.0
        buf = bytearray()

        while time.monotonic() < deadline:
            chunk = self._ser.read(4096)
            if chunk:
                buf.extend(chunk)
                # stop as soon as we have any complete line (CR or LF)
                if b"\n" in chunk or b"\r" in chunk:
                    break

        return buf.decode(errors="replace").strip()

    # ------------------------------------------------------------------
    def detect_baud(
        self,
        probe: str = "?",
        timeout_ms: int = 200,
        candidates: tuple[int, ...] | None = None,
    ) -> int:
        """Probe candidate baud rates fastest-first; return the one that yields valid ASCII.

        Scoring: response must contain >=4 bytes and >=80 % printable ASCII.
        """
        rates = candidates or CANDIDATE_BAUDS
        with self._lock:
            if self._ser is None:
                raise IOError("serial port not open")
            original = self._baud
            best_baud = None
            best_score = 0.0
            for rate in rates:
                try:
                    self._ser.baudrate = rate
                except (serial.SerialException, OSError) as exc:
                    log.debug("baud %d not supported by driver: %s", rate, exc)
                    continue
                self._baud = rate
                try:
                    resp = self._query_locked(probe, timeout_ms)
                except IOError:
                    continue
                score = _ascii_score(resp)
                log.debug("probe %d → %r (score=%.2f)", rate, resp[:40], score)
                if score >= 0.8 and len(resp) >= 4:
                    log.info("detect_baud: %d (score=%.2f, resp=%r)", rate, score, resp[:40])
                    return rate
                if score > best_score:
                    best_score = score
                    best_baud = rate
            # Nothing matched cleanly — restore original and fail
            self._ser.baudrate = original
            self._baud = original
            raise IOError(
                f"baud detect failed (best={best_baud} score={best_score:.2f}); "
                "device may be silent or use binary protocol"
            )

    def detect_eol(self, probe: str = "?", timeout_ms: int = 500) -> str:
        """Send a probe and infer line ending from response. Sets self._eol on success."""
        with self._lock:
            if self._ser is None or not self._ser.is_open:
                raise IOError("serial port not open")
            # Send with bare LF so we don't bias the result
            self._ser.reset_input_buffer()
            self._ser.write(probe.encode() + b"\n")
            self._ser.flush()

            deadline = time.monotonic() + timeout_ms / 1000.0
            buf = bytearray()
            while time.monotonic() < deadline:
                chunk = self._ser.read(4096)
                if chunk:
                    buf.extend(chunk)
                    # wait long enough to get a definitive terminator pair
                    if b"\r\n" in buf or buf.count(b"\n") >= 2 or buf.count(b"\r") >= 2:
                        break

            data = bytes(buf)
            if not data:
                raise IOError("detect_eol: no response from device")
            if b"\r\n" in data:
                detected = "crlf"
            elif b"\n" in data and b"\r" not in data:
                detected = "lf"
            elif b"\r" in data and b"\n" not in data:
                detected = "cr"
            else:
                # mixed / ambiguous — prefer crlf as it's the safe superset
                detected = "crlf"
            self._eol = detected
            log.info("detect_eol: %s (sample=%r)", detected, data[:40])
            return detected

    def ping(self) -> bool:
        if self._ser is None:
            return False
        return self._ser.is_open


def _ascii_score(s: str) -> float:
    """Fraction of characters in *s* that are printable ASCII or whitespace."""
    if not s:
        return 0.0
    good = sum(1 for c in s if 32 <= ord(c) < 127 or c in "\r\n\t")
    return good / len(s)


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

                elif cmd == "set_baud":
                    try:
                        worker.set_baud(int(req["baud"]))
                        _send(conn, {"ok": True, "baud": worker.baud})
                    except (KeyError, ValueError, IOError, serial.SerialException) as exc:
                        _send(conn, make_err(f"set_baud: {exc}"))

                elif cmd == "set_eol":
                    try:
                        worker.set_eol(req["eol"])
                        _send(conn, {"ok": True, "eol": worker.eol})
                    except (KeyError, ValueError) as exc:
                        _send(conn, make_err(f"set_eol: {exc}"))

                elif cmd == "detect_baud":
                    probe = req.get("probe", "?")
                    timeout_ms = int(req.get("timeout_ms", 200))
                    cands = req.get("candidates")
                    cands_t = tuple(int(x) for x in cands) if cands else None
                    try:
                        baud = worker.detect_baud(probe, timeout_ms, cands_t)
                        _send(conn, {"ok": True, "baud": baud})
                    except (IOError, serial.SerialException) as exc:
                        _send(conn, make_err(f"detect_baud: {exc}"))

                elif cmd == "detect_eol":
                    probe = req.get("probe", "?")
                    timeout_ms = int(req.get("timeout_ms", 500))
                    try:
                        eol = worker.detect_eol(probe, timeout_ms)
                        _send(conn, {"ok": True, "eol": eol})
                    except IOError as exc:
                        _send(conn, make_err(f"detect_eol: {exc}"))

                elif cmd == "info":
                    _send(conn, {"ok": True, "info": worker.info()})

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
    ap.add_argument("--eol",    default=DEFAULT_EOL,
                    choices=list(EOL_BYTES.keys()),
                    help="line ending used for outgoing query() calls")
    ap.add_argument("--socket", default=DEFAULT_SOCKET_PATH, help="Unix socket path")
    ap.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = ap.parse_args()

    _setup_logging("awto-serial-daemon", args.log_level)

    worker = SerialWorker(args.port, args.baud, eol=args.eol)
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
