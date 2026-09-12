"""Discover ADC boards by probing candidate serial ports with IDENTIFY.

Port enumeration is not stable across reboots, and a USB vendor ID alone is
not a reliable discriminator (WCH makes both the CH343G bridge and the
WCH-Link programmer). The only trustworthy check is to open a candidate and
ask it to identify: a real ADC board answers IDENTIFY with the expected
signature (protocol 1, 6 channels, 12-bit resolution).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from serial.tools import list_ports  # type: ignore[import-untyped]

from .driver import AdcDriver
from .protocol import IdentifyInfo

WCH_VENDOR_ID = 0x1A86  # WCH (CH343G / CH340 / CH9102) USB-UART bridges

EXPECTED_PROTOCOL = 1
EXPECTED_CHANNELS = 6
EXPECTED_RESOLUTION = 12


@dataclass(frozen=True)
class AdcBoard:
    device: str
    serial: str
    info: IdentifyInfo


def adc_capture_filename(serial: str | None, *, ext: str = "csv") -> str:
    """Build a descriptive capture filename: ``adc_<serial>_<YYYYmmdd_HHMMSS>.<ext>``."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"adc_{serial or 'unknown'}_{stamp}.{ext}"


def serial_for_device(device: str) -> str:
    """Return the USB serial number for a serial device path, or ``""`` if unknown."""
    for port in list_ports.comports():
        if port.device == device:
            return port.serial_number or ""
    return ""


def discover_adc_boards(
    *,
    vendor_id: int | None = WCH_VENDOR_ID,
    baud: int = 2_000_000,
    timeout: float = 0.3,
) -> list[AdcBoard]:
    """Return the ADC boards found by probing serial ports with IDENTIFY.

    Candidate ports are pre-filtered to WCH USB-UART bridges (``vendor_id``),
    then each is opened and sent an IDENTIFY command. A port is reported only
    if it replies with the expected signature. Pass ``vendor_id=None`` to probe
    every serial port instead.
    """
    candidates = [
        port
        for port in list_ports.comports()
        if vendor_id is None or port.vid == vendor_id
    ]

    boards: list[AdcBoard] = []
    for port in candidates:
        driver = AdcDriver()
        try:
            driver.open(port.device, baud=baud, timeout=timeout)
            info = driver.identify(timeout=timeout)
            if (
                info.proto_version == EXPECTED_PROTOCOL
                and info.n_channels == EXPECTED_CHANNELS
                and info.resolution == EXPECTED_RESOLUTION
            ):
                boards.append(
                    AdcBoard(
                        device=port.device,
                        serial=port.serial_number or "",
                        info=info,
                    )
                )
        except Exception:
            # Not an ADC board (no reply, wrong reply, or unreadable): skip.
            continue
        finally:
            driver.close()

    return boards
