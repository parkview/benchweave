"""Runtime channel configuration: profiles keyed by ADC channel, plus settings."""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any, cast

from .driver import Sample

# Canonical ADC channel order (matches the firmware's SAMPLE frame).
CHANNEL_KEYS = ("A0", "A1", "A2", "A3", "A4", "A7")

# Runtime config lives beside the plugin source (gitignored).
CONFIG_PATH = Path(__file__).parent / "config.json"

# Volts per 12-bit count at a 5.0 V reference: 5.0 / 4095.
_DEFAULT_GAIN = 0.001221

DEFAULT_CONFIG: dict[str, Any] = {
    "active_profile": "default",
    "profiles": {
        "default": {
            "channels": {
                key: {"name": key, "unit": "V", "gain": _DEFAULT_GAIN, "offset": 0.0}
                for key in CHANNEL_KEYS
            },
            "computed": [],
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


def evaluate_expr(expr: str, values: dict[str, float]) -> float:
    """Safely evaluate a small arithmetic expression over channel values.

    Supports ``+ - * / ( )``, numeric literals, and channel names (e.g.
    ``(A0 - A1) / 0.1``). No function calls, attributes, or code execution.
    """
    tree = ast.parse(expr, mode="eval")
    return _eval(tree.body, values)


def _eval(node: ast.AST, values: dict[str, float]) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Name) and node.id in values:
        return values[node.id]
    if isinstance(node, ast.BinOp):
        left = _eval(node.left, values)
        right = _eval(node.right, values)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, values)
        if isinstance(node.op, ast.UAdd):
            return operand
        if isinstance(node.op, ast.USub):
            return -operand
    raise ValueError(f"unsupported expression: {ast.dump(node)}")


def convert_channels(sample: Sample, config: dict[str, Any]) -> list[dict[str, object]]:
    """Convert raw counts to engineering values (physical + computed) for the active profile."""
    profile = config["profiles"][config["active_profile"]]
    physical: dict[str, float] = {}
    converted: list[dict[str, object]] = []
    for i, key in enumerate(CHANNEL_KEYS):
        ch = profile["channels"][key]
        value = sample.channels[i] * ch["gain"] + ch["offset"]
        physical[key] = value
        converted.append(
            {"key": key, "name": ch["name"], "unit": ch["unit"], "value": round(value, 6)}
        )
    for comp in profile.get("computed", []):
        try:
            value = evaluate_expr(comp["expr"], physical)
            result: object = round(value, 6)
        except Exception:
            result = None
        converted.append(
            {
                "key": comp.get("key", comp["name"]),
                "name": comp["name"],
                "unit": comp["unit"],
                "value": result,
            }
        )
    return converted
