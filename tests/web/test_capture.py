"""Bounded captures and single-shot sampling over the adapter/host stack."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest import mock

import pytest

from benchweave.web.board import BoardManager
from plugins.adc_6ch_12bit import CHANNEL_KEYS
from plugins.adc_6ch_12bit.config import DEFAULT_CONFIG
from tests.adc.fakeboard import SampleTuple, TransportFactory, wait_until


@pytest.fixture
def stack() -> Iterator[tuple[BoardManager, TransportFactory]]:
    factory = TransportFactory()
    with (
        mock.patch("benchweave.web.board._open_transport", factory),
        mock.patch("benchweave.web.board.save_config"),
    ):
        manager = BoardManager()
        manager._config = copy.deepcopy(DEFAULT_CONFIG)
        manager.connect("/dev/ttyACM2")
        yield manager, factory
        manager.disconnect()


def _samples(n: int) -> list[SampleTuple]:
    return [(i, (i, 1, 2, 3, 4, 5)) for i in range(n)]


def test_capture_samples_returns_n_and_writes_tagged_csv(
    stack: tuple[BoardManager, TransportFactory], tmp_path: Path
) -> None:
    manager, factory = stack
    factory.last.set_samples(_samples(3))
    with mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path):
        result = manager.capture_samples(3, tag="MCP")

    path = cast(str, result["path"])
    channels = cast(list[dict[str, object]], result["channels"])
    assert result["count"] == 3
    assert cast(float, result["samples_per_second"]) > 0
    assert channels
    assert all("name" in c and "min" in c and "mean" in c and "max" in c for c in channels)
    assert "MCP" in Path(path).name

    csv_path = Path(path)
    assert csv_path.exists()
    data_rows = [line for line in csv_path.read_text().splitlines() if not line.startswith("#")]
    assert len(data_rows) == 4  # header row + 3 samples


def test_capture_samples_without_tag_omits_mcp(
    stack: tuple[BoardManager, TransportFactory], tmp_path: Path
) -> None:
    manager, factory = stack
    factory.last.set_samples(_samples(1))
    with mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path):
        result = manager.capture_samples(1)

    assert "MCP" not in Path(cast(str, result["path"])).name


def test_capture_samples_raises_when_not_enough(
    stack: tuple[BoardManager, TransportFactory], tmp_path: Path
) -> None:
    manager, factory = stack
    factory.last.set_samples(_samples(1))
    with (
        mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path),
        pytest.raises(RuntimeError, match="timed out"),
    ):
        manager.capture_samples(3, tag="MCP", timeout=0.05)


def test_capture_seconds_drains_available(
    stack: tuple[BoardManager, TransportFactory], tmp_path: Path
) -> None:
    manager, factory = stack
    factory.last.set_samples(_samples(5))
    with mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path):
        result = manager.capture_seconds(0.3, tag="MCP")

    assert result["count"] == 5
    assert len(cast(list[dict[str, object]], result["channels"])) >= 1


class _SpyRecorder:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.closed = False

    def write(self, counter: int, averaged_n: int, values: list[object]) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_capture_seconds_stops_at_deadline(
    stack: tuple[BoardManager, TransportFactory], tmp_path: Path
) -> None:
    manager, factory = stack
    factory.last.set_samples([])  # the board never produces a sample
    with (
        mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path),
        mock.patch("benchweave.web.board._Recorder", _SpyRecorder),
    ):
        result = manager.capture_seconds(0.05)

    assert result["count"] == 0  # the deadline, not a sample count, ended it


def test_capture_rejects_nonpositive(
    stack: tuple[BoardManager, TransportFactory],
) -> None:
    manager, _ = stack
    with pytest.raises(ValueError, match="positive"):
        manager.capture_samples(0)
    with pytest.raises(ValueError, match="positive"):
        manager.capture_seconds(0.0)


def test_capture_requires_connection() -> None:
    manager = BoardManager()
    with pytest.raises(RuntimeError, match="no ADC board connected"):
        manager.capture_samples(3)


def test_capture_skips_failed_computed_channel(
    stack: tuple[BoardManager, TransportFactory], tmp_path: Path
) -> None:
    manager, factory = stack
    manager._config["profiles"]["default"]["computed"] = [
        {"name": "Bad", "unit": "A", "expr": "A0/0", "show": True}
    ]
    factory.last.set_samples(_samples(3))
    with mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path):
        result = manager.capture_samples(3)

    channels = cast(list[dict[str, object]], result["channels"])
    keys = {c["key"] for c in channels}
    assert "Bad" not in keys  # the failed computed channel is skipped, not summed
    assert keys  # physical channels still summarized


def test_capture_deduplicates_colliding_column_names(
    stack: tuple[BoardManager, TransportFactory], tmp_path: Path
) -> None:
    # A raw channel and a computed channel share the display label "dup" — the
    # fix must make the CSV columns (and the summary) unique without renaming the
    # profile's labels.
    manager, factory = stack
    profile = manager._config["profiles"]["default"]
    profile["channels"] = {
        key: {"name": key, "unit": "V", "gain": 0.001, "offset": 0.0, "show": True}
        for key in CHANNEL_KEYS
    }
    profile["channels"]["A2"]["name"] = "dup"
    profile["computed"] = [{"name": "dup", "unit": "V", "expr": "A2 * 2", "show": True}]
    manager._config["active_profile"] = "default"

    factory.last.set_samples(_samples(2))
    with mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path):
        result = manager.capture_samples(2)

    path = Path(cast(str, result["path"]))
    header = next(line for line in path.read_text().splitlines() if not line.startswith("#"))
    columns = header.split(",")
    assert "dup" in columns
    assert "dup (computed)" in columns
    assert len(columns) == len(set(columns))  # every column name unique

    names = [c["name"] for c in cast(list[dict[str, object]], result["channels"])]
    assert "dup (computed)" in names
    assert len(names) == len(set(names))  # summary names unique too


def test_sample_once_returns_converted_sample(
    stack: tuple[BoardManager, TransportFactory],
) -> None:
    manager, factory = stack
    factory.last.set_samples([(9, (0, 1, 2, 3, 4, 5))])
    result = manager.sample_once()

    channels = cast(list[dict[str, object]], result["channels"])
    assert result["counter"] == 9  # single-shot keeps the firmware counter
    assert result["averaged_n"] == 0
    assert channels
    assert all("name" in c and "value" in c for c in channels)


def test_pump_fault_releases_the_recorder(
    stack: tuple[BoardManager, TransportFactory], tmp_path: Path
) -> None:
    """A fault mid-recording closes the recorder and clears the state (#7).

    The pump's except block must release the recorder itself: ``_streaming``
    is already False there, so no later ``stop_stream()`` would flush the
    file. The fault injected is a CSV write failure, one of the two faults
    the block names; the spy's ``closed`` flag is the pin.
    """

    class _FailingRecorder(_SpyRecorder):
        def __init__(self, path: str, *args: object, **kwargs: object) -> None:
            super().__init__()
            self.path = path  # start_stream reads it back into record_path

        def write(self, counter: int, averaged_n: int, values: list[object]) -> None:
            raise OSError("disk gone")

    spies: list[_FailingRecorder] = []

    def make_recorder(path: str, *args: object, **kwargs: object) -> _FailingRecorder:
        spy = _FailingRecorder(path, *args, **kwargs)
        spies.append(spy)
        return spy

    manager, factory = stack
    factory.last.set_samples(_samples(50))
    with (
        mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path),
        mock.patch("benchweave.web.board._Recorder", side_effect=make_recorder),
    ):
        manager.start_stream(record=True)
        wait_until(lambda: bool(spies) and spies[0].closed)

    status = manager.status()
    assert status["streaming"] is False
    assert status["recording"] is False
    assert str(status["last_error"]).startswith("stream stopped:")
    # A later stop is a clean no-op, not a second close or a traceback.
    manager.stop_stream()
    assert manager.status()["recording"] is False


def test_pump_recorder_fault_stops_the_board(
    stack: tuple[BoardManager, TransportFactory], tmp_path: Path
) -> None:
    """A CSV write failure aborts the acquisition, not just the manager's flag (#12).

    The pump's except block cleared ``_streaming`` without an abort, and
    ``_stop_stream_locked`` returns early on ``_streaming``, so nothing ever
    stopped the board: its orphaned stream filled the serial ring and the
    next stream or capture began with stale samples recorded as fresh.
    """

    class _FailingRecorder(_SpyRecorder):
        def __init__(self, path: str, *args: object, **kwargs: object) -> None:
            super().__init__()
            self.path = path

        def write(self, counter: int, averaged_n: int, values: list[object]) -> None:
            raise OSError("disk full")

    manager, factory = stack
    factory.last.set_samples(_samples(50))
    with (
        mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path),
        mock.patch("benchweave.web.board._Recorder", _FailingRecorder),
    ):
        manager.start_stream(record=True)
        wait_until(lambda: manager.status()["streaming"] is False)

    assert factory.last.streaming is False, "the fault path must stop the board"


def test_locked_stop_closes_the_recorder_when_the_stream_already_stopped(
    stack: tuple[BoardManager, TransportFactory],
) -> None:
    """``_stop_stream_locked`` flushes and closes even with ``_streaming`` False (#7)."""
    manager, _factory = stack
    spy = _SpyRecorder()
    manager._streaming = False
    manager._recording = True
    manager._recorder = spy  # type: ignore[assignment]
    with manager._lock:
        manager._stop_stream_locked()
    assert spy.closed
    assert manager._recorder is None
    assert manager._recording is False
