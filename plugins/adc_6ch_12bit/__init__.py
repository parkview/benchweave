"""6-channel ADC board plugin: OTDP adapter, wire codec, config, discovery."""

from .adapter import CHANNEL_IDS, AdcAdapter, create_plugin
from .config import (
    CHANNEL_KEYS,
    DEFAULT_CONFIG,
    convert_channels,
    estimate_max_sps,
    evaluate_expr,
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
from .protocol import (
    AVERAGING_CHOICES,
    CHANNEL_MASK_ALL,
    FrameType,
    IdentifyInfo,
    Sample,
    SampleMode,
)

__all__ = [
    "AdcAdapter",
    "AdcBoard",
    "CHANNEL_IDS",
    "Sample",
    "adc_capture_filename",
    "capture_dir",
    "convert_channels",
    "create_plugin",
    "discover_adc_boards",
    "estimate_max_sps",
    "evaluate_expr",
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
