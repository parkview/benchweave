import asyncio
from unittest import mock

from benchweave import mcp_server


def test_mcp_server_registers_tools() -> None:
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    assert {
        "list_boards",
        "connect",
        "disconnect",
        "status",
        "get_config",
        "set_config",
        "set_averaging",
        "set_channels",
        "sample_once",
        "capture_samples",
        "capture_seconds",
    } <= names


def test_capture_tools_tag_mcp() -> None:
    with (
        mock.patch.object(mcp_server.manager, "capture_samples", return_value={"count": 5}) as cs,
        mock.patch.object(mcp_server.manager, "capture_seconds", return_value={"count": 7}) as ce,
    ):
        assert mcp_server.capture_samples(5) == {"count": 5}
        cs.assert_called_once_with(5, tag="MCP")
        assert mcp_server.capture_seconds(1.5) == {"count": 7}
        ce.assert_called_once_with(1.5, tag="MCP")
