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

import hashlib
import json
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


def _sha256(path: Path) -> str:
    """Return the hex SHA-256 digest of a file, streamed in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _append_to_manifest(file: str, field: str, entry: dict[str, object]) -> str | None:
    """Append ``entry`` to ``field`` (a list) of the capture's manifest, if it exists.

    The manifest is ``<stem>.json`` next to ``file``. Returns its path on success,
    or ``None`` when there is no readable manifest to update.
    """
    metadata_path = Path(file).with_suffix(".json")
    if not metadata_path.exists():
        return None
    try:
        meta = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    items = meta.setdefault(field, [])
    if not isinstance(items, list):
        items = []
        meta[field] = items
    items.append(entry)
    metadata_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return str(metadata_path)


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
    """Capture a bounded run from the nanoDLA and return a summary.

    ``samplerate`` is in Hz (20 kHz .. 24 MHz); ``samples`` is the run length.
    ``trigger`` is optional, e.g. ``"D0=r"`` (rising) or ``"D0=1"`` (high) —
    note a trigger that never fires will run until ``timeout`` and then fail.

    The acquisition is written to two files under ``<capture-dir>/<stem>``:

    * ``<stem>.vcd`` — a Value Change Dump for the AI to read, parse and decode.
    * ``<stem>.sr`` — a native sigrok session for a human to open in PulseView.

    A JSON capture-metadata manifest ``<stem>.json`` records the capture specs
    plus an ``artifacts`` list (kind, file, size, SHA-256) for both files.
    Without an explicit ``name`` the stem is ``nanodla_<YYYY-MM-DDTHH-MM-SS>``.
    """
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    captured_at = datetime.now().astimezone()
    stem = name or f"nanodla_{captured_at.strftime('%Y-%m-%dT%H-%M-%S')}"
    vcd_path = CAPTURE_DIR / f"{stem}.vcd"
    sr_path = CAPTURE_DIR / f"{stem}.sr"

    args = [
        "-d", "fx2lafw",
        "--config", f"samplerate={samplerate}",
        "--channels", channels,
        "--samples", str(samples),
    ]
    if trigger:
        args += ["--triggers", trigger]
    args += ["-O", "vcd", "-o", str(vcd_path)]
    _checked(args, timeout)

    # Derive the human-facing native session from the VCD (offline, no device).
    _checked(["-i", str(vcd_path), "-O", "srzip", "-o", str(sr_path)], timeout)

    artifacts = [
        {
            "kind": "vcd",
            "file": str(vcd_path),
            "size_bytes": vcd_path.stat().st_size,
            "sha256": _sha256(vcd_path),
        },
        {
            "kind": "sr",
            "file": str(sr_path),
            "size_bytes": sr_path.stat().st_size,
            "sha256": _sha256(sr_path),
        },
    ]
    meta = {
        "captured_at": captured_at.isoformat(),
        "device": "fx2lafw",
        "stem": stem,
        "samplerate_hz": samplerate,
        "samples": samples,
        "channels": channels,
        "trigger": trigger,
        "duration_s": samples / samplerate,
        "artifacts": artifacts,
    }
    metadata_path = CAPTURE_DIR / f"{stem}.json"
    metadata_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return {
        "file": str(vcd_path),
        "sr_file": str(sr_path),
        "metadata_file": str(metadata_path),
        **meta,
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

    If the matching capture-metadata manifest (``<stem>.json``) exists, the decode
    is appended to its ``decodes`` list — decoder, options, filter, ``decoded_at``
    and the full annotation text — so a human can later see what the AI decoded.
    """
    pd = decoder
    if options:
        pd += ":" + ":".join(f"{key}={value}" for key, value in options.items())
    args = ["-i", file, "-P", pd]
    if annotations:
        args += ["-A", annotations]
    out = _checked(args)

    result: dict[str, object] = {"decoder": decoder, "file": file, "annotations": out}

    # Record the decode in the capture-metadata manifest when one exists.
    result["metadata_file"] = _append_to_manifest(
        file,
        "decodes",
        {
            "decoded_at": datetime.now().astimezone().isoformat(),
            "decoder": decoder,
            "options": options,
            "annotation_filter": annotations,
            "annotations": out,
        },
    )

    return result


@mcp.tool()
def annotate(file: str, note: str) -> dict[str, object]:
    """Append a free-form analysis note to a capture's manifest.

    ``file`` is a capture path (``.vcd`` or ``.sr``); ``note`` is the prose to
    record. The note is appended to the manifest's ``notes`` list with a
    timestamp, so a human can later read what the AI concluded. Returns the
    manifest path (``metadata_file``) or ``null``, and whether it was recorded.
    """
    metadata_file = _append_to_manifest(
        file,
        "notes",
        {"noted_at": datetime.now().astimezone().isoformat(), "text": note},
    )
    return {"metadata_file": metadata_file, "recorded": metadata_file is not None}


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
    """List saved captures, newest first, with sizes, session and metadata paths."""
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
        sr_path = path.with_suffix(".sr")
        metadata_path = path.with_suffix(".json")
        rows.append(
            {
                "file": str(path),
                "sr_file": str(sr_path) if sr_path.exists() else None,
                "metadata_file": str(metadata_path) if metadata_path.exists() else None,
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
