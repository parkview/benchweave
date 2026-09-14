# Benchweave — TODO

Open items, newest first. Dates are when the item was logged.

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
