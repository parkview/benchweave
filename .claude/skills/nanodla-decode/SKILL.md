---
name: nanodla-decode
description: Use when capturing, decoding, or annotating a logic-analyser trace with the nanoDLA via the benchweave-nanodla MCP server — sigrok, fx2lafw, UART/I2C/SPI/CAN protocol decode.
---

# nanoDLA capture → decode → annotate

## Purpose

Drive the nanoDLA FX2LP logic analyser through the `benchweave-nanodla` MCP
tools, and record what you find in the capture manifest so a human can read it
later without re-running anything.

## Workflow — always all three steps

1. `capture` — writes one stem's `.vcd` (for the AI), `.sr` (for a human, open in
   PulseView) and a `.json` manifest.
2. `decode` with the right decoder — its output is auto-appended to the
   manifest's `decodes` list.
3. `annotate` a short prose note on what you found — **always**, even if the line
   is idle or the decode is empty. It lands in the manifest's `notes` list.

Never leave a capture without a note. The manifest is the human-facing record.

## Decode quick reference

- **UART** — set `baudrate` and `format=ascii`; sample at ≥ 4× baud. The start bit
  is a falling edge on an idle-high line.
- **I2C** — START = SDA fall while SCL high; 7-bit address + R/W; ACK per byte;
  STOP = SDA rise while SCL high. A NACK at the address usually means wrong
  address or the device held in reset.
- **SPI** — check mode (CPOL/CPHA), bit order, CS polarity and word size.

## Common problems

| Symptom | Cause | Fix |
|---|---|---|
| Garbage decode | Sample rate too low | ≥ 4× bus speed |
| UART `Frame error` / misaligned | Wrong baud, or captured on the start-bit edge | Measure with `guess_bitrate`; capture **without** a falling-edge trigger |
| Empty decode | Floating pin, or wrong channel | Check the `rx`/`data` channel name |
| Capture fails `Unable to claim USB interface` | PulseView holds the device | Close PulseView and retry |

## Attribution

Decode quick reference and common-problems table adapted from
`mohitmishra786/low-level-dev-skills` (`protocol-analysis`), MIT.

Full details and gotchas: `docs/nanodla-mcp-server.md`.
