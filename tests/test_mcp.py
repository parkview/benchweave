import asyncio
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from benchweave import mcp_server
from benchweave.web.library import CaptureLibrary

STEM = "adc_1234_test_20260914_120000"

CSV = (
    "# note: power on\n"
    "# sample_rate_hz: 100.0\n"
    "# A0: Voltage (V)\n"
    "# A1: Current (A)\n"
    "timestamp,elapsed_s,actual_sps,counter,averaged_n,Voltage,Current\n"
    "2026-09-14T12:00:00.000000,0.0,0.0,0,0,1.0,0.5\n"
    "2026-09-14T12:00:01.000000,1.0,1.0,1,0,3.3,0.5\n"
    "2026-09-14T12:00:02.000000,2.0,1.0,2,0,3.3,0.5\n"
)


@pytest.fixture
def capture_library(tmp_path: Path) -> CaptureLibrary:
    (tmp_path / f"{STEM}.csv").write_text(CSV)
    (tmp_path / f"{STEM}.png").write_bytes(b"x")
    return CaptureLibrary(captures_dir=tmp_path, db_path=tmp_path / "library.db")


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
        "list_captures",
        "load_capture",
        "capture_series",
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


def test_list_captures_groups_by_stem(capture_library: CaptureLibrary) -> None:
    with mock.patch.object(mcp_server, "library", capture_library):
        rows: Any = mcp_server.list_captures()
    assert [r["stem"] for r in rows] == [STEM]
    assert rows[0]["kinds"] == ["csv", "png"]
    assert rows[0]["size_bytes"] > 0


def test_load_capture_returns_summary(capture_library: CaptureLibrary) -> None:
    with mock.patch.object(mcp_server, "library", capture_library):
        summary: Any = mcp_server.load_capture(STEM)
    assert summary["name"] == f"{STEM}.csv"
    assert summary["sample_count"] == 3
    assert summary["duration_s"] == 2.0
    assert summary["meta"]["note"] == "power on"
    assert [s["name"] for s in summary["series"]] == ["Voltage", "Current"]
    v = summary["series"][0]
    assert v["unit"] == "V"
    assert v["count"] == 3
    assert v["min"] == 1.0
    assert v["max"] == 3.3
    assert v["first"] == 1.0
    assert v["last"] == 3.3


def test_load_capture_missing_stem_raises(capture_library: CaptureLibrary) -> None:
    with (
        mock.patch.object(mcp_server, "library", capture_library),
        pytest.raises(ValueError, match="no CSV"),
    ):
        mcp_server.load_capture("adc_nope_20260914_120000")


def test_capture_series_returns_points(capture_library: CaptureLibrary) -> None:
    with mock.patch.object(mcp_server, "library", capture_library):
        out: Any = mcp_server.capture_series(STEM, "Current")
    assert out["unit"] == "A"
    assert out["count"] == 3
    assert out["points"] == [[0.0, 0.5], [1.0, 0.5], [2.0, 0.5]]


def test_capture_series_decimates(capture_library: CaptureLibrary) -> None:
    with mock.patch.object(mcp_server, "library", capture_library):
        out: Any = mcp_server.capture_series(STEM, "Voltage", max_points=2)
    assert out["count"] == 2
    assert [p[0] for p in out["points"]] == [0.0, 2.0]


def test_capture_series_missing_channel_raises(capture_library: CaptureLibrary) -> None:
    with (
        mock.patch.object(mcp_server, "library", capture_library),
        pytest.raises(ValueError, match="not found"),
    ):
        mcp_server.capture_series(STEM, "Nope")
