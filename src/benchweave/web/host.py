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
OS buffer), and ``transfer`` waits on that ring via the event loop's
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
#: Ceiling on any single transfer's max_bytes declaration.
TRANSFER_CEILING = 64 * 1024
#: How long one quiet receive waits for bytes before returning empty.
RECEIVE_WAIT_S = 0.1


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

    def read_available(self, max_bytes: int, timeout: float) -> bytes:
        """Up to ``max_bytes`` from the ring, waiting at most ``timeout``."""
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._condition:
            while not self._ring:
                if self._faulted or not self._running:
                    raise ConnectionError("serial link is closed or faulted")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._condition.wait(remaining)
            taken = bytes(self._ring[:max_bytes])
            del self._ring[: len(taken)]
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

    ``transfer`` understands two transaction kinds, matching the adapter's
    vocabulary: ``stream_exchange`` (write ``data``, then one bounded read)
    and ``stream_receive`` (one bounded read only). Declared ``max_bytes``
    are enforced against :data:`TRANSFER_CEILING`.
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

    async def transfer(self, transaction: dict[str, Any], context: Any) -> dict[str, Any]:
        if context.is_cancelled() or self.monotonic() >= context.deadline_monotonic:
            raise TimeoutError("operation cancelled or expired")
        kind = transaction.get("kind")
        max_bytes = min(int(transaction.get("max_bytes", 4096)), TRANSFER_CEILING)
        if max_bytes <= 0:
            raise ValueError(f"max_bytes must be positive: {max_bytes}")
        remaining = context.deadline_monotonic - self.monotonic()
        wait = min(remaining, RECEIVE_WAIT_S)
        loop = asyncio.get_running_loop()
        if kind == "stream_exchange":
            data = bytes(transaction.get("data", b""))
            await loop.run_in_executor(None, self._link.write, data)
            received = await loop.run_in_executor(None, self._link.read_available, max_bytes, wait)
            return {"data": received}
        if kind == "stream_receive":
            received = await loop.run_in_executor(None, self._link.read_available, max_bytes, wait)
            return {"data": received}
        raise ValueError(f"unsupported transaction kind: {kind!r}")

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
