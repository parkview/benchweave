# benchweave-adc

Capture and analysis gateway for a custom **6-channel, 12-bit ADC board**
(WCH CH32V006E8R, 2 Mbps binary serial protocol). Forked from
[BenchWeave](https://github.com/madeinoz67/benchweave) and focused on one
job: streaming samples off the board, recording them as CSV captures, and
analysing them — live in a browser, after the fact in the Analyse tab, or
programmatically over MCP.

What's in the box:

- **Web UI** (FastAPI + SSE): live graph, channel configuration with
  measurement profiles (gain/offset/computed channels), and an **Analyse**
  tab — capture browser, brush-region statistics, power analysis with V/I
  rail pairing, zoom regions, A–Z markers with notes, and self-contained
  HTML report export.
- **MCP server** (stdio): tools to discover/configure the board, run bounded
  captures, and list/load/inspect recorded captures from an agent session.
- **Capture library**: CSV captures on disk with a SQLite overlay for
  projects, retention, annotations, and power-analysis settings.
- **Plugin** `plugins/adc_6ch_12bit/`: the binary protocol codec, serial
  driver, board discovery, and channel-conversion config.
- **Firmware** `firmware/ch32v006e8r_adc/`: the board's CH32V006 firmware
  (MounRiver toolchain Makefile).

## Quickstart

Python 3.13 and [uv](https://docs.astral.sh/uv/) are required.

```sh
uv sync --locked --dev
uv run uvicorn benchweave.web.app:app        # web UI on http://127.0.0.1:8000
```

or use the launcher (binds 127.0.0.1 by default):

```sh
scripts/run_adc_web.sh
```

Plug the board in over USB (CH343 bridge); the UI's board picker probes
candidate ports with an IDENTIFY exchange. One-off captures without the web
UI:

```sh
uv run python scripts/adc_capture.py --seconds 10 --out capture.csv
```

The MCP server is launched by MCP clients as `uv run benchweave-adc-mcp`
(see `.mcp.json`).

## Security posture

The web app has **no authentication, CORS policy, or CSRF protection** — it
is built for a single operator on localhost. The launcher binds `127.0.0.1`
by default; exposing it more widely is at your own risk.

## Documentation

| Topic | Where |
|---|---|
| Analyse tab guide | [docs/analyse-page.md](docs/analyse-page.md) |
| Hardware + wire protocol | [plugins/adc_6ch_12bit/README.md](plugins/adc_6ch_12bit/README.md) |
| Hardware compatibility | [docs/hardware.md](docs/hardware.md) |
| nanoDLA logic analyser MCP server | [docs/nanodla-mcp-server.md](docs/nanodla-mcp-server.md) |
| Firmware | [firmware/ch32v006e8r_adc/](firmware/ch32v006e8r_adc/) |
| Development & CI | [docs/development.md](docs/development.md) |
| Measurement-profiles upstream note | [docs/feature-request-measurement-profiles.md](docs/feature-request-measurement-profiles.md) |

## Relationship to BenchWeave

This project began as a fork of BenchWeave's early scaffold and inherited its
architecture-contract corpus; that machinery now lives (much evolved) in the
upstream project and has been removed here. The ADC plugin is intended to
track BenchWeave's plugin SDK ([benchweave-sdk](https://pypi.org/project/benchweave-sdk/))
as its host-side capture support matures.

## License

[MIT](LICENSE).
