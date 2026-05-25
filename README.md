# awto-mcp

MCP serial daemon for VS Code Copilot — baud/EOL auto-detection, Unix socket IPC, FastMCP tools.

Canonical repository: `awto-au/awto-mcp`.
Some historical references use `awto-mcp-serial`; in `gh` CLI this resolves to the canonical `awto-au/awto-mcp` repo.

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

## Sandbox Access Note

For this project, the supported and documented path is the daemon + MCP architecture shown above.

If you need other host-to-sandbox bridging patterns (`tio` spool logs, `socat`, `tmux`, etc.), see `debris/docs/SANDBOX-SERIAL-ACCESS.md` (archived alternatives and trade-offs). Those patterns are not this repo's primary workflow.

---

## Quick Start

### Requirements

- Python 3.13+ (Python 3.14 free-threaded recommended)
- Fedora: `sudo dnf install python3.14-freethreading`
- `uv` for virtual environment management

### Install

```bash
git clone https://github.com/awto-au/awto-mcp
cd awto-mcp
uv venv --python python3.14t .venv-ft
uv pip install -e . --python .venv-ft/bin/python
```

> **Note:** This project uses a uv-managed venv — use `uv pip` not `pip` directly.
> Activate with `source .venv-ft/bin/activate` for interactive use.

### Run the daemon

```bash
.venv-ft/bin/python serial_daemon.py --port /dev/ttyACM0 --baud 2480000 --eol lf
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
| `serial_query` | `command`, `timeout_ms`, `include_timestamp`, `timestamp_format`, `output_mode` | Send a command, read response (optional timestamp or raw hex mode) |
| `serial_exec` | `argv`, `timeout_ms`, `serial_timeout_ms`, `max_output_bytes` | Execute explicit host argv (no shell), pipe stdout lines into serial queries, return response + exit metadata |
| `serial_info` | — | Show port, baud, eol, is_open |
| `serial_set_baud` | `baud` | Change baud rate live |
| `serial_set_eol` | `eol` | Change line ending (`lf`/`cr`/`crlf`) |
| `serial_detect_baud` | `probe`, `timeout_ms` | Auto-detect baud (fastest-first: 2480000→9600) |
| `serial_detect_eol` | `probe`, `timeout_ms` | Auto-detect line ending |
| `serial_set_echo` | `enabled` | Enable or disable local echo of transmitted commands |
| `serial_set_timestamp` | `format` | Set timestamp format: `iso8601` / `24hour` / `epoch` |
| `serial_log_start` | `path`, `strip`, `max_bytes`, `backups` | Start append-only RX logging with optional size-based rotation |
| `serial_log_stop` | — | Stop RX logging |
| `serial_list_ports` | — | List all serial ports with VID:PID, description |
| `serial_stats` | — | RX/TX byte counters, error count, uptime |
| `serial_history` | `limit`, `offset` | Recent RX lines from ring buffer (newest first) |
| `serial_read_until` | `pattern`, `timeout_ms` | Wait for unsolicited RX text matching a regex |
| `serial_drain` | `max_bytes` | Drain buffered unsolicited RX text |
| `serial_set_line` | `line`, `state` | Set DTR or RTS: `high` / `low` / `toggle` |
| `serial_send_break` | `duration_ms` | Send serial BREAK condition |
| `serial_pulse_line` | `line`, `duration_ms` | Pulse DTR or RTS (assert → wait → release) |
| `serial_completion_schema` | — | Return JSON schema for device monitor tab-completion |

Example Copilot prompts:
```
ping the serial daemon
detect the baud rate
send "status" to the serial port
list available serial ports
show RX/TX stats
show last 20 received lines
set DTR high
send a break
```

---

## CLI Tool (`ttu_cli.py`)

```bash
.venv-ft/bin/python ttu_cli.py ping
.venv-ft/bin/python ttu_cli.py info
.venv-ft/bin/python ttu_cli.py query "status"
.venv-ft/bin/python ttu_cli.py query "status" --output-mode hex
.venv-ft/bin/python ttu_cli.py query "status" --timestamp epoch
.venv-ft/bin/python ttu_cli.py exec -- python3 -c "print('status')"
.venv-ft/bin/python ttu_cli.py set-baud 2480000
.venv-ft/bin/python ttu_cli.py set-eol crlf
.venv-ft/bin/python ttu_cli.py detect-baud --probe "?"
.venv-ft/bin/python ttu_cli.py detect-eol
.venv-ft/bin/python ttu_cli.py set-echo on
.venv-ft/bin/python ttu_cli.py set-timestamp iso8601
.venv-ft/bin/python ttu_cli.py log-start /tmp/awto-rx.log --strip --timestamp 24hour
.venv-ft/bin/python ttu_cli.py log-start /tmp/awto-rx.log --max-bytes 1048576 --backups 3
.venv-ft/bin/python ttu_cli.py log-stop
echo "status" | .venv-ft/bin/python ttu_cli.py query   # pipe from stdin
```

New subcommands:

```bash
.venv-ft/bin/python ttu_cli.py list-ports
.venv-ft/bin/python ttu_cli.py stats
.venv-ft/bin/python ttu_cli.py history --limit 20
.venv-ft/bin/python ttu_cli.py history --limit 50 --offset 50   # pagination
.venv-ft/bin/python ttu_cli.py read-until "boot complete"
.venv-ft/bin/python ttu_cli.py drain
.venv-ft/bin/python ttu_cli.py set-line dtr high
.venv-ft/bin/python ttu_cli.py set-line rts toggle
.venv-ft/bin/python ttu_cli.py send-break --duration 500
.venv-ft/bin/python ttu_cli.py pulse-line dtr --duration 200
```

`exec` is intentionally constrained for safety: explicit argv only (no shell-string evaluation), bounded host-command timeout, and bounded captured stdout size.

---

## Auto-Start with systemd

A ready-made systemd user service unit is provided in `contrib/awto-serial-daemon.service`. It binds to the `/dev/ttyACM0` device unit so the daemon starts automatically when the device is attached and stops when it is removed.

### Install (user service — no root required)

```bash
# Copy the unit file into the user service directory
mkdir -p ~/.config/systemd/user
cp contrib/awto-serial-daemon.service ~/.config/systemd/user/

# Optional: override port/baud/socket by creating an env file
cat > ~/.config/awto-serial.env <<'EOF'
AWTO_PORT=/dev/ttyACM0
AWTO_BAUD=115200
AWTO_SOCKET=/tmp/awto-serial.sock
EOF

# Enable and start
systemctl --user daemon-reload
systemctl --user enable --now awto-serial-daemon.service

# Check status / follow logs
systemctl --user status awto-serial-daemon.service
journalctl --user -u awto-serial-daemon.service -f
```

To target a different device (e.g. `/dev/ttyUSB0`) either edit the `EnvironmentFile` or override the `BindsTo`/`After` directives with a `.d/` drop-in:

```bash
mkdir -p ~/.config/systemd/user/awto-serial-daemon.service.d
cat > ~/.config/systemd/user/awto-serial-daemon.service.d/port.conf <<'EOF'
[Unit]
BindsTo=dev-ttyUSB0.device
After=dev-ttyUSB0.device

[Service]
Environment=AWTO_PORT=/dev/ttyUSB0
EOF
systemctl --user daemon-reload
```

> **Auto-reconnect**: the daemon has built-in reconnect logic; `Restart=on-failure` in the unit is a backstop for unexpected crashes, not a substitute.

---

## Windows FTDI Adapter Support

When opening a serial port on Windows with an FTDI USB-serial adapter (VID 0x0403), the daemon automatically attempts to reduce the latency timer from the Windows default (16 ms) to 1 ms. This reduces per-read latency and improves throughput for high-speed serial applications.

**Behaviour:**

- On non-Windows or non-FTDI devices: no-op, no log output.
- On Windows with FTDI: attempts registry-based `LatencyTimer` reduction (best-effort).
- If registry update fails: logs a warning but does not fail the port open — full functionality is preserved.

No configuration is required; the latency timer reduction happens automatically on every port open (initial and reconnect).

---

## Long-Running Tasks (Agent Workflow)

For long jobs (large ingests, long test runs, large builds), prefer a visible log/tail pattern so humans can watch progress live:

1. Run the long command with output redirected to a log file.
2. Open a visible terminal with `tail -F` on that log.
3. Let the agent monitor completion separately.

Example:

```bash
your_command > "$HOME/.cache/awto-mcp/job.log" 2>&1
tail -F "$HOME/.cache/awto-mcp/job.log"
```

This keeps progress visible even when the command is started from an MCP/agent workflow.

---

## For AI Agents on Other Projects

If you are a Copilot agent working on a project that talks to a serial device (e.g. embedded firmware), you can use this MCP server to interact with the device directly from your workspace.

### 1 — Start the daemon (human does this once)

```bash
cd /path/to/awto-mcp
source .venv-ft/bin/activate
python serial_daemon.py --port /dev/ttyACM0 --baud 2480000
```

### 2 — Add to your project's `.vscode/mcp.json`

```json
{
     "servers": {
          "awto-serial": {
               "type": "stdio",
               "command": "/path/to/awto-mcp/.venv-ft/bin/python",
               "args": ["/path/to/awto-mcp/mcp_server.py"]
          }
     }
}
```

### 3 — Use the tools from Copilot chat

```
ping the serial daemon
send "esp status" to the serial port
show last 30 received lines
set DTR high then send "reboot"
get the completion schema for this device
```

### 4 — Implement tab-completion on your device firmware

Call the `serial_completion_schema` MCP tool from your agent to get the exact JSON format your firmware's `help --json` command should return.  The schema supports hierarchical sub-commands, positional args, and enumerated choices — the same depth as bash completion.

---

## Monitor Mode & Tab-Completion Schema

The `monitor` subcommand provides an interactive readline REPL over the serial port. On startup it sends a command to the device (default: `help --json`) and expects a JSON response that drives bash-style tab-completion.

Run it with:

```bash
python ttu_cli.py monitor
python ttu_cli.py monitor --complete-cmd "? --json"
python ttu_cli.py monitor --complete-file docs/completion-schema.json
```

### Completion JSON format

Query the MCP tool `serial_completion_schema` to get the full schema and a worked example, or see the shape below:

```json
{
     "version": "1",
     "name": "mydevice",
     "commands": [
          {
               "name": "esp",
               "description": "ESP32 subsystem",
               "subcommands": [
                    { "name": "status", "description": "Show link status" },
                    { "name": "reset",  "description": "Hard-reset ESP32" },
                    {
                         "name": "send",
                         "args": [{ "name": "at_cmd", "type": "string" }]
                    }
               ]
          },
          {
               "name": "gpio",
               "subcommands": [
                    {
                         "name": "set",
                         "args": [
                              { "name": "pin",   "type": "integer", "choices": ["0","1","2","3"] },
                              { "name": "value", "type": "choice",  "choices": ["0","1","high","low"] }
                         ]
                    }
               ]
          },
          { "name": "version" },
          { "name": "reboot"  }
     ]
}
```

### Tab-completion behaviour (bash-style)

| Situation | Behaviour |
|-----------|-----------|
| Single match | Complete in-place, append space |
| Multiple matches with common prefix | Complete to common prefix |
| Multiple matches, no common prefix | Beep; second Tab prints all options |
| At arg position, `choices` defined | Tab-complete from `choices` list |
| Unknown root | Beep only |

---

## Tests

```bash
.venv-ft/bin/python test_harness.py -v
```

44 tests across 5 layers — no hardware required (serial port is mocked).

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
