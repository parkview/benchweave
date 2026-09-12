"""Board manager: owns the AdcDriver and bridges its live stream to asyncio."""

from __future__ import annotations

import asyncio
import threading
from contextlib import suppress

from benchweave.adc import CHANNEL_MASK_ALL, AdcDriver, Sample, discover_adc_boards


class BoardManager:
    """Owns one ADC board: lifecycle, control, and live-sample fan-out to SSE clients."""

    def __init__(self) -> None:
        self._driver = AdcDriver()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[Sample]] = set()
        self._worker: threading.Thread | None = None
        self._device: str | None = None
        self._fw_major: int | None = None
        self._fw_minor: int | None = None
        self._averaging = 0
        self._channel_mask = CHANNEL_MASK_ALL
        self._streaming = False

    # -- lifecycle -----------------------------------------------------------

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def discover(self) -> list[dict[str, object]]:
        return [
            {
                "device": board.device,
                "firmware": f"{board.info.fw_major}.{board.info.fw_minor}",
                "channels": board.info.n_channels,
                "resolution": board.info.resolution,
            }
            for board in discover_adc_boards()
        ]

    def connect(self, device: str) -> dict[str, object]:
        with self._lock:
            self._driver.open(device)
            info = self._driver.identify()
            self._device = device
            self._fw_major = info.fw_major
            self._fw_minor = info.fw_minor
            self._averaging = 0
            self._channel_mask = CHANNEL_MASK_ALL
            self._streaming = False
        return self.status()

    def disconnect(self) -> None:
        with self._lock:
            self._stop_stream_locked()
            with suppress(Exception):
                self._driver.close()
            self._device = None
            self._fw_major = None
            self._fw_minor = None

    def status(self) -> dict[str, object]:
        return {
            "connected": self._device is not None,
            "device": self._device,
            "firmware": (
                f"{self._fw_major}.{self._fw_minor}" if self._fw_major is not None else None
            ),
            "averaging": self._averaging,
            "channel_mask": self._channel_mask,
            "streaming": self._streaming,
        }

    # -- control -------------------------------------------------------------

    def set_averaging(self, n: int) -> dict[str, object]:
        with self._lock:
            self._require_idle()
            self._driver.set_averaging(n)
            self._averaging = n
        return self.status()

    def set_channels(self, mask: int) -> dict[str, object]:
        with self._lock:
            self._require_idle()
            self._driver.set_channels(mask)
            self._channel_mask = mask
        return self.status()

    def start_stream(self) -> dict[str, object]:
        with self._lock:
            self._require_connected()
            if not self._streaming:
                self._driver.start_stream()
                self._streaming = True
                self._worker = threading.Thread(
                    target=self._stream_worker, name="adc-stream", daemon=True
                )
                self._worker.start()
        return self.status()

    def stop_stream(self) -> dict[str, object]:
        with self._lock:
            self._stop_stream_locked()
        return self.status()

    def _stop_stream_locked(self) -> None:
        if not self._streaming:
            return
        with suppress(Exception):
            self._driver.stop_stream()
        self._streaming = False

    # -- live stream fan-out -------------------------------------------------

    def subscribe(self) -> asyncio.Queue[Sample]:
        queue: asyncio.Queue[Sample] = asyncio.Queue(maxsize=2000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Sample]) -> None:
        self._subscribers.discard(queue)

    def _stream_worker(self) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            for sample in self._driver.iter_samples():
                loop.call_soon_threadsafe(self._publish, sample)
        except Exception:
            pass

    def _publish(self, sample: Sample) -> None:
        for queue in self._subscribers:
            with suppress(asyncio.QueueFull):
                queue.put_nowait(sample)

    # -- guards --------------------------------------------------------------

    def _require_connected(self) -> None:
        if self._device is None:
            raise RuntimeError("no ADC board connected")

    def _require_idle(self) -> None:
        self._require_connected()
        if self._streaming:
            raise RuntimeError("stop streaming before changing configuration")
