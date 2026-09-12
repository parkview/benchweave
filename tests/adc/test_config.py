from pathlib import Path
from unittest import mock

from plugins.adc_6ch_12bit import config
from plugins.adc_6ch_12bit.config import (
    CHANNEL_KEYS,
    DEFAULT_CONFIG,
    convert_channels,
    load_config,
    save_config,
)
from plugins.adc_6ch_12bit.driver import Sample


def test_default_config_has_six_channels_in_order() -> None:
    channels = DEFAULT_CONFIG["profiles"]["default"]["channels"]
    assert list(channels.keys()) == list(CHANNEL_KEYS)


def test_convert_channels_applies_gain_and_offset() -> None:
    sample = Sample(counter=0, channels=(4095, 100, 0, 0, 0, 0), averaged_n=0)
    cfg = {
        "active_profile": "default",
        "profiles": {
            "default": {
                "channels": {
                    key: {"name": key, "unit": "V", "gain": 0.000806, "offset": 0.5}
                    for key in CHANNEL_KEYS
                }
            }
        },
    }
    converted = convert_channels(sample, cfg)
    assert converted[0]["key"] == "A0"
    assert converted[0]["name"] == "A0"
    assert converted[0]["unit"] == "V"
    assert converted[0]["value"] == round(4095 * 0.000806 + 0.5, 6)
    assert converted[1]["value"] == round(100 * 0.000806 + 0.5, 6)


def test_load_config_returns_default_when_missing(tmp_path: Path) -> None:
    with mock.patch.object(config, "CONFIG_PATH", tmp_path / "config.json"):
        assert load_config()["active_profile"] == "default"


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    with mock.patch.object(config, "CONFIG_PATH", tmp_path / "config.json"):
        cfg = load_config()
        cfg["active_profile"] = "cap-current"
        save_config(cfg)
        assert load_config()["active_profile"] == "cap-current"
