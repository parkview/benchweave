"""Board manager: owns the AdcDriver, bridges its stream to asyncio, and records to CSV."""

from __future__ import annotations

import asyncio
import csv
import threading
import time
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from typing import Any

from plugins.adc_6ch_12bit import (
    CHANNEL_KEYS,
    CHANNEL_MASK_ALL,
    AdcDriver,
    Sample,
    adc_capture_filename,
    capture_dir,
    convert_channels,
    discover_adc_boards,
    estimate_max_sps,
    load_config,
    save_config,
    serial_for_device,
)


class _Recorder:
    """Appends converted samples to a CSV file, with a metadata header."""

    def __init__(
        self,
        path: str,
        column_names: list[str],
        metadata: list[str],
    ) -> None:
        self.path = path
        self._file = open(path, "w", newline="")  # noqa: SIM115 (held open for streaming)
        for line in metadata:
            self._file.write(f"# {line}\n")
        self._writer = csv.writer(self._file)
        self._writer.writerow(
            ["timestamp", "elapsed_s", "actual_sps", "counter", "averaged_n", *column_names]
        )
        self._t0 = time.monotonic()
        self._last_elapsed: float | None = None

    def write(self, counter: int, averaged_n: int, values: list[object]) -> None:
        elapsed = time.monotonic() - self._t0
        if self._last_elapsed is None:
            sps = 0.0
        else:
            delta = elapsed - self._last_elapsed
            sps = round(1.0 / delta, 3) if delta > 0.0 else 0.0
        self._last_elapsed = elapsed
        self._writer.writerow(
            [
                datetime.now().isoformat(timespec="microseconds"),
                round(elapsed, 6),
                sps,
                counter,
                averaged_n,
                *values,
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
        self._config = load_config()
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
            self._close_locked()
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
            self._close_locked()

    def _close_locked(self) -> None:
        """Close any existing connection (idempotent). Caller holds the lock."""
        self._stop_stream_locked()
        with suppress(Exception):
            self._driver.close()
        self._device = None
        self._serial = ""
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
            "max_sps": round(estimate_max_sps(self._averaging, self._channel_mask.bit_count()), 1),
        }

    # -- config --------------------------------------------------------------

    def get_config(self) -> dict[str, Any]:
        return self._config

    def set_config(self, config: dict[str, Any]) -> dict[str, Any]:
        self._config = config
        save_config(config)
        return self._config

    def convert_sample(self, sample: Sample) -> list[dict[str, object]]:
        return convert_channels(sample, self._config)

    def _record_meta(self, note: str) -> tuple[list[str], list[str]]:
        profile_name = str(self._config["active_profile"])
        profile = self._config["profiles"][profile_name]
        names: list[str] = []
        metadata = [f"profile: {profile_name}"]
        if note:
            metadata.append(f"note: {note}")
        rate = self._config.get("settings", {}).get("sample_rate_hz")
        if rate:
            metadata.append(f"sample_rate_hz: {rate}")
        for key in CHANNEL_KEYS:
            ch = profile["channels"][key]
            if not ch.get("show", True):
                continue
            names.append(str(ch["name"]))
            metadata.append(f"{key}: {ch['name']} ({ch['unit']})")
        for comp in profile.get("computed", []):
            if not comp.get("show", True):
                continue
            names.append(str(comp["name"]))
            metadata.append(f"computed: {comp['name']} ({comp['unit']}) = {comp['expr']}")
        return names, metadata

    def _record_interval(self) -> float:
        rate = self._config.get("settings", {}).get("sample_rate_hz")
        if not rate:
            return 0.0
        return 1.0 / float(rate)

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

    def start_stream(self, record: bool = False, note: str = "") -> dict[str, object]:
        with self._lock:
            self._require_connected()
            if not self._streaming:
                self._driver.start_stream()
                self._streaming = True
                if record:
                    path = capture_dir() / adc_capture_filename(self._serial)
                    names, metadata = self._record_meta(note)
                    self._recorder = _Recorder(str(path), names, metadata)
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
        interval = self._record_interval()  # seconds between samples; 0 = every sample
        counter = 0
        last = 0.0
        try:
            for sample in self._driver.iter_samples():
                now = time.monotonic()
                if interval > 0.0 and now - last < interval:
                    continue
                last = now
                # Count recorded samples (post-decimation) so the graph and CSV row
                # order match, instead of the firmware's board-lifetime sample count.
                sample = replace(sample, counter=counter)
                counter += 1
                if recorder is not None:
                    channels = self.convert_sample(sample)
                    recorder.write(
                        sample.counter, sample.averaged_n, [c["value"] for c in channels]
                    )
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
