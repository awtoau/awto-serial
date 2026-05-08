#!/usr/bin/env python3
# Run with free-threaded build for real parallelism:
#   /usr/bin/python3.14t test_harness.py -v
# Fedora install: sudo dnf install python3.14-freethreading
"""
test_harness.py  —  self-contained test suite for awto-mcp-serial.

Tests are structured in three layers:

  Layer 1 — Protocol unit tests (no I/O)
  Layer 2 — SerialWorker unit tests (mock serial port)
  Layer 3 — Integration tests (daemon socket + concurrent clients)
            This layer exercises real threading to verify correctness
            under the no-GIL scheduler (CPython 3.13t / 3.14+).

Run:
    python3 test_harness.py [-v]

No hardware required; the serial port is fully mocked.
"""

import json
import logging
import os
import socket
import sys
import sysconfig
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))

from protocol import (
    DEFAULT_TIMEOUT_MS,
    make_err,
    make_ok,
    recv_response,
    send_request,
)
from serial_daemon import SerialWorker, handle_client, _send

logging.basicConfig(
    level=logging.WARNING,   # keep test output clean
    format="test[%(process)d]: %(levelname)-8s %(name)s: %(message)s",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gil_status() -> str:
    try:
        # 3.13+ free-threaded builds expose this
        enabled = sys._is_gil_enabled()  # type: ignore[attr-defined]
        return "ENABLED (classic GIL)" if enabled else "disabled (free-threaded)"
    except AttributeError:
        pass
    # Fallback: check sysconfig build flag
    disabled = sysconfig.get_config_var("Py_GIL_DISABLED")
    if disabled:
        return "disabled (free-threaded)"
    return "ENABLED (classic GIL)"


def _make_socket_pair() -> tuple[socket.socket, socket.socket]:
    """Return a connected (client, server) socket pair."""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    return a, b


def _make_worker_with_mock(responses: list[bytes]) -> SerialWorker:
    """Build a SerialWorker whose underlying Serial is mocked.

    *responses* is the sequence of byte chunks that ser.read() will return,
    one entry per query() call.
    """
    worker = SerialWorker.__new__(SerialWorker)
    worker._port = "/dev/null"
    worker._baud = 2_000_000
    worker._eol = "lf"
    worker._lock = threading.Lock()
    worker._maps = frozenset()
    worker._log_path = None
    worker._log_file = None
    worker._log_lock = threading.Lock()
    worker._ts_format = None
    worker._log_strip = False
    worker._stats_lock = threading.Lock()
    worker._rx_bytes = 0
    worker._tx_bytes = 0
    worker._error_count = 0
    worker._start_time = time.monotonic()
    worker._history = []
    worker._history_lock = threading.Lock()
    worker._rx_thread = None
    worker._rx_stop = threading.Event()
    worker._drain_buffer = bytearray()
    worker._drain_limit = 64 * 1024
    worker._drain_condition = threading.Condition()
    worker._reconnecting = False
    worker._reconnect_lock = threading.Lock()
    worker._echo = False

    mock_ser = MagicMock()
    mock_ser.is_open = True
    # Each call to read() returns the next canned response then b""
    read_iter = iter(responses)

    def _read(_n):
        try:
            return next(read_iter)
        except StopIteration:
            return b""

    mock_ser.read.side_effect = _read
    worker._ser = mock_ser
    return worker


# ---------------------------------------------------------------------------
# Layer 1 — Protocol unit tests
# ---------------------------------------------------------------------------

class TestProtocol(unittest.TestCase):

    def test_make_ok(self):
        r = make_ok("hello")
        self.assertTrue(r["ok"])
        self.assertEqual(r["response"], "hello")

    def test_make_err(self):
        r = make_err("boom")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "boom")

    def test_send_and_recv_roundtrip(self):
        client, server = _make_socket_pair()
        payload = {"ok": True, "response": "pong"}
        try:
            # send from client side
            client.sendall((json.dumps(payload) + "\n").encode())
            # recv on server side
            received = recv_response(server)
            self.assertEqual(received, payload)
        finally:
            client.close()
            server.close()

    def test_send_request_roundtrip(self):
        client, server = _make_socket_pair()
        req = {"cmd": "ping"}
        try:
            # fire send_request in a thread (it blocks waiting for response)
            resp_holder: list[dict] = []
            exc_holder: list[Exception] = []

            def _caller():
                try:
                    resp_holder.append(send_request(client, req))
                except Exception as exc:
                    exc_holder.append(exc)

            t = threading.Thread(target=_caller)
            t.start()

            # server side: read the request, send a response
            incoming = recv_response(server)
            self.assertEqual(incoming["cmd"], "ping")
            server.sendall((json.dumps(make_ok("pong")) + "\n").encode())

            t.join(timeout=2)
            self.assertFalse(exc_holder, exc_holder)
            self.assertEqual(resp_holder[0]["response"], "pong")
        finally:
            client.close()
            server.close()

    def test_recv_raises_on_closed_socket(self):
        client, server = _make_socket_pair()
        client.close()
        with self.assertRaises(ConnectionError):
            recv_response(server)
        server.close()


# ---------------------------------------------------------------------------
# Layer 2 — SerialWorker unit tests
# ---------------------------------------------------------------------------

class TestSerialWorker(unittest.TestCase):

    def _worker(self, *response_chunks: bytes) -> SerialWorker:
        # Each query call gets one chunk then silence
        responses = list(response_chunks) + [b""] * len(response_chunks)
        return _make_worker_with_mock(responses)

    def test_query_returns_response(self):
        worker = self._worker(b"OK 42\n")
        result = worker.query("status", 200)
        self.assertEqual(result, "OK 42")
        worker._ser.write.assert_called_once_with(b"status\n")

    def test_query_strips_whitespace(self):
        worker = self._worker(b"  value  \n")
        self.assertEqual(worker.query("get", 200), "value")

    def test_query_raises_when_port_closed(self):
        worker = self._worker()
        worker._ser.is_open = False
        with self.assertRaises(IOError):
            worker.query("anything", 100)

    def test_query_raises_when_port_none(self):
        worker = SerialWorker.__new__(SerialWorker)
        worker._port = "/dev/null"
        worker._baud = 9600
        worker._eol = "lf"
        worker._lock = threading.Lock()
        worker._ser = None
        with self.assertRaises(IOError):
            worker.query("anything", 100)

    def test_query_unterminated_no_warning_on_empty(self):
        """Timeout with zero bytes → no warning (device is silent, not partial)."""
        worker = _make_worker_with_mock([b""])
        out = worker.query_full("status", 50)
        self.assertEqual(out["response"], "")
        self.assertNotIn("warning", out)

    def test_query_full_terminated_no_warning(self):
        """EOL-terminated response → no warning in query_full()."""
        worker = self._worker(b"OK 42\n")
        out = worker.query_full("status", 200)
        self.assertEqual(out["response"], "OK 42")
        self.assertNotIn("warning", out)

    def test_query_full_unterminated_warns(self):
        """Data without EOL before timeout → warning key present in query_full()."""
        worker = _make_worker_with_mock([b"partial"])
        out = worker.query_full("status", 50)
        self.assertEqual(out["response"], "partial")
        self.assertIn("warning", out)
        self.assertIn("unterminated", out["warning"])

    def test_query_hex_returns_space_separated_hex(self):
        worker = self._worker(b"OK\n")
        self.assertEqual(worker.query_hex("status", 100), "4f 4b 0a")

    def test_query_full_local_echo_prefixes_command(self):
        worker = self._worker(b"OK 42\n")
        worker.set_echo(True)
        out = worker.query_full("status", 100)
        self.assertEqual(out["response"], "status\nOK 42")

    def test_drain_returns_and_clears_buffer(self):
        worker = self._worker()
        worker._record_rx_line("alpha", "t0")
        worker._record_rx_line("beta", "t1")
        self.assertEqual(worker.drain(), "alpha\nbeta\n")
        self.assertEqual(worker.drain(), "")

    def test_read_until_matches_buffered_text(self):
        worker = self._worker()
        worker._record_rx_line("status ok", "t0")
        self.assertEqual(worker.read_until(r"status\s+ok", 20), "status ok")

    def test_concurrent_queries_serialised(self):
        """Multiple threads calling query() must not interleave."""
        N = 20
        # Each query gets one distinct response chunk
        chunks = [f"resp{i}\n".encode() for i in range(N)]
        worker = _make_worker_with_mock(chunks)

        results: list[str] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def _q(i: int):
            try:
                r = worker.query(f"cmd{i}", 200)
                with lock:
                    results.append(r)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        # Fire all threads at once — real parallel on no-GIL
        with ThreadPoolExecutor(max_workers=N) as pool:
            futs = [pool.submit(_q, i) for i in range(N)]
            for f in as_completed(futs):
                f.result()  # re-raise any exception

        self.assertFalse(errors, errors)
        self.assertEqual(len(results), N)
        # Every result should be one of the canned responses
        for r in results:
            self.assertRegex(r, r"^resp\d+$")


# ---------------------------------------------------------------------------
# Layer 3 — Integration: live daemon socket + concurrent clients
# ---------------------------------------------------------------------------

class _DaemonThread(threading.Thread):
    """Runs the daemon accept-loop in a background thread for testing."""

    def __init__(self, worker: SerialWorker, sock_path: str) -> None:
        super().__init__(daemon=True)
        self._worker = worker
        self._sock_path = sock_path
        self._stop = threading.Event()
        self.server_sock: socket.socket | None = None
        self.ready = threading.Event()

    def run(self) -> None:
        if os.path.exists(self._sock_path):
            os.unlink(self._sock_path)
        self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind(self._sock_path)
        self.server_sock.listen(32)
        self.server_sock.settimeout(0.2)
        self.ready.set()

        while not self._stop.is_set():
            try:
                conn, _ = self.server_sock.accept()
            except TimeoutError:
                continue
            except OSError:
                # socket was closed by stop() — exit cleanly
                break
            t = threading.Thread(
                target=handle_client,
                args=(conn, conn.fileno(), self._worker),
                daemon=True,
            )
            t.start()

    def stop(self) -> None:
        self._stop.set()
        if self.server_sock:
            try:
                self.server_sock.close()
            except OSError:
                pass
        if os.path.exists(self._sock_path):
            os.unlink(self._sock_path)


def _client_query(sock_path: str, req: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(sock_path)
        return send_request(s, req)


class TestIntegration(unittest.TestCase):

    def setUp(self):
        # Temporary socket path unique per test
        self._tmp = tempfile.mktemp(suffix=".sock", prefix="awto_test_")
        # Mock serial: always echoes back "OK <cmd>"
        self._worker = SerialWorker.__new__(SerialWorker)
        self._worker._port = "/dev/null"
        self._worker._baud = 2_000_000
        self._worker._eol = "lf"
        self._worker._lock = threading.Lock()
        self._worker._maps = frozenset()
        self._worker._log_path = None
        self._worker._log_file = None
        self._worker._log_lock = threading.Lock()
        self._worker._ts_format = None
        self._worker._log_strip = False
        self._worker._stats_lock = threading.Lock()
        self._worker._rx_bytes = 0
        self._worker._tx_bytes = 0
        self._worker._error_count = 0
        self._worker._start_time = time.monotonic()
        self._worker._history = []
        self._worker._history_lock = threading.Lock()
        self._worker._rx_thread = None
        self._worker._rx_stop = threading.Event()
        self._worker._drain_buffer = bytearray()
        self._worker._drain_limit = 64 * 1024
        self._worker._drain_condition = threading.Condition()
        self._worker._reconnecting = False
        self._worker._reconnect_lock = threading.Lock()
        self._worker._echo = False

        mock_ser = MagicMock()
        mock_ser.is_open = True

        # Capture written data and return "OK <data>\n"
        written: list[bytes] = []

        def _write(data: bytes):
            written.append(data)

        def _read(_n):
            if written:
                cmd = written.pop(0).decode().strip()
                return f"OK {cmd}\n".encode()
            return b""

        mock_ser.write.side_effect = _write
        mock_ser.read.side_effect = _read
        self._worker._ser = mock_ser
        self._written = written

        self._daemon = _DaemonThread(self._worker, self._tmp)
        self._daemon.start()
        self._daemon.ready.wait(timeout=2)

    def tearDown(self):
        self._daemon.stop()
        self._daemon.join(timeout=2)

    def test_ping(self):
        resp = _client_query(self._tmp, {"cmd": "ping"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["response"], "pong")

    def test_single_query(self):
        resp = _client_query(self._tmp, {"cmd": "query", "line": "status", "timeout_ms": 200})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["response"], "OK status")

    def test_query_include_timestamp_epoch(self):
        resp = _client_query(
            self._tmp,
            {
                "cmd": "query",
                "line": "status",
                "timeout_ms": 200,
                "include_timestamp": True,
                "timestamp_format": "epoch",
            },
        )
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["response"], "OK status")
        self.assertRegex(resp["timestamp"], r"^\d+\.\d{3}$")

    def test_query_output_mode_hex(self):
        resp = _client_query(
            self._tmp,
            {"cmd": "query", "line": "status", "timeout_ms": 200, "output_mode": "hex"},
        )
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["response"], "4f 4b 20 73 74 61 74 75 73 0a")

    def test_set_echo_and_query_text(self):
        resp = _client_query(self._tmp, {"cmd": "set_echo", "enabled": True})
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["echo"])
        resp = _client_query(self._tmp, {"cmd": "query", "line": "status", "timeout_ms": 200})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["response"], "status\nOK status")

    def test_set_echo_and_query_hex(self):
        resp = _client_query(self._tmp, {"cmd": "set_echo", "enabled": True})
        self.assertTrue(resp["ok"])
        resp = _client_query(
            self._tmp,
            {"cmd": "query", "line": "status", "timeout_ms": 200, "output_mode": "hex"},
        )
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["response"], "73 74 61 74 75 73\n4f 4b 20 73 74 61 74 75 73 0a")

    def test_drain_and_read_until(self):
        self._worker._record_rx_line("boot complete", "t0")
        resp = _client_query(self._tmp, {"cmd": "read_until", "pattern": "boot complete", "timeout_ms": 50})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["response"], "boot complete")
        resp = _client_query(self._tmp, {"cmd": "drain"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["response"], "boot complete\n")
        resp = _client_query(self._tmp, {"cmd": "drain"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["response"], "")

    def test_unknown_cmd(self):
        resp = _client_query(self._tmp, {"cmd": "explode"})
        self.assertFalse(resp["ok"])
        self.assertIn("unknown cmd", resp["error"])

    def test_bad_json(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(self._tmp)
            s.sendall(b"not json\n")
            resp = recv_response(s)
        self.assertFalse(resp["ok"])
        self.assertIn("bad JSON", resp["error"])

    def test_concurrent_clients(self):
        """Fire N clients simultaneously — verifies no response mixing."""
        N = 30

        def _do(i: int) -> str:
            resp = _client_query(
                self._tmp,
                {"cmd": "query", "line": f"cmd{i}", "timeout_ms": 500},
            )
            assert resp["ok"], resp
            return resp["response"]

        # ThreadPoolExecutor uses real OS threads — GIL-free on 3.13t/3.14+
        results: list[str] = []
        with ThreadPoolExecutor(max_workers=N) as pool:
            futs = {pool.submit(_do, i): i for i in range(N)}
            for fut, i in futs.items():
                results.append(fut.result(timeout=5))

        self.assertEqual(len(results), N)
        # Every result must be a valid "OK cmd<n>" — no mixing
        for r in results:
            self.assertRegex(r, r"^OK cmd\d+$")

    def test_query_unterminated_response_warns(self):
        """Daemon must surface a 'warning' key when device sends no EOL."""
        # Override the mock to return data with no newline terminator
        written: list[bytes] = []

        def _write(data: bytes):
            written.append(data)

        def _read(_n):
            if written:
                written.pop(0)
                return b"partial-no-eol"   # no \n or \r
            return b""

        self._worker._ser.write.side_effect = _write
        self._worker._ser.read.side_effect = _read

        resp = _client_query(self._tmp, {"cmd": "query", "line": "status", "timeout_ms": 100})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["response"], "partial-no-eol")
        self.assertIn("warning", resp)
        self.assertIn("unterminated", resp["warning"])

    def test_throughput(self):
        """Measure round-trip latency for sequential queries (informational)."""
        N = 50
        start = time.perf_counter()
        for i in range(N):
            resp = _client_query(
                self._tmp,
                {"cmd": "query", "line": f"bench{i}", "timeout_ms": 200},
            )
            self.assertTrue(resp["ok"])
        elapsed = time.perf_counter() - start
        per_call = elapsed / N * 1000
        print(f"\n  throughput: {N} queries in {elapsed:.3f}s  ({per_call:.1f} ms/call)")


# ---------------------------------------------------------------------------
# Layer 4 — Detection logic (set/detect baud + EOL)
# ---------------------------------------------------------------------------

class TestDetection(unittest.TestCase):

    def _worker_with_baud_table(self, working_baud: int, response: bytes):
        """Build a worker whose mock ser only returns *response* when baud matches."""
        worker = SerialWorker.__new__(SerialWorker)
        worker._port = "/dev/null"
        worker._baud = 9600          # start wrong
        worker._eol = "lf"
        worker._lock = threading.Lock()
        worker._maps = frozenset()
        worker._log_path = None
        worker._log_file = None
        worker._log_lock = threading.Lock()
        worker._ts_format = None
        worker._log_strip = False
        worker._stats_lock = threading.Lock()
        worker._rx_bytes = 0
        worker._tx_bytes = 0
        worker._error_count = 0
        worker._start_time = time.monotonic()
        worker._history = []
        worker._history_lock = threading.Lock()
        worker._rx_thread = None
        worker._rx_stop = threading.Event()
        worker._drain_buffer = bytearray()
        worker._drain_limit = 64 * 1024
        worker._drain_condition = threading.Condition()
        worker._reconnecting = False
        worker._reconnect_lock = threading.Lock()
        worker._echo = False

        mock_ser = MagicMock()
        mock_ser.is_open = True
        # baudrate is a real attribute on the mock; track changes
        type(mock_ser).baudrate = PropertyMock(return_value=9600)

        # Track current baud via a closure
        state = {"baud": 9600}

        def _set_baud(val):
            state["baud"] = val
        def _get_baud():
            return state["baud"]
        type(mock_ser).baudrate = property(
            lambda self: _get_baud(),
            lambda self, v: _set_baud(v),
        )

        def _read(_n):
            return response if state["baud"] == working_baud else b"\xff\xfe\x00\x01"
        mock_ser.read.side_effect = _read

        worker._ser = mock_ser
        return worker

    def test_set_baud_changes_attribute(self):
        worker = _make_worker_with_mock([b""])
        worker._eol = "lf"
        worker.set_baud(2_480_000)
        self.assertEqual(worker.baud, 2_480_000)
        self.assertEqual(worker._ser.baudrate, 2_480_000)

    def test_set_eol_validates(self):
        worker = _make_worker_with_mock([b""])
        worker._eol = "lf"
        worker.set_eol("crlf")
        self.assertEqual(worker.eol, "crlf")
        with self.assertRaises(ValueError):
            worker.set_eol("nope")

    def test_query_uses_eol_terminator(self):
        worker = _make_worker_with_mock([b"OK\n"])
        worker._eol = "crlf"
        worker.query("ABC", 100)
        worker._ser.write.assert_called_once_with(b"ABC\r\n")

    def test_detect_baud_picks_working_rate(self):
        worker = self._worker_with_baud_table(2_480_000, b"READY OK 1.0\n")
        baud = worker.detect_baud(probe="?", timeout_ms=50,
                                  candidates=(4_000_000, 2_480_000, 9_600))
        self.assertEqual(baud, 2_480_000)
        self.assertEqual(worker.baud, 2_480_000)

    def test_detect_baud_fastest_first_order(self):
        """Two working rates → must pick the fastest in the candidates tuple."""
        worker = SerialWorker.__new__(SerialWorker)
        worker._port = "/dev/null"
        worker._baud = 9600
        worker._eol = "lf"
        worker._lock = threading.Lock()
        worker._maps = frozenset()
        worker._log_path = None
        worker._log_file = None
        worker._log_lock = threading.Lock()
        worker._ts_format = None
        worker._log_strip = False
        worker._stats_lock = threading.Lock()
        worker._rx_bytes = 0
        worker._tx_bytes = 0
        worker._error_count = 0
        worker._start_time = time.monotonic()
        worker._history = []
        worker._history_lock = threading.Lock()
        worker._rx_thread = None
        worker._rx_stop = threading.Event()
        worker._drain_buffer = bytearray()
        worker._drain_limit = 64 * 1024
        worker._drain_condition = threading.Condition()
        worker._reconnecting = False
        worker._reconnect_lock = threading.Lock()
        worker._echo = False
        mock_ser = MagicMock()
        mock_ser.is_open = True
        # always returns valid ASCII regardless of baud
        mock_ser.read.side_effect = lambda _n: b"OK ready\n"
        worker._ser = mock_ser

        baud = worker.detect_baud(
            probe="?", timeout_ms=50,
            candidates=(2_480_000, 1_000_000, 115_200),
        )
        self.assertEqual(baud, 2_480_000)  # first / fastest

    def test_detect_baud_fails_on_garbage(self):
        worker = _make_worker_with_mock([b"\xff\xfe\x00\x01"] * 20)
        worker._eol = "lf"
        with self.assertRaises(IOError):
            worker.detect_baud(probe="?", timeout_ms=20,
                               candidates=(115_200, 9_600))

    def test_detect_eol_lf(self):
        worker = _make_worker_with_mock([b"line1\nline2\n"])
        worker._eol = "lf"
        self.assertEqual(worker.detect_eol(probe="?", timeout_ms=50), "lf")

    def test_detect_eol_crlf(self):
        worker = _make_worker_with_mock([b"line1\r\nline2\r\n"])
        worker._eol = "lf"
        self.assertEqual(worker.detect_eol(probe="?", timeout_ms=50), "crlf")
        self.assertEqual(worker.eol, "crlf")

    def test_detect_eol_cr(self):
        worker = _make_worker_with_mock([b"line1\rline2\r"])
        worker._eol = "lf"
        self.assertEqual(worker.detect_eol(probe="?", timeout_ms=50), "cr")


# ---------------------------------------------------------------------------
# Layer 5 — End-to-end MCP client (uses official mcp Python SDK)
# ---------------------------------------------------------------------------
# This launches mcp_server.py as a stdio subprocess and drives it as a real
# MCP client. The server in turn talks to the daemon over the Unix socket.
# Optional: skipped automatically if 'mcp' is not installed.

# ---------------------------------------------------------------------------
# Layer 4b — TestMapLogTimestamp
# ---------------------------------------------------------------------------

class TestMapLogTimestamp(unittest.TestCase):

    def _make_worker(self):
        w = SerialWorker.__new__(SerialWorker)
        w._port = "/dev/null"
        w._baud = 115200
        w._eol = "lf"
        w._maps = frozenset()
        w._log_path = None
        w._log_file = None
        w._log_lock = threading.Lock()
        w._ts_format = None
        w._log_strip = False
        w._lock = threading.Lock()
        w._ser = None
        w._stats_lock = threading.Lock()
        w._rx_bytes = 0
        w._tx_bytes = 0
        w._error_count = 0
        w._start_time = time.monotonic()
        w._history = []
        w._history_lock = threading.Lock()
        w._rx_thread = None
        w._rx_stop = threading.Event()
        w._drain_buffer = bytearray()
        w._drain_limit = 64 * 1024
        w._drain_condition = threading.Condition()
        w._reconnecting = False
        w._reconnect_lock = threading.Lock()
        w._echo = False
        return w

    def test_set_map_valid(self):
        w = self._make_worker()
        maps = w.set_map("ONLCRNL,ODELBS")
        self.assertEqual(maps, frozenset({"ONLCRNL", "ODELBS"}))

    def test_set_map_invalid_raises(self):
        w = self._make_worker()
        with self.assertRaises(ValueError):
            w.set_map("BOGUS")

    def test_set_map_clear(self):
        w = self._make_worker()
        w.set_map("ONLCRNL")
        w.set_map("")
        self.assertEqual(w._maps, frozenset())

    def test_apply_output_onlcrnl(self):
        w = self._make_worker()
        w.set_map("ONLCRNL")
        self.assertEqual(w._apply_output_map(b"hello\n"), b"hello\r\n")

    def test_apply_output_odelbs(self):
        w = self._make_worker()
        w.set_map("ODELBS")
        self.assertEqual(w._apply_output_map(b"a\x7f"), b"a\x08")

    def test_apply_input_icrnl(self):
        w = self._make_worker()
        w.set_map("ICRNL")
        self.assertEqual(w._apply_input_map(b"ok\r"), b"ok\n")

    def test_set_timestamp_valid(self):
        w = self._make_worker()
        w.set_timestamp("iso8601")
        self.assertEqual(w._ts_format, "iso8601")

    def test_set_timestamp_invalid_raises(self):
        w = self._make_worker()
        with self.assertRaises(ValueError):
            w.set_timestamp("bogus")

    def test_set_timestamp_clear(self):
        w = self._make_worker()
        w.set_timestamp("epoch")
        w.set_timestamp("")
        self.assertIsNone(w._ts_format)

    def test_format_ts_empty_when_disabled(self):
        w = self._make_worker()
        self.assertEqual(w._format_ts(), "")

    def test_format_ts_epoch(self):
        w = self._make_worker()
        w.set_timestamp("epoch")
        ts = w._format_ts()
        self.assertRegex(ts, r"^\d+\.\d{3}$")
        self.assertIn(".", ts)

    def test_format_ts_iso8601(self):
        w = self._make_worker()
        w.set_timestamp("iso8601")
        ts = w._format_ts()
        self.assertFalse(ts.startswith("["))
        self.assertIn("T", ts)

    def test_log_start_appends_never_overwrites(self):
        import tempfile, os
        w = self._make_worker()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("existing\n")
            path = f.name
        try:
            w.log_start(path)
            w._log_line("new line")
            w.log_stop()
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("existing", content)
            self.assertIn("new line", content)
        finally:
            os.unlink(path)

    def test_log_stop_noop_when_not_started(self):
        w = self._make_worker()
        w.log_stop()  # must not raise

    def test_log_line_noop_when_not_started(self):
        w = self._make_worker()
        w._log_line("should not crash")  # must not raise

    def test_log_includes_timestamp_when_set(self):
        import tempfile, os
        w = self._make_worker()
        w.set_timestamp("epoch")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            w.log_start(path)
            w._log_line("hello")
            w.log_stop()
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("[", content)
            self.assertIn("hello", content)
        finally:
            os.unlink(path)

    def test_log_strip_removes_ansi_sequences(self):
        import tempfile, os
        w = self._make_worker()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            w.set_log_strip(True)
            w.log_start(path)
            w._log_line("\x1b[31mRED\x1b[0m")
            w.log_stop()
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("RED", content)
            self.assertNotIn("\x1b[31m", content)
        finally:
            os.unlink(path)

    def test_log_rotation_creates_backup_generation(self):
        import tempfile
        import os

        w = self._make_worker()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            w.log_start(path, max_bytes=20, backups=1)
            w._log_line("aaaaaaaaaa")
            w._log_line("bbbbbbbbbb")
            w.log_stop()

            self.assertTrue(os.path.exists(path))
            self.assertTrue(os.path.exists(path + ".1"))

            with open(path, encoding="utf-8") as f_active:
                active = f_active.read()
            with open(path + ".1", encoding="utf-8") as f_rot:
                rotated = f_rot.read()

            self.assertIn("bbbbbbbbbb", active)
            self.assertIn("aaaaaaaaaa", rotated)
        finally:
            for p in (path, path + ".1", path + ".2"):
                if os.path.exists(p):
                    os.unlink(p)

    def test_log_rotation_disabled_keeps_single_file(self):
        import tempfile
        import os

        w = self._make_worker()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            w.log_start(path, max_bytes=0, backups=0)
            for _ in range(3):
                w._log_line("0123456789")
            w.log_stop()

            self.assertTrue(os.path.exists(path))
            self.assertFalse(os.path.exists(path + ".1"))
        finally:
            for p in (path, path + ".1"):
                if os.path.exists(p):
                    os.unlink(p)


try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import asyncio
    _HAS_MCP = True
except ImportError:
    _HAS_MCP = False


@unittest.skipUnless(_HAS_MCP, "mcp SDK not installed")
class TestMcpClientEndToEnd(unittest.TestCase):
    """Drive mcp_server.py via the real MCP Python SDK over stdio."""

    @classmethod
    def setUpClass(cls):
        # Spin up a daemon talking to a mocked serial port on a private socket
        cls._sock = tempfile.mktemp(suffix=".sock", prefix="awto_mcp_e2e_")

        worker = SerialWorker.__new__(SerialWorker)
        worker._port = "/dev/null"
        worker._baud = 2_480_000
        worker._eol = "lf"
        worker._lock = threading.Lock()
        worker._maps = frozenset()
        worker._log_path = None
        worker._log_file = None
        worker._log_lock = threading.Lock()
        worker._ts_format = None
        worker._log_strip = False
        worker._stats_lock = threading.Lock()
        worker._rx_bytes = 0
        worker._tx_bytes = 0
        worker._error_count = 0
        worker._start_time = time.monotonic()
        worker._history = []
        worker._history_lock = threading.Lock()
        worker._rx_thread = None
        worker._rx_stop = threading.Event()
        worker._drain_buffer = bytearray()
        worker._drain_limit = 64 * 1024
        worker._drain_condition = threading.Condition()
        worker._reconnecting = False
        worker._reconnect_lock = threading.Lock()
        worker._echo = False
        mock_ser = MagicMock()
        mock_ser.is_open = True
        written: list[bytes] = []
        def _w(b): written.append(b)
        def _r(_n):
            if written:
                msg = written.pop(0).decode().strip()
                return f"OK {msg}\n".encode()
            return b""
        mock_ser.write.side_effect = _w
        mock_ser.read.side_effect = _r
        worker._ser = mock_ser

        cls._daemon = _DaemonThread(worker, cls._sock)
        cls._daemon.start()
        cls._daemon.ready.wait(timeout=2)

    @classmethod
    def tearDownClass(cls):
        cls._daemon.stop()
        cls._daemon.join(timeout=2)

    async def _drive(self):
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(__file__).parent / "mcp_server.py")],
            env={**os.environ, "AWTO_SOCKET": self._sock},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = sorted(t.name for t in tools.tools)
                self.assertIn("serial_ping", names)
                self.assertIn("serial_query", names)
                self.assertIn("serial_set_baud", names)
                self.assertIn("serial_detect_baud", names)
                self.assertIn("serial_detect_eol", names)
                self.assertIn("serial_info", names)

                # ping
                r = await session.call_tool("serial_ping", {})
                self.assertIn("ok", r.content[0].text.lower())

                # query
                r = await session.call_tool("serial_query",
                                            {"command": "hello"})
                self.assertEqual(r.content[0].text, "OK hello")

                # set_baud + info
                r = await session.call_tool("serial_set_baud",
                                            {"baud": 1_500_000})
                self.assertIn("1500000", r.content[0].text)

    def test_mcp_client_end_to_end(self):
        asyncio.run(self._drive())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Python {sys.version}")
    print(f"GIL: {_gil_status()}")
    print()
    unittest.main(verbosity=2)
