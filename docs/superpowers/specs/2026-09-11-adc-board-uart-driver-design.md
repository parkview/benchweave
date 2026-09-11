# ADC Board UART Driver — Design

**Date:** 2026-09-11
**Status:** Approved for implementation
**Author:** Stephen Eaton (with Claude)

## 1. Context and goal

BenchWeave is a Python test-bench gateway. This design adds the first real hardware-facing
piece of software: a Python **master** driver for the 6-channel ADC board, plus the wire
protocol that both the Python driver and the board firmware implement.

The board is the user's CH32V006E8R-based ADC PCB, connected to the laptop over USB via a
CH343G USB-UART bridge. The current firmware (`firmware/ch32v006e8r_adc/`) is test code: it
prints 4 channel voltages as ASCII at 115200 baud, has **no receive path**, and hard-codes
its averaging. This task is **Python-first**: build the driver and protocol now; update the
firmware to match later.

## 2. Device facts (evidence)

| Fact | Value |
|---|---|
| MCU | WCH CH32V006E8R (QingKe V2C RISC-V, `rv32ec`), 48 MHz HSI×2, 8 KB RAM |
| USB-UART bridge | CH343G (supports ≥ 2 Mbps) |
| UART | USART1, TX = PD5, RX = PD6, 8N1, no flow control |
| ADC | internal, currently 10-bit (code uses 1024) → **target 12-bit (4096)** |
| Channels | 6 analogue inputs, canonical order below |
| Trigger | external trigger provisioned on PCB, **not implemented** in firmware |
| Firmware location | `firmware/ch32v006e8r_adc/` (moved out of `src/`) |

### Canonical channel order

Fixed order used in the data frame and the channel mask. Counts are **raw ADC counts**;
scaling/calibration stays in Python.

| Index | Pin | ADC ch | Meaning |
|---|---|---|---|
| 0 | PA2 | 0 (A0) | EN / DC-DC regulator input |
| 1 | PA6 | 1 (A1) | DC-DC output (firmware currently applies ×2) |
| 2 | PC4 | 2 (A2) | unused (reserved) |
| 3 | PD2 | 3 (A3) | R271 cap-current sense L |
| 4 | PD3 | 4 (A4) | R272 cap-current sense R |
| 5 | PD4 | 7 (A7) | unused (reserved) |

## 3. Scope

**In scope (this pass):**

- Wire protocol specification (Section 4) — the contract both sides implement.
- Python driver: `src/benchweave/adc/` (`protocol.py`, `driver.py`, `__init__.py`).
- First runtime dependency: `pyserial>=3.5`.
- Unit tests for the protocol and an in-memory loopback test for the driver lifecycle.

**Out of scope (later, now specified):**

- Firmware changes: 1024→4096 resolution, USART RX path, 2 Mbps, averaging/stream commands,
  cycle/timer sampling, external trigger. These are fully specified by Section 4.

## 4. Wire protocol

All traffic — control, responses, and data — is one binary frame type. Transport is
**8N1, no flow control, 2,000,000 baud** (~200 KB/s). All multi-byte integers are
**little-endian** (matches the RISC-V MCU).

### 4.1 Frame layout

```
offset  0     1     2     3     4     5       5+LEN      7+LEN
      +-----+-----+-----+-----+-----+--------+-----+-----+
      | 0xAA| 0x55| TYPE| SEQ | LEN | PAYLOAD| CRC16 (LE)|
      +-----+-----+-----+-----+-----+--------+-----+-----+
```

- **SYNC** = `0xAA 0x55` (2 bytes).
- **TYPE** = frame type (Section 4.2).
- **SEQ** = 1-byte sequence, increments per frame sent, independent per direction.
- **LEN** = payload length, 0–255.
- **PAYLOAD** = `LEN` bytes, structure per TYPE.
- **CRC16** = CRC-16/CCITT-FALSE: polynomial `0x1021`, init `0xFFFF`, no reflection, no
  final XOR, little-endian, computed over `TYPE..PAYLOAD` (sync excluded). Check value:
  CRC of ASCII `"123456789"` = `0x29B1`.

Maximum frame = 262 bytes (5 header + 255 payload + 2 CRC).

### 4.2 Frame types

| TYPE | Dir | Name | Payload |
|---|---|---|---|
| `0x01` | H→D | `SET_AVERAGING` | `u16` count ∈ {0,4,8,16,32,64,128,256}; 0 = raw |
| `0x02` | H→D | `SET_CHANNELS` | `u8` bitmask, bit *i* = channel *i* |
| `0x03` | H→D | `SET_SAMPLE_MODE` | `u8` — 0 = free-run, 1 = cycle/timer (reserved) |
| `0x04` | H→D | `START_STREAM` | empty |
| `0x05` | H→D | `STOP_STREAM` | empty |
| `0x06` | H→D | `SAMPLE_ONCE` | empty |
| `0x07` | H→D | `RESET` | empty |
| `0x08` | H→D | `IDENTIFY` | empty |
| `0x09` | H→D | `ARM_TRIGGER` | *(reserved; external trigger not implemented)* |
| `0x81` | D→H | `ACK` | `u8 echo_type`, `u16 value` (applied setting; 0 otherwise) |
| `0x82` | D→H | `NAK` | `u8 echo_type`, `u8 error` |
| `0x83` | D→H | `IDENTIFY` | `u8 proto_ver, u8 fw_major, u8 fw_minor, u8 n_channels=6, u8 resolution=12` |
| `0x90` | D→H | `SAMPLE` | 16-byte payload, see 4.3 |

Error codes: `0x01` bad command, `0x02` bad parameter, `0x03` busy, `0x04` unsupported.

### 4.3 Data frame (`SAMPLE`, `0x90`)

Payload = 16 bytes:

```
counter (u32 LE) | ch0 (u16) | ch1 | ch2 | ch3 | ch4 | ch5 (u16 each)
```

- **counter** = monotonically increasing sample number; authoritative for gap/drop
  detection (the 1-byte header SEQ wraps too fast).
- **ch0…ch5** = raw 12-bit counts (0–4095) in canonical channel order (Section 2), one
  `u16` each. 16-bit containers are used instead of 9-byte bit-packing for simplicity; the
  3-byte saving is not worth the complexity at this throughput.

## 5. Python driver

### 5.1 Module layout

```
src/benchweave/adc/
├── __init__.py      # public API re-exports
├── protocol.py      # pure frame encode/decode + CRC, no I/O
└── driver.py        # serial I/O + state machine
```

### 5.2 State machine

```
Disconnected ──open()──▶ Connected ──identify()/configure()──▶ Configured
     ▲                        │                                      │
     │◀──────close()──────────┼──start_stream()──▶ Streaming ──stop_stream()──▶ Configured
                              │                      │
                              └──sample_once()───────┤
```

Every state has a defined reaction to errors and timeouts. Desync in `Streaming` triggers
a **resync** (scan for the next `0xAA 0x55`, drop bad bytes, count the error) rather than
an unknown state; the gap is observable via the `u32` counter.

### 5.3 Public API — `AdcDriver`

- `open(port, baud=2_000_000, timeout=...)` / `close()` — context-manager support.
- `identify() -> IdentifyInfo`, `reset()`.
- `set_averaging(n)`, `set_channels(mask)`, `set_sample_mode(mode)` — validate inputs
  **before** any I/O; expect `ACK` with readback value.
- `start_stream()`, `stop_stream()`, `sample_once() -> Sample`.
- `iter_samples()` → iterator of `Sample` from a thread-safe queue.
  `Sample = (counter: int, channels: tuple[int, ...], averaged_n: int)`. `averaged_n`
  is driver-tracked from the last applied `SET_AVERAGING` ACK (it is not on the wire).
- Exceptions: `AdcConnectionError`, `AdcProtocolError`, `AdcTimeout`, `AdcNotConnected`.

### 5.4 Concurrency

- One background **reader thread** owns RX: parses frames, routes `ACK`/`NAK`/`IDENTIFY`
  to the command waiting on them, and enqueues `SAMPLE` frames.
- Writes are serialized under a lock.
- Commands are request/response with a timeout and are **never auto-retried** (BenchWeave
  no-replay rule); the caller decides after a timeout.

## 6. Testing

- `protocol.py` unit tests: encode/decode round-trips, CRC check vector (`0x29B1`), 12-bit
  values, malformed/truncated/oversized frames, resync.
- An **in-memory loopback fake device** that scripted-responds to commands and emits
  `SAMPLE` frames, driving the full driver lifecycle against exact bytes. This doubles as
  the later cross-check target for the firmware.

## 7. Decisions log

| Decision | Choice | Rationale |
|---|---|---|
| Protocol | Unified binary, single frame type | One parser; firmware-friendly; exploits 2 Mbps |
| Payload | Raw 12-bit counts | Scaling in Python → recalibrate without reflashing |
| Sample encoding | `u16` per channel | Simpler than 9-byte packing; 3 bytes/frame not worth it |
| CRC | CRC-16/CCITT-FALSE | Cheap in C, standard, has a published check vector |
| Gap detection | `u32` counter in `SAMPLE` | 1-byte header SEQ wraps too fast for streaming |
| Concurrency | Background reader thread + queue | Sustained throughput without dropping frames |

## 8. Non-goals

- No firmware changes in this pass.
- No BenchWeave OTDP descriptor/plugin/adapter for the ADC board yet (the gateway host does
  not exist). The driver is a plain importable module; wiring it into a device plugin is a
  later, separate task.
- No external-trigger or cycle-sampling implementation (protocol reserves the fields).
