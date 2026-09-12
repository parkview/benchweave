"""6-channel ADC board serial driver (master side)."""

from .discovery import (
    AdcBoard,
    adc_capture_filename,
    discover_adc_boards,
    serial_for_device,
)
from .driver import (
    AdcConnectionError,
    AdcDriver,
    AdcError,
    AdcNotConnected,
    AdcProtocolError,
    AdcTimeout,
    Sample,
)
from .protocol import (
    AVERAGING_CHOICES,
    CHANNEL_MASK_ALL,
    FrameType,
    IdentifyInfo,
    SampleMode,
)

__all__ = [
    "AdcConnectionError",
    "AdcDriver",
    "AdcError",
    "AdcNotConnected",
    "AdcProtocolError",
    "AdcTimeout",
    "Sample",
    "AdcBoard",
    "adc_capture_filename",
    "discover_adc_boards",
    "serial_for_device",
    "AVERAGING_CHOICES",
    "CHANNEL_MASK_ALL",
    "FrameType",
    "IdentifyInfo",
    "SampleMode",
]
