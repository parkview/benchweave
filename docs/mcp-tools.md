# MCP tools

The MCP server (`src/benchweave/mcp_server.py`, stdio transport) exposes the
same `BoardManager` and `CaptureLibrary` the web UI uses, so an AI client can
configure the board, run bounded captures, and analyse saved captures. MCP
clients launch it as `uv run benchweave-adc-mcp` (see `.mcp.json`). Captures
made over MCP are tagged `MCP` in their CSV filename so they can carry a
distinct retention policy.

## Board control

| Tool | Arguments | Returns |
|---|---|---|
| `list_boards` | — | Discovered boards: device path, serial, firmware, channels, resolution |
| `connect` | `device` | Board status after open + identify + channel restore |
| `disconnect` | — | Board status (disconnected) |
| `status` | — | Connection, firmware, averaging, mask, stream/record flags, est. max SPS |
| `get_config` | — | Runtime config: profiles (name/unit/gain/offset/show/colour, computed) + settings |
| `set_config` | `config` | The stored config; rejects configs that would break recording/conversion |
| `set_averaging` | `n` | Status; `n` must be one of 0, 4, 8, 16, 32, 64, 128, 256 (board must be idle) |
| `set_channels` | `mask` | Status; mask 1..63; bits 0-4 = A0-A4, bit 5 = A7, persisted (board must be idle) |

## Capture

| Tool | Arguments | Returns |
|---|---|---|
| `sample_once` | — | One sample's converted channel values (single-shot acquisition) |
| `capture_samples` | `count` | Summary: CSV path, count, achieved rate, per-channel min/mean/max. Fails if `count` is not reached within 10 s |
| `capture_seconds` | `seconds` | Same summary; blocks for the duration |

Capture summaries are deliberately compact — the full record lives in the
CSV under `plugins/adc_6ch_12bit/captures/`.

## Capture library

| Tool | Arguments | Returns |
|---|---|---|
| `list_captures` | `limit` (default 20; ≤ 0 = all) | Captures newest first, grouped by stem, with file kinds and total size |
| `load_capture` | `stem` | Analysis summary: metadata, per-channel statistics (min/max/mean/RMS/stddev/first/last), letter markers, power setup, assertion results |
| `capture_series` | `stem`, `name`, `max_points` (default 2000; 0 = all) | One channel's `[elapsed_s, value]` trace, matched by channel name and evenly decimated to at most `max_points` |

Bounds worth knowing:

- `capture_series` decimates by default — pass `max_points: 0` for every
  recorded point, at the cost of a large response.
- `load_capture` never returns raw points; use `capture_series` for those.
- Unknown stems and channel names raise errors rather than returning empty
  results.

The library database is shared with the web app (WAL mode, 5 s busy
timeout), so both processes can run at once; see
[library-schema.md](library-schema.md).
