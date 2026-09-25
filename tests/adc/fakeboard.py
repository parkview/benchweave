"""A fake ADC board: the firmware side of the wire protocol behind ``Transport``.

Used by tests that drive the full manager/adapter/host stack: commands written
to the "port" are parsed with the real :class:`protocol.FrameParser` and
answered like firmware 0.2 would (ACK/NAK/IDENTIFY_RSP); START_STREAM makes
subsequent ``read`` calls emit SAMPLE frames from a configurable iterator.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Iterator

from plugins.adc_6ch_12bit import protocol

#: proto 1, fw 0.2, 6 channels, 12-bit — the signature discovery expects.
IDENTIFY_PAYLOAD = bytes((1, 0, 2, 6, 12))

SampleTuple = tuple[int, tuple[int, ...]]


def sample_frame(counter: int, channels: tuple[int, ...]) -> bytes:
    """Encode one firmware SAMPLE frame."""
    payload = counter.to_bytes(4, "little") + b"".join(
        value.to_bytes(2, "little") for value in channels
    )
    return protocol.encode_frame(
        protocol.Frame(type=int(protocol.FrameType.SAMPLE), seq=0, payload=payload)
    )


def _ack(command: int, value: int = 0) -> bytes:
    payload = bytes((command,)) + value.to_bytes(2, "little")
    return protocol.encode_frame(
        protocol.Frame(type=int(protocol.FrameType.ACK), seq=0, payload=payload)
    )


def _nak(command: int, error: int) -> bytes:
    payload = bytes((command, error))
    return protocol.encode_frame(
        protocol.Frame(type=int(protocol.FrameType.NAK), seq=0, payload=payload)
    )


class FakeBoardTransport:
    """Implements ``host.Transport`` plus the firmware's command behaviour.

    Reads and writes arrive from different threads (SerialLink's reader vs
    the host loop's executor), so both buffers sit behind one lock. ``read``
    is non-blocking apart from honouring ``timeout`` with a short sleep when
    nothing is pending — SerialLink's reader loop tolerates empty reads.
    """

    timeout: float | None = 0.005

    def __init__(
        self,
        samples: Iterable[SampleTuple] = (),
        *,
        frames_per_read: int = 1,
    ) -> None:
        self._lock = threading.Lock()
        self._parser = protocol.FrameParser()
        self._out = bytearray()
        self._samples: Iterator[SampleTuple] = iter(samples)
        self._frames_per_read = frames_per_read
        self._open = True
        self.streaming = False
        self.averaging = 0
        self.channel_mask = protocol.CHANNEL_MASK_ALL
        self.commands: list[tuple[int, bytes]] = []

    def set_samples(self, samples: Iterable[SampleTuple]) -> None:
        """Swap the stream source (thread-safe)."""
        with self._lock:
            self._samples = iter(samples)

    # -- Transport interface ---------------------------------------------------

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._out)

    @property
    def is_open(self) -> bool:
        return self._open

    def write(self, data: bytes) -> int | None:
        with self._lock:
            for frame in self._parser.feed(bytes(data)):
                self._handle(frame)
        return len(data)

    def read(self, size: int = 1) -> bytes:
        with self._lock:
            if self.streaming:
                for _ in range(self._frames_per_read):
                    entry = next(self._samples, None)
                    if entry is None:
                        break
                    counter, channels = entry
                    self._out += sample_frame(counter, channels)
            if self._out:
                data = bytes(self._out[:size])
                del self._out[:size]
                return data
        time.sleep(self.timeout or 0.001)
        return b""

    def close(self) -> None:
        self._open = False

    # -- firmware behaviour ------------------------------------------------------

    def _handle(self, frame: protocol.Frame) -> None:
        command = frame.type
        self.commands.append((command, frame.payload))
        if command == protocol.FrameType.IDENTIFY:
            self._out += protocol.encode_frame(
                protocol.Frame(
                    type=int(protocol.FrameType.IDENTIFY_RSP),
                    seq=frame.seq,
                    payload=IDENTIFY_PAYLOAD,
                )
            )
        elif command == protocol.FrameType.SET_AVERAGING:
            self.averaging = int.from_bytes(frame.payload[:2], "little")
            self._out += _ack(command, self.averaging)
        elif command == protocol.FrameType.SET_CHANNELS:
            self.channel_mask = frame.payload[0] if frame.payload else 0
            self._out += _ack(command)
        elif command == protocol.FrameType.START_STREAM:
            self.streaming = True
            self._out += _ack(command)
        elif command == protocol.FrameType.STOP_STREAM:
            self.streaming = False
            self._out += _ack(command)
        elif command == protocol.FrameType.SAMPLE_ONCE:
            self._out += _ack(command)
            entry = next(self._samples, None)
            counter, channels = entry if entry is not None else (0, (0, 0, 0, 0, 0, 0))
            self._out += sample_frame(counter, channels)
        elif command == protocol.FrameType.RESET:
            self.streaming = False
            self.averaging = 0
            self._out += _ack(command)
        else:
            self._out += _nak(command, int(protocol.ErrorCode.BAD_COMMAND))


class TransportFactory:
    """Stands in for ``board._open_transport``: one fresh fake board per connect."""

    def __init__(self) -> None:
        self.samples: Iterable[SampleTuple] = ()
        self.frames_per_read = 1
        self.created: list[FakeBoardTransport] = []

    def __call__(self, device: str) -> FakeBoardTransport:
        transport = FakeBoardTransport(self.samples, frames_per_read=self.frames_per_read)
        self.created.append(transport)
        return transport

    @property
    def last(self) -> FakeBoardTransport:
        return self.created[-1]


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll ``predicate`` until it holds, or fail the test after ``timeout``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met within timeout")
