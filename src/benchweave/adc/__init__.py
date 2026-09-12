"""6-channel ADC board serial driver (master side)."""

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
    "AVERAGING_CHOICES",
    "CHANNEL_MASK_ALL",
    "FrameType",
    "IdentifyInfo",
    "SampleMode",
]
