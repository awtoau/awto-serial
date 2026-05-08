#!/usr/bin/env python3
"""
ttu  —  CLI client for the awto serial daemon.

Subcommands (chosen to mirror tio(1) where applicable):

    ttu_cli.py query "status"            send command, print response
    ttu_cli.py ping                      health-check
    ttu_cli.py info                      show port/baud/eol
    ttu_cli.py set-baud 2480000          change baud rate live
    ttu_cli.py set-eol crlf              set line ending (lf|cr|crlf)
    ttu_cli.py detect-baud               auto-detect baud (fastest first)
    ttu_cli.py detect-eol                auto-detect line ending

Backwards-compat shorthand:
    ttu_cli.py "status"                  ≡ ttu_cli.py query status

Stdin pipe:
    echo status | ttu_cli.py query       reads command from stdin

Requires the free-threaded (no-GIL) CPython build: python3.14t
"""

import argparse
import atexit
import json
import logging
import logging.handlers
import readline
import socket
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from protocol import DEFAULT_SOCKET_PATH, DEFAULT_TIMEOUT_MS, EOL_BYTES, send_request

# ---------------------------------------------------------------------------
# Logging  (syslog + stderr)
# ---------------------------------------------------------------------------

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    root = logging.getLogger()
    root.setLevel(level)

    try:
        syslog = logging.handlers.SysLogHandler(
            address="/dev/log",
            facility=logging.handlers.SysLogHandler.LOG_USER,
        )
        syslog.ident = "awto-ttu: "
        syslog.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(syslog)
    except OSError:
        pass

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(
        logging.Formatter("ttu[%(process)d]: %(levelname)-8s %(message)s")
    )
    root.addHandler(stderr)


log = logging.getLogger("cli")

# ---------------------------------------------------------------------------
# Daemon communication
# ---------------------------------------------------------------------------

def _call(req: dict) -> dict:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(DEFAULT_SOCKET_PATH)
            return send_request(sock, req)
    except FileNotFoundError:
        print(
            f"error: daemon socket not found at {DEFAULT_SOCKET_PATH}\n"
            "       Start the daemon first:  python serial_daemon.py",
            file=sys.stderr,
        )
        sys.exit(1)
    except ConnectionRefusedError:
        print(
            "error: daemon is not running\n"
            "       Start it with:  python serial_daemon.py",
            file=sys.stderr,
        )
        sys.exit(1)


def _print_response(resp: dict) -> None:
    """Pretty-print the most useful field for each response type."""
    if "response" in resp:
        if "timestamp" in resp and resp["timestamp"]:
            print(f"[{resp['timestamp']}] {resp['response']}")
        else:
            print(resp["response"])
    elif "info" in resp:
        info = resp["info"]
        for k, v in info.items():
            print(f"{k}: {v}")
    elif "baud" in resp:
        print(f"baud={resp['baud']}")
    elif "eol" in resp:
        print(f"eol={resp['eol']}")
    else:
        print(resp)


# ---------------------------------------------------------------------------
# Monitor mode helpers
# ---------------------------------------------------------------------------

_HISTORY_FILE = Path("~/.ttu_history").expanduser()


def _load_completion_tree(
    complete_cmd: str,
    complete_file: Optional[str],
    timeout_ms: int,
) -> dict:
    """Return the parsed completion schema dict from file or device query."""
    if complete_file:
        try:
            with open(complete_file) as fh:
                return json.load(fh)
        except Exception as exc:
            print(f"warning: could not load completion file: {exc}", file=sys.stderr)
            return {}

    resp = _call({"cmd": "query", "line": complete_cmd, "timeout_ms": timeout_ms})
    if not resp.get("ok"):
        print(f"warning: completion fetch failed: {resp.get('error')}", file=sys.stderr)
        return {}
    raw = resp.get("response", "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"warning: completion JSON parse error: {exc}", file=sys.stderr)
        return {}


def _build_completer(tree: dict):
    """Return a readline-compatible tab-completer for the given schema tree."""

    def _index(commands: list) -> dict:
        return {
            c["name"]: {
                "desc": c.get("description", ""),
                "args": c.get("args", []),
                "sub": _index(c.get("subcommands", [])),
            }
            for c in commands
            if "name" in c
        }

    root = _index(tree.get("commands", []))
    _matches: list[str] = []

    def completer(text: str, state: int) -> Optional[str]:
        if state == 0:
            line = readline.get_line_buffer()
            begidx = readline.get_begidx()
            before_tokens = line[:begidx].split()

            # Walk the command tree with before_tokens to find what comes next.
            node = root
            args_of_cmd: list = []
            arg_start_idx = 0

            i = 0
            while i < len(before_tokens):
                tok = before_tokens[i]
                if tok in node:
                    entry = node[tok]
                    if entry["sub"]:
                        node = entry["sub"]
                        i += 1
                        arg_start_idx = i
                    else:
                        # Leaf command — remaining tokens are positional args.
                        args_of_cmd = entry["args"]
                        arg_start_idx = i + 1
                        i += 1
                        node = {}
                        break
                else:
                    node = {}
                    args_of_cmd = []
                    break

            if args_of_cmd:
                n_args_typed = len(before_tokens) - arg_start_idx
                if n_args_typed < len(args_of_cmd):
                    choices = args_of_cmd[n_args_typed].get("choices", [])
                    _matches[:] = [c + " " for c in choices if c.startswith(text)]
                else:
                    _matches[:] = []
            else:
                _matches[:] = [k + " " for k in node if k.startswith(text)]

        return _matches[state] if state < len(_matches) else None

    return completer


def _run_monitor(args) -> None:
    """Interactive readline REPL with device-supplied tab-completion."""
    tree = _load_completion_tree(
        complete_cmd=args.complete_cmd,
        complete_file=args.complete_file,
        timeout_ms=args.timeout,
    )

    device_name = tree.get("name", "device")

    # Set up tab completion.
    readline.set_completer(_build_completer(tree))
    readline.parse_and_bind("tab: complete")
    readline.set_completer_delims(" \t")

    # Persistent history.
    try:
        readline.read_history_file(_HISTORY_FILE)
    except FileNotFoundError:
        pass
    readline.set_history_length(1000)
    atexit.register(readline.write_history_file, _HISTORY_FILE)

    cmds = [c["name"] for c in tree.get("commands", [])]
    print(f"awto monitor  [{device_name}]  — Tab to complete, Ctrl-D to exit")
    if cmds:
        print(f"Commands: {', '.join(cmds)}")
    print()

    prompt = f"{device_name}> "
    while True:
        try:
            line = input(prompt).strip()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            continue

        if not line:
            continue

        resp = _call({"cmd": "query", "line": line, "timeout_ms": args.timeout})
        if resp.get("ok"):
            if resp.get("warning"):
                print(f"[warn: {resp['warning']}]", file=sys.stderr)
            response = resp.get("response", "")
            if response:
                print(response)
        else:
            print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        prog="ttu",
        description="Send ASCII commands and control to the awto serial daemon.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    ap.add_argument("--timeout", "-t", type=int, default=DEFAULT_TIMEOUT_MS,
                    metavar="MS",
                    help=f"response timeout in ms (default {DEFAULT_TIMEOUT_MS})")

    sub = ap.add_subparsers(dest="subcmd", metavar="SUBCMD")

    sp_query = sub.add_parser("query", help="send command, print response")
    sp_query.add_argument("text", nargs="?", default=None,
                          help="command text (or omit to read from stdin)")
    sp_query.add_argument("--timestamp", choices=["iso8601", "24hour", "epoch"],
                          help="include timestamp in the response")
    sp_query.add_argument("--output-mode", choices=["text", "hex"], default="text",
                          help="response format (default: text)")

    sub.add_parser("ping", help="health-check the daemon")
    sub.add_parser("info", help="show current port / baud / eol")

    sp_baud = sub.add_parser("set-baud", help="set baud rate live")
    sp_baud.add_argument("baud", type=int)

    sp_eol = sub.add_parser("set-eol", help="set line ending")
    sp_eol.add_argument("eol", choices=list(EOL_BYTES.keys()))

    sp_db = sub.add_parser("detect-baud", help="auto-detect baud rate (fastest first)")
    sp_db.add_argument("--probe", default="?", help="probe string (default '?')")

    sp_de = sub.add_parser("detect-eol", help="auto-detect line ending")
    sp_de.add_argument("--probe", default="?", help="probe string (default '?')")

    sp_sm = sub.add_parser("set-map", help="set character mapping (tio -m style)")
    sp_sm.add_argument("maps", help="comma-separated: INLCRNL,ICRNL,ONLCRNL,ODELBS (empty clears)")

    sp_ls = sub.add_parser("log-start", help="start logging RX data to file (always appended)")
    sp_ls.add_argument("path", help="log file path")
    sp_ls.add_argument("--strip", action="store_true",
                       help="strip ANSI/control chars before logging")
    sp_ls.add_argument("--max-bytes", type=int, default=0,
                       help="rotate when active log exceeds this many bytes (0 disables)")
    sp_ls.add_argument("--backups", type=int, default=0,
                       help="number of rotated log generations to keep")
    sp_ls.add_argument("--timestamp", choices=["iso8601", "24hour", "epoch"],
                       help="also set timestamp format")

    sub.add_parser("log-stop", help="stop logging RX data")

    sp_ts = sub.add_parser("set-timestamp", help="set log timestamp format")
    sp_ts.add_argument("format", nargs="?", default="",
                       choices=["iso8601", "24hour", "epoch", ""],
                       help="timestamp format (empty to disable)")

    sp_se = sub.add_parser("set-echo", help="enable or disable local echo")
    sp_se.add_argument("state", choices=["on", "off"])

    sub.add_parser("stats", help="show RX/TX byte counts and uptime")

    sp_hist = sub.add_parser("history", help="show received lines (newest first)")
    sp_hist.add_argument("--limit", type=int, default=50, help="max lines (default 50)")
    sp_hist.add_argument("--offset", type=int, default=0, help="skip N lines from newest")

    sp_ru = sub.add_parser("read-until", help="wait for unsolicited RX text matching a regex")
    sp_ru.add_argument("pattern", help="Python regex pattern to wait for")

    sp_drain = sub.add_parser("drain", help="drain unsolicited RX buffer")
    sp_drain.add_argument("--max-bytes", type=int, default=0, help="max bytes to return (0 = all)")

    sub.add_parser("list-ports", help="list available serial ports")

    sp_sl = sub.add_parser("set-line", help="set DTR or RTS line state")
    sp_sl.add_argument("line", choices=["dtr", "rts"])
    sp_sl.add_argument("state", choices=["high", "low", "toggle"])

    sp_sb = sub.add_parser("send-break", help="send serial BREAK condition")
    sp_sb.add_argument("--duration", type=int, default=250, metavar="MS",
                       help="duration in ms (default 250)")

    sp_pl = sub.add_parser("pulse-line", help="pulse DTR or RTS (high → wait → low)")
    sp_pl.add_argument("line", choices=["dtr", "rts"])
    sp_pl.add_argument("--duration", type=int, default=100, metavar="MS",
                       help="pulse duration in ms (default 100)")

    sp_mon = sub.add_parser("monitor", help="interactive REPL with tab-completion")
    sp_mon.add_argument(
        "--complete-cmd",
        default="help --json",
        metavar="CMD",
        help="command sent to device to fetch completion JSON (default: 'help --json')",
    )
    sp_mon.add_argument(
        "--complete-file",
        default=None,
        metavar="PATH",
        help="load completion JSON from file instead of querying device",
    )

    # Back-compat: ``ttu_cli.py "status"`` with no subcommand → query
    args, leftover = ap.parse_known_args()
    if args.subcmd is None and leftover:
        args.subcmd = "query"
        args.text = " ".join(leftover)
    elif args.subcmd is None:
        # also handle stdin pipe with no subcommand
        if not sys.stdin.isatty():
            args.subcmd = "query"
            args.text = None
            args.timestamp = None
        else:
            ap.print_help()
            sys.exit(2)

    _setup_logging(args.verbose)

    if args.subcmd == "ping":
        resp = _call({"cmd": "ping"})
    elif args.subcmd == "info":
        resp = _call({"cmd": "info"})
    elif args.subcmd == "set-baud":
        resp = _call({"cmd": "set_baud", "baud": args.baud})
    elif args.subcmd == "set-eol":
        resp = _call({"cmd": "set_eol", "eol": args.eol})
    elif args.subcmd == "detect-baud":
        resp = _call({"cmd": "detect_baud", "probe": args.probe,
                      "timeout_ms": args.timeout})
    elif args.subcmd == "detect-eol":
        resp = _call({"cmd": "detect_eol", "probe": args.probe,
                      "timeout_ms": args.timeout})
    elif args.subcmd == "set-map":
        resp = _call({"cmd": "set_map", "maps": args.maps})
    elif args.subcmd == "log-start":
        resp = _call(
            {
                "cmd": "log_start",
                "path": args.path,
                "strip": args.strip,
                "max_bytes": args.max_bytes,
                "backups": args.backups,
            }
        )
        if resp.get("ok") and getattr(args, "timestamp", None):
            ts_resp = _call({"cmd": "set_timestamp", "format": args.timestamp})
            if not ts_resp.get("ok"):
                print(f"error: {ts_resp.get('error', 'unknown')}", file=sys.stderr)
                sys.exit(1)
    elif args.subcmd == "log-stop":
        resp = _call({"cmd": "log_stop"})
    elif args.subcmd == "set-timestamp":
        resp = _call({"cmd": "set_timestamp", "format": args.format})
    elif args.subcmd == "set-echo":
        resp = _call({"cmd": "set_echo", "enabled": args.state == "on"})
    elif args.subcmd == "stats":
        resp = _call({"cmd": "stats"})
        if resp.get("ok"):
            s = resp.get("stats", {})
            for k, v in s.items():
                print(f"{k}: {v}")
            sys.exit(0)
        else:
            print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
            sys.exit(1)
    elif args.subcmd == "history":
        resp = _call({"cmd": "history", "limit": args.limit, "offset": args.offset})
        if resp.get("ok"):
            for entry in resp.get("lines", []):
                print(f"[{entry.get('ts', '')}] {entry.get('line', '')}")
            print(f"  ({resp.get('total', 0)} total, offset={resp.get('offset', 0)})")
            sys.exit(0)
        else:
            print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
            sys.exit(1)
    elif args.subcmd == "read-until":
        resp = _call({"cmd": "read_until", "pattern": args.pattern, "timeout_ms": args.timeout})
    elif args.subcmd == "drain":
        req = {"cmd": "drain"}
        if args.max_bytes > 0:
            req["max_bytes"] = args.max_bytes
        resp = _call(req)
    elif args.subcmd == "list-ports":
        resp = _call({"cmd": "list_ports"})
        if resp.get("ok"):
            ports = resp.get("ports", [])
            if not ports:
                print("no serial ports found")
            for p in ports:
                vid = f"{p['vid']:04X}" if p.get("vid") else "????"
                pid = f"{p['pid']:04X}" if p.get("pid") else "????"
                desc = p.get("description") or p.get("hwid") or ""
                print(f"{p['device']:<20} {vid}:{pid}  {desc}")
            sys.exit(0)
        else:
            print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
            sys.exit(1)
    elif args.subcmd == "set-line":
        resp = _call({"cmd": "set_line", "line": args.line, "state": args.state})
    elif args.subcmd == "send-break":
        resp = _call({"cmd": "send_break", "duration_ms": args.duration})
    elif args.subcmd == "pulse-line":
        resp = _call({"cmd": "pulse_line", "line": args.line, "duration_ms": args.duration})
    elif args.subcmd == "monitor":
        _run_monitor(args)
        sys.exit(0)
    elif args.subcmd == "query":
        text = args.text
        if text is None:
            if sys.stdin.isatty():
                print("error: no command given (provide arg or pipe to stdin)",
                      file=sys.stderr)
                sys.exit(2)
            text = sys.stdin.read().strip()
        log.debug("query: %r timeout=%dms", text, args.timeout)
        req = {"cmd": "query", "line": text, "timeout_ms": args.timeout}
        req["output_mode"] = getattr(args, "output_mode", "text")
        if getattr(args, "timestamp", None):
            req["include_timestamp"] = True
            req["timestamp_format"] = args.timestamp
        resp = _call(req)
    else:
        ap.print_help()
        sys.exit(2)

    if not resp.get("ok"):
        print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)

    _print_response(resp)


if __name__ == "__main__":
    main()
