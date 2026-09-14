"""MCP server exposing the ADC board for AI configuration and data capture.

This is a pragmatic trial: a small ``adc_*``/short tool surface wrapping the
shared :class:`~benchweave.web.board.BoardManager` backend, so the same capture
logic serves both the web UI and MCP clients. MCP-initiated captures are tagged
``MCP`` in their CSV filename so they can carry a distinct retention policy.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from benchweave.web.board import BoardManager

manager = BoardManager()
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


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
