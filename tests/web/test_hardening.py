"""Targeted regressions for the security/bugfix pass: escaping, validation, caps."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from starlette.types import Message, Receive, Scope, Send

from benchweave.web.app import MAX_BODY_BYTES, BodyLimitMiddleware, _decimate
from benchweave.web.board import _validate_config
from benchweave.web.library import CaptureLibrary
from benchweave.web.report import _esc
from plugins.adc_6ch_12bit.config import DEFAULT_CONFIG


def _valid_config() -> dict[str, Any]:
    import copy

    return copy.deepcopy(DEFAULT_CONFIG)


def test_esc_neutralises_attribute_breakout() -> None:
    hostile = '" onmouseover="alert(1)'
    escaped = _esc(hostile)
    assert '"' not in escaped
    assert "&quot;" in escaped
    assert _esc("<img>") == "&lt;img&gt;"


def test_validate_config_accepts_the_default() -> None:
    _validate_config(_valid_config())


def test_validate_config_rejects_missing_profile_pieces() -> None:
    config = _valid_config()
    del config["profiles"]["default"]["channels"]["A0"]
    with pytest.raises(ValueError, match="missing channel 'A0'"):
        _validate_config(config)

    config = _valid_config()
    config["active_profile"] = "nope"
    with pytest.raises(ValueError, match="active_profile"):
        _validate_config(config)

    config = _valid_config()
    config["profiles"]["default"]["channels"]["A1"]["gain"] = "high"
    with pytest.raises(ValueError, match="numeric 'gain'"):
        _validate_config(config)


def test_validate_config_rejects_malformed_expressions() -> None:
    config = _valid_config()
    config["profiles"]["default"]["computed"] = [
        {"name": "Evil", "unit": "W", "expr": "__import__('os')", "show": True}
    ]
    with pytest.raises(ValueError, match="invalid expression"):
        _validate_config(config)


def test_validate_config_rejects_bad_sample_rate() -> None:
    config = _valid_config()
    config["settings"]["sample_rate_hz"] = -5
    with pytest.raises(ValueError, match="sample_rate_hz"):
        _validate_config(config)


def test_decimate_caps_points_and_keeps_endpoints() -> None:
    points = [[float(i), float(i)] for i in range(10_000)]
    sampled = _decimate(points, 100)
    assert len(sampled) <= 100
    assert sampled[0] == [0.0, 0.0]
    assert sampled[-1] == [9999.0, 9999.0]
    short = [[0.0, 1.0]]
    assert _decimate(short, 100) is short


def test_decimate_never_exceeds_the_budget() -> None:
    # The endpoint used to be appended past the cap (e.g. 10_000 points at
    # limit 100 returned 101); it now replaces the last stride sample instead.
    for n in (1, 2, 99, 100, 101, 150, 1_000, 9_999, 10_000):
        points = [[float(i), float(i)] for i in range(n)]
        for limit in (1, 2, 3, 7, 100):
            sampled = _decimate(points, limit)
            assert len(sampled) <= limit
            assert sampled[-1] == points[-1]
            if limit > 1:
                assert sampled[0] == points[0]


def _run_body_limit(
    headers: list[tuple[bytes, bytes]], chunks: list[bytes]
) -> tuple[int, list[Message]]:
    """Drive BodyLimitMiddleware over a draining inner app; return (status, messages)."""

    async def inner_app(scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] != "http.request" or not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent: list[Message] = []
    pending = list(chunks)

    async def receive() -> Message:
        body = pending.pop(0) if pending else b""
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {"type": "http", "method": "POST", "path": "/api/config", "headers": headers}
    asyncio.run(BodyLimitMiddleware(inner_app)(scope, receive, send))
    status = next(int(m["status"]) for m in sent if m["type"] == "http.response.start")
    return status, sent


def test_body_limit_rejects_oversized_content_length() -> None:
    headers = [(b"content-length", str(MAX_BODY_BYTES + 1).encode())]
    status, _ = _run_body_limit(headers, [b""])
    assert status == 413


def test_body_limit_rejects_chunked_transfer_encoding() -> None:
    status, _ = _run_body_limit([(b"transfer-encoding", b"chunked")], [b""])
    assert status == 411


def test_body_limit_meters_the_receive_channel() -> None:
    # A body that outgrows the cap without a truthful Content-Length is cut
    # off with 413 by the metered receive, whatever the server's framing.
    chunk = b"x" * (8 * 1024 * 1024)
    chunks = [chunk] * ((MAX_BODY_BYTES // len(chunk)) + 2)
    status, _ = _run_body_limit([], chunks)
    assert status == 413


def test_body_limit_passes_a_small_body_through() -> None:
    headers = [(b"content-length", b"2")]
    status, sent = _run_body_limit(headers, [b"{}"])
    assert status == 200
    assert (
        b"".join(bytes(m.get("body", b"")) for m in sent if m["type"] == "http.response.body")
        == b"ok"
    )


def test_csv_unicode_round_trip(tmp_path: Path) -> None:
    # Channel names with non-cp1252 characters used to break every text I/O
    # site on Windows; the explicit utf-8 encoding makes them round-trip.
    stem = "adc_1234_unicode_20260914_120000"
    (tmp_path / f"{stem}.csv").write_text(
        "# profile: default\n"
        "# A0: T° Δµ✓ (°C)\n"
        "timestamp,elapsed_s,actual_sps,counter,averaged_n,T° Δµ✓\n"
        "2026-09-14T12:00:00.000000,0.0,0.0,0,0,21.5\n",
        encoding="utf-8",
    )
    lib = CaptureLibrary(captures_dir=tmp_path, db_path=tmp_path / "library.db")
    path = lib.file_for(stem, "csv")
    assert path is not None
    data = lib.parse_csv(path)
    series = data["series"]
    assert series[0]["name"] == "T° Δµ✓"  # type: ignore[index]
    assert series[0]["unit"] == "°C"  # type: ignore[index]
