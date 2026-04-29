# Python Coding Style Guide — awto-au

This document captures the Python style conventions and preferred modules
used across all awto-au repositories. It is the reference for code
reviews, new modules, and PyPI packaging decisions.

---

## Runtime & Interpreter

- **Python 3.13+ minimum**; actively use **Python 3.14 free-threaded** (`python3.14t`)
  where available.
- Verify GIL status at startup in daemons:
  ```python
  import sys
  if sys.version_info >= (3, 13) and not sys._is_gil_enabled():
      # free-threaded — real parallelism available
      pass
  ```
- Fedora install: `sudo dnf install python3.14-freethreading`
- Binary: `/usr/bin/python3.14t`
- Virtual environment: `.venv-ft/` (`uv venv --python python3.14t`)

---

## Project Layout & Packaging

```
project-root/
├── pyproject.toml          # single source of truth for metadata + deps
├── CODING_STYLE.md
├── README.md
├── protocol.py             # shared constants / helpers (no external deps)
├── serial_daemon.py        # long-running daemon process
├── mcp_server.py           # MCP stdio server
├── ttu_cli.py              # CLI entry point
├── test_harness.py         # unittest test suite
└── scripts/                # shell helpers (not installed)
```

### `pyproject.toml` conventions

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "awto-mcp-serial"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "mcp[cli]",
    "pyserial",
]

[project.scripts]
awto-serial-daemon = "serial_daemon:main"
awto-ttu            = "ttu_cli:main"

[dependency-groups]
dev = ["pytest"]

[tool.uv]
python = "3.14t"
```

- Use `[dependency-groups]` (PEP 735) for dev deps — **not** `[tool.uv.dev-dependencies]` (deprecated).
- Build backend: **hatchling** (no `setup.py`, no `setup.cfg`).
- `[project.scripts]` entry points for every user-facing executable.

---

## Preferred Modules

| Need | Module | Notes |
|------|--------|-------|
| Serial port | `pyserial` | `serial.Serial`, live `ser.baudrate = N` reconfigure |
| MCP server | `mcp[cli]` / `FastMCP` | `instructions=` not `description=` |
| Async runtime | `anyio` | only when async is genuinely needed |
| CLI argument parsing | `argparse` | subcommands via `add_subparsers(dest="cmd")` |
| Logging | `logging` + `SysLogHandler` | see Logging section |
| Threading | `threading.Thread`, `threading.Lock`, `concurrent.futures.ThreadPoolExecutor` | prefer over asyncio for I/O-bound work |
| JSON | `json` (stdlib) | JSON-lines for IPC |
| IPC transport | `socket.AF_UNIX` | Unix domain sockets; TCP loopback fallback |
| Testing | `unittest` | no pytest dependency for daemon tests |
| Packaging | `hatchling` | via `pyproject.toml` |
| HTTP client | `urllib.request` (stdlib) | only for simple outbound calls |
| WebSocket | `websockets` | when async WebSocket bridge is needed |

**Do not add** `click`, `typer`, `httpx`, `requests`, `pydantic` unless there
is a concrete, documented reason.

---

## Type Hints

- Use type hints throughout; import from `__future__` only for Python <3.10 compat.
- Prefer stdlib types: `dict[str, Any]`, `list[str]`, `tuple[int, ...]`.
- Return `None` explicitly where relevant.
- `Any` from `typing` is acceptable; do not force complex generics.

```python
from typing import Any

def make_ok(response: str) -> dict[str, Any]:
    return {"ok": True, "response": response}
```

---

## Argument Parsing (`argparse`)

Use subcommands for multi-function CLIs:

```python
import argparse

def main() -> None:
    p = argparse.ArgumentParser(description="awto serial tool")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("query", help="Send a command and read response")
    q.add_argument("text", nargs="?")

    sub.add_parser("ping", help="Check daemon is alive")

    args = p.parse_args()
    ...
```

- Module-level `parser = argparse.ArgumentParser()` (no subcommands) is acceptable
  only for single-purpose scripts.
- Keep `--help` strings short and imperative ("Send a command", not "This sends a command").

---

## Logging

Always use `logging` with a `SysLogHandler` primary handler plus a stderr
fallback:

```python
import logging
import logging.handlers
from logging.handlers import SysLogHandler

LOG = logging.getLogger("awto-serial")
LOG.setLevel(logging.INFO)

try:
    _h = SysLogHandler(address="/dev/log", facility=SysLogHandler.LOG_DAEMON)
    _h.ident = "awto-serial: "
    LOG.addHandler(_h)
except OSError:
    LOG.addHandler(logging.StreamHandler())
```

- Log names match the process/component name.
- `LOG.info(...)` / `LOG.warning(...)` / `LOG.error(...)` — no `print()` in
  daemon code.
- CLI tools may use `print()` for human output and `LOG` for diagnostics.

---

## Threading Model

- Prefer **real threads** (`threading.Thread`) over `asyncio` for I/O-bound work
  when using the free-threaded build — the GIL is absent and threads give true
  parallelism.
- Shared mutable state → protect with `threading.Lock` held for the minimum scope.
- Use `ThreadPoolExecutor` for bounded fan-out work.
- Daemon threads (`daemon=True`) for background I/O workers so the process exits
  cleanly.
- Use `threading.Event` for ready/stop signalling between threads.

```python
import threading

class Worker:
    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.ready = threading.Event()

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)
```

---

## IPC Protocol (Unix domain socket + JSON-lines)

For local inter-process communication:

- Transport: `socket.AF_UNIX` / `socket.SOCK_STREAM`.
- Encoding: **JSON-lines** — one JSON object per line, UTF-8, `\n` terminated.
- Socket path default: `/tmp/<project>.sock`; overridable via environment variable
  (e.g. `AWTO_SOCKET`) for test isolation.
- Protocol shape:

  ```json
  → {"cmd": "query", "line": "status", "timeout_ms": 500}
  ← {"ok": true, "response": "OK status"}
  ← {"ok": false, "error": "port not open"}
  ```

- Always read the socket path from the environment **at call time**, not at import time:
  ```python
  import os
  from protocol import DEFAULT_SOCKET_PATH as _DEFAULT_SOCKET_PATH

  def _sock_path() -> str:
      return os.environ.get("AWTO_SOCKET", _DEFAULT_SOCKET_PATH)
  ```

---

## Serial Port Conventions

- Library: **pyserial** (`pyserial>=3.5`).
- Live baud-rate change: `ser.baudrate = new_baud` — pyserial reconfigures
  without closing the port.
- Baud probe order: fastest first (`2_480_000 → 9600`).
- Line endings: follow **tio(1)** naming — `lf`, `cr`, `crlf`.
- Detection threshold: ≥80% printable ASCII and ≥4 bytes = valid response.
- Always `try/except serial.SerialException` around port open and read.

---

## EOL / Line-ending Naming

Follow `tio --map` conventions:

| Name | Bytes | Use |
|------|-------|-----|
| `lf` | `\n` | Linux default |
| `cr` | `\r` | legacy/embedded |
| `crlf` | `\r\n` | Windows / some devices |

Expose as `--eol {lf,cr,crlf}` in CLI and MCP tool arguments.

---

## Testing

- Framework: `unittest` (stdlib) — no pytest dependency for core daemon tests.
- Test file: `test_harness.py` at project root; runs with
  `python test_harness.py -v`.
- Layer structure:
  1. `TestProtocol` — pure unit tests, no I/O
  2. `TestSerialWorker` — mocked `serial.Serial`
  3. `TestIntegration` — real daemon thread + real Unix socket
  4. `TestDetection` — baud/EOL detection with mocked serial
  5. `TestMcpClientEndToEnd` — full MCP client via official `mcp` SDK
- Use `threading.Event` for daemon ready signalling in test setUp.
- Socket isolation: use `tempfile.mktemp(suffix=".sock")` per test class,
  set `AWTO_SOCKET` env var before launching subprocesses.
- Mock `serial.Serial` with `unittest.mock.MagicMock`; set `_eol` attribute
  on `SerialWorker.__new__()` constructions.

---

## MCP Server (`FastMCP`)

- Import: `from mcp.server.fastmcp import FastMCP`
- Constructor: `FastMCP("name", instructions="...")` — use `instructions=`,
  **not** `description=`.
- Transport: stdio (`mcp.run()`) — VS Code invokes via `.vscode/mcp.json`.
- Tool return types: `str` for simple responses, `dict` for structured data.
- Socket path must be read at tool-call time (see IPC section above).

```python
mcp = FastMCP("awto-serial", instructions="Controls the serial daemon.")

@mcp.tool()
def serial_ping() -> str:
    """Check the daemon is alive."""
    ...
```

---

## Code Style

- **Line length**: 99 characters (not 79).
- **Indentation**: 4 spaces, no tabs.
- **Quotes**: double quotes for strings; single quotes acceptable in small
  expressions.
- **f-strings** preferred over `%` or `.format()`.
- **Trailing commas** in multi-line collections.
- **Blank lines**: 2 between top-level definitions, 1 between methods.
- `if __name__ == "__main__":` guard in every runnable module.
- No bare `except:`; always catch specific exceptions.
- No `print()` in daemon/server code — use `LOG`.

---

## Git & Commit Messages

- Conventional commits: `feat:`, `fix:`, `docs:`, `test:`, `chore:`.
- Present tense, imperative mood: "add baud detection" not "added".
- Reference GitHub issues: `closes #12`.

---

## PyPI Publishing Checklist

1. Fill `[project]` metadata: `description`, `readme`, `license`, `authors`,
   `keywords`, `classifiers`.
2. Ensure `[project.scripts]` entry points are correct.
3. Use `[dependency-groups] dev = [...]` for dev-only deps.
4. Build: `python -m build` or `uv build`.
5. Publish: `twine upload dist/*` or `uv publish`.
6. Tag release: `git tag v0.1.0 && git push --tags`.
