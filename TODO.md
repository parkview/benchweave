# Benchweave — TODO

Open items, newest first. Dates are when the item was logged.

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
