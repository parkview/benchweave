# Benchweave — TODO

Open items, newest first. Dates are when the item was logged.

## Analyse page — further analysis & measurement features

Logged 2026-09-14 · **partially done** — region inspector (stats + power) shipped; the rest are candidates.

Candidate features for the Analyse tab, building on the existing CSV plot and
brush-region Ah/Wh readout. Roughly in value-per-effort order; pick individually.

- **Region inspector** — ✅ **done 2026-09-14**: brushing a region now reports per-channel
  min / mean / max / RMS / peak-to-peak, plus average & peak power (W) of the V×I pair.
  **Deferred:** dual time cursors (Δt, ΔV, ΔI between two clicks) — a separate
  click-to-place interaction, not the drag-brush.
- **Edge / timing measurements** — rise/fall time (10–90%) and settling time on a
  chosen channel; threshold-crossing finder ("at what elapsed time did EN-Pin cross
  1.8 V?"); inrush detection (peak current, time-to-peak, transient energy).
- **Signal analysis** — frequency / period / duty cycle on a periodic channel
  (zero-crossing or FFT).
- **Compare & regression** — overlay two captures (aligned or time-offset); diff a
  run against a saved "golden" capture and flag channels drifting outside a
  tolerance band.
- **Data integrity** — CSV health check (duplicate columns, elapsed-time gaps,
  non-monotonic timestamps, SPS anomalies); measured-vs-configured sample rate.
- **Power / battery** — full-trace Ah/Wh totals (not just brushed regions);
  sleep/wake profiling and battery-life estimate; internal resistance from a load
  step (ΔV/ΔI).
- **Reporting & automation** — self-contained HTML report export; per-channel
  min/max assertion checks (mini pass/fail); an MCP tool to load a capture and
  answer questions about it.

**Low priority (keep in mind for later):**

- **FFT spectrum view** — find switching noise / ringing / unexpected oscillation.
  Most captures run at low SPS (see the sample-rate table in
  `plugins/adc_6ch_12bit/README.md`), so spectral bandwidth is limited; revisit if
  higher-rate captures become common.

**Note:** CSVs store *converted* engineering values, not raw 12-bit counts, so
LSB-level analysis (missing codes, noise floor in counts) needs a "record raw
counts" option before it becomes possible.

## Mechanical travel kit (v1.0)

Logged 2026-09-14 · **not started**.

Investigate how the bench components can be transported and set up on a table as
a compact kit, and how each board should be housed/protected.

**Components to enclose:**
- ADC PCB (currently bare on the table) — add protection, e.g. a 2 mm clear-acrylic
  lid; same approach for other boards as they're integrated.
- 7-port USB hub (separate self-designed project) — dedicate a couple of ports to
  switchable power so an MCP service can power-cycle/reset anything plugged in.
- MadeInOz portable USB-PD bench power supply (in development) — needs housing.
- CH32V Link-e programmer.
- Logic analyser.
- USB camera (EMeet C960).

Treat this as v1.0 of the travel kit.

**Scope split:** the switchable-power ports pair with an MCP per-port power tool
(software, this codebase — reuse `src/benchweave/mcp_server.py`); the enclosure
itself is mechanical/CAD, tracked outside this repo.

## Contribute ADC plugin upstream

Logged 2026-09-14 · **deferred** — user returns to this **Friday 2026-09-18**.

PR the ADC plugin (and optionally the web gateway + firmware) back to the parent
repo `madeinoz67/benchweave`. The plugin almost certainly needs reworking to fit
the upstream plugin SDK before it can be accepted.

- Draft feature request is committed at `docs/feature-request-measurement-profiles.md`.
- Branch off `upstream/main` — never fork `main`, which carries ~46 commits of
  divergence.
- First step: open an issue to confirm shape and scope before doing SDK-adaptation work.

## Webcam MCP service

Logged 2026-09-14 · **not started**.

Add an MCP service for a USB webcam, mirroring the existing `benchweave-adc`
server (`src/benchweave/mcp_server.py`). Lets Claude Code capture a photo and
analyse it — e.g. confirm a connected ADC board is switched on, or whether its
LED is blinking.

- **Hardware:** EMeet C960 4K UHD autofocus webcam (dual mic) — purchased
  2026-09-14, arrives later that week; CLI-controllable. Expect a UVC device at
  `/dev/video*` (control via `v4l2-ctl`, capture via `ffmpeg`/`fswebcam`).
- Thin `@mcp.tool()` surface over a webcam driver, own `benchweave-*` entry point
  in `pyproject.toml`.
- Return a single downscaled JPEG (not a raw frame) to stay well under the stdio
  transport size limit.

## Fix duplicate channel names in the ADC config

Logged 2026-09-14 · **not started**.

`plugins/adc_6ch_12bit/config.json` (both `default` and `test` profiles) names a
raw channel and a computed channel the same, producing duplicate CSV column names:

- raw channel `A2` → `"5V"` collides with computed `"5V"` (`A2*2`)
- raw channel `A3` → `"EN-Pin"` collides with computed `"EN-Pin"` (`A3*2`)

Rename one side (e.g. the raw channel to `5V-div` / `EN-div`, or the computed one
to `5V-rail` / `EN-Pin-rail`).
