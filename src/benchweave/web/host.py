"""Host-side SDK services for the ADC adapter.

This is the other half of the benchweave-sdk contract: the adapter owns
protocol semantics and calls ``services.transfer`` for every byte; this
module owns the actual serial port, clocks, evidence, and capture artifact
storage. No released BenchWeave gateway implements capture/streaming host
services yet, so the ADC app carries its own — structurally compatible with
``benchweave_sdk.interfaces.HostServices``/``CaptureServices`` (Protocols;
never imported at runtime).

Threading model: a dedicated reader thread drains the serial port into a
bounded ring buffer the moment bytes arrive (2 Mbps never backs up into the
OS buffer). ``transfer`` takes from that ring directly when it already holds
what a receive asks for, and otherwise waits on it via the event loop's
executor, so the host loop stays free while the wire is quiet.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

#: Ring capacity: ~4 s of full-rate SAMPLE traffic; the reader drops the
#: oldest bytes beyond it (a stalled consumer must not grow memory forever).
RING_CAPACITY = 256 * 1024
#: Ceiling on any single receive's max_bytes declaration.
TRANSFER_CEILING = 64 * 1024
#: How long a receive waits for its first byte before answering that the
#: line is quiet (``b""``).
RECEIVE_WAIT_S = 0.1

#: OTDP section 8.1 stream transactions: every field of a kind is required
#: and no other is accepted. A serial line has no message boundary, so
#: ``eom`` termination is refused.
_TERMINATORS = {"lf": b"\n", "crlf": b"\r\n"}
_RECEIVE_FIELDS = frozenset({"max_bytes", "termination", "exact_bytes"})
_FIELDS = {
    "stream_send": frozenset({"kind", "data"}),
    "stream_receive": frozenset({"kind"}) | _RECEIVE_FIELDS,
    "stream_exchange": frozenset({"kind", "data"}) | _RECEIVE_FIELDS,
}


class Transport(Protocol):
    """The slice of ``serial.Serial`` the link needs (injectable in tests)."""

    timeout: float | None

    @property
    def in_waiting(self) -> int: ...

    def read(self, size: int = 1) -> bytes: ...

    def write(self, data: bytes) -> int | None: ...

    def close(self) -> None: ...

    @property
    def is_open(self) -> bool: ...


class SerialLink:
    """Owns one serial transport: a reader thread feeding a bounded ring.

    The reader drains the port continuously so the OS buffer never overflows
    at 2 Mbps; consumers take bytes from the ring with a bounded wait. A
    transport fault flips ``faulted`` and wakes every waiter.
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._ring = bytearray()
        self._condition = threading.Condition()
        self._running = True
        self._faulted = False
        self._reader = threading.Thread(target=self._reader_loop, name="adc-link", daemon=True)
        self._reader.start()

    @property
    def faulted(self) -> bool:
        return self._faulted

    def write(self, data: bytes) -> None:
        if self._faulted or not self._running:
            raise ConnectionError("serial link is closed or faulted")
        try:
            self._transport.write(data)
        except Exception as exc:
            self._fault()
            raise ConnectionError(f"serial write failed: {exc}") from exc

    @property
    def buffered(self) -> int:
        """Bytes read from the port and not yet taken."""
        with self._condition:
            return len(self._ring)

    def take(self, exact: int, terminator: bytes, max_bytes: int, timeout: float) -> bytes | None:
        """One receive from the ring, waiting at most ``timeout`` for it to complete.

        A positive ``exact`` takes exactly that many bytes; otherwise the
        bytes up to and including ``terminator``, which must appear within
        ``max_bytes``. Returns ``None`` while the receive is incomplete, and
        the bytes stay in the ring for the next call.
        """
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._condition:
            while True:
                ring = self._ring
                if exact:
                    if len(ring) >= exact:
                        return self._pop(exact)
                else:
                    end = ring.find(terminator, 0, max_bytes)
                    if end >= 0:
                        return self._pop(end + len(terminator))
                    if len(ring) >= max_bytes:
                        del ring[:max_bytes]  # discard, so the next call can resynchronise
                        raise ValueError(f"no terminator within {max_bytes} bytes")
                if self._faulted or not self._running:
                    raise ConnectionError("serial link is closed or faulted")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def _pop(self, count: int) -> bytes:
        taken = bytes(self._ring[:count])
        del self._ring[:count]
        return taken

    def close(self) -> None:
        self._running = False
        with self._condition:
            self._condition.notify_all()
        if self._reader is not threading.current_thread():
            self._reader.join(timeout=2.0)
        with contextlib.suppress(Exception):  # best-effort close of a dying port
            self._transport.close()

    def _fault(self) -> None:
        self._faulted = True
        with self._condition:
            self._condition.notify_all()

    def _reader_loop(self) -> None:
        while self._running:
            try:
                if not self._transport.is_open:
                    break
                pending = self._transport.in_waiting or 1
                data = self._transport.read(pending)
                if not data:
                    continue
                with self._condition:
                    self._ring += data
                    overflow = len(self._ring) - RING_CAPACITY
                    if overflow > 0:
                        # Drop the OLDEST bytes; the parser resynchronises on
                        # the next SYNC marker downstream.
                        del self._ring[:overflow]
                    self._condition.notify_all()
            except Exception:
                if self._running:
                    self._fault()
                break
        self._running = False
        with self._condition:
            self._condition.notify_all()


@dataclass
class AdcOperationContext:
    """Per-operation identity, deadline, and cancellation (host-issued)."""

    operation_id: str
    deadline_monotonic: float
    dataset_id: str | None = None

    def __post_init__(self) -> None:
        self._cancelled = threading.Event()
        self.dispatched = False

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        self._cancelled.set()

    async def mark_dispatch_started(self) -> None:
        self.dispatched = True


class SerialHostServices:
    """HostServices + CaptureServices over one :class:`SerialLink`.

    ``transfer`` speaks OTDP section 8.1's stream grammar: ``stream_send``
    writes ``data`` and answers ``{}``; ``stream_receive`` reads; and
    ``stream_exchange`` does both. A receive takes exactly ``exact_bytes``
    when that is positive, and otherwise the bytes through an ``lf`` or
    ``crlf`` terminator found within ``max_bytes`` (at most
    :data:`TRANSFER_CEILING`). A receive that sees no bytes at all for
    :data:`RECEIVE_WAIT_S` answers ``b""``: the line is quiet. An unfinished
    receive is never returned early; its bytes stay buffered for the next
    one, across a deadline too.
    """

    def __init__(
        self,
        link: SerialLink,
        *,
        artifact_dir: Path,
        evidence_path: Path | None = None,
    ) -> None:
        self._link = link
        self._artifact_dir = artifact_dir
        self._evidence_path = evidence_path
        self._artifacts: dict[str, Path] = {}

    # -- clocks ----------------------------------------------------------------

    def monotonic(self) -> float:
        return time.monotonic()

    def utc_now(self) -> str:
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    # -- transport ---------------------------------------------------------------

    def _live(self, context: Any) -> None:
        if context.is_cancelled() or self.monotonic() >= context.deadline_monotonic:
            raise TimeoutError("operation cancelled or expired")

    async def transfer(self, transaction: dict[str, Any], context: Any) -> dict[str, Any]:
        kind = transaction.get("kind")
        if kind not in _FIELDS or set(transaction) != _FIELDS[kind]:
            fields = sorted(str(key) for key in transaction)
            raise ValueError(f"not an OTDP section 8.1 stream transaction: {fields}")
        receive = None if kind == "stream_send" else _receive_bounds(transaction)
        if kind != "stream_receive" and not isinstance(transaction["data"], bytes):
            raise ValueError("data must be bytes")
        self._live(context)
        if kind != "stream_receive":
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._link.write, transaction["data"])
        if receive is None:
            return {}
        return {"data": await self._receive(*receive, context)}

    async def _receive(self, exact: int, terminator: bytes, max_bytes: int, context: Any) -> bytes:
        quiet_until = self.monotonic() + RECEIVE_WAIT_S
        # What the ring already holds is taken without a thread hop.
        taken = self._link.take(exact, terminator, max_bytes, 0.0)
        loop = asyncio.get_running_loop()
        while taken is None:
            if not self._link.buffered and self.monotonic() >= quiet_until:
                return b""  # nothing offered: a quiet line, not an error
            self._live(context)  # an unfinished receive stays buffered
            wait = min(RECEIVE_WAIT_S, context.deadline_monotonic - self.monotonic())
            taken = await loop.run_in_executor(
                None, self._link.take, exact, terminator, max_bytes, wait
            )
        return taken

    async def close_transport(self, context: Any) -> None:
        self._link.close()

    # -- evidence ----------------------------------------------------------------

    async def record_evidence(self, entry: dict[str, Any], context: Any) -> None:
        if self._evidence_path is None:
            return
        line = json.dumps(
            {"at": self.utc_now(), "operation_id": context.operation_id, **entry},
            separators=(",", ":"),
        )
        self._evidence_path.parent.mkdir(parents=True, exist_ok=True)
        with self._evidence_path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    # -- capture artifacts ---------------------------------------------------------

    async def artifact_append(self, capture_id: str, data: bytes, context: Any) -> None:
        part = self._artifacts.get(capture_id)
        if part is None:
            self._artifact_dir.mkdir(parents=True, exist_ok=True)
            part = self._artifact_dir / f"{capture_id}.part"
            part.write_bytes(b"")
            self._artifacts[capture_id] = part
        with part.open("ab") as stream:
            stream.write(data)

    async def artifact_finalise(
        self, capture_id: str, metadata: dict[str, Any], context: Any
    ) -> dict[str, Any]:
        part = self._artifacts.pop(capture_id, None)
        if part is None:
            raise ValueError(f"unknown capture artifact: {capture_id}")
        final = part.with_suffix(".bin")
        part.rename(final)
        raw = final.read_bytes()
        return {
            "artifact_id": capture_id,
            "encoding": str(metadata.get("encoding", "u8")),
            "byte_length": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    async def artifact_abort(self, capture_id: str) -> None:
        part = self._artifacts.pop(capture_id, None)
        if part is not None:
            part.unlink(missing_ok=True)


def _receive_bounds(transaction: dict[str, Any]) -> tuple[int, bytes, int]:
    """Validate a receive's fields; returns ``(exact, terminator, max_bytes)``."""
    max_bytes = transaction["max_bytes"]
    termination = transaction["termination"]
    exact = transaction["exact_bytes"]
    if not _is_int(max_bytes) or not 1 <= max_bytes <= TRANSFER_CEILING:
        raise ValueError(f"max_bytes must be 1..{TRANSFER_CEILING}")
    if not isinstance(termination, str) or termination not in _TERMINATORS:
        raise ValueError("termination must be 'lf' or 'crlf' on a serial line")
    if exact is not None and (not _is_int(exact) or not 0 <= exact <= max_bytes):
        raise ValueError("exact_bytes must be None or 0..max_bytes")
    # A positive exact_bytes takes precedence over the terminator.
    return exact or 0, _TERMINATORS[termination], max_bytes


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
