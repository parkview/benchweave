"""6-channel ADC board serial driver (master side)."""

from .config import (
    CHANNEL_KEYS,
    DEFAULT_CONFIG,
    convert_channels,
    estimate_max_sps,
    load_config,
    output_channels,
    save_config,
)
from .discovery import (
    AdcBoard,
    adc_capture_filename,
    capture_dir,
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
    "capture_dir",
    "convert_channels",
    "discover_adc_boards",
    "estimate_max_sps",
    "load_config",
    "output_channels",
    "save_config",
    "serial_for_device",
    "AVERAGING_CHOICES",
    "CHANNEL_KEYS",
    "CHANNEL_MASK_ALL",
    "DEFAULT_CONFIG",
    "FrameType",
    "IdentifyInfo",
    "SampleMode",
]
