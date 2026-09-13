from pathlib import Path
from unittest import mock

import pytest

from plugins.adc_6ch_12bit import config
from plugins.adc_6ch_12bit.config import (
    CHANNEL_KEYS,
    DEFAULT_CONFIG,
    convert_channels,
    estimate_max_sps,
    evaluate_expr,
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


def test_evaluate_expr_arithmetic() -> None:
    values = {"A0": 3.0, "A1": 2.0}
    assert evaluate_expr("(A0 - A1) / 0.1", values) == 10.0
    assert evaluate_expr("A0 * 2 + 1", values) == 7.0


def test_evaluate_expr_rejects_non_arithmetic() -> None:
    with pytest.raises(ValueError):
        evaluate_expr("__import__('os')", {"A0": 1.0})


def test_convert_channels_with_computed() -> None:
    sample = Sample(counter=0, channels=(3000, 2000, 0, 0, 0, 0), averaged_n=0)
    cfg = {
        "active_profile": "default",
        "profiles": {
            "default": {
                "channels": {
                    key: {"name": key, "unit": "V", "gain": 0.001, "offset": 0.0}
                    for key in CHANNEL_KEYS
                },
                "computed": [
                    {"name": "Current", "unit": "A", "expr": "(A0 - A1) / 0.1"}
                ],
            }
        },
    }
    converted = convert_channels(sample, cfg)
    assert len(converted) == 7
    current = converted[6]
    assert current["name"] == "Current"
    assert current["unit"] == "A"
    assert current["value"] == 10.0


def test_estimate_max_sps() -> None:
    raw_6 = estimate_max_sps(0, 6)
    assert 3000 < raw_6 < 3500
    # Higher averaging reduces the max rate.
    assert estimate_max_sps(16, 6) < raw_6
    # Fewer channels increases the max rate.
    assert estimate_max_sps(0, 3) > raw_6


def test_convert_channels_hides_show_false() -> None:
    sample = Sample(counter=0, channels=(1000, 0, 0, 0, 0, 0), averaged_n=0)
    cfg = {
        "active_profile": "default",
        "profiles": {
            "default": {
                "channels": {
                    key: {
                        "name": key,
                        "unit": "V",
                        "gain": 0.001,
                        "offset": 0.0,
                        "show": key != "A0",
                    }
                    for key in CHANNEL_KEYS
                },
                "computed": [],
            }
        },
    }
    converted = convert_channels(sample, cfg)
    assert len(converted) == 5  # A0 hidden
    assert all(c["key"] != "A0" for c in converted)
