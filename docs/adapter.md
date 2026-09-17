# The SDK adapter

The board is driven through a [benchweave-sdk](https://pypi.org/project/benchweave-sdk/)
adapter. This repository implements **both halves of the contract**, because
no released BenchWeave gateway implements capture/streaming host services
yet:

- **Device half** — `plugins/adc_6ch_12bit/adapter.py` (`AdcAdapter`) plus
  `descriptor.json`. Owns protocol framing and command semantics only; every
  byte goes through `services.transfer`, and neither construction nor
  `open()` performs device I/O.
- **Host half** — `src/benchweave/web/host.py`: `SerialLink` (a reader
  thread draining the port into a bounded ring so 2 Mbps never backs up),
  `SerialHostServices` (transfer, clocks, evidence, capture artifacts), and
  `AdcOperationContext` (identity, deadline, cancellation). Structurally
  compatible with `benchweave_sdk.interfaces.HostServices`/`CaptureServices`
  (Protocols; never imported at runtime).

`benchweave.web.board.BoardManager` is the product-side facade: it runs the
adapter on a dedicated event loop and exposes the synchronous API the web
app and MCP server call.

## The descriptor

`descriptor.json` declares the adapter entry point
(`adc_6ch_12bit.adapter:create_plugin`), the serial transport settings
(2 Mbps, 8N1), the `identify`/`invoke`/`reset` operations with their
timeouts (2 s / 30 s / 2 s), the six channels `a0`–`a7`, the
`otdp.daq/1.0.0` actions, and SHA-256-pinned contract schemas. The manager
reads its operation timeouts from here rather than hard-coding them.

## Operation mapping

| Product action (BoardManager) | OTDP operation | Wire frames |
|---|---|---|
| `connect` | `open` (no I/O), `identify`, then a configure | `IDENTIFY` → `IDENTIFY_RSP`; `SET_AVERAGING`, `SET_CHANNELS` → `ACK` |
| `set_averaging` / `set_channels` | `invoke otdp.daq.configure/1.0.0` | `SET_AVERAGING` → `ACK`, `SET_CHANNELS` → `ACK` |
| `sample_once` | configure (`sample_count: 1`, software trigger), `arm`, `trigger`, one `next_event`, `abort` | `SET_AVERAGING`, `SET_CHANNELS` → `ACK`; `SAMPLE_ONCE` → `ACK`, one `SAMPLE`; `STOP_STREAM` → `ACK` |
| `capture_samples` / `capture_seconds` | `arm` (immediate trigger), `next_event` loop, `abort` | `START_STREAM` → `ACK`; `SAMPLE`…; `STOP_STREAM` → `ACK` |
| `start_stream` | `arm` (immediate, 24 h budget), then a long-running `next_event` pump | `START_STREAM` → `ACK`; `SAMPLE`… |
| `pause_stream` / `resume_stream` | `abort` / fresh `arm` | `STOP_STREAM` → `ACK` / `START_STREAM` → `ACK` |
| `stop_stream` | `abort` | `STOP_STREAM` → `ACK` |
| `disconnect` | `close` | none (closes the transport) |
| — (SDK hosts) | `reset`; `invoke otdp.daq.fetch/1.0.0` | `RESET` → `ACK`; fetch drains buffered samples into an `otdp-measurement` waveform dataset, bounded by `max_bytes`/`allow_partial` |

Two mappings deserve a note:

- **Averaging rides on `sample_rate_hz`.** The `otdp.daq.configure` input
  schema is closed — there is no raw averaging knob — so the manager
  requests `sample_rate_hz = estimate_max_sps(averaging, n_channels)` and
  the adapter's nearest-averaging search lands on exactly that averaging.
  The same table drives both sides, so the round trip is exact by
  construction; the achieved rate is reported back in
  `effective_configuration.sample_rate_hz`.
- **Honest dispatch states.** Every wire-writing operation marks dispatch
  immediately before its first transmit; a timeout or transport fault after
  that reports `status: "unknown"` with `dispatch_state: "unknown"` rather
  than claiming a clean error.

## The `x-adc-sample` event extension

`next_event` returns telemetry events whose standard `reading` block carries
only the first active channel (in `count` units). The schema-sanctioned
extension key **`x-adc-sample`** carries the full frame —
`{counter, channels[6], averaged_n}` — so the host never reassembles six
per-channel events per sample. `BoardManager` rebuilds its `Sample` objects
from this key.

## Raw counts at the boundary

The adapter emits **raw 12-bit ADC counts** (`unit: "count"`, calibration
`not_applied`). Gain, offset, names/units, and computed channels are
product-side concerns applied by the measurement profiles in the runtime
config — the upstream contracts have no home for them yet; see
[feature-request-measurement-profiles.md](feature-request-measurement-profiles.md)
for the proposal to change that.

## Conformance tests

```sh
uv run --no-sync pytest tests/adc/test_adapter_conformance.py
```

The suite uses the SDK's `check_lifecycle`/`check_operation` plus a
`MockHost` scripted with **exact frame bytes** (it is not a device
simulator), and validates `descriptor.json`, the telemetry events, and the
fetch datasets against the pinned OTDP schemas. The descriptor's
`provenance.test_vectors` names this file as the adapter's test vector set.
