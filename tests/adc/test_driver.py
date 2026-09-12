from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from benchweave.adc.driver import (
    AdcDriver,
    AdcNotConnected,
    AdcProtocolError,
    AdcTimeout,
)
from benchweave.adc.protocol import Frame, FrameParser, FrameType, IdentifyInfo, encode_frame


class FakeTransport:
    """In-memory loopback that satisfies the Transport protocol."""

    def __init__(self, timeout: float = 0.05) -> None:
        self.timeout: float | None = timeout
        self._inbound = bytearray()
        self._outbound = bytearray()
        self._open = True
        self.responder: Callable[[bytes], None] | None = None

    @property
    def in_waiting(self) -> int:
        return len(self._inbound)

    @property
    def is_open(self) -> bool:
        return self._open

    def read(self, size: int = 1) -> bytes:
        if not self._inbound:
            time.sleep(0.001)
            return b""
        n = min(size, len(self._inbound))
        data = bytes(self._inbound[:n])
        del self._inbound[:n]
        return data

    def write(self, data: bytes) -> int:
        self._outbound += data
        if self.responder is not None:
            self.responder(data)
        return len(data)

    def close(self) -> None:
        self._open = False

    def push(self, data: bytes) -> None:
        self._inbound += data


def ack(cmd: int, value: int = 0) -> bytes:
    return encode_frame(
        Frame(type=FrameType.ACK, seq=0, payload=bytes([cmd]) + value.to_bytes(2, "little"))
    )


def test_identify() -> None:
    fake = FakeTransport()

    def respond(data: bytes) -> None:
        for f in FrameParser().feed(data):
            if f.type == FrameType.IDENTIFY:
                fake.push(encode_frame(Frame(FrameType.IDENTIFY_RSP, 0, bytes([1, 0, 2, 6, 12]))))

    fake.responder = respond
    driver = AdcDriver(transport=fake)
    driver.open()
    assert driver.identify() == IdentifyInfo(1, 0, 2, 6, 12)
    driver.close()


def test_set_averaging_updates_readback() -> None:
    fake = FakeTransport()

    def respond(data: bytes) -> None:
        for f in FrameParser().feed(data):
            if f.type == FrameType.SET_AVERAGING:
                n = int.from_bytes(f.payload[:2], "little")
                fake.push(ack(FrameType.SET_AVERAGING, n))

    fake.responder = respond
    driver = AdcDriver(transport=fake)
    driver.open()
    driver.set_averaging(64)
    assert driver.averaging == 64
    with pytest.raises(ValueError):
        driver.set_averaging(3)
    driver.close()


def test_nak_raises_protocol_error() -> None:
    fake = FakeTransport()

    def respond(data: bytes) -> None:
        for f in FrameParser().feed(data):
            fake.push(encode_frame(Frame(FrameType.NAK, 0, bytes([f.type, 0x02]))))

    fake.responder = respond
    driver = AdcDriver(transport=fake)
    driver.open()
    with pytest.raises(AdcProtocolError):
        driver.set_channels(0x3F)
    driver.close()


def test_command_timeout() -> None:
    fake = FakeTransport()  # no responder -> no response ever
    driver = AdcDriver(transport=fake)
    driver.open()
    with pytest.raises(AdcTimeout):
        driver.identify(timeout=0.05)
    driver.close()


def test_requires_open() -> None:
    driver = AdcDriver(transport=FakeTransport())
    with pytest.raises(AdcNotConnected):
        driver.identify()
