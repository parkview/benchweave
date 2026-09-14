from unittest import mock

import pytest
from fastapi import HTTPException

from benchweave.web import app as web_app
from benchweave.web.board import BoardManager
from plugins.adc_6ch_12bit.driver import Sample
from plugins.adc_6ch_12bit.protocol import IdentifyInfo


class _FakeDriver:
    def __init__(self) -> None:
        self.opened = False
        self.averaging = 0
        self.channel_mask = 0x3F
        self.streaming = False

    def open(self, device: str, *, baud: int = 2_000_000, timeout: float = 1.0) -> None:
        if self.opened:
            raise RuntimeError("already open")
        self.opened = True

    def identify(self, *, timeout: float | None = None) -> IdentifyInfo:
        return IdentifyInfo(1, 0, 2, 6, 12)

    def set_averaging(self, n: int, *, timeout: float | None = None) -> None:
        self.averaging = n

    def set_channels(self, mask: int, *, timeout: float | None = None) -> None:
        self.channel_mask = mask

    def start_stream(self, *, timeout: float | None = None) -> None:
        self.streaming = True

    def stop_stream(self, *, timeout: float | None = None) -> None:
        self.streaming = False

    def close(self) -> None:
        self.opened = False


def test_board_connect_status_and_config() -> None:
    with (
        mock.patch("benchweave.web.board.AdcDriver", _FakeDriver),
        mock.patch("benchweave.web.board.save_config"),
    ):
        manager = BoardManager()
        assert manager.status()["connected"] is False

        manager.connect("/dev/ttyACM2")
        status = manager.status()
        assert status["connected"] is True
        assert status["firmware"] == "0.2"
        assert status["streaming"] is False

        manager.set_averaging(16)
        assert manager.status()["averaging"] == 16

        manager.set_channels(0x0F)
        assert manager.status()["channel_mask"] == 0x0F


def test_set_channels_persists_selection() -> None:
    with (
        mock.patch("benchweave.web.board.AdcDriver", _FakeDriver),
        mock.patch("benchweave.web.board.save_config") as save,
    ):
        manager = BoardManager()
        manager.connect("/dev/ttyACM2")
        manager.set_channels(0b001011)  # a0, a1, a3
        assert manager._config["settings"]["channel_mask"] == 0b001011
        save.assert_called_once_with(manager._config)


def test_connect_restores_channel_mask() -> None:
    with mock.patch("benchweave.web.board.AdcDriver", _FakeDriver):
        manager = BoardManager()
        manager._config = {"settings": {"channel_mask": 0b001011}}
        manager.connect("/dev/ttyACM2")
        assert manager.status()["channel_mask"] == 0b001011


def test_connect_defaults_to_all_channels_when_unsaved() -> None:
    with mock.patch("benchweave.web.board.AdcDriver", _FakeDriver):
        manager = BoardManager()
        manager._config = {}
        manager.connect("/dev/ttyACM2")
        assert manager.status()["channel_mask"] == 0x3F  # CHANNEL_MASK_ALL


def test_board_rejects_config_while_streaming() -> None:
    with mock.patch("benchweave.web.board.AdcDriver", _FakeDriver):
        manager = BoardManager()
        manager.connect("/dev/ttyACM2")
        manager._streaming = True  # simulate an active stream

        with pytest.raises(RuntimeError, match="stop streaming"):
            manager.set_averaging(4)


def test_board_rejects_control_when_disconnected() -> None:
    with mock.patch("benchweave.web.board.AdcDriver", _FakeDriver):
        manager = BoardManager()
        with pytest.raises(RuntimeError, match="no ADC board connected"):
            manager.start_stream()


def test_board_reconnect_closes_previous_connection() -> None:
    with mock.patch("benchweave.web.board.AdcDriver", _FakeDriver):
        manager = BoardManager()
        manager.connect("/dev/ttyACM2")
        # Reconnecting (board unplugged then replugged) must not raise "already open".
        manager.connect("/dev/ttyACM2")
        assert manager.status()["connected"] is True


def test_api_rejects_invalid_averaging() -> None:
    with pytest.raises(HTTPException) as excinfo:
        web_app.set_averaging(web_app.AveragingBody(n=3))
    assert excinfo.value.status_code == 422


def test_api_rejects_invalid_channel_mask() -> None:
    with pytest.raises(HTTPException) as excinfo:
        web_app.set_channels(web_app.ChannelsBody(mask=0x40))
    assert excinfo.value.status_code == 422


def test_record_interval_from_sample_rate() -> None:
    with mock.patch("benchweave.web.board.AdcDriver", _FakeDriver):
        manager = BoardManager()
        manager._config = {"settings": {"sample_rate_hz": 100}}
        assert manager._record_interval() == pytest.approx(0.01)
        manager._config = {"settings": {"sample_rate_hz": None}}
        assert manager._record_interval() == 0.0


def test_stream_worker_assigns_sequential_counter() -> None:
    manager = BoardManager()
    manager._loop = None  # exercise the recording path only
    manager._config = {"settings": {"sample_rate_hz": None}}  # full rate: keep every sample

    written: list[int] = []

    class _SpyRecorder:
        def write(self, counter: int, averaged_n: int, values: list[object]) -> None:
            written.append(counter)

        def close(self) -> None:
            pass

    manager._recorder = _SpyRecorder()  # type: ignore[assignment]
    samples = [
        Sample(counter=1000, channels=(0, 0, 0, 0, 0, 0), averaged_n=0),
        Sample(counter=1001, channels=(0, 0, 0, 0, 0, 0), averaged_n=0),
        Sample(counter=1002, channels=(0, 0, 0, 0, 0, 0), averaged_n=0),
    ]
    with (
        mock.patch.object(manager._driver, "iter_samples", return_value=iter(samples)),
        mock.patch.object(manager, "convert_sample", return_value=[]),
    ):
        manager._stream_worker()

    assert written == [0, 1, 2]


def test_stream_worker_decimates_to_sample_rate() -> None:
    manager = BoardManager()
    manager._loop = None
    manager._config = {"settings": {"sample_rate_hz": 2}}  # 0.5 s between samples

    written: list[int] = []

    class _SpyRecorder:
        def write(self, counter: int, averaged_n: int, values: list[object]) -> None:
            written.append(counter)

        def close(self) -> None:
            pass

    manager._recorder = _SpyRecorder()  # type: ignore[assignment]
    samples = [
        Sample(counter=i, channels=(0, 0, 0, 0, 0, 0), averaged_n=0) for i in range(5)
    ]
    clock = iter([0.5, 0.75, 1.0, 1.25, 1.5])
    with (
        mock.patch.object(manager._driver, "iter_samples", return_value=iter(samples)),
        mock.patch.object(manager, "convert_sample", return_value=[]),
        mock.patch("benchweave.web.board.time.monotonic", side_effect=lambda: next(clock)),
    ):
        manager._stream_worker()

    # Only the samples at 0.5, 1.0, 1.5 s survive the 0.5 s decimation.
    assert written == [0, 1, 2]


class _SpyRecorder:
    def __init__(self) -> None:
        self.closed = False
        self.resumed = False
        self.writes = 0

    def write(self, counter: int, averaged_n: int, values: list[object]) -> None:
        self.writes += 1

    def close(self) -> None:
        self.closed = True

    def resume(self) -> None:
        self.resumed = True


def test_pause_stops_driver_and_keeps_recorder_open() -> None:
    with mock.patch("benchweave.web.board.AdcDriver", _FakeDriver):
        manager = BoardManager()
        manager.connect("/dev/ttyACM2")
        manager._driver.start_stream()
        manager._streaming = True
        manager._recording = True
        recorder = _SpyRecorder()
        manager._recorder = recorder  # type: ignore[assignment]
        manager._worker = None

        status = manager.pause_stream()

        assert status["paused"] is True
        assert status["streaming"] is True
        assert manager._driver.streaming is False  # type: ignore[attr-defined]
        assert recorder.closed is False


def test_resume_restarts_driver_and_recorder() -> None:
    with mock.patch("benchweave.web.board.AdcDriver", _FakeDriver):
        manager = BoardManager()
        manager.connect("/dev/ttyACM2")
        manager._streaming = True
        manager._paused = True
        manager._counter = 42
        recorder = _SpyRecorder()
        manager._recorder = recorder  # type: ignore[assignment]
        manager._worker = None

        with mock.patch.object(manager, "_stream_worker", return_value=None):
            status = manager.resume_stream()

        assert status["paused"] is False
        assert manager._driver.streaming is True  # type: ignore[attr-defined]
        assert recorder.resumed is True
        assert manager._counter == 42  # counter survives a resume


def test_stop_after_pause_closes_recorder() -> None:
    with mock.patch("benchweave.web.board.AdcDriver", _FakeDriver):
        manager = BoardManager()
        manager.connect("/dev/ttyACM2")
        manager._streaming = True
        manager._paused = True
        recorder = _SpyRecorder()
        manager._recorder = recorder  # type: ignore[assignment]
        manager._worker = None

        status = manager.stop_stream()

        assert status["streaming"] is False
        assert status["paused"] is False
        assert recorder.closed is True


def test_save_graph_png_names_after_csv(tmp_path) -> None:
    manager = BoardManager()
    manager._record_path = "/captures/adc_ABC_20260913_120000.csv"
    with mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path):
        result = manager.save_graph_png(b"\x89PNG\r\n\x1a\n")
    assert result["name"] == "adc_ABC_20260913_120000.png"
    assert (tmp_path / "adc_ABC_20260913_120000.png").read_bytes() == b"\x89PNG\r\n\x1a\n"


def test_save_graph_png_falls_back_without_recording(tmp_path) -> None:
    manager = BoardManager()
    manager._serial = "XYZ"
    with mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path):
        result = manager.save_graph_png(b"png")
    assert result["name"].startswith("adc_XYZ_")
    assert result["name"].endswith(".png")
    assert (tmp_path / result["name"]).read_bytes() == b"png"


def test_api_rejects_invalid_png_data() -> None:
    with pytest.raises(HTTPException) as excinfo:
        web_app.graph_export(web_app.GraphExportBody(image="not-base64!!!"))
    assert excinfo.value.status_code == 422


def test_reveal_graph_png_opens_last_saved(tmp_path) -> None:
    manager = BoardManager()
    png = tmp_path / "adc_X.png"
    png.write_bytes(b"png")
    manager._last_png_path = str(png)
    with (
        mock.patch("benchweave.web.board.shutil.which", return_value="/usr/bin/dolphin"),
        mock.patch("benchweave.web.board.subprocess.Popen") as popen,
    ):
        result = manager.reveal_graph_png()
    assert result["path"] == str(png)
    popen.assert_called_once_with(["/usr/bin/dolphin", "--select", str(png)])


def test_reveal_graph_png_falls_back_to_capture_dir(tmp_path) -> None:
    manager = BoardManager()
    manager._last_png_path = None
    with (
        mock.patch("benchweave.web.board.capture_dir", return_value=tmp_path),
        mock.patch("benchweave.web.board.shutil.which", return_value=None),
        mock.patch("benchweave.web.board.subprocess.Popen") as popen,
    ):
        result = manager.reveal_graph_png()
    assert result["path"] == str(tmp_path)
    popen.assert_called_once_with(["xdg-open", str(tmp_path)])
