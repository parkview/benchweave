# nanoDLA logic analyser — MCP server

A thin MCP server (`benchweave-nanodla`) that lets an AI client drive the nanoDLA
logic analyser through the local `sigrok-cli` binary. It handles device discovery,
bounded capture to a Value Change Dump (VCD) file, and protocol decoding with
libsigrokdecode's UART/I²C/SPI/CAN/… decoders.

The nanoDLA is an FX2LP USB device (USB ID `1d50:608c`) running the `fx2lafw`
firmware — a bulk-USB sample-streaming device, *not* a serial UART. It exposes 8
channels (D0–D7) at 20 kHz–24 MHz sample rates.

**Board revisions.** Tested against the nanoDLA **v1.3** board. See the
[hardware compatibility table](hardware.md) for the current status of each
revision, including **v2.1** (awaiting testing).

## Scope

This server is deliberately **not** an OTDP adapter. OTDP's transport model only has
`serial` (and one `can`) bindings, so a bulk-USB device cannot be represented by an
OTDP descriptor today, and capture/streaming is still an open design question
(upstream issue `madeinoz67/benchweave#43`). `sigrok-cli` already handles firmware,
sampling, triggers and decoding, so this server only shells out and shapes the
results — it owns no device protocol logic.

## Prior art

Three existing MCP servers already wrap sigrok/PulseView for logic-analyser control;
reviewed before this server was written:

| Project | Language | Notes |
|---|---|---|
| [daedalus/mcp-sigrok](https://github.com/daedalus/mcp-sigrok) | Python | Thin `sigrok-cli` wrapper — closest to this server's shape (a subprocess wrapper exposing scan/capture/decode). |
| [KenosInc/sigrok-mcp-server](https://github.com/KenosInc/sigrok-mcp-server) | Go | Headless stdio server, structured JSON output; no UI. |
| [sandraschi/logic-analyzer-mcp](https://github.com/sandraschi/logic-analyzer-mcp) | Python (FastMCP) | Ships a browser webapp with a **trace preview** view, plus VCD export for PulseView. |

None of them reimplements full PulseView-grade waveform rendering on a webpage: the
only browser visualisation is sandraschi's downsampled trace preview, and all three
delegate full inspection to PulseView. That matches BenchWeave's own position
(visualisation is gateway-owned; see the capture/streaming design, issue `#43`).

## Prerequisites

- `sigrok-cli` installed and on `PATH` (0.7.x tested; option names differ from older
  documentation).
- The nanoDLA plugged in and visible:
  ```sh
  sigrok-cli --scan
  # fx2lafw:conn=3.77 - sigrok FX2 LA (8ch) [S/N: sigrok FX2 8ch] with 8 channels: D0 D1 D2 D3 D4 D5 D6 D7
  ```
- **Close PulseView before capturing.** PulseView holds the fx2lafw USB interface,
  and a capture will fail with `Unable to claim USB interface`.

## Registration

The server is part of the BenchWeave package:

- Entry point (`pyproject.toml`): `nanodla-mcp = "benchweave.nanodla_mcp:main"`.
- MCP registration (`.mcp.json`):
  ```json
  { "benchweave-nanodla": { "command": "uv", "args": ["run", "nanodla-mcp"] } }
  ```

Claude Code reads `.mcp.json` at startup, so restart the session to load a newly
added server's tools.

## Configuration

Environment variables, all optional:

| Variable | Default | Purpose |
|---|---|---|
| `SIGROK_CLI` | `sigrok-cli` | Path to the sigrok-cli binary |
| `NANODLA_CAPTURE_DIR` | `captures/nanodla` | Directory that captures are written to |
| `NANODLA_TIMEOUT` | `30` | Seconds before a capture/scan subprocess is killed |

## Tools

Eight tools are exposed. Most map to a single `sigrok-cli` invocation, shown in the
examples for transparency; `list_captures` and `annotate` touch the capture store
directly.

### `scan_devices`

List logic analyser devices visible to sigrok.

```json
{"spec": "fx2lafw:conn=3.77", "description": "sigrok FX2 LA (8ch) [S/N: sigrok FX2 8ch] with 8 channels: D0 D1 D2 D3 D4 D5 D6 D7"}
```

Backing command: `sigrok-cli --scan`.

### `device_info`

Show a device's capabilities (channels, samplerates, triggers). `spec` defaults to
`fx2lafw`. Returns the raw `sigrok-cli -d <spec> --show` text.

### `capture`

Capture a bounded run to VCD + native-session files and return a summary.

| Parameter | Default | Meaning |
|---|---|---|
| `samplerate` | `1000000` | Hz, `20000`..`24000000` |
| `samples` | `1000` | Run length in samples |
| `channels` | `"D0,D1,D2,D3,D4,D5,D6,D7"` | Comma-separated channel names |
| `trigger` | `None` | Optional, e.g. `"D0=r"` (rising) or `"D0=1"` (high) |
| `name` | ISO-8601 timestamp | File stem (default `nanodla_<YYYY-MM-DDTHH-MM-SS>`); writes `<stem>.vcd`, `<stem>.sr` and `<stem>.json` |
| `timeout` | `30` | Seconds before giving up |

```json
{
  "file": "captures/nanodla/nanodla_2026-09-18T16-03-39.vcd",
  "sr_file": "captures/nanodla/nanodla_2026-09-18T16-03-39.sr",
  "metadata_file": "captures/nanodla/nanodla_2026-09-18T16-03-39.json",
  "captured_at": "2026-09-18T16:03:39.123456+10:00",
  "device": "fx2lafw",
  "stem": "nanodla_2026-09-18T16-03-39",
  "samplerate_hz": 2000000,
  "samples": 4000000,
  "channels": "D0",
  "trigger": null,
  "duration_s": 2.0,
  "artifacts": [
    {"kind": "vcd", "file": "captures/nanodla/nanodla_2026-09-18T16-03-39.vcd", "size_bytes": 20454, "sha256": "…"},
    {"kind": "sr", "file": "captures/nanodla/nanodla_2026-09-18T16-03-39.sr", "size_bytes": 188320, "sha256": "…"}
  ]
}
```

One acquisition is written to three files of a single stem: a VCD (for the AI to read
and decode), a native sigrok session `.sr` (for a human to open in PulseView), and a
JSON capture-metadata manifest. The manifest records the capture specs
(`captured_at`, device, rate, samples, channels, trigger, duration) and an
`artifacts` list with each file's kind, path, size and SHA-256, so a human can
recover the specs without opening either file. The VCD header itself only records
rate/channels in a `$comment` and does not store baud or trigger.

Backing commands:
```sh
sigrok-cli -d fx2lafw --config samplerate=2000000 --channels D0 \
  --samples 4000000 -O vcd -o captures/nanodla/uart_loopback.vcd
sigrok-cli -i captures/nanodla/uart_loopback.vcd -O srzip -o captures/nanodla/uart_loopback.sr
```

A `trigger` that never fires runs until `timeout` and then raises — see
[Trigger and the UART start bit](#trigger-and-the-uart-start-bit).

### `decode`

Decode a captured VCD with a libsigrokdecode protocol decoder. `options` are decoder
options (e.g. `{"rx": "D0", "baudrate": 115200, "format": "ascii"}`), joined as
`key=value`. `annotations` selects output classes (e.g. `uart=rx-data`); omit it to
receive every annotation.

When the matching capture-metadata manifest (`<stem>.json`) exists, the decode is
appended to its `decodes` list — `decoder`, `options`, `annotation_filter`,
`decoded_at` and the full annotation text — so a human can later see what the AI
decoded without re-running it.

```json
{"decoder": "uart", "file": "captures/nanodla/uart_loopback.vcd", "metadata_file": "captures/nanodla/uart_loopback.json", "annotations": "uart-1: B\nuart-1: e\n…"}
```

Backing command:
```sh
sigrok-cli -i captures/nanodla/uart_loopback.vcd \
  -P uart:rx=D0:baudrate=115200:format=ascii -A uart=rx-data
```

### `annotate`

Append a free-form analysis note to a capture's manifest. `file` is a capture path
(`.vcd` or `.sr`); `note` is the prose to record. The note is appended to the
manifest's `notes` list with a timestamp, so a human can later read what the AI
concluded without re-running anything.

```json
{"metadata_file": "captures/nanodla/uart_loopback.json", "recorded": true}
```

### `list_decoders`

List all protocol decoders available to sigrok (111 with the standard install), each
as `{"name", "description"}`. Backing command: `sigrok-cli -L`.

### `decoder_help`

Show a decoder's options, input channels and annotation classes. Returns the raw
`sigrok-cli -P <decoder> --show` text.

### `list_captures`

List saved VCDs, newest first, as `{"file", "sr_file", "metadata_file", "size_bytes", "modified"}`.
`sr_file` is the matching native-session path and `metadata_file` the capture-metadata path,
each `null` when absent. `limit` defaults to `20`.

## End-to-end example: UART loopback

The canonical use: a host UART's Tx wired to channel D0, transmit a known frame while
capturing, then decode it back.

1. **Discover the device** (`scan_devices`) to confirm `fx2lafw` is present.

2. **Capture** channel D0 at 2 MHz with no trigger — a 2 s window leaves room to
   transmit inside it:

   ```json
   capture({"samplerate": 2000000, "samples": 4000000, "channels": "D0", "name": "uart_loopback"})
   ```

3. **Transmit** from the host while the capture runs, e.g. at 115200 8N1:
   ```sh
   stty -F /dev/ttyACM0 115200 raw -echo -hupcl
   printf 'BenchWeave UART loopback @ 115200 baud 8N1 - line 1\r\n' > /dev/ttyACM0
   ```

4. **Decode** the capture:
   ```json
   decode({
     "file": "captures/nanodla/uart_loopback.vcd",
     "decoder": "uart",
     "options": {"rx": "D0", "baudrate": 115200, "format": "ascii"},
     "annotations": "uart=rx-data"
   })
   ```

   Expected annotations, one byte per line:

   ```text
   uart-1: B
   uart-1: e
   uart-1: n
   uart-1: c
   uart-1: h
   uart-1: W
   uart-1: e
   uart-1: a
   uart-1: v
   uart-1: e
   …
   ```

   Non-printable bytes render in hex brackets (`[0D]`, `[0A]` for CR/LF).

## What to expect from results

- **`capture` returns a summary, not the samples.** The waveform lives in two files:
  the VCD named by `file` (for the AI to read/decode) and the native session named by
  `sr_file` (for a human to open in PulseView). The manifest named by `metadata_file`
  records the specs and each artifact's SHA-256, so a human can recover the capture
  specs without opening either file.
- **Decode output is plain text.** With `format=ascii`, printable bytes appear as
  characters; with the default (hex) they appear as byte values (`42`, `65`, …). The
  `annotations` field is the full annotation stream, one line per annotation.
- **Captures accumulate** in `captures/nanodla` (override with `NANODLA_CAPTURE_DIR`).
  Each capture writes a `.vcd`, a `.sr` and a `.json` capture-metadata manifest; use
  `list_captures` to find them (`sr_file` names the session, `metadata_file` the manifest).

### Trigger and the UART start bit

Do **not** trigger a UART capture on the falling edge (`trigger="D0=f"`). The start
bit's falling edge is the trigger, so the capture begins with D0 already low and the
high→low start-bit edge is never written to the VCD. The decoder then latches onto the
next falling edge (a data-bit edge) as a false start bit and misaligns — every byte
comes back as garbage with intermittent `Frame error`. Capture **without** a trigger
instead, so the idle-high period precedes the data and the start-bit edge is recorded.

### Empty or garbage decode

- Empty annotations usually mean there was no signal on the mapped channel (floating
  pin), or `options["rx"]` points at the wrong channel name.
- Garbage with `Frame error` usually means a baud mismatch. Measure the actual line
  baud instead of guessing:
  ```sh
  sigrok-cli -i capture.vcd -P guess_bitrate:data=D0
  ```

### Device busy

If a capture fails with `Unable to claim USB interface`, another program (typically
PulseView) holds the device. Close it and retry.

## Limitations

- Bounded capture only — there is no continuous streaming. That capability is the
  subject of the capture/streaming design (`madeinoz67/benchweave#43`).
- Single-device assumption: the driver is hard-coded to `fx2lafw`; other sigrok
  devices are not targeted.
- 8 channels, 20 kHz–24 MHz sample rates (device limits).
- `decode` returns text annotations only — sigrok-cli cannot save an *annotated* `.sr`,
  so decoder results are not written back into the session file. A human who wants
  decoded waveforms opens the `.sr` in PulseView and adds the decoder there.
