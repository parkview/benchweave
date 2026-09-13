# ADC 6-channel 12-bit board

Python master driver for the BenchWeave 6-channel, 12-bit ADC board.

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

## Usage

```python
from plugins.adc_6ch_12bit import AdcDriver, discover_adc_boards

boards = discover_adc_boards()      # probe serial ports with IDENTIFY
driver = AdcDriver()
driver.open(boards[0].device)       # 2 Mbps
info = driver.identify()            # proto, firmware, channels, resolution
driver.set_averaging(16)
driver.start_stream()
for sample in driver.iter_samples():
    print(sample.counter, sample.channels)
driver.stop_stream()
driver.close()
```

`discover_adc_boards()` enumerates WCH USB-UART ports and probes each with
`IDENTIFY`, accepting only ports that reply with the ADC signature — so the
`/dev/ttyACM*` node can move between reboots without breaking the code.

### Web frontend

```sh
./scripts/run_adc_web.sh           # -> http://localhost:8000
```

Discover/connect, configure averaging + channels, start/stop streaming, and a
live 6-channel graph (SSE). Optionally records to
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

## For AI agents

Context for a fresh AI session continuing work on this module. Read this, then
the linked spec, before changing anything.

**What this is:** a Python master driver for a 6-channel, 12-bit ADC board
(WCH CH32V006E8R + CH343G USB-UART) speaking a custom binary protocol over UART
at 2 Mbps. Host is the master: it sends commands, the board replies or streams.

**Layout:**
- `plugins/adc_6ch_12bit/` — this plugin (`protocol.py` codec, `driver.py`,
  `discovery.py`).
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
sampling and external trigger.
