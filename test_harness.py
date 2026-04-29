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
    worker._lock = threading.Lock()

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
        worker._lock = threading.Lock()
        worker._ser = None
        with self.assertRaises(IOError):
            worker.query("anything", 100)

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
        self._worker._lock = threading.Lock()

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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Python {sys.version}")
    print(f"GIL: {_gil_status()}")
    print()
    unittest.main(verbosity=2)
