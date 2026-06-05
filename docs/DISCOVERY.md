# Device discovery

`serial_daemon.discover()` is a generic, device-agnostic primitive for finding
serial devices safely and fast. Every device repo (riden, rigol, can, hantek, …)
should call it instead of re-implementing port scanning, so the hard-won rules
below live in one place.

```python
from serial_daemon import discover

result = discover(
    probe=my_probe,            # (serial.Serial) -> dict | None   (device-specific)
    vid_allowlist={0x1A86},    # CH34x; restricts what gets opened
    bauds=[115_200],           # optional; default CANDIDATE_BAUDS, fastest first
    dtr="pulse",               # optional DTR assert/pulse before probing
    timeout_s=0.2,
    max_scan_s=5.0,
    max_workers=8,
)
# result -> {ports_scanned, bauds_scanned, found:[{port,baud,probe_ms,identity}],
#            found_count, errors, skipped, timed_out, max_scan_s, scan_ms}
```

The caller supplies only the two device-specific pieces — the **probe callback**
and the **VID allowlist**. Everything else is generic.

## The probe callback

`probe(ser)` receives an **already-open** `serial.Serial` (at the baud under
test, with DTR applied) and does its own I/O — a Modbus identity read, a SCPI
`*IDN?`, whatever identifies *your* device. Return an identity `dict` on a match
or `None` otherwise.

> **The probe must never open its own port.** `discover()` owns the port
> lifecycle (one `open()` per port). A probe that opens a second handle on the
> same device breaks the one-owner-thread-per-port guarantee below.

For plain ASCII-console devices, `make_ascii_probe()` builds a ready-made probe
that accepts any port returning readable ASCII (the same scoring rule as
`SerialWorker.detect_baud`).

## What `discover()` guarantees

1. **Identify before open.** Candidate ports come from `list_candidate_ports()`,
   which filters `serial.tools.list_ports.comports()` by USB VID *before*
   opening anything. Passing `vid_allowlist={0x1A86}` skips ST-Link / Pico
   CMSIS-DAP / ESP32-JTAG CDC-ACM devices entirely — they are not the target and
   a blind `open()`/probe on them can hang for tens of seconds.
   *(In awto-riden this took a full scan from ~65 s down to ~0.39 s.)*
2. **One owner thread per port** — see the proof below.
3. **Hard time budget.** `max_scan_s` caps wall-clock. Any port whose thread has
   not finished by the deadline is reported under `skipped` — never silently
   dropped — and its daemon thread is abandoned.
4. **Baud scanning.** Each port is tried across `bauds` (default
   `CANDIDATE_BAUDS`, fastest first). The first baud the probe accepts wins (one
   device per port). This is the generalisation of `detect_baud`, except *your*
   probe is the oracle instead of an ASCII heuristic.
5. **DTR probing.** `dtr='high'|'low'|'pulse'` drives DTR before each probe via
   the same helpers as `SerialWorker.set_line` / `pulse_line`. Some adapters
   need DTR toggled to respond, or held off to avoid pinning the MCU in reset.
6. **High-resolution timing logs.** Per-probe milliseconds (`perf_counter`) at
   `DEBUG`, totals at `INFO`.

## One thread per port (proven)

**Never open the same serial port from more than one thread at once.**
`discover()` fans out across *ports* in parallel (bounded by a semaphore) but
probes the bauds *within* a port serially, in that port's single daemon thread.
This is not caution — it is a measured requirement.

Controlled trial against a live device on `/dev/ttyUSB0` (5 reads of the
identity block each), from awto-riden:

| Trial | Setup | Result |
|---|---|---|
| A | 5 reads, **sequential**, same port | **5/5 succeed** (~135 ms each) |
| B | 5 threads, **same port + addr**, concurrent | **0/5** — all time out |
| C | 5 threads, same port, **different addrs**, concurrent | **0/5** — all time out |

### Why — and what it is *not*

It is **not** a Python problem and **not** a libusb thread-safety problem.
`/dev/ttyUSB*` is a kernel character device served by an in-kernel UART driver
(`ch341-uart`, `ftdi_sio`, …); pyserial talks to it with plain
`open()`/`read()`/`write()` syscalls — **libusb is not in the path at all**.
libusb's threading caveats are for userspace USB drivers that claim the
interface directly; this stack never touches it.

The real cause is **sharing one half-duplex, unframed byte stream**:

1. The kernel allows multiple `open()`s of one tty and does **not** serialize them.
2. Each probe does `reset_input_buffer()` (a `TCIFLUSH` on the *shared* RX
   buffer) → write request → read reply.
3. The wire carries no in-stream tag saying whose reply is whose. With threads
   interleaved, one thread's flush discards bytes another is waiting for, and
   replies fragment across readers. Every frame/CRC check fails → every probe
   times out (which is why *all* fail, not just the losers).

A C program doing the same thing would fail identically. The fix is not a lock
or a "thread-safe" library — it is **one owner thread per port**, which is how
`discover()` is written.

### Why daemon threads

A serial `open()` can block well past the scan budget (the read timeout does not
bound `open()`). `discover()` uses `daemon=True` worker threads so a stuck
`open()` is abandoned cleanly when the process exits, instead of a non-daemon
worker keeping the whole process alive at interpreter exit even after the result
is in hand.

## References

- Original fix & full mechanism writeup: `USB_TESTING.md` in awto-riden
  (awtoau/awto-riden@feff804).
- Threading / logging / bounded fan-out conventions: awto-dan
  `python/python-coding-style.md`.
</content>
</invoke>
