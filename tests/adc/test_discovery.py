"""Discovery probes ports with a one-shot IDENTIFY over the raw wire codec."""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from plugins.adc_6ch_12bit import protocol
from plugins.adc_6ch_12bit.discovery import (
    WCH_VENDOR_ID,
    _probe,
    adc_capture_filename,
    discover_adc_boards,
)
from plugins.adc_6ch_12bit.protocol import IdentifyInfo

ADC_PORT = "/dev/ttyACM2"

IDENTIFY_RSP = protocol.encode_frame(
    protocol.Frame(
        type=int(protocol.FrameType.IDENTIFY_RSP),
        seq=0,
        payload=bytes((1, 0, 2, 6, 12)),  # proto 1, fw 0.2, 6 channels, 12-bit
    )
)

IDENTIFY_CMD = protocol.encode_frame(
    protocol.Frame(type=int(protocol.FrameType.IDENTIFY), seq=0, payload=b"")
)


class _FakeSerial:
    """serial.Serial stand-in: answers IDENTIFY only on the ADC port.

    The ADC port's reply arrives behind line noise, so the probe's parser
    must resynchronise. Constructed ports are recorded to prove the vendor
    pre-filter keeps foreign ports untouched.
    """

    constructed: ClassVar[list[str]] = []
    instances: ClassVar[list[_FakeSerial]] = []

    def __init__(self, port: str, baud: int, timeout: float = 0.05) -> None:
        _FakeSerial.constructed.append(port)
        if port == "/dev/ttyBROKEN":
            raise OSError("cannot open port")
        _FakeSerial.instances.append(self)
        self._reply = bytearray(b"\x00\xaanoise" + IDENTIFY_RSP if port == ADC_PORT else b"")
        self.written = b""
        self.closed = False

    @property
    def in_waiting(self) -> int:
        return len(self._reply)

    def write(self, data: bytes) -> int:
        self.written += data
        return len(data)

    def read(self, size: int = 1) -> bytes:
        data = bytes(self._reply[:size])
        del self._reply[:size]
        return data

    def close(self) -> None:
        self.closed = True


def _port(device: str, vid: int | None, serial: str = "SERIAL") -> SimpleNamespace:
    return SimpleNamespace(device=device, vid=vid, serial_number=serial)


def test_discover_filters_by_vendor_and_probes() -> None:
    _FakeSerial.constructed = []
    with (
        mock.patch("serial.tools.list_ports.comports") as comports,
        mock.patch("plugins.adc_6ch_12bit.discovery.serial.Serial", _FakeSerial),
    ):
        comports.return_value = [
            _port(ADC_PORT, WCH_VENDOR_ID),  # the ADC board
            _port("/dev/ttyACM3", WCH_VENDOR_ID),  # WCH but not an ADC board
            _port("/dev/ttyUSB0", 0x1234),  # non-WCH: filtered out, never probed
        ]
        boards = discover_adc_boards(timeout=0.05)

    assert [b.device for b in boards] == [ADC_PORT]
    assert boards[0].serial == "SERIAL"
    assert boards[0].info.n_channels == 6
    assert boards[0].info.resolution == 12
    assert _FakeSerial.constructed == [ADC_PORT, "/dev/ttyACM3"]


def test_discover_returns_empty_when_no_board() -> None:
    _FakeSerial.constructed = []
    with (
        mock.patch("serial.tools.list_ports.comports") as comports,
        mock.patch("plugins.adc_6ch_12bit.discovery.serial.Serial", _FakeSerial),
    ):
        comports.return_value = [
            _port("/dev/ttyACM3", WCH_VENDOR_ID),  # WCH but not an ADC board
        ]
        boards = discover_adc_boards(timeout=0.05)

    assert boards == []


def test_probe_sends_identify_and_parses_reply() -> None:
    _FakeSerial.constructed = []
    with mock.patch("plugins.adc_6ch_12bit.discovery.serial.Serial", _FakeSerial):
        info = _probe(ADC_PORT, 2_000_000, timeout=0.2)

    assert info == IdentifyInfo(1, 0, 2, 6, 12)


def test_probe_writes_one_identify_frame_and_closes() -> None:
    _FakeSerial.constructed = []
    _FakeSerial.instances = []
    with mock.patch("plugins.adc_6ch_12bit.discovery.serial.Serial", _FakeSerial):
        _probe(ADC_PORT, 2_000_000, timeout=0.2)

    assert len(_FakeSerial.instances) == 1
    assert _FakeSerial.instances[0].written == IDENTIFY_CMD
    assert _FakeSerial.instances[0].closed is True


def test_probe_returns_none_on_silence() -> None:
    _FakeSerial.constructed = []
    with mock.patch("plugins.adc_6ch_12bit.discovery.serial.Serial", _FakeSerial):
        assert _probe("/dev/ttyACM3", 2_000_000, timeout=0.05) is None


def test_probe_returns_none_when_port_cannot_open() -> None:
    _FakeSerial.constructed = []
    with mock.patch("plugins.adc_6ch_12bit.discovery.serial.Serial", _FakeSerial):
        assert _probe("/dev/ttyBROKEN", 2_000_000, timeout=0.05) is None


def test_adc_capture_filename_includes_tag() -> None:
    tagged = adc_capture_filename("ABC", tag="MCP")
    assert tagged.startswith("adc_ABC_MCP_")
    assert tagged.endswith(".csv")


def test_adc_capture_filename_omits_tag_when_absent() -> None:
    name = adc_capture_filename("ABC")
    assert name.startswith("adc_ABC_")
    assert "_MCP_" not in name
    assert name.endswith(".csv")


def test_adc_capture_filename_respects_extension_and_unknown_serial() -> None:
    name = adc_capture_filename(None, ext="png", tag="MCP")
    assert name.startswith("adc_unknown_MCP_")
    assert name.endswith(".png")
