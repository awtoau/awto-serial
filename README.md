# awto-mcp-serial

MCP serial daemon for VS Code Copilot — baud/EOL auto-detection, Unix socket IPC, FastMCP tools.

Lets GitHub Copilot (or any MCP client) send commands to a serial device and read responses, with automatic baud-rate and line-ending detection.

---

## Architecture

```
VS Code Copilot
      │  stdio (MCP protocol)
      ▼
 mcp_server.py          ← FastMCP stdio server, one tool per operation
      │  AF_UNIX socket  (/tmp/awto-serial.sock)
      ▼
 serial_daemon.py        ← owns the serial port, multiplexes clients
      │  pyserial
      ▼
 /dev/ttyACM0  (or any serial port)
```

---

## Quick Start

### Requirements

- Python 3.13+ (Python 3.14 free-threaded recommended)
- Fedora: `sudo dnf install python3.14-freethreading`
- `uv` for virtual environment management

### Install

```bash
git clone https://github.com/awto-au/awto-mcp-serial
cd awto-mcp-serial
uv venv --python python3.14t .venv-ft
source .venv-ft/bin/activate
uv pip install -e .
```

### Run the daemon

```bash
python serial_daemon.py --port /dev/ttyACM0 --baud 2480000 --eol lf
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | `/dev/ttyACM0` | Serial device path |
| `--baud` | `2000000` | Initial baud rate |
| `--eol` | `lf` | Line ending: `lf`, `cr`, or `crlf` |
| `--socket` | `/tmp/awto-serial.sock` | Unix socket path |

### VS Code MCP integration

`.vscode/mcp.json` is already configured. Once the daemon is running, open VS Code in this folder and the `awto-serial` MCP server will be available to Copilot automatically.

---

## MCP Tools

| Tool | Arguments | Description |
|------|-----------|-------------|
| `serial_ping` | — | Check daemon is alive |
| `serial_query` | `command`, `timeout_ms` | Send a command, read response |
| `serial_info` | — | Show port, baud, eol, is_open |
| `serial_set_baud` | `baud` | Change baud rate live |
| `serial_set_eol` | `eol` | Change line ending (`lf`/`cr`/`crlf`) |
| `serial_detect_baud` | `probe`, `timeout_ms` | Auto-detect baud (fastest-first: 2480000→9600) |
| `serial_detect_eol` | `probe`, `timeout_ms` | Auto-detect line ending |

Example Copilot prompts:
```
ping the serial daemon
detect the baud rate
send "status" to the serial port
```

---

## CLI Tool (`ttu_cli.py`)

```bash
python ttu_cli.py ping
python ttu_cli.py info
python ttu_cli.py query "status"
python ttu_cli.py set-baud 2480000
python ttu_cli.py set-eol crlf
python ttu_cli.py detect-baud --probe "?"
python ttu_cli.py detect-eol
echo "status" | python ttu_cli.py query   # pipe from stdin
```

---

## Tests

```bash
source .venv-ft/bin/activate
python test_harness.py -v
```

26 tests across 5 layers — no hardware required (serial port is mocked).

---

## Baud Rate Detection

The daemon probes candidate rates fastest-first and selects the first that returns ≥80% printable ASCII with ≥4 bytes:

```
2_480_000 → 2_000_000 → 1_500_000 → 1_152_000 → 1_000_000
→ 921_600 → 576_000 → 500_000 → 460_800 → 230_400
→ 115_200 → 57_600 → 38_400 → 19_200 → 9_600
```

---

## Code Style

See [CODING_STYLE.md](CODING_STYLE.md) for conventions used across all awto-au Python repositories.

---

## License

MIT
