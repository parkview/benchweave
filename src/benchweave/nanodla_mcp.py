"""MCP server exposing the nanoDLA logic analyser via sigrok-cli.

A thin wrapper around the local ``sigrok-cli`` binary so an AI client can
discover the device, capture a bounded run to a VCD file, and decode it with
one of libsigrokdecode's protocol decoders (UART, I2C, SPI, CAN, ...).

The device (1d50:608c, an FX2LP running the fx2lafw firmware) is a bulk-USB
sample-streaming device, so this is deliberately *not* an OTDP adapter; the
same capture/streaming semantics live behind the design discussion upstream.
``sigrok-cli`` already handles firmware, sampling, triggers and decoding, so
this server only shells out and shapes the results.

Tested against the nanoDLA v1.3 board (FX2LP + fx2lafw, USB 1d50:608c). A v2.1
board revision exists and has not been tested with this server.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

from mcp.server.mcpserver import MCPServer

SIGROK_CLI = os.environ.get("SIGROK_CLI") or "sigrok-cli"
CAPTURE_DIR = Path(os.environ.get("NANODLA_CAPTURE_DIR") or "captures/nanodla")
DEFAULT_TIMEOUT = float(os.environ.get("NANODLA_TIMEOUT") or "30")

DEFAULT_CHANNELS = "D0,D1,D2,D3,D4,D5,D6,D7"

mcp = MCPServer("benchweave-nanodla")


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run sigrok-cli, translating the two environment failures we care about."""
    try:
        return subprocess.run(
            [SIGROK_CLI, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"sigrok-cli timed out after {timeout}s: {' '.join(args)}"
        ) from exc
    except FileNotFoundError as exc:
        raise RuntimeError(f"{SIGROK_CLI} not found on PATH") from exc


def _checked(args: list[str], timeout: float = DEFAULT_TIMEOUT) -> str:
    """Run sigrok-cli and return stdout, raising a clear error on failure."""
    proc = _run(args, timeout)
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise ValueError(f"sigrok-cli failed: {message}")
    return proc.stdout


@mcp.tool()
def scan_devices() -> list[dict[str, str]]:
    """List logic analyser devices visible to sigrok."""
    out = _checked(["--scan"])
    devices: list[dict[str, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("The following"):
            continue
        spec, sep, rest = line.partition(" - ")
        devices.append({"spec": spec, "description": rest if sep else line})
    return devices


@mcp.tool()
def device_info(spec: str | None = None) -> str:
    """Show a device's capabilities (channels, samplerates, triggers)."""
    return _checked(["-d", spec or "fx2lafw", "--show"])


@mcp.tool()
def capture(
    samplerate: int = 1_000_000,
    samples: int = 1000,
    channels: str = DEFAULT_CHANNELS,
    trigger: str | None = None,
    name: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, object]:
    """Capture a bounded run from the nanoDLA to a VCD file and return a summary.

    ``samplerate`` is in Hz (20 kHz .. 24 MHz); ``samples`` is the run length.
    ``trigger`` is optional, e.g. ``"D0=r"`` (rising) or ``"D0=1"`` (high) —
    note a trigger that never fires will run until ``timeout`` and then fail.
    """
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    stem = name or f"nanodla_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    out_path = CAPTURE_DIR / f"{stem}.vcd"
    args = [
        "-d", "fx2lafw",
        "--config", f"samplerate={samplerate}",
        "--channels", channels,
        "--samples", str(samples),
    ]
    if trigger:
        args += ["--triggers", trigger]
    args += ["-O", "vcd", "-o", str(out_path)]
    _checked(args, timeout)
    return {
        "file": str(out_path),
        "stem": stem,
        "samplerate_hz": samplerate,
        "samples": samples,
        "channels": channels,
        "trigger": trigger,
        "duration_s": samples / samplerate,
        "size_bytes": out_path.stat().st_size,
    }


@mcp.tool()
def decode(
    file: str,
    decoder: str,
    options: dict[str, object] | None = None,
    annotations: str | None = None,
) -> dict[str, object]:
    """Decode a captured trace with a protocol decoder (e.g. uart, i2c, spi).

    ``options`` map decoder options, e.g. ``{"rx": "D0", "baudrate": 115200}``.
    ``annotations`` selects output classes, e.g. ``"uart=rx-data"``; see
    ``decoder_help`` for a decoder's classes.
    """
    pd = decoder
    if options:
        pd += ":" + ":".join(f"{key}={value}" for key, value in options.items())
    args = ["-i", file, "-P", pd]
    if annotations:
        args += ["-A", annotations]
    out = _checked(args)
    return {"decoder": decoder, "file": file, "annotations": out}


@mcp.tool()
def list_decoders() -> list[dict[str, str]]:
    """List all protocol decoders available to sigrok."""
    out = _checked(["-L"])
    decoders: list[dict[str, str]] = []
    in_decoders = False
    for line in out.splitlines():
        if line.strip() == "Supported protocol decoders:":
            in_decoders = True
            continue
        if in_decoders:
            if line and not line[0].isspace():
                break
            stripped = line.strip()
            if not stripped:
                continue
            name, _, desc = stripped.partition(" ")
            decoders.append({"name": name, "description": desc.strip()})
    return decoders


@mcp.tool()
def decoder_help(decoder: str) -> str:
    """Show a decoder's options, channels and annotation classes."""
    return _checked(["-P", decoder, "--show"])


@mcp.tool()
def list_captures(limit: int = 20) -> list[dict[str, object]]:
    """List saved captures, newest first, with sizes."""
    if not CAPTURE_DIR.exists():
        return []
    files = sorted(
        CAPTURE_DIR.glob("*.vcd"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    rows: list[dict[str, object]] = []
    for path in files[:limit]:
        stat = path.stat()
        rows.append(
            {
                "file": str(path),
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
        )
    return rows


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
