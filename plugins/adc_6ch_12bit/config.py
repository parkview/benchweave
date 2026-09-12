"""Runtime channel configuration: profiles keyed by ADC channel, plus settings."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

from .driver import Sample

# Canonical ADC channel order (matches the firmware's SAMPLE frame).
CHANNEL_KEYS = ("A0", "A1", "A2", "A3", "A4", "A7")

# Runtime config lives beside the plugin source (gitignored).
CONFIG_PATH = Path(__file__).parent / "config.json"

# Volts per 12-bit count at a 3.3 V reference: 3.3 / 4095.
_DEFAULT_GAIN = 0.000806

DEFAULT_CONFIG: dict[str, Any] = {
    "active_profile": "default",
    "profiles": {
        "default": {
            "channels": {
                key: {"name": key, "unit": "V", "gain": _DEFAULT_GAIN, "offset": 0.0}
                for key in CHANNEL_KEYS
            }
        }
    },
    "settings": {
        "graph_points": 300,
        "graph_scroll": True,
        "retention_days": 7,
    },
}


def load_config() -> dict[str, Any]:
    """Return the runtime config, falling back to defaults when missing or invalid."""
    if not CONFIG_PATH.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        return cast(dict[str, Any], json.loads(CONFIG_PATH.read_text()))
    except (OSError, ValueError):
        return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config: dict[str, Any]) -> None:
    """Persist the runtime config to the plugin directory."""
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


def convert_channels(sample: Sample, config: dict[str, Any]) -> list[dict[str, object]]:
    """Convert a sample's raw counts to engineering values for the active profile."""
    profile = config["profiles"][config["active_profile"]]
    converted: list[dict[str, object]] = []
    for i, key in enumerate(CHANNEL_KEYS):
        ch = profile["channels"][key]
        value = sample.channels[i] * ch["gain"] + ch["offset"]
        converted.append(
            {"key": key, "name": ch["name"], "unit": ch["unit"], "value": round(value, 6)}
        )
    return converted
