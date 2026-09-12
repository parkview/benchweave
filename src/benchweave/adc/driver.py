"""Serial driver for the 6-channel ADC board (master side)."""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol

from benchweave.adc import protocol

DEFAULT_BAUD = 2_000_000


class AdcError(Exception):
    """Base class for ADC driver errors."""


class AdcConnectionError(AdcError):
    """The serial transport is unavailable or faulted."""


class AdcProtocolError(AdcError):
    """The device returned an unexpected or NAK response."""


class AdcTimeout(AdcError):
    """No response arrived within the deadline."""


class AdcNotConnected(AdcError):
    """An operation was attempted before open()."""


class Transport(Protocol):
    timeout: float | None

    @property
    def in_waiting(self) -> int: ...

    def read(self, size: int = 1) -> bytes: ...

    def write(self, data: bytes) -> int | None: ...

    def close(self) -> None: ...

    @property
    def is_open(self) -> bool: ...


@dataclass(frozen=True)
class Sample:
    counter: int
    channels: tuple[int, ...]
    averaged_n: int


class _State(Enum):
    DISCONNECTED = auto()
    CONNECTED = auto()
    CONFIGURED = auto()
    STREAMING = auto()


class AdcDriver:
    """Master driver for the 6-channel ADC board."""

    def __init__(self, transport: Transport | None = None) -> None:
        self._transport = transport
        self._parser = protocol.FrameParser()
        self._state = _State.DISCONNECTED
        self._running = False
        self._faulted = False
        self._reader: threading.Thread | None = None
        self._cmd_lock = threading.Lock()
        self._response_cond = threading.Condition()
        self._pending_response: protocol.Frame | None = None
        self._pending_cmd: protocol.FrameType | None = None
        self._sample_queue: queue.Queue[Sample] = queue.Queue()
        self._seq = 0
        self._averaged_n = 0
        self._timeout = 1.0
        self._awaiting_single = False
        self._single_sample: Sample | None = None
        self._single_event = threading.Event()

    # -- lifecycle ----------------------------------------------------------

    def open(
        self, port: str | None = None, *, baud: int = DEFAULT_BAUD, timeout: float = 1.0
    ) -> None:
        if self._state is not _State.DISCONNECTED:
            raise AdcError("already open")
        if port is not None:
            import serial  # type: ignore[import-untyped]

            self._transport = serial.Serial(port=port, baudrate=baud, timeout=timeout)
        if self._transport is None:
            raise AdcConnectionError("no transport: pass a port or inject a transport")
        self._timeout = 1.0 if timeout is None else timeout
        self._faulted = False
        self._parser = protocol.FrameParser()
        self._pending_response = None
        self._pending_cmd = None
        self._running = True
        self._reader = threading.Thread(target=self._reader_loop, name="adc-reader", daemon=True)
        self._reader.start()
        self._state = _State.CONNECTED

    def close(self) -> None:
        self._running = False
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=2.0)
        if self._transport is not None:
            with contextlib.suppress(Exception):
                self._transport.close()
        self._reader = None
        self._state = _State.DISCONNECTED

    def __enter__(self) -> AdcDriver:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- properties ---------------------------------------------------------

    @property
    def averaging(self) -> int:
        return self._averaged_n

    # -- commands -----------------------------------------------------------

    def identify(self, *, timeout: float | None = None) -> protocol.IdentifyInfo:
        self._require_state(_State.CONNECTED, _State.CONFIGURED)
        resp = self._transact(protocol.FrameType.IDENTIFY, b"", timeout=timeout)
        if resp.type != protocol.FrameType.IDENTIFY_RSP:
            raise AdcProtocolError(f"unexpected response type {resp.type:#x}")
        return protocol.parse_identify(resp.payload)

    def reset(self, *, timeout: float | None = None) -> None:
        self._require_state(_State.CONNECTED, _State.CONFIGURED, _State.STREAMING)
        self._command(protocol.FrameType.RESET, b"", timeout=timeout)
        self._state = _State.CONNECTED

    def set_averaging(self, n: int, *, timeout: float | None = None) -> None:
        if n not in protocol.AVERAGING_CHOICES:
            raise ValueError(f"averaging must be one of {protocol.AVERAGING_CHOICES}")
        self._require_state(_State.CONNECTED, _State.CONFIGURED)
        _, value = self._command(
            protocol.FrameType.SET_AVERAGING, protocol.build_set_averaging(n), timeout=timeout
        )
        self._averaged_n = value
        self._state = _State.CONFIGURED

    def set_channels(self, mask: int, *, timeout: float | None = None) -> None:
        if not 0 <= mask <= protocol.CHANNEL_MASK_ALL:
            raise ValueError(f"channel mask out of range: {mask}")
        self._require_state(_State.CONNECTED, _State.CONFIGURED)
        self._command(
            protocol.FrameType.SET_CHANNELS, protocol.build_set_channels(mask), timeout=timeout
        )
        self._state = _State.CONFIGURED

    def set_sample_mode(self, mode: int, *, timeout: float | None = None) -> None:
        if mode not in (protocol.SampleMode.FREE_RUN, protocol.SampleMode.CYCLE):
            raise ValueError(f"invalid sample mode: {mode}")
        self._require_state(_State.CONNECTED, _State.CONFIGURED)
        self._command(
            protocol.FrameType.SET_SAMPLE_MODE,
            protocol.build_set_sample_mode(mode),
            timeout=timeout,
        )
        self._state = _State.CONFIGURED

    def start_stream(self, *, timeout: float | None = None) -> None:
        self._require_state(_State.CONNECTED, _State.CONFIGURED)
        self._command(protocol.FrameType.START_STREAM, b"", timeout=timeout)
        self._state = _State.STREAMING

    def stop_stream(self, *, timeout: float | None = None) -> None:
        self._require_state(_State.STREAMING)
        self._command(protocol.FrameType.STOP_STREAM, b"", timeout=timeout)
        self._state = _State.CONFIGURED

    def sample_once(self, *, timeout: float | None = None) -> Sample:
        self._require_state(_State.CONNECTED, _State.CONFIGURED)
        self._single_sample = None
        self._awaiting_single = True
        self._single_event.clear()
        try:
            self._transact(protocol.FrameType.SAMPLE_ONCE, b"", timeout=timeout)
            wait = timeout if timeout is not None else self._timeout
            if not self._single_event.wait(wait):
                raise AdcTimeout("timed out waiting for sample")
            assert self._single_sample is not None
            return self._single_sample
        finally:
            self._awaiting_single = False

    def iter_samples(self) -> Iterator[Sample]:
        while self._state is _State.STREAMING:
            try:
                yield self._sample_queue.get(timeout=0.5)
            except queue.Empty:
                continue

    # -- internals ----------------------------------------------------------

    def _require_state(self, *allowed: _State) -> None:
        if self._faulted:
            raise AdcConnectionError("transport faulted")
        if self._state is _State.DISCONNECTED:
            raise AdcNotConnected("not connected: call open() first")
        if self._state not in allowed:
            raise AdcError(f"invalid state {self._state.name} for this operation")

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFF
        return seq

    def _command(
        self, cmd: protocol.FrameType, payload: bytes, *, timeout: float | None = None
    ) -> tuple[int, int]:
        resp = self._transact(cmd, payload, timeout=timeout)
        if resp.type == protocol.FrameType.NAK:
            echo, error = protocol.parse_nak(resp.payload)
            raise AdcProtocolError(f"device NAK for {cmd.name}: error {error:#x}")
        if resp.type != protocol.FrameType.ACK:
            raise AdcProtocolError(f"unexpected response type {resp.type:#x}")
        return protocol.parse_ack(resp.payload)

    def _transact(
        self, cmd: protocol.FrameType, payload: bytes, *, timeout: float | None = None
    ) -> protocol.Frame:
        with self._cmd_lock:
            if self._transport is None:
                raise AdcNotConnected("no transport")
            frame = protocol.Frame(type=int(cmd), seq=self._next_seq(), payload=payload)
            wait = timeout if timeout is not None else self._timeout
            deadline = time.monotonic() + wait
            with self._response_cond:
                self._pending_cmd = cmd
                self._pending_response = None
            self._transport.write(protocol.encode_frame(frame))
            with self._response_cond:
                while self._pending_response is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._pending_cmd = None
                        raise AdcTimeout(f"no response to {cmd.name}")
                    self._response_cond.wait(remaining)
                resp = self._pending_response
                self._pending_response = None
                self._pending_cmd = None
            return resp

    def _reader_loop(self) -> None:
        while self._running:
            try:
                transport = self._transport
                if transport is None or not transport.is_open:
                    break
                n = transport.in_waiting or 1
                data = transport.read(n)
                if not data:
                    continue
                for frame in self._parser.feed(data):
                    self._handle_frame(frame)
            except Exception:
                if self._running:
                    self._faulted = True
                    with self._response_cond:
                        self._response_cond.notify_all()
                break
        self._running = False

    def _matches_pending(self, frame: protocol.Frame) -> bool:
        cmd = self._pending_cmd
        if cmd is None:
            return False
        if cmd is protocol.FrameType.IDENTIFY:
            return frame.type == protocol.FrameType.IDENTIFY_RSP
        if frame.type == protocol.FrameType.IDENTIFY_RSP:
            return False
        # ACK and NAK both carry echo_type as their first payload byte.
        return len(frame.payload) >= 1 and frame.payload[0] == int(cmd)

    def _handle_frame(self, frame: protocol.Frame) -> None:
        if frame.type in (
            protocol.FrameType.ACK,
            protocol.FrameType.NAK,
            protocol.FrameType.IDENTIFY_RSP,
        ):
            with self._response_cond:
                if self._matches_pending(frame):
                    self._pending_response = frame
                    self._response_cond.notify_all()
        elif frame.type == protocol.FrameType.SAMPLE:
            counter, channels = protocol.parse_sample(frame.payload)
            sample = Sample(counter=counter, channels=channels, averaged_n=self._averaged_n)
            if self._state is _State.STREAMING:
                self._sample_queue.put(sample)
            if self._awaiting_single:
                self._single_sample = sample
                self._single_event.set()
