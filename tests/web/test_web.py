"""BoardManager behaviour over the full adapter/host stack, on a fake board."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest import mock

import pytest
from fastapi import HTTPException

from benchweave.web import app as web_app
from benchweave.web.board import BoardManager
from plugins.adc_6ch_12bit.config import DEFAULT_CONFIG
from tests.adc.fakeboard import SampleTuple, TransportFactory, wait_until

ZERO_CHANNELS = (0, 0, 0, 0, 0, 0)


def _zeros(n: int, start: int = 0) -> list[SampleTuple]:
    return [(start + i, ZERO_CHANNELS) for i in range(n)]


def _endless_zeros() -> Iterator[SampleTuple]:
    counter = 0
    while True:
        yield (counter, ZERO_CHANNELS)
        counter += 1


class _SpyRecorder:
    """Stands in for _Recorder where the CSV contents do not matter."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.path = "spy.csv"
        self.written: list[int] = []
        self.closed = False
        self.resumed = False

    def write(self, counter: int, averaged_n: int, values: list[object]) -> None:
        self.written.append(counter)

    def close(self) -> None:
        self.closed = True

    def resume(self) -> None:
        self.resumed = True


@pytest.fixture
def factory() -> Iterator[TransportFactory]:
    transport_factory = TransportFactory()
    with (
        mock.patch("benchweave.web.board._open_transport", transport_factory),
        mock.patch("benchweave.web.board.save_config"),
    ):
        yield transport_factory


def _manager() -> BoardManager:
    manager = BoardManager()
    manager._config = copy.deepcopy(DEFAULT_CONFIG)
    return manager


def test_board_connect_status_and_config(factory: TransportFactory) -> None:
    manager = _manager()
    assert manager.status()["connected"] is False

    manager.connect("/dev/ttyACM2")
    try:
        status = manager.status()
        assert status["connected"] is True
        assert status["firmware"] == "0.2"
        assert status["streaming"] is False

        manager.set_averaging(16)
        assert manager.status()["averaging"] == 16
        assert factory.last.averaging == 16  # reached the wire

        manager.set_channels(0x0F)
        assert manager.status()["channel_mask"] == 0x0F
        assert factory.last.channel_mask == 0x0F
    finally:
        manager.disconnect()


def test_set_channels_persists_selection(factory: TransportFactory) -> None:
    with mock.patch("benchweave.web.board.save_config") as save:
        manager = _manager()
        manager.connect("/dev/ttyACM2")
        try:
            manager.set_channels(0b001011)  # a0, a1, a3
            assert manager._config["settings"]["channel_mask"] == 0b001011
            save.assert_called_once_with(manager._config)
        finally:
            manager.disconnect()


def test_connect_restores_channel_mask(factory: TransportFactory) -> None:
    manager = BoardManager()
    manager._config = {"settings": {"channel_mask": 0b001011}}
    manager.connect("/dev/ttyACM2")
    try:
        assert manager.status()["channel_mask"] == 0b001011
        assert factory.last.channel_mask == 0b001011  # re-applied on the wire
    finally:
        manager.disconnect()


def test_connect_defaults_to_all_channels_when_unsaved(factory: TransportFactory) -> None:
    manager = BoardManager()
    manager._config = {}
    manager.connect("/dev/ttyACM2")
    try:
        assert manager.status()["channel_mask"] == 0x3F  # CHANNEL_MASK_ALL
    finally:
        manager.disconnect()


def test_board_rejects_config_while_streaming(factory: TransportFactory) -> None:
    manager = _manager()
    manager.connect("/dev/ttyACM2")
    try:
        manager._streaming = True  # simulate an active stream

        with pytest.raises(RuntimeError, match="stop streaming"):
            manager.set_averaging(4)
    finally:
        manager._streaming = False
        manager.disconnect()


def test_board_rejects_control_when_disconnected() -> None:
    manager = BoardManager()
    with pytest.raises(RuntimeError, match="no ADC board connected"):
        manager.start_stream()


def test_board_reconnect_closes_previous_connection(factory: TransportFactory) -> None:
    manager = _manager()
    manager.connect("/dev/ttyACM2")
    # Reconnecting (board unplugged then replugged) must not leak the old port.
    manager.connect("/dev/ttyACM2")
    try:
        assert manager.status()["connected"] is True
        assert len(factory.created) == 2
        assert factory.created[0].is_open is False  # first session torn down
        assert factory.created[1].is_open is True
    finally:
        manager.disconnect()


def test_api_rejects_invalid_averaging() -> None:
    with pytest.raises(HTTPException) as excinfo:
        web_app.set_averaging(web_app.AveragingBody(n=3))
    assert excinfo.value.status_code == 422


def test_api_rejects_invalid_channel_mask() -> None:
    with pytest.raises(HTTPException) as excinfo:
        web_app.set_channels(web_app.ChannelsBody(mask=0x40))
    assert excinfo.value.status_code == 422


def test_record_interval_from_sample_rate() -> None:
    manager = BoardManager()
    manager._config = {"settings": {"sample_rate_hz": 100}}
    assert manager._record_interval() == pytest.approx(0.01)
    manager._config = {"settings": {"sample_rate_hz": None}}
    assert manager._record_interval() == 0.0


def test_stream_pump_assigns_sequential_counters(factory: TransportFactory) -> None:
    # Firmware counters (1000...) must be re-stamped with the manager's own
    # recorded-sample counter, starting at zero.
    factory.samples = _zeros(3, start=1000)
    with mock.patch("benchweave.web.board._Recorder", _SpyRecorder):
        manager = _manager()  # default config: sample_rate None keeps every sample
        manager.connect("/dev/ttyACM2")
        try:
            manager.start_stream(record=True)
            spy = cast(_SpyRecorder, manager._recorder)
            wait_until(lambda: len(spy.written) >= 3)
            manager.stop_stream()
            assert spy.written == [0, 1, 2]
        finally:
            manager.disconnect()


def test_stream_pump_decimates_to_sample_rate(factory: TransportFactory) -> None:
    factory.samples = _zeros(5)
    with mock.patch("benchweave.web.board._Recorder", _SpyRecorder):
        manager = _manager()
        manager._config["settings"]["sample_rate_hz"] = 1  # 1 s between samples
        manager.connect("/dev/ttyACM2")
        try:
            manager.start_stream(record=True)
            spy = cast(_SpyRecorder, manager._recorder)
            wait_until(lambda: len(spy.written) >= 1)
            # All five samples arrive within far less than the 1 s interval,
            # so only the first survives the decimation.
            wait_until(lambda: not factory.last.streaming or factory.last.in_waiting == 0)
            manager.stop_stream()
            assert spy.written == [0]
        finally:
            manager.disconnect()


def test_pause_stops_board_and_keeps_recorder_open(factory: TransportFactory) -> None:
    factory.samples = _endless_zeros()
    with mock.patch("benchweave.web.board._Recorder", _SpyRecorder):
        manager = _manager()
        manager.connect("/dev/ttyACM2")
        try:
            manager.start_stream(record=True)
            spy = cast(_SpyRecorder, manager._recorder)
            assert factory.last.streaming is True

            status = manager.pause_stream()

            assert status["paused"] is True
            assert status["streaming"] is True
            assert factory.last.streaming is False  # STOP_STREAM reached the board
            assert spy.closed is False  # the CSV stays open across a pause
        finally:
            manager.disconnect()


def test_resume_restarts_board_and_recorder(factory: TransportFactory) -> None:
    factory.samples = ()  # nothing to emit: the counter must not move
    with mock.patch("benchweave.web.board._Recorder", _SpyRecorder):
        manager = _manager()
        manager.connect("/dev/ttyACM2")
        try:
            manager.start_stream(record=True)
            spy = cast(_SpyRecorder, manager._recorder)
            manager.pause_stream()
            manager._counter = 42

            status = manager.resume_stream()

            assert status["paused"] is False
            assert factory.last.streaming is True  # a fresh acquisition was armed
            assert spy.resumed is True
            assert manager._counter == 42  # counter survives a resume
        finally:
            manager.disconnect()


def test_stop_after_pause_closes_recorder(factory: TransportFactory) -> None:
    factory.samples = ()
    with mock.patch("benchweave.web.board._Recorder", _SpyRecorder):
        manager = _manager()
        manager.connect("/dev/ttyACM2")
        try:
            manager.start_stream(record=True)
            spy = cast(_SpyRecorder, manager._recorder)
            manager.pause_stream()

            status = manager.stop_stream()

            assert status["streaming"] is False
            assert status["paused"] is False
            assert spy.closed is True
        finally:
            manager.disconnect()


def test_save_graph_png_names_after_csv(tmp_path: Path) -> None:
    manager = BoardManager()
    manager._record_path = "/captures/adc_ABC_20260913_120000.csv"
    with mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path):
        result = manager.save_graph_png(b"\x89PNG\r\n\x1a\n")
    assert result["name"] == "adc_ABC_20260913_120000.png"
    assert (tmp_path / "adc_ABC_20260913_120000.png").read_bytes() == b"\x89PNG\r\n\x1a\n"


def test_save_graph_png_falls_back_without_recording(tmp_path: Path) -> None:
    manager = BoardManager()
    manager._serial = "XYZ"
    with mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path):
        result = manager.save_graph_png(b"png")
    assert str(result["name"]).startswith("adc_XYZ_")
    assert str(result["name"]).endswith(".png")
    assert (tmp_path / str(result["name"])).read_bytes() == b"png"


def test_api_rejects_invalid_png_data() -> None:
    with pytest.raises(HTTPException) as excinfo:
        web_app.graph_export(web_app.GraphExportBody(image="not-base64!!!"))
    assert excinfo.value.status_code == 422


def test_reveal_graph_png_opens_last_saved(tmp_path: Path) -> None:
    manager = BoardManager()
    png = tmp_path / "adc_X.png"
    png.write_bytes(b"png")
    manager._last_png_path = str(png)
    with (
        mock.patch("benchweave.web.board.sys.platform", "linux"),
        mock.patch("benchweave.web.board.shutil.which", return_value="/usr/bin/dolphin"),
        mock.patch("benchweave.web.board.subprocess.Popen") as popen,
    ):
        result = manager.reveal_graph_png()
    assert result["path"] == str(png)
    popen.assert_called_once_with(["/usr/bin/dolphin", "--select", str(png)])


def test_reveal_graph_png_falls_back_to_capture_dir(tmp_path: Path) -> None:
    manager = BoardManager()
    manager._last_png_path = None

    def which(tool: str) -> str | None:
        return "/usr/bin/xdg-open" if tool == "xdg-open" else None

    with (
        mock.patch("benchweave.web.board.sys.platform", "linux"),
        mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path),
        mock.patch("benchweave.web.board.shutil.which", side_effect=which),
        mock.patch("benchweave.web.board.subprocess.Popen") as popen,
    ):
        result = manager.reveal_graph_png()
    assert result["path"] == str(tmp_path)
    popen.assert_called_once_with(["/usr/bin/xdg-open", str(tmp_path)])


def test_reveal_graph_png_uses_explorer_on_windows(tmp_path: Path) -> None:
    manager = BoardManager()
    png = tmp_path / "adc_X.png"
    png.write_bytes(b"png")
    manager._last_png_path = str(png)
    with (
        mock.patch("benchweave.web.board.sys.platform", "win32"),
        mock.patch("benchweave.web.board.subprocess.Popen") as popen,
    ):
        result = manager.reveal_graph_png()
    assert result["path"] == str(png)
    popen.assert_called_once_with(["explorer", "/select,", str(png)])
