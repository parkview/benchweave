"""Host-side SDK services: SerialLink ring, section 8.1 transfers, capture artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from benchweave.web.host import (
    RECEIVE_WAIT_S,
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
        held = serial_link.buffered
        if held:
            collected += serial_link.take(held, b"\n", held, 0.0) or b""
        elif collected:
            break
        else:
            time.sleep(0.01)
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


def test_link_take_times_out_as_incomplete(link: SerialLink) -> None:
    start = time.monotonic()
    assert link.take(4, b"\n", 64, 0.05) is None
    assert time.monotonic() - start >= 0.04


def test_link_take_leaves_an_incomplete_receive_in_the_ring(
    transport: ScriptedTransport, link: SerialLink
) -> None:
    transport.feed(b"abc")
    _wait_consumed(transport)
    assert link.take(5, b"\n", 5, 0.05) is None
    assert link.buffered == 3
    transport.feed(b"de")
    assert link.take(5, b"\n", 5, 1.0) == b"abcde"


def test_link_take_through_a_terminator(transport: ScriptedTransport, link: SerialLink) -> None:
    transport.feed(b"one\r\ntwo")
    _wait_consumed(transport)
    assert link.take(0, b"\r\n", 64, 1.0) == b"one\r\n"
    assert link.take(0, b"\r\n", 64, 0.05) is None  # "two" has no terminator yet
    assert link.buffered == 3


def test_link_take_discards_an_over_long_line(
    transport: ScriptedTransport, link: SerialLink
) -> None:
    transport.feed(b"x" * 10 + b"\nok\n")
    _wait_consumed(transport)
    with pytest.raises(ValueError, match="no terminator within 8 bytes"):
        link.take(0, b"\n", 8, 1.0)
    assert link.take(0, b"\n", 8, 1.0) == b"xx\n"  # resynchronised on the next terminator
    assert link.take(0, b"\n", 8, 1.0) == b"ok\n"


def test_link_write_fault_flips_faulted(transport: ScriptedTransport, link: SerialLink) -> None:
    transport.fail_writes = True
    with pytest.raises(ConnectionError):
        link.write(b"x")
    assert link.faulted
    with pytest.raises(ConnectionError):
        link.take(1, b"\n", 1, 0.1)


@pytest.mark.parametrize("sent", [0, 2])
def test_link_short_write_is_a_fault(
    transport: ScriptedTransport, link: SerialLink, monkeypatch: pytest.MonkeyPatch, sent: int
) -> None:
    monkeypatch.setattr(transport, "write", lambda data: sent)
    with pytest.raises(ConnectionError, match=f"reported {sent} of 3 bytes"):
        link.write(b"cmd")
    assert link.faulted


def test_link_write_trusts_a_transport_that_reports_no_count(
    transport: ScriptedTransport, link: SerialLink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pyserial's rs485.RS485.write returns None on every call."""
    monkeypatch.setattr(transport, "write", lambda data: None)
    link.write(b"cmd")
    assert not link.faulted


def test_link_read_fault_wakes_waiters(transport: ScriptedTransport, link: SerialLink) -> None:
    transport.fail_reads = True
    with pytest.raises(ConnectionError):
        # The reader thread faults; the bounded wait must not run its course.
        link.take(1, b"\n", 1, 5.0)
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
        link.take(1, b"\n", 1, 0.1)


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


def _receive(
    max_bytes: int, *, exact: int | None = None, termination: str = "lf"
) -> dict[str, Any]:
    return {
        "kind": "stream_receive",
        "max_bytes": max_bytes,
        "termination": termination,
        "exact_bytes": exact,
    }


def test_transfer_send_writes_and_answers_empty(
    transport: ScriptedTransport, link: SerialLink, tmp_path: Path
) -> None:
    services = _services(link, tmp_path)
    result = asyncio.run(services.transfer({"kind": "stream_send", "data": b"cmd"}, _context()))
    assert result == {}
    assert transport.written == [b"cmd"]


def test_transfer_exchange_writes_then_takes_exact_bytes(
    transport: ScriptedTransport, link: SerialLink, tmp_path: Path
) -> None:
    services = _services(link, tmp_path)
    transport.feed(b"device-reply")
    _wait_consumed(transport)

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        exchange = await services.transfer(
            {**_receive(64, exact=6), "kind": "stream_exchange", "data": b"cmd"},
            _context(),
        )
        rest = await services.transfer(_receive(64, exact=6), _context())
        return exchange, rest

    exchange, rest = asyncio.run(scenario())
    assert exchange == {"data": b"device"}  # exact_bytes wins over the lf terminator
    assert rest == {"data": b"-reply"}
    assert transport.written == [b"cmd"]


def test_transfer_receive_takes_a_terminated_line_without_writing(
    transport: ScriptedTransport, link: SerialLink, tmp_path: Path
) -> None:
    services = _services(link, tmp_path)
    transport.feed(b"line one\nrest")
    _wait_consumed(transport)

    result = asyncio.run(services.transfer(_receive(64), _context()))
    assert result == {"data": b"line one\n"}
    assert transport.written == []


@pytest.mark.parametrize("exact", [None, 5])
def test_transfer_on_a_quiet_line_answers_empty_before_the_deadline(
    link: SerialLink, tmp_path: Path, exact: int | None
) -> None:
    services = _services(link, tmp_path)
    started = time.monotonic()
    result = asyncio.run(services.transfer(_receive(64, exact=exact), _context(5.0)))
    assert result == {"data": b""}
    assert time.monotonic() - started < RECEIVE_WAIT_S + 1.0  # not the 5 s deadline


def test_transfer_keeps_an_incomplete_frame_across_the_deadline(
    transport: ScriptedTransport, link: SerialLink, tmp_path: Path
) -> None:
    services = _services(link, tmp_path)
    transport.feed(b"abc")
    _wait_consumed(transport)
    with pytest.raises(TimeoutError):
        asyncio.run(services.transfer(_receive(5, exact=5), _context(0.3)))
    transport.feed(b"de")
    result = asyncio.run(services.transfer(_receive(5, exact=5), _context()))
    assert result == {"data": b"abcde"}  # never returned early, never lost


@pytest.mark.parametrize(
    "transaction",
    [
        {"kind": "dma", "max_bytes": 16},
        {"kind": "stream_send", "data": b"x", "max_bytes": 16},  # extra field
        {"kind": "stream_receive", "max_bytes": 16, "termination": "lf"},  # missing field
        {**_receive(16), "kind": "stream_exchange"},  # no data
        {"kind": "stream_send", "data": "text"},
        _receive(16, termination="eom"),  # a serial line has no message boundary
        _receive(0),
        _receive(TRANSFER_CEILING + 1),
        _receive(True),
        _receive(16, exact=17),
        _receive(16, exact=-1),
    ],
    ids=[
        "unknown-kind",
        "extra-field",
        "missing-field",
        "exchange-without-data",
        "text-data",
        "eom",
        "zero-max",
        "over-ceiling",
        "bool-max",
        "exact-over-max",
        "negative-exact",
    ],
)
def test_transfer_refuses_what_section_8_1_does_not_allow(
    transport: ScriptedTransport, link: SerialLink, tmp_path: Path, transaction: dict[str, Any]
) -> None:
    services = _services(link, tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(services.transfer(transaction, _context()))
    assert transport.written == []


def test_transfer_refuses_cancelled_or_expired_context(
    transport: ScriptedTransport, link: SerialLink, tmp_path: Path
) -> None:
    services = _services(link, tmp_path)
    cancelled = _context()
    cancelled.cancel()
    with pytest.raises(TimeoutError):
        asyncio.run(services.transfer({"kind": "stream_send", "data": b"x"}, cancelled))
    expired = AdcOperationContext("op-late", time.monotonic() - 1.0)
    with pytest.raises(TimeoutError):
        asyncio.run(services.transfer(_receive(16, exact=4), expired))
    assert transport.written == []


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


def test_record_evidence_host_fields_win_over_the_entry(
    link: SerialLink, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence.jsonl"
    services = _services(link, tmp_path, evidence_path=evidence)
    monkeypatch.setattr(services, "utc_now", lambda: "host-clock")
    entry = {"kind": "probe", "at": "caller", "operation_id": "op-spoof"}
    asyncio.run(services.record_evidence(entry, _context()))

    record = json.loads(evidence.read_text(encoding="utf-8"))
    assert record == {"kind": "probe", "at": "host-clock", "operation_id": "op-test"}
    assert entry["operation_id"] == "op-spoof"  # the caller's dict is not mutated


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
