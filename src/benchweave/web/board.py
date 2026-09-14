"""Board manager: owns the AdcDriver, bridges its stream to asyncio, and records to CSV."""

from __future__ import annotations

import asyncio
import csv
import json
import queue
import shutil
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

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

    def resume(self) -> None:
        """Shift the origin so elapsed time excludes the paused gap."""
        if self._last_elapsed is not None:
            self._t0 = time.monotonic() - self._last_elapsed


def _open_file_manager(path: str) -> None:
    """Open the user's file manager at ``path``, selecting it where supported."""
    target = Path(path)
    dolphin = shutil.which("dolphin")
    if dolphin:
        if target.is_file():
            subprocess.Popen([dolphin, "--select", str(target)])
        else:
            subprocess.Popen([dolphin, str(target)])
    else:
        folder = target if target.is_dir() else target.parent
        subprocess.Popen(["xdg-open", str(folder)])


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
        self._paused = False
        self._counter = 0
        self._recording = False
        self._recorder: _Recorder | None = None
        self._record_path: str | None = None
        self._last_png_path: str | None = None

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
            self._streaming = False
            # Re-apply the persisted channel selection to the freshly opened board.
            mask = self._persisted_channel_mask()
            self._driver.set_channels(mask)
            self._channel_mask = mask
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
            "paused": self._paused,
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

    def save_graph_png(self, data: bytes) -> dict[str, object]:
        """Write a PNG chart image to the capture dir, named after the current CSV."""
        if self._record_path:
            name = Path(self._record_path).with_suffix(".png").name
        else:
            name = adc_capture_filename(self._serial, ext="png")
        path = capture_dir() / name
        path.write_bytes(data)
        self._last_png_path = str(path)
        return {"path": str(path), "name": name}

    def reveal_graph_png(self) -> dict[str, object]:
        """Open the file manager at the most recently saved PNG (or the capture dir)."""
        target = self._last_png_path or str(capture_dir())
        _open_file_manager(target)
        return {"path": target}

    def convert_sample(self, sample: Sample) -> list[dict[str, object]]:
        return convert_channels(sample, self._config)

    def _record_meta(self, note: str) -> tuple[list[str], list[str]]:
        profile_name = str(self._config["active_profile"])
        profile = self._config["profiles"][profile_name]
        names: list[str] = []
        metadata = [f"profile: {profile_name}"]
        metadata.append(
            "config: "
            + json.dumps(
                {
                    "active_profile": profile_name,
                    "profile": profile,
                    "settings": self._config.get("settings", {}),
                },
                separators=(",", ":"),
            )
        )
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

    def _persisted_channel_mask(self) -> int:
        """The last saved channel selection, defaulting to all channels."""
        value = self._config.get("settings", {}).get("channel_mask", CHANNEL_MASK_ALL)
        try:
            mask = int(value)
        except (TypeError, ValueError):
            return CHANNEL_MASK_ALL
        return mask if 0 <= mask <= CHANNEL_MASK_ALL else CHANNEL_MASK_ALL

    # -- capture -------------------------------------------------------------

    def sample_once(self) -> dict[str, object]:
        """Read a single sample (in single-shot mode) and return converted values."""
        with self._lock:
            self._require_idle()
            sample = self._driver.sample_once()
            return {
                "counter": sample.counter,
                "averaged_n": sample.averaged_n,
                "channels": self.convert_sample(sample),
            }

    def capture_samples(
        self, count: int, *, tag: str | None = None, timeout: float = 10.0
    ) -> dict[str, object]:
        """Capture exactly ``count`` samples to CSV; return a summary with per-channel stats."""
        return self._capture(count=count, tag=tag, timeout=timeout)

    def capture_seconds(self, seconds: float, *, tag: str | None = None) -> dict[str, object]:
        """Capture ``seconds`` seconds of samples to CSV; return a compact summary."""
        return self._capture(seconds=seconds, tag=tag)

    def _capture(
        self,
        *,
        count: int | None = None,
        seconds: float | None = None,
        tag: str | None = None,
        timeout: float = 10.0,
    ) -> dict[str, object]:
        """Run a bounded capture: stream samples into a CSV and summarize the channels.

        Exactly one of ``count`` (fixed number of samples) or ``seconds`` (a
        duration) must be given. A background thread drains ``iter_samples`` into
        a queue with an end-of-stream sentinel, so a board that stops sending
        never blocks the deadline check on the main thread. The return value is a
        compact summary (path, count, actual rate, per-channel min/mean/max) - the
        full record lives in the CSV, so a long capture never balloons the response.
        """
        with self._lock:
            self._require_idle()
            if (count is None) == (seconds is None):
                raise ValueError("specify exactly one of count or seconds")
            if count is not None and count <= 0:
                raise ValueError("count must be positive")
            if seconds is not None and seconds <= 0:
                raise ValueError("seconds must be positive")

            if count is not None:
                note = f"{tag or 'capture'}: {count} samples"
                duration = float(timeout)
                target: int | None = count
            else:
                assert seconds is not None  # guaranteed by the either/or check above
                note = f"{tag or 'capture'}: {seconds:.3f} s"
                duration = float(seconds)
                target = None

            names, metadata = self._record_meta(note)
            path = capture_dir() / adc_capture_filename(self._serial, tag=tag)
            recorder = _Recorder(str(path), names, metadata)
            inbox: queue.Queue[Sample | None] = queue.Queue()

            def pump() -> None:
                try:
                    for sample in self._driver.iter_samples():
                        inbox.put(sample)
                finally:
                    inbox.put(None)  # end-of-stream sentinel

            self._driver.start_stream()
            self._streaming = True
            self._counter = 0
            worker = threading.Thread(target=pump, name="adc-capture", daemon=True)
            worker.start()

            start = time.monotonic()
            deadline = start + duration
            collected = 0
            mins: dict[str, float] = {}
            maxs: dict[str, float] = {}
            totals: dict[str, float] = {}
            counts: dict[str, int] = {}
            names_by_key: dict[str, str] = {}
            units_by_key: dict[str, str] = {}
            order: list[str] = []
            try:
                while (target is None or collected < target) and time.monotonic() < deadline:
                    try:
                        sample = inbox.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if sample is None:
                        break
                    sample = replace(sample, counter=self._counter)
                    self._counter += 1
                    channels = self.convert_sample(sample)
                    recorder.write(
                        sample.counter,
                        sample.averaged_n,
                        [c["value"] for c in channels],
                    )
                    collected += 1
                    for c in channels:
                        key = str(c["key"])
                        value = cast(float | None, c["value"])
                        if value is None:
                            continue  # a computed channel that failed to evaluate
                        if key not in mins:
                            order.append(key)
                            names_by_key[key] = str(c["name"])
                            units_by_key[key] = str(c["unit"])
                            mins[key] = value
                            maxs[key] = value
                            totals[key] = 0.0
                            counts[key] = 0
                        mins[key] = min(mins[key], value)
                        maxs[key] = max(maxs[key], value)
                        totals[key] += value
                        counts[key] += 1
                if target is not None and collected < target:
                    raise RuntimeError(
                        f"capture timed out: got {collected}/{target} samples in {duration:.1f}s"
                    )
                elapsed = time.monotonic() - start
                channels_summary = [
                    {
                        "key": key,
                        "name": names_by_key[key],
                        "unit": units_by_key[key],
                        "min": round(mins[key], 6),
                        "mean": round(totals[key] / counts[key], 6),
                        "max": round(maxs[key], 6),
                    }
                    for key in order
                ]
                return {
                    "path": str(path),
                    "count": collected,
                    "duration_s": round(elapsed, 3),
                    "samples_per_second": round(collected / elapsed, 1) if elapsed > 0 else 0.0,
                    "channels": channels_summary,
                }
            finally:
                with suppress(Exception):
                    self._driver.stop_stream()
                self._streaming = False
                worker.join(timeout=1.0)
                recorder.close()

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
            self._config.setdefault("settings", {})["channel_mask"] = mask
            save_config(self._config)
        return self.status()

    def start_stream(self, record: bool = False, note: str = "") -> dict[str, object]:
        with self._lock:
            self._require_connected()
            if not self._streaming:
                self._driver.start_stream()
                self._streaming = True
                self._paused = False
                self._counter = 0
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

    def pause_stream(self) -> dict[str, object]:
        """Halt data collection, keeping the CSV file (if any) open."""
        with self._lock:
            self._require_connected()
            if not self._streaming or self._paused:
                return self.status()
            self._paused = True
            with suppress(Exception):
                self._driver.stop_stream()
            self._join_worker()
        return self.status()

    def resume_stream(self) -> dict[str, object]:
        """Resume collection after a pause, reusing the open CSV file."""
        with self._lock:
            self._require_connected()
            if not self._streaming or not self._paused:
                return self.status()
            self._paused = False
            with suppress(Exception):
                self._driver.start_stream()
            if self._recorder is not None:
                self._recorder.resume()
            self._worker = threading.Thread(
                target=self._stream_worker, name="adc-stream", daemon=True
            )
            self._worker.start()
        return self.status()

    def _join_worker(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.join(timeout=2.0)
            self._worker = None

    def _stop_stream_locked(self) -> None:
        if not self._streaming:
            return
        with suppress(Exception):
            self._driver.stop_stream()
        self._streaming = False
        self._paused = False
        self._join_worker()
        self._recording = False
        if self._recorder is not None:
            with suppress(Exception):
                self._recorder.close()
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
        last = 0.0
        try:
            for sample in self._driver.iter_samples():
                now = time.monotonic()
                if interval > 0.0 and now - last < interval:
                    continue
                last = now
                # Count recorded samples (post-decimation) so the graph and CSV row
                # order match, instead of the firmware's board-lifetime sample count.
                # The counter lives on the manager so it survives a pause/resume.
                sample = replace(sample, counter=self._counter)
                self._counter += 1
                if recorder is not None:
                    channels = self.convert_sample(sample)
                    recorder.write(
                        sample.counter, sample.averaged_n, [c["value"] for c in channels]
                    )
                if loop is not None:
                    loop.call_soon_threadsafe(self._publish, sample)
        except Exception:
            pass

    def _publish(self, sample: Sample) -> None:
        for q in self._subscribers:
            with suppress(asyncio.QueueFull):
                q.put_nowait(sample)

    # -- guards --------------------------------------------------------------

    def _require_connected(self) -> None:
        if self._device is None:
            raise RuntimeError("no ADC board connected")

    def _require_idle(self) -> None:
        self._require_connected()
        if self._streaming:
            raise RuntimeError("stop streaming before changing configuration")
