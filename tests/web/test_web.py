from unittest import mock

import pytest
from fastapi import HTTPException

from benchweave.web import app as web_app
from benchweave.web.app import SSE_MIN_INTERVAL, _sse_interval
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
    with mock.patch("benchweave.web.board.AdcDriver", _FakeDriver):
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


def test_sse_interval_honours_sample_rate() -> None:
    # Slow rates throttle the graph to match the CSV recording rate.
    assert _sse_interval({"settings": {"sample_rate_hz": 0.5}}) == pytest.approx(2.0)
    assert _sse_interval({"settings": {"sample_rate_hz": 0.1}}) == pytest.approx(10.0)
    # No rate -> the ~30 Hz live cap.
    assert _sse_interval({"settings": {"sample_rate_hz": None}}) == pytest.approx(
        SSE_MIN_INTERVAL
    )
    assert _sse_interval({}) == pytest.approx(SSE_MIN_INTERVAL)
    # Rates faster than the live cap stay capped, not sped up.
    assert _sse_interval({"settings": {"sample_rate_hz": 100}}) == pytest.approx(
        SSE_MIN_INTERVAL
    )


def test_stream_worker_rebases_counter_per_stream() -> None:
    manager = BoardManager()
    manager._loop = None  # exercise the recording path only

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
    with mock.patch.object(manager._driver, "iter_samples", return_value=iter(samples)):
        manager._stream_worker()

    assert written == [0, 1, 2]
