from types import SimpleNamespace
from unittest import mock

from benchweave.adc.discovery import WCH_VENDOR_ID, discover_adc_boards
from benchweave.adc.driver import AdcTimeout
from benchweave.adc.protocol import IdentifyInfo


class _FakeDriver:
    def __init__(self) -> None:
        self._is_adc = False

    def open(self, device: str, *, baud: int, timeout: float) -> None:
        self._is_adc = device == "/dev/ttyACM2"

    def identify(self, *, timeout: float) -> IdentifyInfo:
        if not self._is_adc:
            raise AdcTimeout("no response")
        return IdentifyInfo(1, 0, 2, 6, 12)

    def close(self) -> None:
        pass


def _port(device: str, vid: int | None, serial: str = "SERIAL") -> SimpleNamespace:
    return SimpleNamespace(device=device, vid=vid, serial_number=serial)


def test_discover_filters_by_vendor_and_probes() -> None:
    with mock.patch("serial.tools.list_ports.comports") as comports, mock.patch(
        "benchweave.adc.discovery.AdcDriver", _FakeDriver
    ):
        comports.return_value = [
            _port("/dev/ttyACM2", WCH_VENDOR_ID),  # the ADC board
            _port("/dev/ttyACM3", WCH_VENDOR_ID),  # WCH but not an ADC board
            _port("/dev/ttyUSB0", 0x1234),  # non-WCH: filtered out, never probed
        ]
        boards = discover_adc_boards()

    assert [b.device for b in boards] == ["/dev/ttyACM2"]
    assert boards[0].serial == "SERIAL"
    assert boards[0].info.n_channels == 6
    assert boards[0].info.resolution == 12


def test_discover_returns_empty_when_no_board() -> None:
    with mock.patch("serial.tools.list_ports.comports") as comports, mock.patch(
        "benchweave.adc.discovery.AdcDriver", _FakeDriver
    ):
        comports.return_value = [
            _port("/dev/ttyACM3", WCH_VENDOR_ID),  # WCH but not an ADC board
        ]
        boards = discover_adc_boards()

    assert boards == []
