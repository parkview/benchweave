"""6-channel ADC board serial driver (master side)."""

from benchweave.adc.discovery import AdcBoard, discover_adc_boards
from benchweave.adc.driver import (
    AdcConnectionError,
    AdcDriver,
    AdcError,
    AdcNotConnected,
    AdcProtocolError,
    AdcTimeout,
    Sample,
)
from benchweave.adc.protocol import (
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
    "discover_adc_boards",
    "AVERAGING_CHOICES",
    "CHANNEL_MASK_ALL",
    "FrameType",
    "IdentifyInfo",
    "SampleMode",
]
