#!/usr/bin/env python3
"""Capture streamed ADC samples from the BenchWeave ADC board to a CSV file.

Columns: timestamp, elapsed_s, counter, averaged_n, a0, a1, a2, a3, a4, a7
(6 channels in canonical order: A0, A1, A2, A3, A4, A7; raw 12-bit counts).

Usage:
    uv run scripts/adc_capture.py --port /dev/ttyACM2 --seconds 5 --averaging 0
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

from benchweave.adc import (
    AVERAGING_CHOICES,
    AdcDriver,
    adc_capture_filename,
    discover_adc_boards,
    serial_for_device,
)

CHANNEL_NAMES = ("a0", "a1", "a2", "a3", "a4", "a7")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=None, help="serial port (default: auto-discover)")
    parser.add_argument("--baud", type=int, default=2_000_000)
    parser.add_argument("--averaging", type=int, default=0, choices=AVERAGING_CHOICES)
    parser.add_argument("--seconds", type=float, default=5.0, help="capture duration")
    parser.add_argument(
        "--output",
        default=None,
        help="CSV path (default: captures/adc_<serial>_<timestamp>.csv)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    port = args.port
    serial = ""
    if port is None:
        boards = discover_adc_boards()
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
        serial = boards[0].serial
        print(f"found ADC board on {port} (serial {serial or 'unknown'})")
    else:
        serial = serial_for_device(port)

    if args.output:
        output = args.output
    else:
        Path("captures").mkdir(parents=True, exist_ok=True)
        output = str(Path("captures") / adc_capture_filename(serial))

    driver = AdcDriver()
    driver.open(port, baud=args.baud)
    streaming = False

    try:
        info = driver.identify()
        print(
            f"device: proto={info.proto_version} fw={info.fw_major}.{info.fw_minor} "
            f"channels={info.n_channels} resolution={info.resolution}bit"
        )

        driver.set_averaging(args.averaging)
        driver.start_stream()
        streaming = True

        header = ["timestamp", "elapsed_s", "counter", "averaged_n", *CHANNEL_NAMES]
        written = 0
        t0 = time.monotonic()

        with open(output, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            try:
                for sample in driver.iter_samples():
                    writer.writerow(
                        [
                            datetime.now().isoformat(timespec="microseconds"),
                            round(time.monotonic() - t0, 6),
                            sample.counter,
                            sample.averaged_n,
                            *sample.channels,
                        ]
                    )
                    written += 1
                    if time.monotonic() - t0 >= args.seconds:
                        break
            except KeyboardInterrupt:
                pass

        print(f"captured {written} samples -> {output}")

    finally:
        if streaming:
            with contextlib.suppress(Exception):
                driver.stop_stream()
        driver.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
