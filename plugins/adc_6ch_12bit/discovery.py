"""Discover ADC boards by probing candidate serial ports with IDENTIFY.

Port enumeration is not stable across reboots, and a USB vendor ID alone is
not a reliable discriminator (WCH makes both the CH343G bridge and the
WCH-Link programmer). The only trustworthy check is to open a candidate and
ask it to identify: a real ADC board answers IDENTIFY with the expected
signature (protocol 1, 6 channels, 12-bit resolution).
"""

from __future__ import annotations

import contextlib
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import serial  # type: ignore[import-untyped]
from serial.tools import list_ports  # type: ignore[import-untyped]

from . import protocol
from .protocol import IdentifyInfo

WCH_VENDOR_ID = 0x1A86  # WCH (CH343G / CH340 / CH9102) USB-UART bridges

EXPECTED_PROTOCOL = 1
EXPECTED_CHANNELS = 6
EXPECTED_RESOLUTION = 12

#: Per-read serial timeout while probing; the overall budget is the caller's.
_PROBE_READ_TIMEOUT = 0.05


@dataclass(frozen=True)
class AdcBoard:
    device: str
    serial: str
    info: IdentifyInfo


def adc_capture_filename(serial: str | None, *, ext: str = "csv", tag: str | None = None) -> str:
    """Build a descriptive capture filename: ``adc_<serial>[_<tag>]_<YYYYmmdd_HHMMSS>.<ext>``."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    middle = f"_{tag}" if tag else ""
    return f"adc_{serial or 'unknown'}{middle}_{stamp}.{ext}"


def capture_dir() -> Path:
    """Return (and create) the ``captures/`` directory for this plugin's data.

    By default captures live in ``captures/`` beside the plugin source. The
    ``BENCHWEAVE_ADC_DATA_DIR`` environment variable (a development/test
    knob) overrides the data root: when set, this returns
    ``$BENCHWEAVE_ADC_DATA_DIR/captures`` instead. In both cases the
    directory (and any missing parents) is created on demand.
    """
    root = os.environ.get("BENCHWEAVE_ADC_DATA_DIR")
    base = Path(root) if root else Path(__file__).parent
    path = base / "captures"
    path.mkdir(parents=True, exist_ok=True)
    return path


def serial_for_device(device: str) -> str:
    """Return the USB serial number for a serial device path, or ``""`` if unknown."""
    for port in list_ports.comports():
        if port.device == device:
            return port.serial_number or ""
    return ""


def _probe(port_device: str, baud: int, timeout: float) -> IdentifyInfo | None:
    """One-shot IDENTIFY probe of a serial port, straight over the wire codec.

    Opens the port, transmits a single IDENTIFY frame, and feeds whatever
    arrives within ``timeout`` seconds to a :class:`protocol.FrameParser`.
    The first IDENTIFY_RSP wins; anything else (silence, noise, an alien
    protocol) yields ``None``. The port is always closed before returning.
    """
    try:
        transport = serial.Serial(port_device, baud, timeout=_PROBE_READ_TIMEOUT)
    except Exception:
        return None
    try:
        frame = protocol.Frame(type=int(protocol.FrameType.IDENTIFY), seq=0, payload=b"")
        transport.write(protocol.encode_frame(frame))
        parser = protocol.FrameParser()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = transport.read(transport.in_waiting or 1)
            if not data:
                continue
            for received in parser.feed(bytes(data)):
                if received.type == protocol.FrameType.IDENTIFY_RSP:
                    return protocol.parse_identify(received.payload)
        return None
    except Exception:
        # Not an ADC board (unreadable, malformed reply, or vanished): skip.
        return None
    finally:
        with contextlib.suppress(Exception):
            transport.close()


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
        port for port in list_ports.comports() if vendor_id is None or port.vid == vendor_id
    ]

    boards: list[AdcBoard] = []
    for port in candidates:
        info = _probe(port.device, baud, timeout)
        if info is not None and (
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

    return boards
