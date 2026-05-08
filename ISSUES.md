# awto-mcp — Issue Backlog

Canonical repository: `awto-au/awto-mcp`.
Historical references to `awto-mcp-serial` may still appear in notes and scripts; `gh` resolves that alias to `awto-au/awto-mcp`.

Priority key (this is an MCP automation tool, not a human terminal):
- **P0** critical / blocks basic usefulness
- **P1** high value, planned next
- **P2** useful, not urgent
- **P3** nice to have
- **WONTFIX** irrelevant for an MCP/automation-first tool

Sources reviewed: tio 3.9, pyserial miniterm, putty, python-serial-monitor repos.

---

## P0 — Critical

### #1 Auto baud-rate detection
Probe the device at a standard set of baud rates (300, 1200, 4800, 9600, 19200,
38400, 57600, 115200, 230400, 460800, 921600, 1000000, 2000000) and detect which
one produces valid ASCII printable output.  Send a known probe string (e.g.
empty line `\n`) at each rate, collect N bytes, score by printable-char ratio.
Return detected baud and set it on the `SerialWorker`.

MCP tool: `serial_detect_baud(probe_cmd?)`

### #2 Line-ending auto-detection (CR / LF / CRLF)
Send the same probe command with CR, LF, and CRLF variants.  Check which
produces a meaningful non-empty response.  Store detected line ending in
`SerialWorker` and use it for all subsequent `query()` calls.

Also expose as explicit config: `line_ending = cr | lf | crlf | auto`

MCP tool: `serial_detect_line_ending(probe_cmd?)`

### #3 Logging to file (tio -L / --log-file / --log-append)
From the reference command line:
```
exec tio "$dev" -b "$BAUD" -m "$MODE" -t -L --log-file "${logfile}" --log-append
```
Daemon should optionally write all RX bytes to a log file.
- `--log-file <path>` — set log path
- `--log-append` — append instead of overwrite (default: overwrite)
- `--log-strip` — strip ANSI/control chars before logging

MCP tool: `serial_log_start(path, append?)`, `serial_log_stop()`

### #4 Per-line timestamps (tio -t / --timestamp)
From the same reference command:  `-t` adds ISO8601 or 24-hour timestamp to the
start of every received line.  Log file should include timestamps.  MCP
`serial_query` response should optionally include timestamps when requested.

Option: `timestamp_format = 24hour | iso8601 | epoch`

### #5 Character / line-ending mapping (tio -m / --map)
From the reference command: `-m "$MODE"` maps characters on input or output.
Needed for devices that send `\r\n` when we only want `\n`, or send bare `\r`.

Maps to implement first:
- `INLCRNL` — input: NL → CR+NL
- `ICRNL` — input: CR → NL
- `ONLCRNL` — output: NL → CR+NL
- `ODELBS` — output: DEL → BS

Store mapping in `SerialWorker`, apply in `query()`.

### #6 Review best Python serial-monitor / terminal projects on GitHub
Research and document which existing projects are worth borrowing from.
Top candidates from search (sorted by relevance, not just stars):

| Project | Stars | Notes |
|---|---|---|
| [pyserial/pyserial miniterm](https://github.com/pyserial/pyserial) | 3k | Reference impl, CRLF/CR/LF, filters, hex mode, DTR/RTS |
| [tio/tio](https://github.com/tio/tio) | 2.9k | C, best feature set, Lua scripting, auto-reconnect, log |
| [adamwtow/python-serial-monitor](https://github.com/adamwtow/python-serial-monitor) | 23 | Python, speedy reconnect, instant char send |
| [ZulNs/SerialMonitor](https://github.com/ZulNs/SerialMonitor) | 22 | Python Tkinter GUI — WONTFIX for us |
| [PBahner/Serial-Monitor](https://github.com/PBahner/Serial-Monitor) | 12 | Python CLI, Arduino-focused |

Action: read pyserial miniterm's Transform pipeline (CRLF/CR/LF/hex/colorize)
and port the relevant transforms to `serial_daemon.py`.

---

## P1 — High value

### #7 Auto-reconnect on device disconnect
If `serial.read()` raises `SerialException`, close the port and retry open with
exponential back-off (100 ms, 200 ms, 400 ms … cap 5 s).  Daemon stays alive.
Clients get `{"ok": false, "error": "reconnecting..."}` until port is back.

### #8 Serial port enumeration / listing (tio -l)
MCP tool: `serial_list_ports()` — returns list of available serial devices with
driver name, description, USB VID:PID, by-id symlink, and uptime (newest last).
Use `serial.tools.list_ports` from pyserial.

### #9 Configuration file / profiles
Support a TOML config file at `~/.config/awto-serial/config.toml`:
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
Daemon loads profile by name: `serial_daemon.py --profile ttu`

### #10 `--exec` / shell command with I/O redirected to device (tio --exec)
From tio: `ctrl-t R` or `--exec <cmd>` pipes a shell command's stdout/stdin
through the serial port.  For MCP this means:

MCP tool: `serial_exec(shell_cmd, timeout_ms?)` — run shell command, pipe its
output to serial, return serial's response.

---

## P2 — Useful, not urgent

### #11 Hex input / output mode (miniterm / tio --output-mode hex)
`serial_query` option `output_mode=hex` — return response as space-separated hex
bytes. Useful for debugging binary-ish frames mixed into ASCII console.

### #12 RX/TX byte statistics (tio ctrl-t s)
MCP tool: `serial_stats()` — return cumulative RX bytes, TX bytes, uptime,
error count since daemon start.

### #13 Local echo control
Option to have daemon echo TX back in the RX stream (for devices that don't echo).
`echo = true | false`

### #14 DTR / RTS / BREAK line control (miniterm ctrl-t toggle, tio --line)
MCP tools:
- `serial_set_line(line, state)` — DTR/RTS high/low/toggle
- `serial_send_break(duration_ms?)`
Useful for STM32 reset-via-DTR patterns.

### #15 Modem line pulse with configurable duration (tio line-pulse-duration)
`serial_pulse_line(line, duration_ms)` — DTR or RTS pulse.  Needed for MCU
reset/boot-mode entry.

### #16 Continuous RX background reader / monitor mode
Add a background thread in the daemon that continuously reads RX bytes into a
ring buffer regardless of active queries.  Expose:
- `serial_read_until(pattern, timeout_ms)` — wait for regex match in RX stream
- `serial_drain(max_bytes?)` — return whatever is buffered since last drain

This is needed when device sends unsolicited data (faults, status updates).

### #17 Pipe / stdin support
`echo "status" | python3.14t ttu_cli.py` — read command from stdin if no
positional arg, write response to stdout.  Already partially possible; make
explicit.

---

## P3 — Nice to have

### #18 Socket redirect (tio --socket inet:PORT)
Daemon listens on TCP port as well as Unix socket, so remote machines (or WSL)
can send commands without SSH.  Use with caution — no auth, loopback only.

### #19 Xmodem / Ymodem file transfer (tio ctrl-t x/y)
`serial_send_file(path, protocol)` — XMODEM_1K / XMODEM_CRC / YMODEM.
Only relevant if target firmware supports it.

### #20 RS-485 half-duplex mode (tio --rs-485)
Set `serial.rs485_mode` on the port object.  Pyserial supports this natively.

### #21 Output delay (tio -o / -O)
`per_char_delay_ms` and `per_line_delay_ms` — slow down TX for devices that
can't buffer fast input (e.g. bootloaders with 9600 baud UART).

### #22 ANSI colour output in syslog / stderr (tio --color)
Colour-code RX vs TX in stderr output when running interactively.
Respect `NO_COLOR` env variable.

### #23 systemd service unit
`awto-serial-daemon.service` — starts daemon on boot, restarts on failure,
`After=dev-ttyACM0.device` dependency.

---

## WONTFIX — Not applicable to an MCP/automation tool

| # | Feature | Reason |
|---|---|---|
| W1 | Interactive TUI / curses terminal | We are an AI tool, not a human terminal |
| W2 | GUI (Tkinter / Qt / web) | Same — MCP exposes structured tools, not UI |
| W3 | SSH / Telnet session | Out of scope; use SSH client directly |
| W4 | Kermit / ZModem file transfer | Xmodem/Ymodem sufficient if ever needed |
| W5 | VT100/ANSI terminal emulation | No human sees the output; irrelevant |
| W6 | Screen / tmux integration | Not relevant for Copilot/MCP calls |
| W7 | Multi-session mux (screen-like) | Unix socket already handles multi-client |

---

## Free-threaded Python status

| Item | Status |
|---|---|
| Package | `python3.14-freethreading` (Fedora 43, already installed) |
| Binary | `/usr/bin/python3.14t` |
| GIL | **Disabled** — `sys._is_gil_enabled()` → `False` |
| MCP server | `.vscode/mcp.json` uses `/usr/bin/python3.14t` |
| Tests | `test_harness.py` — run with `/usr/bin/python3.14t test_harness.py -v` |
| uv config | `pyproject.toml` `python = "3.14t"` |

Benefit: `ThreadPoolExecutor` calls in the daemon and test harness run truly in
parallel — no GIL contention between the serial lock thread, client handler
threads, and MCP call threads.
