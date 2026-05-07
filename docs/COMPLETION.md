# awto-serial Monitor Completion — Firmware Implementer Guide

This guide explains how to add tab-completion support to your embedded device so it works with `ttu_cli.py monitor` and Copilot agents using this MCP server.

---

## How it works

When `ttu_cli.py monitor` starts, it sends one command to the device over the serial port (default: `help --json`) and reads back a single JSON line. That JSON describes every command your device understands. The host uses it to drive readline tab-completion — no further device interaction is needed for completion.

```
host                           device
 │                               │
 │  help --json\n  ──────────►  │
 │                               │
 │  ◄──────── {"version":"1",…} │
 │                               │
 │  (interactive REPL opens)     │
```

---

## The JSON format

Full machine-readable schema: [`completion-schema.json`](completion-schema.json)

Minimal valid response:

```json
{"version":"1","commands":[{"name":"status"}]}
```

Full example with subcommands and args:

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
        { "name": "reset",  "description": "Hard-reset ESP32"  },
        {
          "name": "send",
          "description": "Send raw AT command",
          "args": [
            { "name": "at_cmd", "type": "string" }
          ]
        }
      ]
    },
    {
      "name": "gpio",
      "description": "GPIO control",
      "subcommands": [
        {
          "name": "set",
          "args": [
            { "name": "pin",   "type": "integer", "choices": ["0","1","2","3"] },
            { "name": "value", "type": "choice",  "choices": ["0","1","high","low"] }
          ]
        },
        {
          "name": "read",
          "args": [
            { "name": "pin", "type": "integer", "choices": ["0","1","2","3"] }
          ]
        }
      ]
    },
    { "name": "version", "description": "Show firmware version" },
    { "name": "reboot",  "description": "Reboot the device"     }
  ]
}
```

### Field reference

| Field | Required | Description |
|-------|----------|-------------|
| `version` | yes | Must be `"1"` |
| `name` | no | Device/product name (informational) |
| `commands` | yes | Array of command objects |

**Command object:**

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Token typed on the command line |
| `description` | no | One-line help string shown on double-Tab |
| `subcommands` | no | Nested commands (same structure, recursive) |
| `args` | no | Positional arguments (see below) |

**Arg object:**

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Argument name (shown in completion hints) |
| `type` | yes | `string` / `integer` / `choice` / `path` |
| `description` | no | One-line hint |
| `choices` | no | Enumerated values used for tab-completion |

> `subcommands` and `args` are mutually exclusive per command node.

---

## Implementation examples

### Arduino / C (ESP32, STM32, etc.)

```c
// Handle help --json command in your serial dispatch loop
if (strcmp(cmd, "help") == 0 && strcmp(arg, "--json") == 0) {
    Serial.println(
        "{\"version\":\"1\",\"name\":\"mydevice\","
        "\"commands\":["
          "{\"name\":\"esp\",\"subcommands\":["
            "{\"name\":\"status\"},"
            "{\"name\":\"reset\"}"
          "]},"
          "{\"name\":\"version\"},"
          "{\"name\":\"reboot\"}"
        "]}"
    );
    return;
}
```

Key rules:
- Output must be **one complete JSON line** (no embedded newlines)
- Terminate with `\n` (or `\r\n` if your EOL is CRLF)
- Must be valid JSON — no trailing commas

### MicroPython

```python
import json

COMPLETION = {
    "version": "1",
    "name": "mydevice",
    "commands": [
        {"name": "esp", "subcommands": [
            {"name": "status"},
            {"name": "reset"},
        ]},
        {"name": "gpio", "subcommands": [
            {"name": "set",  "args": [
                {"name": "pin",   "type": "integer", "choices": ["0","1","2","3"]},
                {"name": "value", "type": "choice",  "choices": ["0","1","high","low"]},
            ]},
            {"name": "read", "args": [
                {"name": "pin", "type": "integer", "choices": ["0","1","2","3"]},
            ]},
        ]},
        {"name": "version"},
        {"name": "reboot"},
    ],
}

def handle_command(line):
    parts = line.strip().split()
    if parts == ["help", "--json"]:
        print(json.dumps(COMPLETION))
        return
    # ... rest of dispatch
```

### Zephyr RTOS / C (compact)

```c
static const char HELP_JSON[] =
    "{\"version\":\"1\",\"name\":\"mydev\","
    "\"commands\":["
    "{\"name\":\"sensor\",\"subcommands\":["
      "{\"name\":\"read\",\"args\":[{\"name\":\"id\",\"type\":\"integer\"}]},"
      "{\"name\":\"calibrate\"}"
    "]},"
    "{\"name\":\"reboot\"}"
    "]}";

/* in your shell/UART RX handler: */
if (strncmp(buf, "help --json", 11) == 0) {
    uart_write(HELP_JSON);
    uart_write("\n");
}
```

---

## Connecting to ttu_cli.py monitor

Once your device responds to `help --json`, the monitor mode picks it up automatically:

```bash
# Start monitor (fetches completion JSON from device at startup)
python ttu_cli.py monitor

# Override the command the host sends to fetch completion data
python ttu_cli.py monitor --complete-cmd "? --json"

# Or load completion from a local file (no device query)
python ttu_cli.py monitor --complete-file my-device.json
```

---

## Copilot / AI agent usage

If you are an AI agent working on firmware for a device that will be connected to this MCP server, you can:

1. Call the `serial_completion_schema` MCP tool — it returns the full JSON schema and a worked example
2. Generate a `help --json` handler for your target platform using the examples above
3. Test with `ttu_cli.py query "help --json"` — the response should be a single JSON line

Schema reference URL: https://github.com/awto-au/awto-mcp-serial/blob/main/docs/completion-schema.json
