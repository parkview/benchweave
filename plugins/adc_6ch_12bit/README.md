# ADC 6-channel 12-bit board

BenchWeave plugin for the 6-channel, 12-bit ADC board: an OTDP descriptor and
async SDK adapter over a custom binary UART protocol.

## Hardware

| | |
|---|---|
| MCU | WCH CH32V006E8R (RISC-V, 48 MHz) |
| USB-UART | CH343G (up to 6 Mbps) |
| ADC | 6 channels, 12-bit (0–4095) |
| UART | USART1, TX=PD5 / RX=PD6, 2,000,000 baud, 8N1, no flow control |

Canonical channel order (fixed by the wire protocol):

| Index | Pin | ADC ch | Meaning |
|---|---|---|---|
| 0 | PA2 | A0 | EN / DC-DC regulator input |
| 1 | PA6 | A1 | DC-DC output |
| 2 | PC4 | A2 | (reserved) |
| 3 | PD2 | A3 | R271 cap-current sense L |
| 4 | PD3 | A4 | R272 cap-current sense R |
| 5 | PD4 | A7 | (reserved) |

## Wire protocol

Binary frames at 2 Mbps:

```
[0xAA 0x55][type][seq][len][payload...][crc16 LE]
```

- CRC-16/CCITT-FALSE (poly `0x1021`, init `0xFFFF`) over `type..payload`.
- Commands (host → board): `identify`, `set_averaging`, `set_channels`,
  `set_sample_mode`, `start_stream`, `stop_stream`, `sample_once`, `reset`.
- Responses (board → host): `ACK`, `NAK`, `IDENTIFY_RSP`.
- Data (board → host): `SAMPLE` = u32 counter + 6× u16 (12-bit, little-endian).
- Averaging choices: `{0, 4, 8, 16, 32, 64, 128, 256}`.

Full spec: `docs/superpowers/specs/2026-09-11-adc-board-uart-driver-design.md`.

## Sample rate

A `SAMPLE` frame is 23 bytes, so the UART at 2 Mbps (200,000 bytes/s) tops out
at ~8,700 frames/s. In practice the ADC conversion time is the limiter: the
board does ~3,300 samples/s at averaging 0. Expected sample rates (6 channels):

| Averaging | Est. SPS |
|---|---|
| 0 (raw) | ~3,300 |
| 4 | ~1,140 |
| 8 | ~610 |
| 16 | ~320 |
| 32 | ~160 |
| 64 | ~81 |
| 128 | ~41 |
| 256 | ~20 |

Averaging trades noise against speed; a `sample_rate_hz` recording setting
(backend decimation) caps how many of those samples are recorded.

Python-side recording is not the bottleneck: the CSV writer benchmarks at
~230,000 rows/s (4.3 µs/row), so even the ~8,700 frames/s UART ceiling uses only
~4% of the write budget — the ADC (~3,300 SPS) is the limiter, not the recorder.
At very high rates (>~20K SPS) the per-sample `call_soon_threadsafe` used to feed
the SSE display would start to matter; downsample in the host-loop pump instead.

## Usage

The supported host-side surface is `benchweave.web.board.BoardManager`, which
drives the plugin's SDK adapter (`adapter.py` + `descriptor.json`) on a
dedicated event loop:

```python
from benchweave.web.board import BoardManager
from plugins.adc_6ch_12bit import discover_adc_boards

boards = discover_adc_boards()  # probe serial ports with IDENTIFY
manager = BoardManager()
manager.connect(boards[0].device)  # open + identify + restore channels
manager.set_averaging(16)
print(manager.capture_seconds(5.0))  # CSV + per-channel summary
manager.disconnect()
```

Direct adapter use (async, one session per connection) follows the
benchweave-sdk contract: `create_plugin()`, `open(descriptor, services, ctx)`,
`execute` with `identify`/`reset`/`invoke` (`otdp.daq.*` actions), and
`next_event` for streamed samples. `benchweave.web.host` provides the matching
host services over a serial port.

`discover_adc_boards()` enumerates WCH USB-UART ports and probes each with
`IDENTIFY`, accepting only ports that reply with the ADC signature — so the
`/dev/ttyACM*` node can move between reboots without breaking the code.

### Web frontend

```sh
./scripts/run_adc_web.sh           # -> http://localhost:8000
```

Discover/connect, configure averaging + channels, start/stop streaming, and a
live 6-channel graph (SSE). While streaming, **Pause** halts data collection
(the live graph freezes) but keeps the CSV open; **Resume** continues collection
into the same file, with `elapsed_s` and SPS continuous across the pause gap.
**Stop** ends the session and closes the CSV (from either a running or paused
state). Optionally records to
`plugins/adc_6ch_12bit/captures/adc_<serial>_<timestamp>.csv`.

### CSV capture

```sh
uv run scripts/adc_capture.py --seconds 5 --averaging 16
# -> plugins/adc_6ch_12bit/captures/adc_<serial>_<timestamp>.csv
```

## Firmware

`firmware/ch32v006e8r_adc/` — the CH32V006 implementation of the same protocol.
Build from the CLI with `make` (uses the MRS-bundled `riscv-wch-elf-gcc` toolchain).

## Deferred

- **ADC scan mode + DMA** — replace the per-channel polled conversion with the
  ADC scan sequencer + DMA, raising the raw rate from ~3,300 toward the ~8,700
  frames/s UART ceiling.
- **Link "show" to sampling** — stop sampling hidden channels (drive firmware
  `SET_CHANNELS` from the config `show` flags), so unticking channels speeds up
  collection. The `SAMPLE` frame stays fixed at 6× u16, so the speedup is
  bounded by the fixed transfer time.
- Timer-triggered ("cycle") sampling and external trigger (exact, jitter-free
  rate).
- **Separate graph update rate (multi-mode graphing)** — the live graph currently
  follows `sample_rate_hz` (same as recording). Explore a dedicated graph-update-rate
  setting so the graph can run faster *or* slower than the recorded rate, and a
  "live vs recorded-rate" graphing mode toggle.

## For AI agents

Context for a fresh AI session continuing work on this module. Read this, then
the linked spec, before changing anything.

**What this is:** an OTDP adapter plugin for a 6-channel, 12-bit ADC board
(WCH CH32V006E8R + CH343G USB-UART) speaking a custom binary protocol over UART
at 2 Mbps. Host is the master: it sends commands, the board replies or streams.

**Layout:**
- `plugins/adc_6ch_12bit/` — this plugin (`protocol.py` codec — including the
  parsed `Sample` vocabulary — `adapter.py` + `descriptor.json` SDK adapter,
  `discovery.py`).
- `src/benchweave/web/host.py` — host half of the SDK contract (SerialLink,
  operation contexts, transfer + capture-artifact services). `transfer` speaks
  OTDP §8.1's stream grammar: the adapter sends each command with
  `stream_send` and reads with exact-byte `stream_receive` calls (a frame's
  header, then the rest); an empty receive means the line is quiet.
- `src/benchweave/web/board.py` — BoardManager: the sync facade that runs the
  adapter on a dedicated host event loop.
- `firmware/ch32v006e8r_adc/` — matching CH32V006 firmware (C; `make`).
- `src/benchweave/web/` — FastAPI frontend (REST + SSE + live graph).
- `scripts/adc_capture.py` / `scripts/run_adc_web.sh` — CLI capture / web launcher.
- `tests/adc/`, `tests/web/` — tests.
- `docs/superpowers/specs/2026-09-11-adc-board-uart-driver-design.md` — the
  wire-protocol spec (**source of truth**).

**Protocol (summary):** `[0xAA 0x55][type][seq][len][payload][CRC16 LE]`,
CRC-16/CCITT-FALSE over type..payload. Commands: `identify`, `set_averaging`,
`set_channels`, `set_sample_mode`, `start_stream`, `stop_stream`, `sample_once`,
`reset`; responses `ACK`/`NAK`/`IDENTIFY_RSP`; data `SAMPLE` = u32 counter +
6× u16. Averaging ∈ `{0,4,8,16,32,64,128,256}`. `protocol.py` and firmware
`main.c` must stay byte-compatible.

**Conventions:**
- Wire-protocol changes go in the spec first, then both ends together.
- Python: ruff (line-length 100) + mypy strict + pytest — run
  `uv run pytest`, `uv run ruff check .`, `uv run mypy`.
- Firmware: `make -C firmware/ch32v006e8r_adc`.
- Captured data goes to `plugins/adc_6ch_12bit/captures/` (gitignored).

**Deferred work (next):** ADC scan mode + DMA (raw rate → UART ceiling); link
the config `show` flag to firmware `SET_CHANNELS` so hidden channels are not
sampled (speedup bounded by the fixed 6× u16 frame); timer-triggered "cycle"
sampling and external trigger; a separate graph-update-rate setting (the live
graph currently follows `sample_rate_hz`; see the Deferred section above).
