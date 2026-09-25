#!/usr/bin/env python3
"""Capture streamed ADC samples from the BenchWeave ADC board to a CSV file.

The capture runs through the shared :class:`~benchweave.web.board.BoardManager`
backend (the same SDK adapter stack as the web UI and MCP server), so the CSV
carries the manager's metadata header and converted engineering columns.

Usage:
    uv run scripts/adc_capture.py --port /dev/ttyACM2 --seconds 5 --averaging 0
"""

from __future__ import annotations

import argparse
import shutil
import sys

from benchweave.web import board as board_module
from benchweave.web.board import BoardManager
from plugins.adc_6ch_12bit import AVERAGING_CHOICES, discover_adc_boards
from plugins.adc_6ch_12bit.discovery import _probe
from plugins.adc_6ch_12bit.protocol import IdentifyInfo


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=None, help="serial port (default: auto-discover)")
    parser.add_argument("--baud", type=int, default=2_000_000)
    parser.add_argument("--averaging", type=int, default=0, choices=AVERAGING_CHOICES)
    parser.add_argument("--seconds", type=float, default=5.0, help="capture duration")
    parser.add_argument(
        "--output",
        default=None,
        help="CSV path (default: the plugin's captures/ directory)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    port = args.port
    info: IdentifyInfo | None
    if port is None:
        boards = discover_adc_boards(baud=args.baud)
        if not boards:
            print("error: no ADC board found on any serial port", file=sys.stderr)
            return 1
        if len(boards) > 1:
            print("error: multiple ADC boards found:", file=sys.stderr)
            for board in boards:
                print(f"  {board.device}", file=sys.stderr)
            print("re-run with --port to choose one", file=sys.stderr)
            return 1
        port = boards[0].device
        info = boards[0].info
        print(f"found ADC board on {port} (serial {boards[0].serial or 'unknown'})")
    else:
        info = _probe(port, args.baud, timeout=1.0)
        if info is None:
            print(f"error: no ADC board answered IDENTIFY on {port}", file=sys.stderr)
            return 1

    print(
        f"device: proto={info.proto_version} fw={info.fw_major}.{info.fw_minor} "
        f"channels={info.n_channels} resolution={info.resolution}bit"
    )

    board_module.DEFAULT_BAUD = args.baud
    manager = BoardManager()
    try:
        manager.connect(port)
        manager.set_averaging(args.averaging)
        result = manager.capture_seconds(args.seconds)
    finally:
        manager.disconnect()

    output = str(result["path"])
    if args.output:
        shutil.move(output, args.output)
        output = args.output

    print(f"captured {result['count']} samples -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
