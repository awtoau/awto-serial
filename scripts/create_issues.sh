#!/usr/bin/env bash
# Create all GitHub issues for awto-au/awto-mcp-serial
set -e
REPO="awto-au/awto-mcp-serial"

create() {
    local prio="$1" labels="$2" title="$3" body="$4"
    echo "Creating: [$prio] $title"
    gh issue create --repo "$REPO" --label "$labels" --title "$title" --body "$body"
}

# ── P0 ──────────────────────────────────────────────────────────────────────

create P0 "P0,detection,enhancement" \
"Auto baud-rate detection" \
'Probe device at standard rates (300→2000000), detect which produces valid ASCII output by printable-char ratio.

**MCP tool:** `serial_detect_baud(probe_cmd?)`

Rates to try: 300, 1200, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600, 1000000, 2000000'

create P0 "P0,detection,protocol,enhancement" \
"Line-ending auto-detection (CR / LF / CRLF)" \
'Send the same probe command with CR, LF, and CRLF variants. Check which produces a meaningful response. Store detected line ending in SerialWorker and use it for all subsequent `query()` calls.

**Config option:** `line_ending = cr | lf | crlf | auto`

**MCP tool:** `serial_detect_line_ending(probe_cmd?)`'

create P0 "P0,logging,enhancement" \
"Log to file (tio -L / --log-file / --log-append)" \
'From the reference command line:
```
exec tio "$dev" -b "$BAUD" -m "$MODE" -t -L --log-file "${logfile}" --log-append
```
Daemon should optionally write all RX bytes to a rotating log file.

- `--log-file <path>` — set log path
- `--log-append` — append instead of overwrite
- `--log-strip` — strip ANSI/control chars before logging

**MCP tools:** `serial_log_start(path, append?)`, `serial_log_stop()`'

create P0 "P0,logging,enhancement" \
"Per-line timestamps (tio -t / --timestamp)" \
'From the reference command: `-t` adds a timestamp to every received line.

- Log file should include timestamps
- `serial_query` response should optionally include timestamps
- **Option:** `timestamp_format = 24hour | iso8601 | epoch`

Referenced in: `exec tio "$dev" -b "$BAUD" -m "$MODE" -t -L ...`'

create P0 "P0,protocol,enhancement" \
"Character / line-ending mapping (tio -m / --map)" \
'From the reference command: `-m "$MODE"` maps characters on input or output.

**Maps to implement first:**
- `INLCRNL` — input: NL → CR+NL
- `ICRNL` — input: CR → NL
- `ONLCRNL` — output: NL → CR+NL
- `ODELBS` — output: DEL → BS

Store mapping in SerialWorker, apply in `query()`.

Referenced in: `exec tio "$dev" -b "$BAUD" -m "$MODE" -t -L ...`'

create P0 "P0,research" \
"Review best Python serial-monitor projects on GitHub" \
'Research and document which existing projects are worth borrowing from.

**Top candidates:**

| Project | Stars | Notes |
|---|---|---|
| [pyserial miniterm](https://github.com/pyserial/pyserial) | 3k | Reference impl, CRLF/CR/LF filters, hex mode, DTR/RTS |
| [tio/tio](https://github.com/tio/tio) | 2.9k | C, best feature set, Lua scripting, auto-reconnect, log |
| [adamwtow/python-serial-monitor](https://github.com/adamwtow/python-serial-monitor) | 23 | Python, speedy reconnect, instant char send |
| [PBahner/Serial-Monitor](https://github.com/PBahner/Serial-Monitor) | 12 | Python CLI, Arduino-focused |

**Action:** Read pyserial miniterm Transform pipeline (CRLF/CR/LF/hex/colorize) and port relevant transforms to `serial_daemon.py`.'

# ── P1 ──────────────────────────────────────────────────────────────────────

create P1 "P1,enhancement" \
"Auto-reconnect on device disconnect" \
'If `serial.read()` raises `SerialException`, close port and retry open with exponential back-off (100ms, 200ms, 400ms… cap 5s). Daemon stays alive. Clients receive `{"ok": false, "error": "reconnecting..."}` until port is back.'

create P1 "P1,enhancement" \
"Serial port enumeration / listing (tio -l)" \
'**MCP tool:** `serial_list_ports()` — returns list of available serial devices with driver name, description, USB VID:PID, by-id symlink, and uptime (newest last).

Use `serial.tools.list_ports` from pyserial.'

create P1 "P1,enhancement" \
"Configuration file / profiles (TOML)" \
'Support `~/.config/awto-serial/config.toml`:

```toml
[default]
port = "/dev/ttyACM0"
baud = 2_000_000
line_ending = "lf"

[ttu]
port = "/dev/serial/by-id/usb-STMicro..."
baud = 2_000_000
log_file = "/tmp/ttu.log"
log_append = true
```

Daemon loads profile by name: `serial_daemon.py --profile ttu`'

create P1 "P1,enhancement" \
"Shell command exec with I/O redirected to device (tio --exec)" \
'From tio: `--exec <cmd>` pipes a shell command stdout/stdin through the serial port.

**MCP tool:** `serial_exec(shell_cmd, timeout_ms?)` — run shell command, pipe its output to serial, return serial response.'

# ── P2 ──────────────────────────────────────────────────────────────────────

create P2 "P2,protocol,enhancement" \
"Hex input/output mode (tio --output-mode hex)" \
'`serial_query` option `output_mode=hex` — return response as space-separated hex bytes. Useful for debugging binary-ish frames mixed into ASCII console.'

create P2 "P2,enhancement" \
"RX/TX byte statistics (tio ctrl-t s)" \
'**MCP tool:** `serial_stats()` — return cumulative RX bytes, TX bytes, uptime, error count since daemon start.'

create P2 "P2,enhancement" \
"Local echo control" \
'Option to have daemon echo TX back in the RX stream for devices that do not echo themselves.

**Config:** `echo = true | false`'

create P2 "P2,enhancement" \
"DTR / RTS / BREAK line control" \
'**MCP tools:**
- `serial_set_line(line, state)` — DTR/RTS high/low/toggle
- `serial_send_break(duration_ms?)`

Needed for STM32 reset-via-DTR patterns. Maps to pyserial miniterm Ctrl+T/Ctrl+D.'

create P2 "P2,enhancement" \
"Modem line pulse with configurable duration (tio line-pulse-duration)" \
'`serial_pulse_line(line, duration_ms)` — DTR or RTS pulse.

Needed for MCU reset/boot-mode entry. Common pattern: DTR high → 100ms → low.'

create P2 "P2,enhancement" \
"Continuous RX background reader / monitor mode" \
'Add a background thread in the daemon that continuously reads RX bytes into a ring buffer regardless of active queries. Expose:

- `serial_read_until(pattern, timeout_ms)` — wait for regex match in RX stream
- `serial_drain(max_bytes?)` — return whatever is buffered since last drain

Needed when device sends unsolicited data (faults, status updates).'

create P2 "P2,enhancement" \
"Stdin pipe support for ttu_cli" \
'`echo "status" | python3.14t ttu_cli.py` — read command from stdin if no positional arg, write response to stdout. Makes it composable with shell scripts and other tools.'

# ── P3 ──────────────────────────────────────────────────────────────────────

create P3 "P3,enhancement" \
"TCP socket redirect (tio --socket inet:PORT)" \
'Daemon optionally listens on TCP port as well as Unix socket. Loopback only, no auth. Useful for WSL or remote-machine access without SSH.'

create P3 "P3,enhancement" \
"Xmodem / Ymodem file transfer" \
'`serial_send_file(path, protocol)` — XMODEM_1K / XMODEM_CRC / YMODEM.

Only relevant if target firmware supports it. Low priority — binary protocol on an ASCII console is an edge case.'

create P3 "P3,enhancement" \
"RS-485 half-duplex mode" \
'Set `serial.rs485_mode` on the port object. pyserial supports this natively via `serial.rs485.RS485Settings`.'

create P3 "P3,enhancement" \
"Output delay per character / per line (tio -o / -O)" \
'`per_char_delay_ms` and `per_line_delay_ms` — slow down TX for devices that cannot buffer fast input (e.g. bootloaders at 9600 baud).'

create P3 "P3,enhancement" \
"ANSI colour output in stderr / logs" \
'Colour-code RX vs TX in stderr when running interactively. Respect `NO_COLOR` env variable per no-color.org. Maps to tio `--color` option.'

create P3 "P3,enhancement" \
"systemd service unit file" \
'`awto-serial-daemon.service` — starts daemon on boot, restarts on failure, `After=dev-ttyACM0.device` dependency.

```ini
[Unit]
Description=awto serial daemon
After=dev-ttyACM0.device

[Service]
ExecStart=/usr/bin/python3.14t /opt/awto-mcp-serial/serial_daemon.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```'

# ── WONTFIX ─────────────────────────────────────────────────────────────────

create WONTFIX "wontfix" \
"WONTFIX: Interactive TUI / curses terminal" \
'This is an MCP automation tool — no human sits at the terminal. A curses TUI adds complexity with zero benefit for Copilot/LLM callers.'

create WONTFIX "wontfix" \
"WONTFIX: GUI (Tkinter / Qt / web)" \
'Same rationale as TUI. MCP exposes structured tools, not a visual interface.'

create WONTFIX "wontfix" \
"WONTFIX: SSH / Telnet session handling" \
'Out of scope. Use an SSH client directly. The daemon handles local serial only.'

create WONTFIX "wontfix" \
"WONTFIX: Kermit / ZModem file transfer" \
'Xmodem/Ymodem (P3) is sufficient if file transfer is ever needed. ZModem/Kermit add significant complexity for negligible embedded use.'

create WONTFIX "wontfix" \
"WONTFIX: VT100 / ANSI terminal emulation" \
'No human sees the raw output stream. Terminal emulation state machines are unnecessary — we strip control characters in logging instead.'

create WONTFIX "wontfix" \
"WONTFIX: Screen / tmux integration" \
'Not relevant for Copilot/MCP calls. The Unix socket already allows multiple independent clients.'

create WONTFIX "wontfix" \
"WONTFIX: Multi-session terminal mux (screen-like)" \
'The daemon Unix socket already handles multiple simultaneous clients with proper locking. A full mux layer adds no value.'

echo ""
echo "All issues created."
