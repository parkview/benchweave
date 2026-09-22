"""Throughput proof: a bulk chunk of SAMPLE frames with no per-sample I/O.

At 2 Mbps the board can push hundreds of SAMPLE frames between host pulls.
This test feeds one read's worth of concatenated frames through the REAL
SerialLink + adapter stack and asserts every sample surfaces in order from
``next_event``. OTDP section 8.1 exact-byte reads cost two transfers per
frame (its header, then the rest), so the cost that matters is waiting on
the reader thread: a burst already in the ring is taken on the host loop,
and draining it waits a handful of times, not once per sample.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from benchweave.web.host import AdcOperationContext, SerialHostServices, SerialLink
from plugins.adc_6ch_12bit.adapter import AdcAdapter, create_plugin
from tests.adc.fakeboard import FakeBoardTransport

DESCRIPTOR_PATH = Path(__file__).resolve().parents[2] / "plugins/adc_6ch_12bit/descriptor.json"
DESCRIPTOR: dict[str, Any] = json.loads(DESCRIPTOR_PATH.read_text(encoding="utf-8"))

#: A burst of SAMPLE frames (23 bytes each) landing between host pulls.
FRAMES = 150


class _CountingLink(SerialLink):
    """Counts the receives that had to wait on the reader thread."""

    def __init__(self, transport: FakeBoardTransport) -> None:
        super().__init__(transport)
        self.waits = 0

    def take(self, exact: int, terminator: bytes, max_bytes: int, timeout: float) -> bytes | None:
        if timeout > 0:
            self.waits += 1
        return super().take(exact, terminator, max_bytes, timeout)


class _CountingServices(SerialHostServices):
    def __init__(self, link: SerialLink, *, artifact_dir: Path) -> None:
        super().__init__(link, artifact_dir=artifact_dir)
        self.transfers = 0

    async def transfer(self, transaction: dict[str, Any], context: Any) -> dict[str, Any]:
        self.transfers += 1
        return await super().transfer(transaction, context)


def _context(name: str, timeout_s: float = 10.0) -> AdcOperationContext:
    return AdcOperationContext(name, time.monotonic() + timeout_s)


async def _invoke(
    adapter: AdcAdapter, operation_id: str, action_id: str, action_input: dict[str, Any]
) -> dict[str, Any]:
    result = await adapter.execute(
        {
            "operation_id": operation_id,
            "verb": "invoke",
            "arguments": {"action_id": action_id, "input": action_input},
        },
        _context(operation_id),
    )
    assert result["status"] == "ok", result
    return result


def test_bulk_sample_chunk_arrives_in_order_without_io_amplification(
    tmp_path: Path,
) -> None:
    samples = [(i, (i & 0xFFF, 1, 2, 3, 4, 5)) for i in range(FRAMES)]
    # The whole burst lands in the fake's outgoing buffer on ONE read call.
    transport = FakeBoardTransport(samples, frames_per_read=FRAMES)
    link = _CountingLink(transport)
    services = _CountingServices(link, artifact_dir=tmp_path)

    async def scenario() -> tuple[list[int], int, int]:
        adapter = create_plugin()
        await adapter.open(DESCRIPTOR, services, _context("open"))
        await _invoke(
            adapter,
            "op-configure",
            "otdp.daq.configure/1.0.0",
            {
                "configuration_id": "cfg-bulk",
                "channels": [
                    {
                        "channel": "a0",
                        "quantity": "voltage",
                        "unit": "V",
                        "range": {"mode": "auto"},
                    }
                ],
                "sample_rate_hz": 1000.0,
                "sample_count": 1_000_000,
                "sampling": "simultaneous",
                "trigger": {"kind": "immediate"},
            },
        )
        await _invoke(
            adapter,
            "op-arm",
            "otdp.daq.arm/1.0.0",
            {
                "configuration_id": "cfg-bulk",
                "acquisition_id": "acq-bulk",
                "max_duration_ms": 60_000,
            },
        )

        transfers_before = services.transfers
        waits_before = link.waits
        counters: list[int] = []
        deadline = time.monotonic() + 10.0
        while len(counters) < FRAMES and time.monotonic() < deadline:
            event = await adapter.next_event("acq-bulk", _context("op-event"))
            if event is None:
                continue
            counters.append(int(event["x-adc-sample"]["counter"]))
        transfers_during = services.transfers - transfers_before
        waits_during = link.waits - waits_before

        await _invoke(adapter, "op-abort", "otdp.daq.abort/1.0.0", {"acquisition_id": "acq-bulk"})
        await adapter.close(_context("close"))
        return counters, transfers_during, waits_during

    try:
        counters, transfers_during, waits_during = asyncio.run(scenario())
    finally:
        link.close()

    assert counters == list(range(FRAMES))  # every sample, in firmware order
    # Draining 150 buffered samples waits on the reader thread a few times,
    # never once per sample...
    assert waits_during <= FRAMES // 10
    # ...and costs two exact reads per frame; anything more is a quiet read,
    # which only happens after a wait.
    assert 2 * FRAMES <= transfers_during <= 2 * FRAMES + waits_during
