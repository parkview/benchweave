from __future__ import annotations

import pytest

from plugins.adc_6ch_12bit.protocol import (
    CHANNEL_MASK_ALL,
    Frame,
    FrameParser,
    FrameType,
    IdentifyInfo,
    SampleMode,
    build_set_averaging,
    build_set_channels,
    build_set_sample_mode,
    crc16,
    encode_frame,
    parse_ack,
    parse_identify,
    parse_nak,
    parse_sample,
)


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


def test_build_set_averaging() -> None:
    assert build_set_averaging(64) == (64).to_bytes(2, "little")
    assert build_set_averaging(0) == b"\x00\x00"
    with pytest.raises(ValueError):
        build_set_averaging(3)


def test_build_set_channels() -> None:
    assert build_set_channels(CHANNEL_MASK_ALL) == b"\x3f"
    with pytest.raises(ValueError):
        build_set_channels(0x40)


def test_build_set_sample_mode() -> None:
    assert build_set_sample_mode(SampleMode.FREE_RUN) == b"\x00"
    with pytest.raises(ValueError):
        build_set_sample_mode(9)


def test_parse_ack() -> None:
    assert parse_ack(bytes([FrameType.SET_AVERAGING, 0x20, 0x00])) == (FrameType.SET_AVERAGING, 32)


def test_parse_nak() -> None:
    assert parse_nak(bytes([FrameType.START_STREAM, 0x03])) == (FrameType.START_STREAM, 3)


def test_parse_identify() -> None:
    assert parse_identify(bytes([1, 0, 2, 6, 12])) == IdentifyInfo(1, 0, 2, 6, 12)


def test_parse_sample() -> None:
    counter = 0x00010203
    channels = (0, 1, 2, 3, 4, 5)
    payload = counter.to_bytes(4, "little") + b"".join(c.to_bytes(2, "little") for c in channels)
    assert parse_sample(payload) == (counter, channels)


def test_parser_drains_valid_frame_after_corrupt() -> None:
    bad = bytes([0xAA, 0x55, 0x90, 0x00, 0x01, 0x00, 0x00, 0x00])  # wrong CRC
    good = Frame(type=FrameType.SAMPLE, seq=2, payload=b"\x01" * 16)
    parser = FrameParser()
    assert parser.feed(bad + encode_frame(good)) == [good]
