"""MCP server exposing the ADC board for AI configuration and data capture.

This is a pragmatic trial: a small ``adc_*``/short tool surface wrapping the
shared :class:`~benchweave.web.board.BoardManager` backend, so the same capture
logic serves both the web UI and MCP clients. MCP-initiated captures are tagged
``MCP`` in their CSV filename so they can carry a distinct retention policy.

It also exposes the :class:`~benchweave.web.library.CaptureLibrary` so an AI
client can list saved captures, load one as a compact per-channel summary, and
pull a single channel's full trace to answer questions about a run.
"""

from __future__ import annotations

import math
from typing import Any, cast

from mcp.server.mcpserver import MCPServer

from benchweave.web.board import BoardManager
from benchweave.web.library import CaptureLibrary

manager = BoardManager()
library = CaptureLibrary()
mcp = MCPServer("benchweave-adc")


@mcp.tool()
def list_boards() -> list[dict[str, object]]:
    """List ADC boards discovered on attached serial ports."""
    return manager.discover()


@mcp.tool()
def connect(device: str) -> dict[str, object]:
    """Connect to an ADC board by its serial device path (e.g. /dev/ttyACM0)."""
    return manager.connect(device)


@mcp.tool()
def disconnect() -> dict[str, object]:
    """Disconnect from the currently connected ADC board."""
    manager.disconnect()
    return manager.status()


@mcp.tool()
def status() -> dict[str, object]:
    """Return the current board status (connection, firmware, config)."""
    return manager.status()


@mcp.tool()
def get_config() -> dict[str, Any]:
    """Return the full runtime configuration."""
    return manager.get_config()


@mcp.tool()
def set_config(config: dict[str, Any]) -> dict[str, Any]:
    """Replace the runtime configuration with the given object."""
    return manager.set_config(config)


@mcp.tool()
def set_averaging(n: int) -> dict[str, object]:
    """Set the per-channel averaging depth (must be a supported choice)."""
    return manager.set_averaging(n)


@mcp.tool()
def set_channels(mask: int) -> dict[str, object]:
    """Set the enabled channel bitmask (0..63)."""
    return manager.set_channels(mask)


@mcp.tool()
def sample_once() -> dict[str, object]:
    """Read a single sample and return converted channel values."""
    return manager.sample_once()


@mcp.tool()
def capture_samples(count: int) -> dict[str, object]:
    """Capture exactly ``count`` samples to CSV (tagged MCP) and return a summary."""
    return manager.capture_samples(count, tag="MCP")


@mcp.tool()
def capture_seconds(seconds: float) -> dict[str, object]:
    """Capture samples for ``seconds`` seconds to CSV (tagged MCP) and return a summary."""
    return manager.capture_seconds(seconds, tag="MCP")


def _series_stats(s: dict[str, Any]) -> dict[str, object]:
    """Summarise one parsed series as per-channel statistics over its samples."""
    points = cast(list[list[float | None]], s.get("points") or [])
    vals = [v for _, v in points if v is not None]
    if not vals:
        return {"name": str(s.get("name", "")), "unit": str(s.get("unit") or ""), "count": 0}
    n = len(vals)
    mean = sum(vals) / n
    rms = math.sqrt(sum(v * v for v in vals) / n)
    variance = sum((v - mean) ** 2 for v in vals) / n
    return {
        "name": str(s.get("name", "")),
        "unit": str(s.get("unit") or ""),
        "count": n,
        "min": min(vals),
        "max": max(vals),
        "mean": mean,
        "rms": rms,
        "stddev": math.sqrt(variance),
        "first": vals[0],
        "last": vals[-1],
    }


@mcp.tool()
def list_captures(limit: int = 20) -> list[dict[str, object]]:
    """List saved captures, newest first, grouped by stem with the file kinds on disk."""
    kinds: dict[str, set[str]] = {}
    sizes: dict[str, int] = {}
    info: dict[str, dict[str, object]] = {}
    order: list[str] = []
    for r in library.scan():
        stem = str(r["stem"])
        if stem not in info:
            info[stem] = {
                "stem": stem,
                "serial": r.get("serial"),
                "tag": r.get("tag"),
                "captured_at": r.get("captured_at"),
                "project": r.get("project"),
            }
            kinds[stem] = set()
            sizes[stem] = 0
            order.append(stem)
        kinds[stem].add(str(r.get("kind")))
        sizes[stem] += cast(int, r.get("size_bytes"))
    rows = [
        {**info[stem], "size_bytes": sizes[stem], "kinds": sorted(kinds[stem])} for stem in order
    ]
    return rows[:limit] if limit > 0 else rows


@mcp.tool()
def load_capture(stem: str) -> dict[str, object]:
    """Load a capture by its stem and return an analysis summary: metadata,
    per-channel statistics, letter markers, power analysis, and assertion results."""
    csv = library.file_for(stem, "csv")
    if csv is None:
        raise ValueError(f"no CSV for capture '{stem}'")
    data = library.parse_csv(csv)
    series = cast(list[dict[str, Any]], data.get("series") or [])
    assertion_result: dict[str, object]
    if library.get_assertions():
        assertion_result = library.check_assertions(stem)
    else:
        assertion_result = {"results": [], "passed": 0, "checks": 0}
    return {
        "stem": stem,
        "name": data.get("name"),
        "meta": data.get("meta"),
        "sample_count": data.get("sample_count"),
        "duration_s": data.get("duration_s"),
        "series": [_series_stats(s) for s in series],
        "annotations": library.get_annotations(stem),
        "power": library.get_power(stem),
        "assertions": assertion_result,
    }


@mcp.tool()
def capture_series(stem: str, name: str, max_points: int = 2000) -> dict[str, object]:
    """Return one channel's full time series from a capture, matched by channel name.

    ``max_points`` evenly decimates a long trace (0 = return every point)."""
    csv = library.file_for(stem, "csv")
    if csv is None:
        raise ValueError(f"no CSV for capture '{stem}'")
    data = library.parse_csv(csv)
    series = cast(list[dict[str, Any]], data.get("series") or [])
    for s in series:
        if str(s.get("name", "")) != name:
            continue
        points = cast(list[list[float | None]], s.get("points") or [])
        if max_points > 0 and len(points) > max_points:
            step = len(points) / max_points
            points = [points[round(i * step)] for i in range(max_points)]
        return {
            "stem": stem,
            "name": name,
            "unit": str(s.get("unit") or ""),
            "count": len(points),
            "points": points,
        }
    raise ValueError(f"channel '{name}' not found in capture '{stem}'")


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
