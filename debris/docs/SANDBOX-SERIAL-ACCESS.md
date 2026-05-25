# Sandbox Serial Access Patterns

Because VS Code Copilot agents run inside a mount-namespace sandbox (via `bwrap`), they cannot directly interact with host block or character devices like `/dev/ttyACM*`, `/dev/ttyUSB*`, or `/dev/serial/*`. 

This limits the agent's ability to directly run `tio`, `stty`, `picotool`, or `probe-rs`.

This document lists practical topologies for bridging serial and hardware access into the agent sandbox.

For this repository, the supported default is the `awto-mcp` daemon plus MCP tools (the architecture in `README.md`). The other sections are alternatives for other environments or troubleshooting scenarios.

## 1. Project Default: Local MCP Serial Bridge (Supported)

Create or use a Model Context Protocol (MCP) server running **unsandboxed** on the host. The MCP server exposes high-level, tightly scoped tools for interacting with the hardware.

*   **Host Mechanism**: `serial_daemon.py` owns `/dev/tty*`; `mcp_server.py` exposes MCP tools over stdio via a Unix socket.
*   **Agent Interaction**: Standard MCP tool calls such as `serial_query`, `serial_history`, and `serial_log_start`.
*   **Pros**: Lowest friction for Copilot, structured API surface, and consistent behavior across sessions.
*   **Cons**: Requires the daemon and MCP server setup.

## 2. Alternative: Spooling / `tail` + `tee`

Run a continuous serial monitor on the host that writes to a known log file inside the shared workspace `tmp/` folder. The sandboxed agent parses the file to read.

*   **Host Mechanism**: `tio /dev/ttyACM0 | tee ./tmp/log.con &`
*   **Agent Interaction**:
    *   *Read*: `tail -n 50 ./tmp/log.con` (or `awk`, `grep`)
    *   *Write*: user-defined host write path (for example `tio`/`stty`/`printf` wrappers)
*   **Pros**: Unbreakable audit trail, minimal setup, very robust.
*   **Cons**: High friction for TX (sending); this repo does not ship a standard write wrapper for this mode.

## 3. Alternative: `socat` TCP Bridge

Bind the serial port to a local TCP socket. The VS Code sandbox shares the local network interface, allowing the agent to open a standard TCP socket to read and write bytes natively.

*   **Host Mechanism**: 
    ```bash
    socat TCP-LISTEN:54321,reuseaddr,fork file:/dev/ttyACM0,nonblock,b115200,raw,echo=0
    ```
*   **Agent Interaction**: Python `socket` connections, `nc localhost 54321`, or `telnet`.
*   **Pros**: Exceedingly fast and clean. Allows complex, bidirectional Python test scripts to run *inside* the sandbox.
*   **Cons**: Doesn't produce an automatic on-disk transcript of the conversation unless recorded explicitly.

## 4. Alternative: `tmux` IPC Socket (Human-Attended Debugging)

Start the serial monitor inside a detached `tmux` session mapped to a specific socket file inside the workspace `tmp/` directory.

*   **Host Mechanism**:
    ```bash
    tmux -S ./tmp/tmux.sock new-session -d -s pico_serial "tio /dev/ttyACM0"
    ```
*   **Agent Interaction**:
    *   *Write*: `tmux -S ./tmp/tmux.sock send-keys -t pico_serial "cmd" C-m`
    *   *Read*: `tmux -S ./tmp/tmux.sock capture-pane -t pico_serial -p`
*   **Pros**: Highly observable. A human dev can `tmux -S ./tmp/tmux.sock attach` and watch exactly what the AI is typing and seeing on the serial console in real-time.
*   **Cons**: Needs screen-scraping logic. `tmux`'s `capture-pane` has rendering limits. Not part of this repo's primary MCP workflow.

## 5. Named Pipes (Not Recommended)

Mapping `cat /dev/ttyACM0 > ./tmp/rx_pipe` and `cat ./tmp/tx_pipe > /dev/ttyACM0`.

*   **Cons**: Named pipes in Unix block on open. If the agent makes a mistake in the read/write synchronization logic, the terminal process hangs indefinitely. Avoid named pipes for agent-driven scripts.
