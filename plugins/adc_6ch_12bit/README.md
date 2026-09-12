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
`captures/adc_<serial>_<timestamp>.csv`.

### CSV capture

```sh
uv run scripts/adc_capture.py --seconds 5 --averaging 16
# -> captures/adc_<serial>_<timestamp>.csv
```

## Firmware

`firmware/ch32v006e8r_adc/` — the CH32V006 implementation of the same protocol.
Build from the CLI with `make` (uses the MRS-bundled `riscv-wch-elf-gcc` toolchain).

## Deferred

- 6× WS2812 status LEDs (one per channel, data line PB1; green = active @15%,
  red = inactive).
- Timer-triggered ("cycle") sampling and external trigger.
