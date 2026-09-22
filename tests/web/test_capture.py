import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest import mock

import pytest

from benchweave.web.board import BoardManager
from plugins.adc_6ch_12bit import CHANNEL_KEYS
from plugins.adc_6ch_12bit.driver import Sample
from plugins.adc_6ch_12bit.protocol import IdentifyInfo


class _CaptureDriver:
    """Minimal fake of AdcDriver for capture tests."""

    def __init__(self) -> None:
        self.opened = False
        self.streaming = False
        self.samples: Iterator[Sample] = iter([])

    def open(self, device: str, *, baud: int = 2_000_000, timeout: float = 1.0) -> None:
        self.opened = True

    def identify(self, *, timeout: float | None = None) -> IdentifyInfo:
        return IdentifyInfo(1, 0, 2, 6, 12)

    def set_averaging(self, n: int, *, timeout: float | None = None) -> None:
        pass

    def set_channels(self, mask: int, *, timeout: float | None = None) -> None:
        pass

    def start_stream(self, *, timeout: float | None = None) -> None:
        self.streaming = True

    def stop_stream(self, *, timeout: float | None = None) -> None:
        self.streaming = False

    def iter_samples(self) -> Iterator[Sample]:
        return self.samples

    def close(self) -> None:
        self.opened = False


def _driver_of(m: BoardManager) -> _CaptureDriver:
    return cast(_CaptureDriver, m._driver)


@pytest.fixture
def manager() -> Iterator[BoardManager]:
    with (
        mock.patch("benchweave.web.board.AdcDriver", _CaptureDriver),
        mock.patch("benchweave.web.board.save_config"),
    ):
        m = BoardManager()
        m.connect("/dev/ttyACM2")
        yield m


def _samples(n: int) -> list[Sample]:
    return [Sample(counter=i, channels=(i, 1, 2, 3, 4, 5), averaged_n=0) for i in range(n)]


def test_capture_samples_returns_n_and_writes_tagged_csv(
    manager: BoardManager, tmp_path: Path
) -> None:
    _driver_of(manager).samples = iter(_samples(3))
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


def test_capture_samples_without_tag_omits_mcp(manager: BoardManager, tmp_path: Path) -> None:
    _driver_of(manager).samples = iter(_samples(1))
    with mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path):
        result = manager.capture_samples(1)

    assert "MCP" not in Path(cast(str, result["path"])).name


def test_capture_samples_raises_when_not_enough(manager: BoardManager, tmp_path: Path) -> None:
    _driver_of(manager).samples = iter(_samples(1))
    with (
        mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path),
        pytest.raises(RuntimeError, match="timed out"),
    ):
        manager.capture_samples(3, tag="MCP", timeout=0.05)


def test_capture_seconds_drains_available(manager: BoardManager, tmp_path: Path) -> None:
    _driver_of(manager).samples = iter(_samples(5))
    with mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path):
        result = manager.capture_seconds(60.0, tag="MCP")

    assert result["count"] == 5
    assert len(cast(list[dict[str, object]], result["channels"])) >= 1


class _SpyRecorder:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.closed = False

    def write(self, counter: int, averaged_n: int, values: list[object]) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_capture_seconds_stops_at_deadline(manager: BoardManager, tmp_path: Path) -> None:
    _driver_of(manager).samples = iter(_samples(10))
    clock = iter([0.0, 0.25, 0.25])  # start 0.0; deadline 0.2; loop check and elapsed both 0.25
    with (
        mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path),
        mock.patch("benchweave.web.board._Recorder", _SpyRecorder),
        mock.patch("benchweave.web.board.time.monotonic", side_effect=lambda: next(clock)),
    ):
        result = manager.capture_seconds(0.2)

    assert result["count"] == 0


def test_capture_rejects_nonpositive(manager: BoardManager) -> None:
    with pytest.raises(ValueError, match="positive"):
        manager.capture_samples(0)
    with pytest.raises(ValueError, match="positive"):
        manager.capture_seconds(0.0)


def test_capture_requires_connection(tmp_path: Path) -> None:
    with (
        mock.patch("benchweave.web.board.AdcDriver", _CaptureDriver),
        mock.patch("benchweave.web.board.save_config"),
    ):
        m = BoardManager()

    with pytest.raises(RuntimeError, match="no ADC board connected"):
        m.capture_samples(3)


def test_capture_skips_failed_computed_channel(manager: BoardManager, tmp_path: Path) -> None:
    manager._config["profiles"]["default"]["computed"] = [
        {"name": "Bad", "unit": "A", "expr": "A0/0", "show": True}
    ]
    _driver_of(manager).samples = iter(_samples(3))
    with mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path):
        result = manager.capture_samples(3)

    channels = cast(list[dict[str, object]], result["channels"])
    keys = {c["key"] for c in channels}
    assert "Bad" not in keys  # the failed computed channel is skipped, not summed
    assert keys  # physical channels still summarized


def test_capture_deduplicates_colliding_column_names(manager: BoardManager, tmp_path: Path) -> None:
    # A raw channel and a computed channel share the display label "dup" — the
    # fix must make the CSV columns (and the summary) unique without renaming the
    # profile's labels.
    profile = manager._config["profiles"]["default"]
    profile["channels"] = {
        key: {"name": key, "unit": "V", "gain": 0.001, "offset": 0.0, "show": True}
        for key in CHANNEL_KEYS
    }
    profile["channels"]["A2"]["name"] = "dup"
    profile["computed"] = [{"name": "dup", "unit": "V", "expr": "A2 * 2", "show": True}]
    manager._config["active_profile"] = "default"

    _driver_of(manager).samples = iter(_samples(2))
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


def test_sample_once_returns_converted_sample(manager: BoardManager) -> None:
    manager._driver.sample_once = mock.Mock(  # type: ignore[method-assign]
        return_value=Sample(counter=9, channels=(0, 1, 2, 3, 4, 5), averaged_n=0)
    )
    result = manager.sample_once()

    channels = cast(list[dict[str, object]], result["channels"])
    assert result["counter"] == 9
    assert result["averaged_n"] == 0
    assert channels
    assert all("name" in c and "value" in c for c in channels)


def test_worker_fault_releases_the_recorder(manager: BoardManager, tmp_path: Path) -> None:
    """A driver fault mid-recording closes the recorder and clears the state (#7).

    The worker's except block used to leave ``_recorder`` open and
    ``_recording`` True while ``_streaming`` went False, and
    ``_stop_stream_locked`` returned early on ``_streaming``, so no later
    ``stop_stream()`` ever flushed the file: buffered rows were lost and the
    handle leaked until exit. The spy recorder's ``closed`` flag is the pin.
    """

    class _PathSpy(_SpyRecorder):
        def __init__(self, path: str, *args: object, **kwargs: object) -> None:
            super().__init__()
            self.path = path  # start_stream reads it back into record_path

    spies: list[_PathSpy] = []

    def make_recorder(path: str, *args: object, **kwargs: object) -> _PathSpy:
        spy = _PathSpy(path, *args, **kwargs)
        spies.append(spy)
        return spy

    def faulting() -> Iterator[Sample]:
        yield from _samples(1)
        raise OSError("serial gone")

    _driver_of(manager).samples = faulting()
    with (
        mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path),
        mock.patch("benchweave.web.board._Recorder", side_effect=make_recorder),
    ):
        manager.start_stream(record=True)
        # The worker owns the failure path; it may already be gone when we
        # look, so wait for the outcome rather than for the thread object.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not (spies and spies[0].closed):
            time.sleep(0.01)

    assert spies and spies[0].closed, "the fault path must close the recorder"
    status = manager.status()
    assert status["streaming"] is False
    assert status["recording"] is False
    assert str(status["last_error"]).startswith("stream stopped:")
    # A later stop is a clean no-op, not a second close or a traceback.
    manager.stop_stream()
    assert manager.status()["recording"] is False


def test_locked_stop_closes_the_recorder_when_the_stream_already_stopped(
    manager: BoardManager,
) -> None:
    """``_stop_stream_locked`` flushes and closes even with ``_streaming`` False (#7)."""
    spy = _SpyRecorder()
    manager._streaming = False
    manager._recording = True
    manager._recorder = spy  # type: ignore[assignment]
    with manager._lock:
        manager._stop_stream_locked()
    assert spy.closed
    assert manager._recorder is None
    assert manager._recording is False
