"""Binary wire protocol for the 6-channel ADC board.

Pure codec with no I/O: frame encode/decode plus payload builders and parsers.
See docs/superpowers/specs/2026-09-11-adc-board-uart-driver-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

SYNC = b"\xaa\x55"
HEADER_LEN = 5  # sync(2) + type(1) + seq(1) + len(1)
CRC_LEN = 2
MAX_PAYLOAD = 255
MAX_FRAME = HEADER_LEN + MAX_PAYLOAD + CRC_LEN

N_CHANNELS = 6
AVERAGING_CHOICES = (0, 4, 8, 16, 32, 64, 128, 256)
CHANNEL_MASK_ALL = 0b0011_1111  # all six channels


class FrameType(IntEnum):
    SET_AVERAGING = 0x01
    SET_CHANNELS = 0x02
    SET_SAMPLE_MODE = 0x03
    START_STREAM = 0x04
    STOP_STREAM = 0x05
    SAMPLE_ONCE = 0x06
    RESET = 0x07
    IDENTIFY = 0x08
    ARM_TRIGGER = 0x09  # reserved: external trigger not implemented
    ACK = 0x81
    NAK = 0x82
    IDENTIFY_RSP = 0x83
    SAMPLE = 0x90


class ErrorCode(IntEnum):
    BAD_COMMAND = 0x01
    BAD_PARAMETER = 0x02
    BUSY = 0x03
    UNSUPPORTED = 0x04


class SampleMode(IntEnum):
    FREE_RUN = 0
    CYCLE = 1  # reserved


@dataclass(frozen=True)
class Frame:
    type: int
    seq: int
    payload: bytes


@dataclass(frozen=True)
class IdentifyInfo:
    proto_version: int
    fw_major: int
    fw_minor: int
    n_channels: int
    resolution: int


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflect, no xorout."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def encode_frame(frame: Frame) -> bytes:
    if not 0 <= frame.type <= 0xFF:
        raise ValueError(f"type out of range: {frame.type}")
    if not 0 <= frame.seq <= 0xFF:
        raise ValueError(f"seq out of range: {frame.seq}")
    if len(frame.payload) > MAX_PAYLOAD:
        raise ValueError(f"payload too long: {len(frame.payload)}")
    body = bytes((SYNC[0], SYNC[1], frame.type, frame.seq, len(frame.payload))) + frame.payload
    return body + crc16(body[2:]).to_bytes(CRC_LEN, "little")


class FrameParser:
    """Incremental decoder that resynchronises on corrupt bytes or a bad CRC."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.error_count = 0

    def feed(self, data: bytes) -> list[Frame]:
        self._buffer += data
        frames: list[Frame] = []
        while True:
            frame = self._extract()
            if frame is None:
                break
            frames.append(frame)
        return frames

    def _extract(self) -> Frame | None:
        buf = self._buffer
        start = buf.find(SYNC)
        if start < 0:
            keep = 1 if len(buf) > 1 else len(buf)
            self._buffer = buf[-keep:]
            return None
        if start > 0:
            self.error_count += 1
            del buf[:start]
        if len(buf) < HEADER_LEN:
            return None
        length = buf[4]
        total = HEADER_LEN + length + CRC_LEN
        if len(buf) < total:
            return None
        frame_bytes = bytes(buf[:total])
        got_crc = int.from_bytes(frame_bytes[-CRC_LEN:], "little")
        if crc16(frame_bytes[2 : HEADER_LEN + length]) != got_crc:
            self.error_count += 1
            del buf[0]
            return None
        del buf[:total]
        return Frame(
            type=frame_bytes[2], seq=frame_bytes[3], payload=frame_bytes[5 : HEADER_LEN + length]
        )
