from __future__ import annotations

import pytest

from benchweave.adc.protocol import Frame, FrameParser, FrameType, crc16, encode_frame


def test_crc16_check_vector() -> None:
    assert crc16(b"123456789") == 0x29B1


def test_encode_decode_round_trip() -> None:
    frame = Frame(type=FrameType.SAMPLE, seq=7, payload=bytes(range(16)))
    assert FrameParser().feed(encode_frame(frame)) == [frame]


def test_parser_survives_split_input() -> None:
    frame = Frame(type=FrameType.ACK, seq=0, payload=b"\x01\x02\x03")
    raw = encode_frame(frame)
    parser = FrameParser()
    out: list[Frame] = []
    for byte in raw:
        out += parser.feed(bytes([byte]))
    assert out == [frame]


def test_encode_rejects_long_payload() -> None:
    with pytest.raises(ValueError):
        encode_frame(Frame(type=FrameType.SAMPLE, seq=0, payload=bytes(256)))


def test_encode_rejects_bad_type() -> None:
    with pytest.raises(ValueError):
        encode_frame(Frame(type=256, seq=0, payload=b""))


def test_parser_recovers_after_garbage() -> None:
    frame = Frame(type=FrameType.SAMPLE, seq=1, payload=b"\x00" * 16)
    parser = FrameParser()
    assert parser.feed(b"\x13\x37\xff" + encode_frame(frame)) == [frame]
    assert parser.error_count >= 1


def test_parser_drops_corrupt_frame() -> None:
    # Hand-built frame with a wrong CRC; payload chosen to contain no sync bytes.
    bad = bytes([0xAA, 0x55, 0x90, 0x00, 0x01, 0x00, 0x00, 0x00])
    parser = FrameParser()
    assert parser.feed(bad) == []
    assert parser.error_count >= 1
    good = Frame(type=FrameType.SAMPLE, seq=2, payload=b"\x01" * 16)
    assert parser.feed(encode_frame(good)) == [good]
