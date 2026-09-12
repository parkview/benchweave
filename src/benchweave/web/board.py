"""Board manager: owns the AdcDriver, bridges its stream to asyncio, and records to CSV."""

from __future__ import annotations

import asyncio
import csv
import threading
import time
from contextlib import suppress
from datetime import datetime

from plugins.adc_6ch_12bit import (
    CHANNEL_MASK_ALL,
    AdcDriver,
    Sample,
    adc_capture_filename,
    capture_dir,
    discover_adc_boards,
    serial_for_device,
)

CHANNEL_NAMES = ("a0", "a1", "a2", "a3", "a4", "a7")
CSV_HEADER = ["timestamp", "elapsed_s", "counter", "averaged_n", *CHANNEL_NAMES]


class _Recorder:
    """Appends samples to a CSV file, one row per sample."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._file = open(path, "w", newline="")  # noqa: SIM115 (held open for streaming)
        self._writer = csv.writer(self._file)
        self._writer.writerow(CSV_HEADER)
        self._t0 = time.monotonic()

    def write(self, sample: Sample) -> None:
        self._writer.writerow(
            [
                datetime.now().isoformat(timespec="microseconds"),
                round(time.monotonic() - self._t0, 6),
                sample.counter,
                sample.averaged_n,
                *sample.channels,
            ]
        )

    def close(self) -> None:
        self._file.close()


class BoardManager:
    """Owns one ADC board: lifecycle, control, live fan-out, and optional CSV recording."""

    def __init__(self) -> None:
        self._driver = AdcDriver()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[Sample]] = set()
        self._worker: threading.Thread | None = None
        self._device: str | None = None
        self._serial = ""
        self._fw_major: int | None = None
        self._fw_minor: int | None = None
        self._averaging = 0
        self._channel_mask = CHANNEL_MASK_ALL
        self._streaming = False
        self._recording = False
        self._recorder: _Recorder | None = None
        self._record_path: str | None = None

    # -- lifecycle -----------------------------------------------------------

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def discover(self) -> list[dict[str, object]]:
        return [
            {
                "device": board.device,
                "serial": board.serial,
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
            self._serial = serial_for_device(device)
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
            "serial": self._serial,
            "firmware": (
                f"{self._fw_major}.{self._fw_minor}" if self._fw_major is not None else None
            ),
            "averaging": self._averaging,
            "channel_mask": self._channel_mask,
            "streaming": self._streaming,
            "recording": self._recording,
            "record_path": self._record_path,
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

    def start_stream(self, record: bool = False) -> dict[str, object]:
        with self._lock:
            self._require_connected()
            if not self._streaming:
                self._driver.start_stream()
                self._streaming = True
                if record:
                    path = capture_dir() / adc_capture_filename(self._serial)
                    self._recorder = _Recorder(str(path))
                else:
                    self._recorder = None
                self._record_path = self._recorder.path if self._recorder else None
                self._recording = record
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
        worker = self._worker
        if worker is not None:
            worker.join(timeout=2.0)
            self._worker = None
        self._recording = False
        self._recorder = None

    # -- live stream fan-out -------------------------------------------------

    def subscribe(self) -> asyncio.Queue[Sample]:
        queue: asyncio.Queue[Sample] = asyncio.Queue(maxsize=2000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Sample]) -> None:
        self._subscribers.discard(queue)

    def _stream_worker(self) -> None:
        loop = self._loop
        recorder = self._recorder
        try:
            for sample in self._driver.iter_samples():
                if recorder is not None:
                    recorder.write(sample)
                if loop is not None:
                    loop.call_soon_threadsafe(self._publish, sample)
        except Exception:
            pass
        finally:
            if recorder is not None:
                with suppress(Exception):
                    recorder.close()

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
