"""Host-side SDK services: SerialLink ring, transfer kinds, capture artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from benchweave.web.host import (
    RING_CAPACITY,
    TRANSFER_CEILING,
    AdcOperationContext,
    SerialHostServices,
    SerialLink,
)


class ScriptedTransport:
    """Fake serial port: reads drain a thread-safe buffer fed by the test."""

    timeout: float | None = 0.01

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending = bytearray()
        self._open = True
        self.written: list[bytes] = []
        self.fail_reads = False
        self.fail_writes = False

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def is_open(self) -> bool:
        return self._open

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._pending += data

    def read(self, size: int = 1) -> bytes:
        if self.fail_reads:
            raise OSError("injected read fault")
        with self._lock:
            if self._pending:
                data = bytes(self._pending[:size])
                del self._pending[:size]
                return data
        time.sleep(0.001)
        return b""

    def write(self, data: bytes) -> int | None:
        if self.fail_writes:
            raise OSError("injected write fault")
        self.written.append(bytes(data))
        return len(data)

    def close(self) -> None:
        self._open = False


@pytest.fixture
def transport() -> ScriptedTransport:
    return ScriptedTransport()


@pytest.fixture
def link(transport: ScriptedTransport) -> Iterator[SerialLink]:
    serial_link = SerialLink(transport)
    yield serial_link
    serial_link.close()


def _drain(serial_link: SerialLink, *, deadline_s: float = 2.0) -> bytes:
    """Everything the ring yields until it stays quiet."""
    collected = bytearray()
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        chunk = serial_link.read_available(TRANSFER_CEILING, 0.05)
        if chunk:
            collected += chunk
        elif collected:
            break
    return bytes(collected)


def _wait_consumed(transport: ScriptedTransport, deadline_s: float = 2.0) -> None:
    deadline = time.monotonic() + deadline_s
    while transport.in_waiting and time.monotonic() < deadline:
        time.sleep(0.005)
    time.sleep(0.05)  # let the reader append its last chunk to the ring


# -- SerialLink ---------------------------------------------------------------


def test_link_round_trips_bytes(transport: ScriptedTransport, link: SerialLink) -> None:
    link.write(b"cmd")
    transport.feed(b"reply-bytes")
    assert _drain(link) == b"reply-bytes"
    assert transport.written == [b"cmd"]


def test_link_read_timeout_returns_empty(link: SerialLink) -> None:
    start = time.monotonic()
    assert link.read_available(64, 0.05) == b""
    assert time.monotonic() - start >= 0.04


def test_link_write_fault_flips_faulted(transport: ScriptedTransport, link: SerialLink) -> None:
    transport.fail_writes = True
    with pytest.raises(ConnectionError):
        link.write(b"x")
    assert link.faulted
    with pytest.raises(ConnectionError):
        link.read_available(1, 0.1)


def test_link_read_fault_wakes_waiters(transport: ScriptedTransport, link: SerialLink) -> None:
    transport.fail_reads = True
    with pytest.raises(ConnectionError):
        # The reader thread faults; the bounded wait must not run its course.
        link.read_available(1, 5.0)
    assert link.faulted
    with pytest.raises(ConnectionError):
        link.write(b"x")


def test_link_overflow_drops_oldest_bytes(transport: ScriptedTransport, link: SerialLink) -> None:
    transport.feed(b"x" * RING_CAPACITY)
    transport.feed(b"y" * 16)
    _wait_consumed(transport)
    data = _drain(link, deadline_s=5.0)
    assert len(data) == RING_CAPACITY  # capped: the 16 oldest bytes were dropped
    assert data.endswith(b"y" * 16)  # the newest bytes survive


def test_link_close_raises_for_late_users(transport: ScriptedTransport, link: SerialLink) -> None:
    link.close()
    assert not transport.is_open
    with pytest.raises(ConnectionError):
        link.write(b"x")
    with pytest.raises(ConnectionError):
        link.read_available(1, 0.1)


# -- AdcOperationContext -------------------------------------------------------


def test_context_cancel_and_dispatch_flags() -> None:
    context = AdcOperationContext("op-1", time.monotonic() + 1.0)
    assert not context.is_cancelled()
    assert not context.dispatched
    context.cancel()
    assert context.is_cancelled()
    asyncio.run(context.mark_dispatch_started())
    assert context.dispatched


# -- SerialHostServices.transfer -------------------------------------------------


def _context(timeout_s: float = 5.0) -> AdcOperationContext:
    return AdcOperationContext("op-test", time.monotonic() + timeout_s)


def _services(
    link: SerialLink, artifact_dir: Path, evidence_path: Path | None = None
) -> SerialHostServices:
    return SerialHostServices(link, artifact_dir=artifact_dir, evidence_path=evidence_path)


def test_transfer_exchange_writes_then_reads(
    transport: ScriptedTransport, link: SerialLink, tmp_path: Path
) -> None:
    services = _services(link, tmp_path)
    transport.feed(b"device-reply")
    _wait_consumed(transport)

    result = asyncio.run(
        services.transfer({"kind": "stream_exchange", "data": b"cmd", "max_bytes": 64}, _context())
    )
    assert result["data"] == b"device-reply"
    assert transport.written == [b"cmd"]


def test_transfer_receive_reads_without_writing(
    transport: ScriptedTransport, link: SerialLink, tmp_path: Path
) -> None:
    services = _services(link, tmp_path)
    transport.feed(b"unsolicited")
    _wait_consumed(transport)

    result = asyncio.run(services.transfer({"kind": "stream_receive", "max_bytes": 64}, _context()))
    assert result["data"] == b"unsolicited"
    assert transport.written == []


def test_transfer_enforces_max_bytes_ceiling(
    transport: ScriptedTransport, link: SerialLink, tmp_path: Path
) -> None:
    services = _services(link, tmp_path)
    transport.feed(b"z" * (TRANSFER_CEILING + 100))
    _wait_consumed(transport)

    async def scenario() -> tuple[bytes, bytes]:
        first = await services.transfer(
            {"kind": "stream_receive", "max_bytes": 1_000_000}, _context()
        )
        second = await services.transfer(
            {"kind": "stream_receive", "max_bytes": 1_000_000}, _context()
        )
        return bytes(first["data"]), bytes(second["data"])

    first, second = asyncio.run(scenario())
    assert len(first) == TRANSFER_CEILING  # declared budget clamped to the ceiling
    assert second == b"z" * 100  # the excess stays in the ring for the next pull


def test_transfer_rejects_bad_transactions(link: SerialLink, tmp_path: Path) -> None:
    services = _services(link, tmp_path)
    with pytest.raises(ValueError, match="unsupported transaction kind"):
        asyncio.run(services.transfer({"kind": "dma", "max_bytes": 16}, _context()))
    with pytest.raises(ValueError, match="max_bytes must be positive"):
        asyncio.run(services.transfer({"kind": "stream_receive", "max_bytes": 0}, _context()))


def test_transfer_refuses_cancelled_or_expired_context(link: SerialLink, tmp_path: Path) -> None:
    services = _services(link, tmp_path)
    cancelled = _context()
    cancelled.cancel()
    with pytest.raises(TimeoutError):
        asyncio.run(services.transfer({"kind": "stream_receive", "max_bytes": 16}, cancelled))
    expired = AdcOperationContext("op-late", time.monotonic() - 1.0)
    with pytest.raises(TimeoutError):
        asyncio.run(services.transfer({"kind": "stream_receive", "max_bytes": 16}, expired))


def test_close_transport_closes_the_link(
    transport: ScriptedTransport, link: SerialLink, tmp_path: Path
) -> None:
    services = _services(link, tmp_path)
    asyncio.run(services.close_transport(_context()))
    assert not transport.is_open


# -- evidence ------------------------------------------------------------------


def test_record_evidence_appends_json_lines(link: SerialLink, tmp_path: Path) -> None:
    evidence = tmp_path / "logs" / "evidence.jsonl"
    services = _services(link, tmp_path, evidence_path=evidence)
    asyncio.run(services.record_evidence({"kind": "probe"}, _context()))

    lines = evidence.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert '"operation_id":"op-test"' in lines[0]
    assert '"kind":"probe"' in lines[0]


def test_record_evidence_is_a_noop_without_a_path(link: SerialLink, tmp_path: Path) -> None:
    services = _services(link, tmp_path)
    asyncio.run(services.record_evidence({"kind": "probe"}, _context()))
    assert list(tmp_path.iterdir()) == []


# -- capture artifacts -----------------------------------------------------------


def test_artifact_append_finalise_round_trip(link: SerialLink, tmp_path: Path) -> None:
    artifact_dir = tmp_path / "captures"
    services = _services(link, artifact_dir)

    async def scenario() -> dict[str, Any]:
        context = _context()
        await services.artifact_append("cap-1", b"head,", context)
        await services.artifact_append("cap-1", b"tail", context)
        return await services.artifact_finalise("cap-1", {"encoding": "csv"}, context)

    summary = asyncio.run(scenario())
    final = artifact_dir / "cap-1.bin"
    assert final.read_bytes() == b"head,tail"
    assert not (artifact_dir / "cap-1.part").exists()
    assert summary == {
        "artifact_id": "cap-1",
        "encoding": "csv",
        "byte_length": 9,
        "sha256": hashlib.sha256(b"head,tail").hexdigest(),
    }


def test_artifact_finalise_unknown_id_raises(link: SerialLink, tmp_path: Path) -> None:
    services = _services(link, tmp_path)
    with pytest.raises(ValueError, match="unknown capture artifact"):
        asyncio.run(services.artifact_finalise("nope", {}, _context()))


def test_artifact_abort_discards_partial_data(link: SerialLink, tmp_path: Path) -> None:
    artifact_dir = tmp_path / "captures"
    services = _services(link, artifact_dir)

    async def scenario() -> None:
        await services.artifact_append("cap-2", b"partial", _context())
        await services.artifact_abort("cap-2")
        await services.artifact_abort("never-started")  # tolerated

    asyncio.run(scenario())
    assert not (artifact_dir / "cap-2.part").exists()
    assert not (artifact_dir / "cap-2.bin").exists()
