"""Test doubles and shared fixture data for the web API tests."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

from benchweave.web.board import _validate_config
from plugins.adc_6ch_12bit.config import DEFAULT_CONFIG
from plugins.adc_6ch_12bit.driver import Sample

#: A stem matching the ``adc_<serial>[_<tag>]_<YYYYmmdd_HHMMSS>`` naming scheme.
STEM = "adc_1234_test_20260914_120000"

#: The canonical two-row capture CSV (same shape as tests/web/test_library.py).
CSV = (
    "# profile: default\n"
    "# note: test capture\n"
    "# sample_rate_hz: 100.0\n"
    "# A0: Voltage (V)\n"
    "# A1: Current (A)\n"
    "# computed: Power (W) = A0*A1\n"
    "timestamp,elapsed_s,actual_sps,counter,averaged_n,Voltage,Current,Power\n"
    "2026-09-14T12:00:00.000000,0.0,0.0,0,0,1.0,2.0,2.0\n"
    "2026-09-14T12:00:01.000000,1.0,1.0,1,0,1.1,2.2,2.42\n"
)


class FakeBoardManager:
    """Stands in for ``BoardManager``: records calls, returns canned payloads.

    ``set_config`` still runs the real ``_validate_config`` so route tests
    exercise the genuine validation rules end to end. ``subscribe`` hands out
    ``self.queue`` — pre-load it with ``put_nowait`` to drive the SSE endpoint.
    Set ``connect_error`` / ``subscribe_error`` / ``reveal_error`` to make the
    matching method raise ``RuntimeError`` with that message.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.config: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self.queue: asyncio.Queue[Sample] = asyncio.Queue()
        self.unsubscribed: list[asyncio.Queue[Sample]] = []
        self.connect_error: str | None = None
        self.subscribe_error: str | None = None
        self.reveal_error: str | None = None
        self.streaming = False
        self.recording = False
        self.paused = False

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))

    def called(self, name: str) -> list[tuple[object, ...]]:
        """Return the recorded argument tuples for every call to ``name``."""
        return [args for called, args in self.calls if called == name]

    # -- lifecycle (the app lifespan calls set_loop and disconnect) ----------

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._record("set_loop", loop)

    def disconnect(self) -> None:
        self._record("disconnect")

    def discover(self) -> list[dict[str, object]]:
        self._record("discover")
        return [
            {
                "device": "COM7",
                "serial": "SN123",
                "firmware": "0.2",
                "channels": 6,
                "resolution": 12,
            }
        ]

    def connect(self, device: str) -> dict[str, object]:
        self._record("connect", device)
        if self.connect_error is not None:
            raise RuntimeError(self.connect_error)
        return self.status()

    def status(self) -> dict[str, object]:
        return {
            "connected": True,
            "device": "COM7",
            "serial": "SN123",
            "firmware": "0.2",
            "averaging": 0,
            "channel_mask": 0x3F,
            "streaming": self.streaming,
            "paused": self.paused,
            "recording": self.recording,
            "record_path": None,
            "last_error": None,
            "max_sps": 1234.5,
        }

    # -- config ---------------------------------------------------------------

    def get_config(self) -> dict[str, Any]:
        self._record("get_config")
        return self.config

    def set_config(self, config: dict[str, Any]) -> dict[str, Any]:
        _validate_config(config)  # the real rules; raises ValueError like the real manager
        self._record("set_config", config)
        self.config = config
        return self.config

    # -- control ----------------------------------------------------------------

    def set_averaging(self, n: int) -> dict[str, object]:
        self._record("set_averaging", n)
        return self.status()

    def set_channels(self, mask: int) -> dict[str, object]:
        self._record("set_channels", mask)
        return self.status()

    def start_stream(self, record: bool = False, note: str = "") -> dict[str, object]:
        self._record("start_stream", record, note)
        self.streaming = True
        self.recording = record
        self.paused = False
        return self.status()

    def stop_stream(self) -> dict[str, object]:
        self._record("stop_stream")
        self.streaming = False
        self.recording = False
        self.paused = False
        return self.status()

    def pause_stream(self) -> dict[str, object]:
        self._record("pause_stream")
        self.paused = True
        return self.status()

    def resume_stream(self) -> dict[str, object]:
        self._record("resume_stream")
        self.paused = False
        return self.status()

    # -- graph ------------------------------------------------------------------

    def save_graph_png(self, data: bytes) -> dict[str, object]:
        self._record("save_graph_png", data)
        return {"path": f"captures/graph-{len(data)}.png", "name": "graph.png"}

    def reveal_graph_png(self) -> dict[str, object]:
        self._record("reveal_graph_png")
        if self.reveal_error is not None:
            raise RuntimeError(self.reveal_error)
        return {"path": "captures"}

    # -- live stream fan-out ------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[Sample]:
        self._record("subscribe")
        if self.subscribe_error is not None:
            raise RuntimeError(self.subscribe_error)
        return self.queue

    def unsubscribe(self, queue: asyncio.Queue[Sample]) -> None:
        self._record("unsubscribe", queue)
        self.unsubscribed.append(queue)

    def convert_sample(self, sample: Sample) -> list[dict[str, object]]:
        return [
            {
                "key": "A0",
                "name": "Voltage",
                "unit": "V",
                "value": round(sample.channels[0] * 0.001221, 6),
            }
        ]
