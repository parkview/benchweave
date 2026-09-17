"""Board manager: drives the ADC board through its OTDP adapter, records to CSV.

The manager keeps the synchronous public API the web app and MCP server call,
but everything device-shaped now flows through the SDK stack: a
``serial.Serial`` port wrapped in :class:`~benchweave.web.host.SerialLink`,
:class:`~benchweave.web.host.SerialHostServices` implementing the host side of
the contract, and the plugin's :class:`~plugins.adc_6ch_12bit.adapter.AdcAdapter`
owning protocol semantics.

Threading model: the adapter is async and single-session, so the manager runs
a dedicated HOST event loop on its own thread (started lazily on first
connect). Sync methods submit coroutines with ``run_coroutine_threadsafe`` and
block on the result; the adapter and its coroutines are only ever touched from
that loop. Live samples are pumped by a long-running coroutine on the host
loop and fanned out to SSE subscriber queues living on the WEB app's loop
(``set_loop``) via ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import csv
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Coroutine
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar, cast

import serial  # type: ignore[import-untyped]

from benchweave.web.host import (
    AdcOperationContext,
    SerialHostServices,
    SerialLink,
    Transport,
)
from plugins import adc_6ch_12bit as _plugin_pkg
from plugins.adc_6ch_12bit import (
    AVERAGING_CHOICES,
    CHANNEL_IDS,
    CHANNEL_KEYS,
    CHANNEL_MASK_ALL,
    AdcAdapter,
    Sample,
    adc_capture_filename,
    capture_dir,
    convert_channels,
    create_plugin,
    discover_adc_boards,
    estimate_max_sps,
    evaluate_expr,
    load_config,
    output_channels,
    save_config,
    serial_for_device,
)

_LOG = logging.getLogger(__name__)

_T = TypeVar("_T")

#: Live SSE subscribers each hold a 2000-deep queue; cap how many exist.
MAX_SUBSCRIBERS = 32

#: The board's fixed UART rate (scripts may override before connecting).
DEFAULT_BAUD = 2_000_000

#: The plugin's OTDP descriptor, loaded once: operation timeouts and identity.
_DESCRIPTOR_PATH = Path(_plugin_pkg.__file__).resolve().parent / "descriptor.json"
_DESCRIPTOR: dict[str, Any] = json.loads(_DESCRIPTOR_PATH.read_text(encoding="utf-8"))


def _operation_timeout(verb: str, fallback: float) -> float:
    try:
        return float(_DESCRIPTOR["operations"][verb]["timeout_ms"]) / 1000.0
    except (KeyError, TypeError, ValueError):
        return fallback


_IDENTIFY_TIMEOUT_S = _operation_timeout("identify", 2.0)
_INVOKE_TIMEOUT_S = _operation_timeout("invoke", 30.0)
#: Extra slack the sync caller grants the host loop beyond an op's deadline.
_SUBMIT_MARGIN_S = 5.0
#: An open-ended live stream: one arm covers a day, the pump context likewise.
_STREAM_MAX_DURATION_MS = 24 * 60 * 60 * 1000
_PUMP_DEADLINE_S = 24 * 60 * 60.0
#: How long a single-shot acquisition waits for its sample frame.
_SINGLE_SAMPLE_TIMEOUT_S = 2.0
#: "Unbounded" sample budget for streaming configurations.
_STREAM_SAMPLE_COUNT = 1_000_000

_ACTION_CONFIGURE = "otdp.daq.configure/1.0.0"
_ACTION_ARM = "otdp.daq.arm/1.0.0"
_ACTION_TRIGGER = "otdp.daq.trigger/1.0.0"
_ACTION_ABORT = "otdp.daq.abort/1.0.0"


def _open_transport(device: str) -> Transport:
    """Open the board's serial port (module-level so tests can inject a fake)."""
    return cast(Transport, serial.Serial(device, DEFAULT_BAUD, timeout=0.05))


def _parse_firmware(value: object) -> tuple[int | None, int | None]:
    """Split the adapter's ``"major.minor"`` firmware string back into ints."""
    major_s, _, minor_s = str(value or "").partition(".")
    try:
        return int(major_s), int(minor_s)
    except ValueError:
        return None, None


def _sample_from_event(event: dict[str, Any]) -> Sample:
    """Rebuild the full-frame :class:`Sample` from an event's x-adc-sample."""
    raw = cast(dict[str, Any], event.get("x-adc-sample") or {})
    return Sample(
        counter=int(raw.get("counter", 0)),
        channels=tuple(int(value) for value in raw.get("channels", ())),
        averaged_n=int(raw.get("averaged_n", 0)),
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
        # noqa: SIM115 — held open for streaming writes
        self._file = open(path, "w", newline="", encoding="utf-8")  # noqa: SIM115
        try:
            for line in metadata:
                self._file.write(f"# {line}\n")
            self._writer = csv.writer(self._file)
            self._writer.writerow(
                ["timestamp", "elapsed_s", "actual_sps", "counter", "averaged_n", *column_names]
            )
        except BaseException:
            # A failed header write must not leak the open handle.
            self._file.close()
            raise
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
    if sys.platform == "win32":
        if target.is_file():
            subprocess.Popen(["explorer", "/select,", str(target)])
        else:
            subprocess.Popen(["explorer", str(target)])
        return
    if sys.platform == "darwin":
        if target.is_file():
            subprocess.Popen(["open", "-R", str(target)])
        else:
            subprocess.Popen(["open", str(target if target.is_dir() else target.parent)])
        return
    dolphin = shutil.which("dolphin")
    if dolphin:
        if target.is_file():
            subprocess.Popen([dolphin, "--select", str(target)])
        else:
            subprocess.Popen([dolphin, str(target)])
        return
    folder = target if target.is_dir() else target.parent
    opener = shutil.which("xdg-open")
    if opener is None:
        raise RuntimeError("no file manager available (install a desktop opener)")
    subprocess.Popen([opener, str(folder)])


class BoardManager:
    """Owns one ADC board: lifecycle, control, live fan-out, and optional CSV recording."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None  # the WEB app's loop
        self._host_loop: asyncio.AbstractEventLoop | None = None
        self._host_thread: threading.Thread | None = None
        self._adapter: AdcAdapter | None = None
        self._link: SerialLink | None = None
        self._subscribers: set[asyncio.Queue[Sample]] = set()
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
        self._last_error: str | None = None
        self._id_counter = 0
        self._stream_config_id: str | None = None
        self._acquisition_id: str | None = None
        self._pump_context: AdcOperationContext | None = None
        self._pump_future: concurrent.futures.Future[None] | None = None

    # -- host loop plumbing ----------------------------------------------------

    def _ensure_host_loop(self) -> asyncio.AbstractEventLoop:
        """The dedicated host event loop, started lazily on first use."""
        if self._host_loop is not None:
            return self._host_loop
        loop = asyncio.new_event_loop()
        started = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(started.set)
            loop.run_forever()

        thread = threading.Thread(target=run, name="adc-host", daemon=True)
        thread.start()
        started.wait(timeout=5.0)
        self._host_loop = loop
        self._host_thread = thread
        return loop

    def _submit(self, coro: Coroutine[Any, Any, _T], timeout: float) -> _T:
        """Run a coroutine on the host loop; block the caller for its result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._ensure_host_loop())
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise RuntimeError("board operation timed out on the host loop") from None

    def _next_id(self, prefix: str) -> str:
        self._id_counter += 1
        return f"{prefix}-{self._id_counter}"

    def _context(self, operation: str, timeout_s: float) -> AdcOperationContext:
        return AdcOperationContext(
            operation_id=self._next_id(operation),
            deadline_monotonic=time.monotonic() + timeout_s,
        )

    def _require_adapter(self) -> AdcAdapter:
        if self._adapter is None:
            raise RuntimeError("no ADC board connected")
        return self._adapter

    def _run_operation(
        self, verb: str, arguments: dict[str, Any], timeout_s: float
    ) -> dict[str, Any]:
        """Execute one adapter operation; unwrap the envelope or raise."""
        adapter = self._require_adapter()
        context = self._context(verb, timeout_s)
        request = {
            "operation_id": context.operation_id,
            "verb": verb,
            "arguments": arguments,
        }
        result = self._submit(adapter.execute(request, context), timeout_s + _SUBMIT_MARGIN_S)
        if result.get("status") != "ok":
            error = cast(dict[str, Any], result.get("error") or {})
            code = error.get("code", "ERROR")
            message = error.get("message", "operation failed")
            raise RuntimeError(f"{code}: {message}")
        return cast(dict[str, Any], result.get("data") or {})

    def _invoke(self, action_id: str, action_input: dict[str, Any]) -> dict[str, Any]:
        data = self._run_operation(
            "invoke", {"action_id": action_id, "input": action_input}, _INVOKE_TIMEOUT_S
        )
        return cast(dict[str, Any], data.get("result") or {})

    def _configure_stream_locked(self, averaging: int, mask: int) -> str:
        """Express averaging + channel mask through ``otdp.daq.configure``.

        The configure input schema is closed (no raw averaging knob), so the
        mapping is INVERTED: requesting ``sample_rate_hz =
        estimate_max_sps(averaging, n_channels)`` makes the adapter's
        nearest-averaging search land on exactly ``averaging`` — the same
        table drives both sides, so the round trip is exact by construction.
        """
        channels = [cid for index, cid in enumerate(CHANNEL_IDS) if mask & (1 << index)]
        if not channels:
            raise ValueError("channel mask must select at least one channel")
        configuration_id = self._next_id("cfg")
        self._invoke(
            _ACTION_CONFIGURE,
            {
                "configuration_id": configuration_id,
                "channels": [
                    {
                        "channel": cid,
                        "quantity": "voltage",
                        "unit": "V",
                        "range": {"mode": "auto"},
                    }
                    for cid in channels
                ],
                "sample_rate_hz": estimate_max_sps(averaging, len(channels)),
                "sample_count": _STREAM_SAMPLE_COUNT,
                "sampling": "simultaneous",
                "trigger": {"kind": "immediate"},
            },
        )
        self._stream_config_id = configuration_id
        return configuration_id

    # -- lifecycle -----------------------------------------------------------

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def discover(self) -> list[dict[str, object]]:
        # Under the lock: probing candidate ports while a capture is running
        # would disturb the active serial connection.
        with self._lock:
            return self._discover_locked()

    def _discover_locked(self) -> list[dict[str, object]]:
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
            transport = _open_transport(device)
            self._link = SerialLink(transport)
            services = SerialHostServices(self._link, artifact_dir=capture_dir())
            adapter = create_plugin()
            self._adapter = adapter
            try:
                self._submit(
                    adapter.open(_DESCRIPTOR, services, self._context("open", _IDENTIFY_TIMEOUT_S)),
                    _IDENTIFY_TIMEOUT_S + _SUBMIT_MARGIN_S,
                )
                info = self._run_operation("identify", {}, _IDENTIFY_TIMEOUT_S)
                self._fw_major, self._fw_minor = _parse_firmware(info.get("firmware"))
                self._device = device
                self._serial = serial_for_device(device)
                self._averaging = 0
                self._streaming = False
                self._last_error = None
                # Re-apply the persisted channel selection to the freshly
                # opened board (the configure invoke also pins averaging 0,
                # so the device matches the state the manager reports).
                mask = self._persisted_channel_mask() or CHANNEL_MASK_ALL
                self._configure_stream_locked(self._averaging, mask)
                self._channel_mask = mask
            except BaseException:
                self._close_locked()
                raise
        return self.status()

    def disconnect(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        """Close any existing session (idempotent). Caller holds the lock."""
        self._stop_stream_locked()
        adapter = self._adapter
        link = self._link
        self._adapter = None
        self._link = None
        if adapter is not None:
            with suppress(Exception):
                self._submit(
                    adapter.close(self._context("close", _IDENTIFY_TIMEOUT_S)),
                    _IDENTIFY_TIMEOUT_S + _SUBMIT_MARGIN_S,
                )
        if link is not None:
            # adapter.close already closed the transport via the services;
            # belt-and-braces for a session that failed before open() bound them.
            with suppress(Exception):
                link.close()
        self._stream_config_id = None
        self._acquisition_id = None
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
            "last_error": self._last_error,
            "max_sps": round(estimate_max_sps(self._averaging, self._channel_mask.bit_count()), 1),
        }

    # -- config --------------------------------------------------------------

    def get_config(self) -> dict[str, Any]:
        return self._config

    def set_config(self, config: dict[str, Any]) -> dict[str, Any]:
        _validate_config(config)
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
            metadata.append(f"{key}: {ch['name']} ({ch['unit']})")
        for comp in profile.get("computed", []):
            if not comp.get("show", True):
                continue
            metadata.append(f"computed: {comp['name']} ({comp['unit']}) = {comp['expr']}")
        # Column names are deduplicated (not the metadata above), so a profile that
        # reuses a label across a raw and a computed channel still yields a CSV with
        # unique columns.
        names = [c["name"] for c in output_channels(self._config)]
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
        """Read a single sample (single-shot acquisition) and return converted values."""
        with self._lock:
            self._require_idle()
            n_channels = max(self._channel_mask.bit_count(), 1)
            configuration_id = self._next_id("cfg")
            acquisition_id = self._next_id("acq")
            self._invoke(
                _ACTION_CONFIGURE,
                {
                    "configuration_id": configuration_id,
                    "channels": [
                        {
                            "channel": cid,
                            "quantity": "voltage",
                            "unit": "V",
                            "range": {"mode": "auto"},
                        }
                        for index, cid in enumerate(CHANNEL_IDS)
                        if self._channel_mask & (1 << index)
                    ]
                    or [
                        {
                            "channel": cid,
                            "quantity": "voltage",
                            "unit": "V",
                            "range": {"mode": "auto"},
                        }
                        for cid in CHANNEL_IDS
                    ],
                    "sample_rate_hz": estimate_max_sps(self._averaging, n_channels),
                    "sample_count": 1,
                    "sampling": "simultaneous",
                    "trigger": {"kind": "software"},
                },
            )
            self._invoke(
                _ACTION_ARM,
                {
                    "configuration_id": configuration_id,
                    "acquisition_id": acquisition_id,
                    "max_duration_ms": 10_000,
                },
            )
            self._invoke(_ACTION_TRIGGER, {"acquisition_id": acquisition_id})
            try:
                context = self._context("single", _SINGLE_SAMPLE_TIMEOUT_S)
                event = self._submit(
                    self._await_sample_event(acquisition_id, context),
                    _SINGLE_SAMPLE_TIMEOUT_S + _SUBMIT_MARGIN_S,
                )
            finally:
                with suppress(Exception):
                    self._invoke(_ACTION_ABORT, {"acquisition_id": acquisition_id})
            if event is None:
                raise RuntimeError("timed out waiting for sample")
            sample = _sample_from_event(event)
            return {
                "counter": sample.counter,
                "averaged_n": sample.averaged_n,
                "channels": self.convert_sample(sample),
            }

    async def _await_sample_event(
        self, acquisition_id: str, context: AdcOperationContext
    ) -> dict[str, Any] | None:
        """Drain ``next_event`` until a sample arrives or the deadline passes."""
        adapter = self._require_adapter()
        while not context.is_cancelled() and time.monotonic() < context.deadline_monotonic:
            try:
                event = await adapter.next_event(acquisition_id, context)
            except (TimeoutError, ConnectionError):
                return None
            if event is not None:
                return event
        return None

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
        duration) must be given. The whole capture is one coroutine on the
        host loop — arm an immediate acquisition, drain ``next_event`` until
        the target or the deadline, abort — with the calling thread blocked on
        its result. The return value is a compact summary (path, count, actual
        rate, per-channel min/mean/max) - the full record lives in the CSV, so
        a long capture never balloons the response.
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
            col_names = {c["key"]: c["name"] for c in output_channels(self._config)}
            path = capture_dir() / adc_capture_filename(self._serial, tag=tag)
            recorder = _Recorder(str(path), names, metadata)

            configuration_id = self._stream_config_id
            acquisition_id = self._next_id("acq")
            try:
                if configuration_id is None:
                    configuration_id = self._configure_stream_locked(
                        self._averaging, self._channel_mask
                    )
                self._invoke(
                    _ACTION_ARM,
                    {
                        "configuration_id": configuration_id,
                        "acquisition_id": acquisition_id,
                        "max_duration_ms": max(int(duration * 1000) + 1000, 1000),
                    },
                )
            except BaseException:
                recorder.close()
                raise
            self._streaming = True
            self._counter = 0
            context = AdcOperationContext(
                operation_id=self._next_id("capture"),
                deadline_monotonic=time.monotonic() + duration + 30.0,
            )
            try:
                return self._submit(
                    self._capture_pump(
                        acquisition_id, target, duration, str(path), recorder, col_names, context
                    ),
                    duration + 30.0 + _SUBMIT_MARGIN_S,
                )
            finally:
                context.cancel()
                with suppress(Exception):
                    self._invoke(_ACTION_ABORT, {"acquisition_id": acquisition_id})
                self._streaming = False
                recorder.close()

    async def _capture_pump(
        self,
        acquisition_id: str,
        target: int | None,
        duration: float,
        path: str,
        recorder: _Recorder,
        col_names: dict[str, str],
        context: AdcOperationContext,
    ) -> dict[str, object]:
        """The bounded capture loop; runs on the host loop."""
        adapter = self._require_adapter()
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
        while (target is None or collected < target) and time.monotonic() < deadline:
            event = await adapter.next_event(acquisition_id, context)
            if event is None:
                continue
            # The x-adc-sample carries the FIRMWARE counter; re-stamp with the
            # manager counter so CSV rows count recorded samples from zero.
            sample = replace(_sample_from_event(event), counter=self._counter)
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
                    names_by_key[key] = col_names.get(key, str(c["name"]))
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
            "path": path,
            "count": collected,
            "duration_s": round(elapsed, 3),
            "samples_per_second": round(collected / elapsed, 1) if elapsed > 0 else 0.0,
            "channels": channels_summary,
        }

    # -- control -------------------------------------------------------------

    def set_averaging(self, n: int) -> dict[str, object]:
        with self._lock:
            self._require_idle()
            if n not in AVERAGING_CHOICES:
                raise ValueError(f"averaging must be one of {AVERAGING_CHOICES}")
            self._configure_stream_locked(n, self._channel_mask)
            self._averaging = n
        return self.status()

    def set_channels(self, mask: int) -> dict[str, object]:
        with self._lock:
            self._require_idle()
            if not 0 <= mask <= CHANNEL_MASK_ALL:
                raise ValueError(f"channel mask out of range: {mask}")
            self._configure_stream_locked(self._averaging, mask)
            self._channel_mask = mask
            self._config.setdefault("settings", {})["channel_mask"] = mask
            save_config(self._config)
        return self.status()

    def start_stream(self, record: bool = False, note: str = "") -> dict[str, object]:
        with self._lock:
            self._require_connected()
            if not self._streaming:
                configuration_id = self._stream_config_id
                if configuration_id is None:
                    configuration_id = self._configure_stream_locked(
                        self._averaging, self._channel_mask
                    )
                acquisition_id = self._next_id("acq")
                self._invoke(
                    _ACTION_ARM,
                    {
                        "configuration_id": configuration_id,
                        "acquisition_id": acquisition_id,
                        "max_duration_ms": _STREAM_MAX_DURATION_MS,
                    },
                )
                self._acquisition_id = acquisition_id
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
                self._start_pump_locked(acquisition_id)
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
            self._stop_pump_locked()
            if self._acquisition_id is not None:
                with suppress(Exception):
                    self._invoke(_ACTION_ABORT, {"acquisition_id": self._acquisition_id})
                self._acquisition_id = None
        return self.status()

    def resume_stream(self) -> dict[str, object]:
        """Resume collection after a pause: fresh acquisition, same CSV file."""
        with self._lock:
            self._require_connected()
            if not self._streaming or not self._paused:
                return self.status()
            self._paused = False
            acquisition_id = self._next_id("acq")
            with suppress(Exception):
                configuration_id = self._stream_config_id
                if configuration_id is None:
                    configuration_id = self._configure_stream_locked(
                        self._averaging, self._channel_mask
                    )
                self._invoke(
                    _ACTION_ARM,
                    {
                        "configuration_id": configuration_id,
                        "acquisition_id": acquisition_id,
                        "max_duration_ms": _STREAM_MAX_DURATION_MS,
                    },
                )
                self._acquisition_id = acquisition_id
            if self._recorder is not None:
                self._recorder.resume()
            self._start_pump_locked(acquisition_id)
        return self.status()

    def _stop_stream_locked(self) -> None:
        if not self._streaming:
            return
        self._stop_pump_locked()
        if self._acquisition_id is not None:
            with suppress(Exception):
                self._invoke(_ACTION_ABORT, {"acquisition_id": self._acquisition_id})
            self._acquisition_id = None
        self._streaming = False
        self._paused = False
        self._recording = False
        if self._recorder is not None:
            with suppress(Exception):
                self._recorder.close()
            self._recorder = None

    # -- live stream pump ------------------------------------------------------

    def _start_pump_locked(self, acquisition_id: str) -> None:
        context = AdcOperationContext(
            operation_id=self._next_id("pump"),
            deadline_monotonic=time.monotonic() + _PUMP_DEADLINE_S,
        )
        self._pump_context = context
        self._pump_future = asyncio.run_coroutine_threadsafe(
            self._stream_pump(acquisition_id, context), self._ensure_host_loop()
        )

    def _stop_pump_locked(self) -> None:
        """Cancel the pump and wait for it to drain off the host loop."""
        context = self._pump_context
        future = self._pump_future
        self._pump_context = None
        self._pump_future = None
        if context is not None:
            context.cancel()
        if future is not None:
            with suppress(Exception):
                future.result(timeout=2.0)
            future.cancel()

    async def _stream_pump(self, acquisition_id: str, context: AdcOperationContext) -> None:
        """Long-running sample pump; runs on the host loop until cancelled."""
        adapter = self._adapter
        if adapter is None:
            return
        recorder = self._recorder
        web_loop = self._loop
        interval = self._record_interval()  # seconds between samples; 0 = every sample
        last = 0.0
        try:
            while not context.is_cancelled():
                event = await adapter.next_event(acquisition_id, context)
                if context.is_cancelled():
                    break
                if event is None:
                    continue
                now = time.monotonic()
                if interval > 0.0 and now - last < interval:
                    continue
                last = now
                # Count recorded samples (post-decimation) so the graph and CSV row
                # order match, instead of the firmware's board-lifetime sample count.
                # The counter lives on the manager so it survives a pause/resume.
                sample = replace(_sample_from_event(event), counter=self._counter)
                self._counter += 1
                if recorder is not None:
                    channels = self.convert_sample(sample)
                    recorder.write(
                        sample.counter, sample.averaged_n, [c["value"] for c in channels]
                    )
                if web_loop is not None:
                    # The SSE queues live on the web app's loop, not the host loop.
                    web_loop.call_soon_threadsafe(self._publish, sample)
        except Exception as exc:
            if context.is_cancelled():
                return  # a cancelled pump surfacing as TimeoutError is a clean stop
            # A transport fault or CSV write failure must not vanish while
            # status() keeps claiming streaming: log it, surface it, and stop
            # claiming a stream that is no longer running.
            _LOG.exception("stream pump stopped on error")
            self._last_error = f"stream stopped: {exc}"
            self._streaming = False
            self._paused = False

    # -- live stream fan-out -------------------------------------------------

    def subscribe(self) -> asyncio.Queue[Sample]:
        if len(self._subscribers) >= MAX_SUBSCRIBERS:
            raise RuntimeError("too many live-stream subscribers")
        queue: asyncio.Queue[Sample] = asyncio.Queue(maxsize=2000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Sample]) -> None:
        self._subscribers.discard(queue)

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


def _validate_config(config: dict[str, Any]) -> None:
    """Reject configs that would break recording or conversion later.

    ``PUT /api/config`` used to persist anything, and the first recording
    then died on a KeyError in ``_record_meta``. Validation failures raise
    ``ValueError`` (surfaced as HTTP 422).
    """
    profiles = config.get("profiles")
    active = config.get("active_profile")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("config needs a non-empty 'profiles' mapping")
    if not isinstance(active, str) or active not in profiles:
        raise ValueError("'active_profile' must name one of the profiles")
    for profile_name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise ValueError(f"profile '{profile_name}' must be an object")
        channels = profile.get("channels")
        if not isinstance(channels, dict):
            raise ValueError(f"profile '{profile_name}' needs a 'channels' mapping")
        for key in CHANNEL_KEYS:
            channel = channels.get(key)
            if not isinstance(channel, dict):
                raise ValueError(f"profile '{profile_name}' is missing channel '{key}'")
            if not isinstance(channel.get("name"), str) or not isinstance(channel.get("unit"), str):
                raise ValueError(f"channel '{key}' needs string 'name' and 'unit'")
            for field in ("gain", "offset"):
                value = channel.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"channel '{key}' needs a numeric '{field}'")
        computed = profile.get("computed", [])
        if not isinstance(computed, list):
            raise ValueError(f"profile '{profile_name}' 'computed' must be a list")
        for comp in computed:
            if (
                not isinstance(comp, dict)
                or not isinstance(comp.get("name"), str)
                or not isinstance(comp.get("unit"), str)
                or not isinstance(comp.get("expr"), str)
            ):
                raise ValueError("computed channels need string 'name', 'unit' and 'expr'")
            try:
                evaluate_expr(comp["expr"], {key: 0.0 for key in CHANNEL_KEYS})
            except ZeroDivisionError:
                pass  # structurally valid; zeros in the probe divided
            except (ValueError, SyntaxError) as exc:
                raise ValueError(f"computed channel '{comp['name']}': invalid expression") from exc
    settings = config.get("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("'settings' must be an object")
    rate = settings.get("sample_rate_hz")
    if rate is not None and (
        isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate <= 0
    ):
        raise ValueError("'settings.sample_rate_hz' must be a positive number or null")
