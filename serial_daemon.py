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
import collections
import datetime
import json
import logging
import logging.handlers
import os
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable
import serial
import serial.tools.list_ports

# Windows registry access for FTDI latency timer fix
if sys.platform == "win32":
    import winreg


# ---------------------------------------------------------------------------
# Line Transform Pipeline (miniterm-compatible)
# ---------------------------------------------------------------------------
# Extract line-by-line transformations for serial responses, inspired by
# pyserial's miniterm transform pipeline.

class LineTransform:
    """Base class: do-nothing, forward all data unchanged."""

    def transform(self, text: str) -> str:
        """Transform received line."""
        return text


class TransformCRLF(LineTransform):
    """Normalize different line endings to LF."""

    def transform(self, text: str) -> str:
        """Replace CRLF and CR with LF."""
        return text.replace("\r\n", "\n").replace("\r", "\n")


class TransformHexDump(LineTransform):
    """True hex dump: every non-printable byte shown as [0xNN], no exceptions.

    Use this when you want to see exactly what the device sent — LF, CR, tab,
    ESC and all. For a more readable view that keeps line layout, use 'safe'
    or 'visualize-controls'.
    """

    def transform(self, text: str) -> str:
        result = []
        for c in text:
            if 32 <= ord(c) <= 126:
                result.append(c)
            else:
                result.append(f"[0x{ord(c):02x}]")
        return "".join(result)


class TransformSafe(LineTransform):
    """cat -v style: make bytes safe to display in a terminal without side effects.

    Replaces control bytes with caret notation (^G for bell, ^[ for ESC, etc.)
    and high bytes with M- prefix, so nothing rings the bell, moves the cursor,
    or otherwise drives the terminal. Keeps \\n and \\t literal so line layout
    and indentation are preserved.
    """

    def transform(self, text: str) -> str:
        result = []
        for c in text:
            o = ord(c)
            if c in "\n\t":
                result.append(c)
            elif 32 <= o <= 126:
                result.append(c)
            elif o < 32:
                result.append("^" + chr(o + 64))  # 0x07 -> ^G, 0x1b -> ^[
            elif o == 0x7F:
                result.append("^?")
            elif o < 0xA0:
                # High control range 0x80-0x9F → M-^X
                result.append("M-^" + chr((o - 0x80) + 64))
            else:
                # Printable high byte 0xA0-0xFF → M-x
                result.append("M-" + chr(o - 0x80))
        return "".join(result)


class TransformVisualizeControls(LineTransform):
    """Visualize control characters using Unicode symbols (like miniterm)."""

    # Map control codes to Unicode symbols (U+2400 block)
    CONTROL_MAP = {
        ord(c): 0x2400 + ord(c) for c in map(chr, range(32)) if c not in "\r\n\t"
    }
    CONTROL_MAP[0x7F] = 0x2421  # DEL
    CONTROL_MAP[0x9B] = 0x2425  # CSI

    def transform(self, text: str) -> str:
        """Replace control codes with Unicode symbols."""
        return text.translate(self.CONTROL_MAP)


# Registry of available transforms
LINE_TRANSFORMS = {
    "crlf": TransformCRLF,
    "hex": TransformHexDump,
    "safe": TransformSafe,
    "visualize-controls": TransformVisualizeControls,
}

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

_VALID_MAPS: frozenset[str] = frozenset({"INLCRNL", "ICRNL", "ONLCRNL", "ODELBS"})
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_RESET = "\x1b[0m"
_ANSI_TX = "\x1b[2;36m"
_EXEC_TIMEOUT_MS_MAX = 60_000
_EXEC_OUTPUT_BYTES_MAX = 65_536
_EXEC_STDERR_BYTES_MAX = 4_096
_FTDI_VID = 0x0403


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class _InteractiveStderrFormatter(logging.Formatter):
    """Optional color formatter for interactive stderr output."""

    def __init__(self, fmt: str, enable_color: bool) -> None:
        super().__init__(fmt)
        self._enable_color = enable_color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self._enable_color:
            return text
        # Keep RX plain by default; dim/cyan highlight TX tokens only.
        return re.sub(r"\bTX\b", f"{_ANSI_TX}TX{_ANSI_RESET}", text)


def _use_stderr_color(stream) -> bool:
    """Enable ANSI color only for interactive terminals and when NO_COLOR is unset."""
    if "NO_COLOR" in os.environ:
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _make_stderr_formatter(ident: str, stream) -> logging.Formatter:
    fmt = f"{ident}[%(process)d]: %(levelname)-8s %(name)s: %(message)s"
    return _InteractiveStderrFormatter(fmt, enable_color=_use_stderr_color(stream))

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
    stderr.setFormatter(_make_stderr_formatter(ident, sys.stderr))
    root.addHandler(stderr)


# ---------------------------------------------------------------------------
# DTR / RTS line control (shared by SerialWorker and discover())
# ---------------------------------------------------------------------------

def _serial_set_line(ser: serial.Serial, line: str, state: str) -> None:
    """Drive DTR or RTS on an open serial port. state: 'high'|'low'|'toggle'.

    Free helper so both SerialWorker.set_line and discover()'s DTR probing
    drive the modem line through one implementation. Caller holds any lock and
    guarantees the port is open.
    """
    line = line.lower()
    state = state.lower()
    if line not in ("dtr", "rts"):
        raise ValueError(f"line must be 'dtr' or 'rts', got {line!r}")
    if state not in ("high", "low", "toggle"):
        raise ValueError(f"state must be 'high'|'low'|'toggle', got {state!r}")
    current = ser.dtr if line == "dtr" else ser.rts
    new_val = (not current) if state == "toggle" else (state == "high")
    if line == "dtr":
        ser.dtr = new_val
    else:
        ser.rts = new_val


def _serial_pulse_line(ser: serial.Serial, line: str, duration_ms: int = 100) -> None:
    """Pulse DTR or RTS: assert high, hold *duration_ms*, then low.

    The hold is a genuine hardware requirement, not an arbitrary wait: many
    USB-serial adapters wire DTR/RTS to the target's reset/boot pins, and the
    MCU needs the line held for a bounded window to register the level. 100 ms
    is the long-standing default (matches esptool's reset pulse and the prior
    pulse_line default); callers tune it per adapter.
    """
    line = line.lower()
    if line not in ("dtr", "rts"):
        raise ValueError(f"line must be 'dtr' or 'rts', got {line!r}")
    if line == "dtr":
        ser.dtr = True
        time.sleep(duration_ms / 1000.0)
        ser.dtr = False
    else:
        ser.rts = True
        time.sleep(duration_ms / 1000.0)
        ser.rts = False


# ---------------------------------------------------------------------------
# Serial worker
# ---------------------------------------------------------------------------

class SerialWorker:
    """Owns the serial port and exposes a thread-safe query() method."""

    def __init__(self, port: str, baud: int, eol: str = DEFAULT_EOL,
                 history_size: int = 1000) -> None:
        self._port = port
        self._baud = baud
        self._eol = eol
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()
        self._maps: frozenset[str] = frozenset()
        self._log_path: str | None = None
        self._log_file = None
        self._log_lock = threading.Lock()
        self._log_strip = False
        self._log_max_bytes = 0
        self._log_backups = 0
        self._ts_format: str | None = None
        self._echo = False

        # RX/TX statistics
        self._rx_bytes = 0
        self._tx_bytes = 0
        self._error_count = 0
        self._start_time = time.monotonic()
        self._stats_lock = threading.Lock()

        # RX ring buffer (background reader)
        self._history: collections.deque = collections.deque(maxlen=history_size)
        self._history_lock = threading.Lock()
        self._rx_thread: threading.Thread | None = None
        self._rx_stop = threading.Event()
        self._drain_buffer = bytearray()
        self._drain_limit = 64 * 1024
        self._drain_condition = threading.Condition()

        # Auto-reconnect state
        self._reconnecting = False
        self._reconnect_lock = threading.Lock()

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
        # Windows FTDI adapter: reduce latency timer from default 16 ms to 1 ms
        _reduce_ftdi_latency_timer(self._port, self._ser)
        log.info("serial open: %s @ %d (eol=%s)", self._port, self._baud, self._eol)
        self._start_rx_thread()

    def close(self) -> None:
        self._stop_rx_thread()
        self.log_stop()
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
            "echo": self._echo,
            "is_open": is_open,
            "maps": sorted(self._maps),
            "log_path": self._log_path,
            "log_strip": self._log_strip,
            "ts_format": self._ts_format,
        }

    # ------------------------------------------------------------------
    def query(self, line: str, timeout_ms: int) -> str:
        """Send *line* terminated by current EOL and collect the response.

        Returns as soon as a newline (\\n or \\r) is seen in the response,
        or when the deadline expires — whichever comes first.
        """
        with self._lock:
            raw, _terminated = self._query_locked(line, timeout_ms)
            return self._decode_response(raw)

    def query_hex(self, line: str, timeout_ms: int) -> str:
        """Send query and return response bytes as lowercase space-separated hex."""
        with self._lock:
            raw, _terminated = self._query_locked(line, timeout_ms)
            return " ".join(f"{b:02x}" for b in raw)

    def query_hex_full(self, line: str, timeout_ms: int) -> dict:
        """Like query_hex() but includes warning when bytes are unterminated."""
        with self._lock:
            raw, terminated = self._query_locked(line, timeout_ms)
            hex_resp = " ".join(f"{b:02x}" for b in raw)
            if self._echo:
                prefix = " ".join(f"{b:02x}" for b in self._apply_output_map(line.encode()))
                hex_resp = f"{prefix}\n{hex_resp}" if hex_resp else prefix
            out: dict = {"response": hex_resp}
            if not terminated and raw:
                out["warning"] = (
                    "unterminated response: data received but no EOL (CR/LF) "
                    "before timeout — device may have sent a partial line"
                )
                log.warning("unterminated response from device (hex mode): %r", raw[:40])
            return out

    def query_full(self, line: str, timeout_ms: int, transform_names: list[str] | None = None) -> dict:
        """Like query() but returns a dict with an optional 'warning' key.

        If the device sends data with no EOL terminator before the deadline,
        ``warning`` is set to explain the issue so callers can alert the user.

        Args:
            line: command to send
            timeout_ms: deadline in milliseconds
            transform_names: optional list of transform names to apply (e.g., ["crlf", "hex"])
        """
        with self._lock:
            raw, terminated = self._query_locked(line, timeout_ms)
            result = self._decode_response(raw)
            result = self._echo_response(line, result)
            
            # Apply line transforms if requested
            if transform_names:
                for name in transform_names:
                    if name in LINE_TRANSFORMS:
                        transformer = LINE_TRANSFORMS[name]()
                        result = transformer.transform(result)
                    else:
                        log.warning("unknown transform: %r", name)
            
            out: dict = {"response": result}
            if not terminated and result:
                out["warning"] = (
                    "unterminated response: data received but no EOL (CR/LF) "
                    "before timeout — device may have sent a partial line"
                )
                log.warning("unterminated response from device: %r", result[:80])
            return out

    def query_with_timestamp(self, line: str, timeout_ms: int, ts_format: str | None) -> dict:
        """Send query and optionally include a timestamp in the response payload."""
        with self._lock:
            raw, terminated = self._query_locked(line, timeout_ms)
            result = self._decode_response(raw)
            result = self._echo_response(line, result)
            fmt = self._normalize_ts_format(ts_format) if ts_format is not None else self._ts_format
            ts = self._format_ts_for(fmt)
            out: dict = {"response": result}
            if ts:
                out["timestamp"] = ts
            if not terminated and result:
                out["warning"] = (
                    "unterminated response: data received but no EOL (CR/LF) "
                    "before timeout — device may have sent a partial line"
                )
                log.warning("unterminated response from device: %r", result[:80])
            return out

    def _query_locked(self, line: str, timeout_ms: int) -> tuple[bytes, bool]:
        """Core send/receive. Returns (raw_bytes, terminated).

        *terminated* is True when the loop exited because an EOL byte was
        seen, False when it exited because the deadline expired.  Callers
        should treat (non-empty result, terminated=False) as a warning.
        """
        if self._ser is None or not self._ser.is_open:
            raise IOError("serial port not open")

        terminator = EOL_BYTES[self._eol]
        payload = self._apply_output_map(line.encode() + terminator)
        self._ser.reset_input_buffer()
        self._ser.write(payload)
        self._ser.flush()
        with self._stats_lock:
            self._tx_bytes += len(payload)

        deadline = time.monotonic() + timeout_ms / 1000.0
        buf = bytearray()
        terminated = False

        while time.monotonic() < deadline:
            chunk = self._ser.read(4096)
            if chunk:
                buf.extend(chunk)
                with self._stats_lock:
                    self._rx_bytes += len(chunk)
                # stop as soon as we have any complete line (CR or LF)
                if b"\n" in chunk or b"\r" in chunk:
                    terminated = True
                    break

        return bytes(buf), terminated

    def _decode_response(self, data: bytes) -> str:
        result = self._apply_input_map(data).decode(errors="replace").strip()
        self._log_line(result)
        return result

    def _echo_response(self, line: str, response: str) -> str:
        if not self._echo:
            return response
        if response:
            return f"{line}\n{response}"
        return line

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
                    raw, _terminated = self._query_locked(probe, timeout_ms)
                except IOError:
                    continue
                resp = self._apply_input_map(raw).decode(errors="replace").strip()
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

    # ------------------------------------------------------------------
    def set_map(self, maps_str: str) -> frozenset[str]:
        """Set character mapping. maps_str is comma-separated names, or empty to clear."""
        if not maps_str.strip():
            with self._lock:
                self._maps = frozenset()
            return self._maps
        names = frozenset(m.strip().upper() for m in maps_str.split(",") if m.strip())
        invalid = names - _VALID_MAPS
        if invalid:
            raise ValueError(f"unknown maps: {sorted(invalid)}; valid: {sorted(_VALID_MAPS)}")
        with self._lock:
            self._maps = names
        log.info("maps set: %s", sorted(names))
        return names

    def set_timestamp(self, fmt: str | None) -> None:
        """Set timestamp format: 'iso8601', '24hour', 'epoch', or None/empty to disable."""
        fmt = self._normalize_ts_format(fmt)
        with self._lock:
            self._ts_format = fmt
        log.info("timestamp format: %s", self._ts_format)

    def set_echo(self, enabled: bool) -> None:
        with self._lock:
            self._echo = enabled
        log.info("local echo: %s", enabled)

    def _normalize_ts_format(self, fmt: str | None) -> str | None:
        if fmt in (None, ""):
            return None
        if fmt not in ("iso8601", "24hour", "epoch"):
            raise ValueError("timestamp format must be iso8601, 24hour, epoch or empty")
        return fmt

    def log_start(self, path: str, max_bytes: int = 0, backups: int = 0) -> None:
        """Open log file in append mode with optional size-based rotation."""
        if max_bytes < 0:
            raise ValueError("max_bytes must be >= 0")
        if backups < 0:
            raise ValueError("backups must be >= 0")
        with self._log_lock:
            if self._log_file is not None:
                self._log_file.flush()
                self._log_file.close()
            self._log_path = path
            self._log_max_bytes = int(max_bytes)
            self._log_backups = int(backups)
            self._log_file = open(path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115
            log.info(
                "log started: %s (rotation: max_bytes=%d backups=%d)",
                path,
                self._log_max_bytes,
                self._log_backups,
            )

    def set_log_strip(self, enabled: bool) -> None:
        """Enable or disable ANSI/control-character stripping for log writes."""
        with self._log_lock:
            self._log_strip = enabled
        log.info("log strip: %s", enabled)

    def log_stop(self) -> None:
        """Flush and close the log file."""
        with self._log_lock:
            if self._log_file is not None:
                self._log_file.flush()
                self._log_file.close()
                self._log_file = None
                log.info("log stopped: %s", self._log_path)

    def _apply_output_map(self, data: bytes) -> bytes:
        maps = self._maps
        if not maps:
            return data
        if "ONLCRNL" in maps:
            data = data.replace(b"\n", b"\r\n")
        if "ODELBS" in maps:
            data = data.replace(b"\x7f", b"\x08")
        return data

    def _apply_input_map(self, data: bytes) -> bytes:
        maps = self._maps
        if not maps:
            return data
        if "ICRNL" in maps:
            data = data.replace(b"\r", b"\n")
        if "INLCRNL" in maps:
            data = data.replace(b"\n", b"\r\n")
        return data

    def _format_ts(self) -> str:
        return self._format_ts_for(self._ts_format)

    def _format_ts_for(self, fmt: str | None) -> str:
        if not fmt:
            return ""
        now = datetime.datetime.now()
        if fmt == "epoch":
            return f"{now.timestamp():.3f}"
        elif fmt == "iso8601":
            return now.isoformat(timespec="milliseconds")
        else:  # 24hour
            return now.strftime("%H:%M:%S.%f")[:12]

    def _strip_for_log(self, text: str) -> str:
        """Drop ANSI escapes and non-printable control chars except tab."""
        text = _ANSI_RE.sub("", text)
        return "".join(ch for ch in text if ch == "\t" or ch >= " " )

    def _log_line(self, line: str) -> None:
        """Write a received line to the log file (no-op if log not active)."""
        with self._log_lock:
            if self._log_file is not None:
                try:
                    payload = self._strip_for_log(line) if self._log_strip else line
                    ts = self._format_ts()
                    prefix = f"[{ts}] " if ts else ""
                    msg = prefix + payload + "\n"
                    self._rotate_log_if_needed(len(msg.encode("utf-8", errors="replace")))
                    self._log_file.write(msg)
                    self._log_file.flush()
                except OSError as exc:
                    log.warning("log write failed: %s", exc)

    def _rotate_log_if_needed(self, incoming_size: int) -> None:
        """Rotate active log file on write boundaries when size threshold is reached."""
        if self._log_file is None or not self._log_path:
            return
        if self._log_max_bytes <= 0:
            return

        try:
            current_size = os.path.getsize(self._log_path)
        except OSError:
            current_size = 0

        if current_size + incoming_size <= self._log_max_bytes:
            return

        self._log_file.flush()
        self._log_file.close()
        self._log_file = None

        if self._log_backups > 0:
            for idx in range(self._log_backups, 0, -1):
                src = f"{self._log_path}.{idx}"
                dst = f"{self._log_path}.{idx + 1}"
                if os.path.exists(src):
                    if idx == self._log_backups:
                        os.unlink(src)
                    else:
                        os.replace(src, dst)
            if os.path.exists(self._log_path):
                os.replace(self._log_path, f"{self._log_path}.1")
        else:
            if os.path.exists(self._log_path):
                os.unlink(self._log_path)

        self._log_file = open(self._log_path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115

    def ping(self) -> bool:
        if self._ser is None:
            return False
        return self._ser.is_open

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        with self._stats_lock:
            return {
                "rx_bytes": self._rx_bytes,
                "tx_bytes": self._tx_bytes,
                "error_count": self._error_count,
                "uptime_s": round(time.monotonic() - self._start_time, 1),
            }

    # ------------------------------------------------------------------
    # RX history ring buffer
    # ------------------------------------------------------------------
    def history(self, limit: int = 50, offset: int = 0) -> dict:
        with self._history_lock:
            # deque is newest-last internally; reverse for newest-first output
            items = list(reversed(self._history))
            total = len(items)
        page = items[offset: offset + limit]
        return {"lines": page, "total": total, "offset": offset}

    def read_until(self, pattern: str, timeout_ms: int = 500) -> str:
        deadline = time.monotonic() + timeout_ms / 1000.0
        regex = re.compile(pattern)
        with self._drain_condition:
            while True:
                text = self._drain_buffer.decode(errors="replace")
                match = regex.search(text)
                if match:
                    return match.group(0)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"read_until timeout waiting for pattern: {pattern}")
                self._drain_condition.wait(timeout=remaining)

    def drain(self, max_bytes: int | None = None) -> str:
        with self._drain_condition:
            if max_bytes is None or max_bytes >= len(self._drain_buffer):
                data = bytes(self._drain_buffer)
                self._drain_buffer.clear()
            else:
                data = bytes(self._drain_buffer[:max_bytes])
                del self._drain_buffer[:max_bytes]
        return data.decode(errors="replace")

    def _record_rx_line(self, text: str, ts: str) -> None:
        entry = {"ts": ts, "line": text}
        with self._history_lock:
            self._history.append(entry)
        with self._drain_condition:
            self._drain_buffer.extend((text + "\n").encode())
            if len(self._drain_buffer) > self._drain_limit:
                del self._drain_buffer[:len(self._drain_buffer) - self._drain_limit]
            self._drain_condition.notify_all()
        self._log_line(text)

    # ------------------------------------------------------------------
    # DTR / RTS / BREAK
    # ------------------------------------------------------------------
    def set_line(self, line: str, state: str) -> None:
        """Set DTR or RTS high/low/toggle.  line: 'dtr'|'rts', state: 'high'|'low'|'toggle'."""
        with self._lock:
            if self._ser is None or not self._ser.is_open:
                raise IOError("serial port not open")
            _serial_set_line(self._ser, line, state)
        log.info("set_line: %s=%s", line.lower(), state.lower())

    def send_break(self, duration_ms: int = 250) -> None:
        with self._lock:
            if self._ser is None or not self._ser.is_open:
                raise IOError("serial port not open")
            self._ser.send_break(duration=duration_ms / 1000.0)
        log.info("send_break: %d ms", duration_ms)

    def pulse_line(self, line: str, duration_ms: int = 100) -> None:
        """Pulse DTR or RTS: assert high, wait duration_ms, then low."""
        with self._lock:
            if self._ser is None or not self._ser.is_open:
                raise IOError("serial port not open")
            _serial_pulse_line(self._ser, line, duration_ms)
        log.info("pulse_line: %s %d ms", line.lower(), duration_ms)

    # ------------------------------------------------------------------
    # Background RX reader thread
    # ------------------------------------------------------------------
    def _start_rx_thread(self) -> None:
        self._rx_stop.clear()
        self._rx_thread = threading.Thread(
            target=self._rx_reader_loop, daemon=True, name="rx-reader"
        )
        self._rx_thread.start()

    def _stop_rx_thread(self) -> None:
        self._rx_stop.set()
        if self._rx_thread and self._rx_thread.is_alive():
            self._rx_thread.join(timeout=1.0)
        self._rx_thread = None

    def _rx_reader_loop(self) -> None:
        """Background thread: continuously read RX bytes into history ring buffer.

        Skips reads while _lock is held (i.e. while query() is active) to avoid
        competing with the query send/receive cycle and double-counting stats.
        """
        line_buf = bytearray()
        while not self._rx_stop.is_set():
            with self._reconnect_lock:
                reconnecting = self._reconnecting
            if reconnecting:
                time.sleep(0.05)
                continue
            # Only read passively when no query is in flight
            acquired = self._lock.acquire(blocking=False)
            if not acquired:
                time.sleep(0.001)
                continue
            try:
                ser = self._ser
                if ser is None or not ser.is_open:
                    time.sleep(0.05)
                    continue
                chunk = ser.read(256)  # small read to yield lock quickly
            except serial.SerialException as exc:
                log.warning("rx reader: serial error: %s — reconnecting", exc)
                with self._stats_lock:
                    self._error_count += 1
                self._schedule_reconnect()
                time.sleep(0.1)
                continue
            except OSError:
                time.sleep(0.05)
                continue
            finally:
                self._lock.release()

            if chunk:
                with self._stats_lock:
                    self._rx_bytes += len(chunk)
                line_buf.extend(chunk)
                while True:
                    for sep in (b"\r\n", b"\n", b"\r"):
                        idx = line_buf.find(sep)
                        if idx >= 0:
                            raw_line = line_buf[:idx]
                            line_buf = line_buf[idx + len(sep):]
                            text = raw_line.decode(errors="replace").strip()
                            if text:
                                ts = (self._format_ts_for(self._ts_format)
                                      or datetime.datetime.now().isoformat(timespec="milliseconds"))
                                self._record_rx_line(text, ts)
                            break
                    else:
                        break

    # ------------------------------------------------------------------
    # Auto-reconnect
    # ------------------------------------------------------------------
    def _schedule_reconnect(self) -> None:
        with self._reconnect_lock:
            if self._reconnecting:
                return
            self._reconnecting = True
        t = threading.Thread(target=self._reconnect_loop, daemon=True, name="reconnect")
        t.start()

    def _reconnect_loop(self) -> None:
        delay = 0.1
        max_delay = 5.0
        while not self._rx_stop.is_set():
            log.info("reconnect: closing port, retry in %.1fs", delay)
            try:
                with self._lock:
                    if self._ser and self._ser.is_open:
                        self._ser.close()
                    self._ser = None
            except OSError:
                pass
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
            try:
                ser = serial.Serial(
                    self._port,
                    baudrate=self._baud,
                    timeout=0.01,
                    write_timeout=0.2,
                )
                # Windows FTDI adapter: reduce latency timer from default 16 ms to 1 ms
                _reduce_ftdi_latency_timer(self._port, ser)
                with self._lock:
                    self._ser = ser
                with self._reconnect_lock:
                    self._reconnecting = False
                log.info("reconnect: port re-opened successfully")
                return
            except serial.SerialException as exc:
                log.debug("reconnect: still failing: %s", exc)


def _reduce_ftdi_latency_timer(port: str, ser: serial.Serial) -> None:
    """On Windows, attempt to reduce FTDI adapter latency timer to 1 ms for lower latency.

    This is a best-effort helper — it logs warnings but does not raise.
    """
    if sys.platform != "win32":
        return

    # Detect FTDI by VID
    is_ftdi = False
    try:
        for p in serial.tools.list_ports.comports():
            if p.device == port and p.vid == _FTDI_VID:
                is_ftdi = True
                log.debug("ftdi latency: detected VID 0x%04x on %s", _FTDI_VID, port)
                break
    except Exception as exc:
        log.warning("ftdi latency: port scan failed: %s", exc)
        return

    if not is_ftdi:
        return

    # Attempt registry-based latency timer reduction.
    # HKLM\SYSTEM\CurrentControlSet\Enum\FTDIBUS\<device>\0000\Device Parameters\LatencyTimer = 1
    try:
        # Extract device ID from port name (e.g., COM3 → 3)
        com_num = port.split("COM")[-1]
        # Registry key lookup is complex; simplify by iterating FTDIBUS keys
        reg_path = r"SYSTEM\CurrentControlSet\Enum\FTDIBUS"
        registry = winreg.ConnectRegistry(None, winreg.HKEY_LOCAL_MACHINE)
        try:
            key = winreg.OpenKey(registry, reg_path, access=winreg.KEY_ENUMERATE_SUB_KEYS)
        except (OSError, FileNotFoundError) as exc:
            log.warning("ftdi latency: registry key not found: %s", exc)
            return

        # Iterate FTDI device subkeys to find the matching port
        try:
            idx = 0
            while True:
                try:
                    device_id = winreg.EnumKey(key, idx)
                    idx += 1
                    # Try to open Device Parameters subkey
                    device_key_path = f"{reg_path}\\{device_id}\\0000\\Device Parameters"
                    device_key = winreg.OpenKey(registry, device_key_path, access=winreg.KEY_WRITE)
                    winreg.SetValueEx(device_key, "LatencyTimer", 0, winreg.REG_BINARY, b"\x01")
                    log.info("ftdi latency: set LatencyTimer=1 for %s", port)
                    winreg.CloseKey(device_key)
                    return
                except (OSError, FileNotFoundError):
                    # Device key doesn't have Device Parameters or wrong device, continue
                    continue
        except (WindowsError, OSError):
            pass
        finally:
            winreg.CloseKey(key)
            registry.Close()
    except Exception as exc:
        log.warning("ftdi latency: registry update failed: %s", exc)


def _list_ports() -> list[dict]:
    """Return available serial ports as a list of dicts, newest (highest tty num) last."""
    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append({
            "device": p.device,
            "description": p.description or "",
            "hwid": p.hwid or "",
            "vid": p.vid,
            "pid": p.pid,
            "serial_number": p.serial_number or "",
            "manufacturer": p.manufacturer or "",
            "product": p.product or "",
        })
    ports.sort(key=lambda x: x["device"])
    return ports


def _ascii_score(s: str) -> float:
    """Fraction of characters in *s* that are printable ASCII or whitespace."""
    if not s:
        return 0.0
    good = sum(1 for c in s if 32 <= ord(c) < 127 or c in "\r\n\t")
    return good / len(s)


# ---------------------------------------------------------------------------
# Device discovery
# ---------------------------------------------------------------------------
# A generic, device-agnostic discovery primitive. The hard-won lessons baked in
# here — identify-before-open, one-owner-thread-per-port, a hard time budget,
# baud scanning and DTR probing — are generic serial concerns, so every device
# repo (riden, rigol, can, hantek, …) gets correct discovery for free by passing
# a device-specific probe callback + VID allowlist. See docs/DISCOVERY.md for
# the one-thread-per-port rule and the measured contention proof behind it.

DISCOVERY_DEFAULT_MAX_WORKERS = 8


def _port_type_score(device: str) -> int:
    """Rank a serial device path by how likely it is a real USB-serial target.

    ttyUSB* (FTDI/CP210x/CH34x bridge) outscores ttyACM*/COM*/rfcomm*, which
    outscore anything else. This only ORDERS probing; the VID allowlist does the
    actual filtering.
    """
    if "ttyUSB" in device:
        return 2
    if "ttyACM" in device or "rfcomm" in device or device.upper().startswith("COM"):
        return 1
    return 0


def list_candidate_ports(vid_allowlist: set[int] | None = None) -> list[str]:
    """Serial ports worth probing, best-first, identified by USB VID — NOT by opening.

    With *vid_allowlist* (e.g. ``{0x1A86}`` for CH34x), only ports whose USB VID
    is in the set are returned. This is the core "identify before open" win: it
    skips ST-Link / Pico-CMSIS-DAP / ESP32-JTAG CDC-ACM devices that are not the
    target and can hang for tens of seconds on a blind ``open()``/probe. With
    *vid_allowlist* None, every detected serial port is returned (slower, may
    touch debug adapters) — pass an allowlist whenever you can.

    Ordering: allowlisted VID first, then port-type score, then device name for
    a stable order so the most likely ports are probed before the time budget is
    exhausted.
    """
    scored: list[tuple[int, str]] = []
    for p in serial.tools.list_ports.comports():
        if vid_allowlist is not None and p.vid not in vid_allowlist:
            continue
        score = _port_type_score(p.device)
        if vid_allowlist is not None:
            score += 10  # an allowlisted VID always ranks above generic ordering
        scored.append((score, p.device))
    scored.sort(key=lambda sd: (-sd[0], sd[1]))
    return [dev for _score, dev in scored]


def make_ascii_probe(
    probe_str: str = "?",
    min_score: float = 0.8,
    min_bytes: int = 4,
) -> Callable[[serial.Serial], dict | None]:
    """Build a generic probe callback that accepts a port returning readable ASCII.

    This is the device-neutral probe — the same ASCII-scoring rule as
    ``SerialWorker.detect_baud`` (>= *min_bytes* bytes and >= *min_score*
    printable), reused via ``_ascii_score``. Devices with a real identity
    handshake (Modbus identity read, SCPI ``*IDN?``, …) should pass their own
    probe instead.
    """
    def _probe(ser: serial.Serial) -> dict | None:
        try:
            ser.reset_input_buffer()
            ser.write(probe_str.encode() + b"\n")
            ser.flush()
        except (serial.SerialException, OSError):
            return None
        deadline = time.monotonic() + (ser.timeout or 0.2)
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = ser.read(4096)
            if chunk:
                buf.extend(chunk)
                if b"\n" in buf or b"\r" in buf:
                    break
        resp = bytes(buf).decode(errors="replace").strip()
        score = _ascii_score(resp)
        if score >= min_score and len(resp) >= min_bytes:
            return {"probe": "ascii", "score": round(score, 3), "sample": resp[:80]}
        return None
    return _probe


def discover(
    *,
    probe: Callable[[serial.Serial], dict | None],
    vid_allowlist: set[int] | None = None,
    bauds: list[int] | None = None,
    dtr: str | None = None,
    dtr_pulse_ms: int = 100,
    timeout_s: float = 0.2,
    max_scan_s: float = 5.0,
    max_workers: int = DISCOVERY_DEFAULT_MAX_WORKERS,
    ports: list[str] | None = None,
    include_errors: bool = False,
) -> dict[str, Any]:
    """Discover serial devices: identify by USB ID, then probe candidates in parallel.

    Device-agnostic. The caller supplies the only two device-specific pieces:

      * *probe* — ``Callable[[serial.Serial], dict | None]``. Receives an OPEN
        serial port (already at the baud under test, with DTR applied) and does
        its own I/O — a Modbus identity read, a ``*IDN?`` query, whatever.
        Returns an identity dict on a match, or ``None``. It MUST NOT open its
        own port (see rule 2).
      * *vid_allowlist* — USB VIDs to restrict probing to, e.g. ``{0x1A86}``.

    The generic concerns handled here, each proven the hard way:

    1. **Identify-before-open** — candidate ports come from
       :func:`list_candidate_ports` (VID filter + type score), so non-target
       CDC-ACM debug adapters are never opened. (Override with *ports* to probe
       an explicit list.)
    2. **One owner thread per port** — the scan fans out across *ports* (bounded
       by *max_workers* via a semaphore) but probes a port's bauds SERIALLY
       inside that port's single daemon thread. Opening one serial device from
       two threads at once corrupts every reader; see docs/DISCOVERY.md. Daemon
       threads are deliberate: a serial ``open()`` can block past the budget and
       a non-daemon worker would keep the process alive at interpreter exit.
    3. **Bounded scan + hard budget** — *max_scan_s* caps wall-clock. Any port
       whose thread has not finished by the deadline is reported under
       ``skipped`` (no silent truncation) and its daemon thread abandoned.
    4. **Baud scanning** — each port is tried across *bauds* (default
       :data:`CANDIDATE_BAUDS`, fastest first); the generalisation of
       ``detect_baud``, except the *probe* callback is the oracle instead of an
       ASCII heuristic. First baud the probe accepts wins (one device per port).
    5. **DTR probing** — *dtr* (``'high'``|``'low'``|``'pulse'``) drives DTR
       before each probe via the same helpers as ``SerialWorker.set_line`` /
       ``pulse_line``; *dtr_pulse_ms* is the pulse hold.
    6. **High-resolution timing** — per-probe ms (``perf_counter``) at DEBUG,
       totals at INFO.

    Returns a dict: ``ports_scanned``, ``bauds_scanned``, ``found`` (list of
    ``{port, baud, probe_ms, identity}``), ``found_count``, ``errors`` (only when
    *include_errors*), ``skipped``, ``timed_out``, ``max_scan_s``, ``scan_ms``.
    """
    if not callable(probe):
        raise ValueError("probe must be a callable: (serial.Serial) -> dict | None")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be > 0")
    if max_scan_s <= 0:
        raise ValueError("max_scan_s must be > 0")
    if dtr is not None and dtr not in ("high", "low", "pulse"):
        raise ValueError("dtr must be one of 'high'|'low'|'pulse' or None")
    bauds = list(bauds) if bauds else list(CANDIDATE_BAUDS)

    if ports is None:
        ports = list_candidate_ports(vid_allowlist)

    found: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    timed_out = False

    log.info(
        "discover: %d port(s) × %d baud(s), timeout=%.0fms dtr=%s budget=%.1fs, parallel by port",
        len(ports), len(bauds), timeout_s * 1000, dtr, max_scan_s,
    )

    if not ports:
        return {
            "ports_scanned": [], "bauds_scanned": bauds,
            "found": [], "found_count": 0,
            "errors": [], "skipped": [], "timed_out": False,
            "max_scan_s": float(max_scan_s), "scan_ms": 0.0,
        }

    def _apply_dtr(ser: serial.Serial) -> None:
        if dtr is None:
            return
        if dtr == "pulse":
            _serial_pulse_line(ser, "dtr", dtr_pulse_ms)
        else:
            _serial_set_line(ser, "dtr", dtr)

    def _scan_port(port: str) -> dict[str, Any]:
        """Probe all bauds on ONE port, serially, in this port's single thread.

        Parallelism is across ports, never within a port: opening the same
        serial device from multiple threads at once corrupts every reader, so
        each port has exactly one owner thread that tries its bauds in sequence.
        The port is opened exactly once (the costly, possibly-blocking step);
        each baud is a live ``ser.baudrate =`` reconfigure. Stops at the first
        baud the probe accepts (one device per port). Times each probe at the
        highest resolution available (perf_counter).
        """
        port_found: list[dict[str, Any]] = []
        port_errors: list[dict[str, Any]] = []
        try:
            ser = serial.Serial(port, baudrate=bauds[0], timeout=timeout_s,
                                write_timeout=timeout_s)
        except (serial.SerialException, OSError) as exc:
            log.debug("discover: open %s failed: %s", port, exc)
            port_errors.append({"port": port, "error": f"open failed: {exc}"})
            return {"found": port_found, "errors": port_errors}
        try:
            for baud in bauds:
                t0 = time.perf_counter()
                try:
                    ser.baudrate = baud
                except (serial.SerialException, OSError) as exc:
                    log.debug("discover: baud %d unsupported on %s: %s", baud, port, exc)
                    continue
                try:
                    ser.reset_input_buffer()
                    ser.reset_output_buffer()
                    _apply_dtr(ser)
                    result = probe(ser)
                except Exception as exc:
                    ms = (time.perf_counter() - t0) * 1000.0
                    log.debug("discover: probe %s @ %d → error in %.3fms (%s)",
                              port, baud, ms, exc)
                    port_errors.append({"port": port, "baud": int(baud),
                                        "error": str(exc), "probe_ms": round(ms, 3)})
                    continue
                ms = (time.perf_counter() - t0) * 1000.0
                if result:
                    log.debug("discover: probe %s @ %d → FOUND in %.3fms", port, baud, ms)
                    port_found.append({"port": port, "baud": int(baud),
                                       "probe_ms": round(ms, 3), "identity": result})
                    break  # one device per port
                log.debug("discover: probe %s @ %d → none in %.3fms", port, baud, ms)
        finally:
            try:
                ser.close()
            except Exception:
                pass
        return {"found": port_found, "errors": port_errors}

    # Fan out across PORTS (bounded fan-out per the coding style). One DAEMON
    # thread per port, capped by a semaphore — never multiple threads on the
    # same device. Daemon threads are deliberate: a serial open() can block past
    # the budget (the read timeout does not bound open()), and a non-daemon
    # worker with a stuck open() would keep the process alive at interpreter
    # exit even after we have the result.
    workers = max(1, min(int(max_workers), len(ports)))
    slots = threading.Semaphore(workers)
    results_lock = threading.Lock()
    results: dict[str, dict[str, Any]] = {}

    def _worker(port: str) -> None:
        with slots:
            res = _scan_port(port)
        with results_lock:
            results[port] = res

    t_start = time.perf_counter()
    threads: list[tuple[str, threading.Thread]] = []
    for port in ports:
        t = threading.Thread(target=_worker, args=(port,), daemon=True,
                            name=f"awto-discover-{port}")
        t.start()
        threads.append((port, t))

    # Join each port's thread within the remaining budget. A port whose thread
    # has not finished by the deadline is recorded as skipped and its daemon
    # thread abandoned — no silent truncation, no hang.
    deadline = time.monotonic() + float(max_scan_s)
    for port, t in threads:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            t.join(timeout=remaining)
        if t.is_alive():
            timed_out = True
            skipped.append({"port": port, "reason": "scan budget exceeded"})

    with results_lock:
        for port, _t in threads:
            res = results.get(port)
            if res is None:
                continue  # already counted as skipped above
            found.extend(res["found"])
            if include_errors:
                errors.extend(res["errors"])

    total_ms = (time.perf_counter() - t_start) * 1000.0
    log.info("discover: %d found, %d skipped (timed_out=%s) in %.3fms",
             len(found), len(skipped), timed_out, total_ms)

    return {
        "ports_scanned": ports,
        "bauds_scanned": bauds,
        "found": found,
        "found_count": len(found),
        "errors": errors if include_errors else [],
        "skipped": skipped,
        "timed_out": timed_out,
        "max_scan_s": float(max_scan_s),
        "scan_ms": round(total_ms, 3),
    }


def _run_exec_argv(argv: list[str], timeout_ms: int, max_output_bytes: int) -> dict:
    """Run explicit argv command with bounded runtime/output.

    Returns dict with stdout lines, stderr text, exit code, and truncation flag.
    """
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
        raise ValueError("exec: argv must be a non-empty list of non-empty strings")
    if timeout_ms <= 0 or timeout_ms > _EXEC_TIMEOUT_MS_MAX:
        raise ValueError(f"exec: timeout_ms must be in 1..{_EXEC_TIMEOUT_MS_MAX}")
    if max_output_bytes <= 0 or max_output_bytes > _EXEC_OUTPUT_BYTES_MAX:
        raise ValueError(f"exec: max_output_bytes must be in 1..{_EXEC_OUTPUT_BYTES_MAX}")

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=False,
            timeout=timeout_ms / 1000.0,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"exec: command timed out after {timeout_ms}ms") from exc

    stdout_bytes = proc.stdout or b""
    stderr_bytes = proc.stderr or b""
    stdout_truncated = len(stdout_bytes) > max_output_bytes
    if stdout_truncated:
        stdout_bytes = stdout_bytes[:max_output_bytes]
    if len(stderr_bytes) > _EXEC_STDERR_BYTES_MAX:
        stderr_bytes = stderr_bytes[:_EXEC_STDERR_BYTES_MAX]

    lines = [ln.strip() for ln in stdout_bytes.decode(errors="replace").splitlines() if ln.strip()]
    stderr_text = stderr_bytes.decode(errors="replace")

    return {
        "lines": lines,
        "exit_code": int(proc.returncode),
        "stderr": stderr_text,
        "stdout_truncated": stdout_truncated,
    }


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
                    with worker._reconnect_lock:
                        reconnecting = worker._reconnecting
                    if reconnecting:
                        _send(conn, make_err("reconnecting: serial port temporarily unavailable"))
                    else:
                        _send(conn, make_ok("pong"))

                elif cmd == "query":
                    with worker._reconnect_lock:
                        reconnecting = worker._reconnecting
                    if reconnecting:
                        _send(conn, make_err("reconnecting: serial port temporarily unavailable"))
                        continue
                    line_str = req.get("line", "")
                    timeout_ms = int(req.get("timeout_ms", 500))
                    include_ts = bool(req.get("include_timestamp", False))
                    ts_fmt = req.get("timestamp_format")
                    output_mode = str(req.get("output_mode", "text")).lower()
                    transform_names = req.get("transform", [])
                    if isinstance(transform_names, str):
                        transform_names = [transform_names]
                    try:
                        if output_mode not in ("text", "hex"):
                            _send(conn, make_err("query: output_mode must be 'text' or 'hex'"))
                            continue
                        if include_ts:
                            if output_mode != "text":
                                _send(conn, make_err("query: include_timestamp requires output_mode='text'"))
                                continue
                            out = worker.query_with_timestamp(line_str, timeout_ms, ts_fmt)
                            _send(conn, {"ok": True, **out})
                        elif output_mode == "hex":
                            out = worker.query_hex_full(line_str, timeout_ms)
                            _send(conn, {"ok": True, **out})
                        else:
                            out = worker.query_full(line_str, timeout_ms, transform_names or None)
                            _send(conn, {"ok": True, **out})
                    except (IOError, ValueError) as exc:
                        _send(conn, make_err(str(exc)))

                elif cmd == "exec":
                    with worker._reconnect_lock:
                        reconnecting = worker._reconnecting
                    if reconnecting:
                        _send(conn, make_err("reconnecting: serial port temporarily unavailable"))
                        continue

                    argv = req.get("argv", [])
                    timeout_ms = int(req.get("timeout_ms", 3000))
                    max_output_bytes = int(req.get("max_output_bytes", 4096))
                    serial_timeout_ms = int(req.get("serial_timeout_ms", 500))

                    try:
                        if serial_timeout_ms <= 0 or serial_timeout_ms > 10_000:
                            _send(conn, make_err("exec: serial_timeout_ms must be in 1..10000"))
                            continue
                        cmd_out = _run_exec_argv(argv, timeout_ms, max_output_bytes)
                        responses: list[str] = []
                        warnings: list[str] = []
                        for line in cmd_out["lines"]:
                            out = worker.query_full(line, serial_timeout_ms)
                            responses.append(out.get("response", ""))
                            warning = out.get("warning")
                            if warning:
                                warnings.append(warning)

                        payload = {
                            "ok": True,
                            "response": "\n".join(x for x in responses if x),
                            "exit_code": cmd_out["exit_code"],
                            "stderr": cmd_out["stderr"],
                            "sent_lines": len(cmd_out["lines"]),
                            "stdout_truncated": cmd_out["stdout_truncated"],
                        }
                        if warnings:
                            payload["warning"] = "; ".join(warnings)
                        _send(conn, payload)
                    except (ValueError, TimeoutError, IOError) as exc:
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

                elif cmd == "set_map":
                    try:
                        maps = worker.set_map(req.get("maps", ""))
                        _send(conn, {"ok": True, "maps": sorted(maps)})
                    except ValueError as exc:
                        _send(conn, make_err(f"set_map: {exc}"))

                elif cmd == "set_timestamp":
                    try:
                        worker.set_timestamp(req.get("format"))
                        _send(conn, {"ok": True, "ts_format": worker._ts_format})
                    except ValueError as exc:
                        _send(conn, make_err(f"set_timestamp: {exc}"))

                elif cmd == "set_echo":
                    worker.set_echo(bool(req.get("enabled", False)))
                    _send(conn, {"ok": True, "echo": worker.info()["echo"]})

                elif cmd == "log_start":
                    path = req.get("path", "")
                    strip = bool(req.get("strip", False))
                    max_bytes = int(req.get("max_bytes", 0))
                    backups = int(req.get("backups", 0))
                    if not path:
                        _send(conn, make_err("log_start: path required"))
                    else:
                        try:
                            worker.set_log_strip(strip)
                            worker.log_start(path, max_bytes=max_bytes, backups=backups)
                            _send(
                                conn,
                                {
                                    "ok": True,
                                    "log_path": path,
                                    "log_strip": strip,
                                    "max_bytes": max_bytes,
                                    "backups": backups,
                                },
                            )
                        except (OSError, ValueError) as exc:
                            _send(conn, make_err(f"log_start: {exc}"))

                elif cmd == "log_stop":
                    worker.log_stop()
                    _send(conn, make_ok("log stopped"))

                elif cmd == "stats":
                    _send(conn, {"ok": True, "stats": worker.stats()})

                elif cmd == "history":
                    limit = int(req.get("limit", 50))
                    offset = int(req.get("offset", 0))
                    _send(conn, {"ok": True, **worker.history(limit, offset)})

                elif cmd == "read_until":
                    try:
                        pattern = str(req.get("pattern", ""))
                        timeout_ms = int(req.get("timeout_ms", 500))
                        if not pattern:
                            _send(conn, make_err("read_until: pattern required"))
                        else:
                            match_text = worker.read_until(pattern, timeout_ms)
                            _send(conn, {"ok": True, "response": match_text})
                    except (ValueError, TimeoutError, re.error) as exc:
                        _send(conn, make_err(f"read_until: {exc}"))

                elif cmd == "drain":
                    max_bytes = req.get("max_bytes")
                    try:
                        limit = int(max_bytes) if max_bytes is not None else None
                        _send(conn, {"ok": True, "response": worker.drain(limit)})
                    except ValueError as exc:
                        _send(conn, make_err(f"drain: {exc}"))

                elif cmd == "list_ports":
                    _send(conn, {"ok": True, "ports": _list_ports()})

                elif cmd == "set_line":
                    try:
                        worker.set_line(req.get("line", ""), req.get("state", ""))
                        _send(conn, {"ok": True, "line": req.get("line"), "state": req.get("state")})
                    except (ValueError, IOError) as exc:
                        _send(conn, make_err(f"set_line: {exc}"))

                elif cmd == "send_break":
                    try:
                        worker.send_break(int(req.get("duration_ms", 250)))
                        _send(conn, make_ok("break sent"))
                    except (IOError, ValueError) as exc:
                        _send(conn, make_err(f"send_break: {exc}"))

                elif cmd == "pulse_line":
                    try:
                        worker.pulse_line(req.get("line", ""), int(req.get("duration_ms", 100)))
                        _send(conn, make_ok(f"pulsed {req.get('line')}"))
                    except (ValueError, IOError) as exc:
                        _send(conn, make_err(f"pulse_line: {exc}"))

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

def _load_profile(profile: str) -> dict:
    """Load named profile from ~/.config/awto-serial/config.toml.

    Returns a dict of key→value for the profile (merged with [default]).
    Returns empty dict if config file or profile doesn't exist.
    """
    try:
        import tomllib
    except ImportError:
        log.warning("tomllib not available (Python < 3.11); profiles disabled")
        return {}

    config_path = os.path.expanduser("~/.config/awto-serial/config.toml")
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
    except Exception as exc:
        log.warning("failed to load config %s: %s", config_path, exc)
        return {}

    result = dict(config.get("default", {}))
    if profile != "default" and profile in config:
        result.update(config[profile])
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="awto serial daemon")
    ap.add_argument("--profile", default=None, metavar="NAME",
                    help="load settings from ~/.config/awto-serial/config.toml profile")
    ap.add_argument("--port",   default=None,        help="serial device")
    ap.add_argument("--baud",   default=None, type=int, help="baud rate")
    ap.add_argument("--eol",    default=None,
                    choices=list(EOL_BYTES.keys()),
                    help="line ending used for outgoing query() calls")
    ap.add_argument("--socket", default=None, help="Unix socket path")
    ap.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    ap.add_argument("--map", default=None, metavar="MAPS",
                    help="comma-separated char maps: INLCRNL,ICRNL,ONLCRNL,ODELBS")
    ap.add_argument("--log-file", default=None, metavar="PATH",
                    help="log all RX data to this file (always appended, never deleted)")
    ap.add_argument("--log-max-bytes", default=None, type=int, metavar="N",
                    help="rotate log when size exceeds N bytes (0 disables rotation)")
    ap.add_argument("--log-backups", default=None, type=int, metavar="N",
                    help="number of rotated log generations to keep (default 0)")
    ap.add_argument("--log-strip", action="store_true",
                    help="strip ANSI/control chars before writing log lines")
    ap.add_argument("--timestamp", default=None, choices=["iso8601", "24hour", "epoch"],
                    help="prepend timestamp to log lines")
    ap.add_argument("--echo", action="store_true",
                    help="echo transmitted commands into returned responses")
    ap.add_argument("--history-size", default=None, type=int, metavar="N",
                    help="RX ring buffer size in lines (default 1000)")
    args = ap.parse_args()

    # --- merge profile defaults → env → CLI args ---
    profile_cfg: dict = {}
    if args.profile:
        profile_cfg = _load_profile(args.profile)
        log.debug("profile %r: %s", args.profile, profile_cfg)

    def _get(cli_val, profile_key: str, default):
        if cli_val is not None:
            return cli_val
        if profile_key in profile_cfg:
            return profile_cfg[profile_key]
        return default

    port         = _get(args.port,         "port",         DEFAULT_PORT)
    baud         = _get(args.baud,         "baud",         DEFAULT_BAUD)
    eol          = _get(args.eol,          "line_ending",  DEFAULT_EOL)
    socket_path  = _get(args.socket,       "socket",       DEFAULT_SOCKET_PATH)
    map_str      = _get(args.map,          "map",          "")
    log_file     = _get(args.log_file,     "log_file",     None)
    log_max_bytes = _get(args.log_max_bytes, "log_max_bytes", 0)
    log_backups  = _get(args.log_backups,  "log_backups",  0)
    log_strip    = args.log_strip or bool(profile_cfg.get("log_strip", False))
    timestamp    = _get(args.timestamp,    "timestamp",    None)
    echo_enabled = args.echo or bool(profile_cfg.get("echo", False))
    history_size = _get(args.history_size, "history_size", 1000)

    _setup_logging("awto-serial-daemon", args.log_level)

    worker = SerialWorker(port, baud, eol=eol, history_size=history_size)
    if map_str:
        worker.set_map(map_str)
    if log_file:
        worker.set_log_strip(log_strip)
        worker.log_start(log_file, max_bytes=int(log_max_bytes), backups=int(log_backups))
    if timestamp:
        worker.set_timestamp(timestamp)
    if echo_enabled:
        worker.set_echo(True)
    try:
        worker.open()
    except serial.SerialException as exc:
        log.error("cannot open serial port: %s", exc)
        sys.exit(1)

    # remove stale socket
    if os.path.exists(socket_path):
        os.unlink(socket_path)

    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(socket_path)
    os.chmod(socket_path, 0o600)
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
        if os.path.exists(socket_path):
            os.unlink(socket_path)
        worker.close()


if __name__ == "__main__":
    main()
