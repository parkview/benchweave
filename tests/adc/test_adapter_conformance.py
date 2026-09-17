"""SDK conformance for the ADC adapter: exact bytes, honest dispatch states."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from benchweave_sdk.conformance import check_lifecycle, check_operation
from benchweave_sdk.testing import MockContext, MockHost
from benchweave_sdk.validation import validate, validate_descriptor

from plugins.adc_6ch_12bit import protocol
from plugins.adc_6ch_12bit.adapter import (
    EXCHANGE,
    RECEIVE,
    RECEIVE_MAX_BYTES,
    AdcAdapter,
    _nearest_averaging,
    create_plugin,
)

DESCRIPTOR_PATH = Path(__file__).resolve().parents[2] / "plugins/adc_6ch_12bit/descriptor.json"
DESCRIPTOR: dict[str, Any] = json.loads(DESCRIPTOR_PATH.read_text(encoding="utf-8"))

CONFIGURE = "otdp.daq.configure/1.0.0"
ARM = "otdp.daq.arm/1.0.0"
TRIGGER = "otdp.daq.trigger/1.0.0"
FETCH = "otdp.daq.fetch/1.0.0"
ABORT = "otdp.daq.abort/1.0.0"


def _exchange(command: protocol.FrameType, seq: int, payload: bytes = b"") -> dict[str, Any]:
    frame = protocol.Frame(type=int(command), seq=seq, payload=payload)
    return {"kind": EXCHANGE, "data": protocol.encode_frame(frame), "max_bytes": protocol.MAX_FRAME}


def _receive() -> dict[str, Any]:
    return {"kind": RECEIVE, "max_bytes": RECEIVE_MAX_BYTES}


def _ack(command: protocol.FrameType, value: int = 0) -> bytes:
    payload = bytes((int(command),)) + value.to_bytes(2, "little")
    return protocol.encode_frame(
        protocol.Frame(type=int(protocol.FrameType.ACK), seq=0, payload=payload)
    )


def _nak(command: protocol.FrameType, error: int) -> bytes:
    payload = bytes((int(command), error))
    return protocol.encode_frame(
        protocol.Frame(type=int(protocol.FrameType.NAK), seq=0, payload=payload)
    )


def _identify_rsp() -> bytes:
    payload = bytes((1, 0, 2, 6, 12))  # proto 1, fw 0.2, 6 channels, 12-bit
    return protocol.encode_frame(
        protocol.Frame(type=int(protocol.FrameType.IDENTIFY_RSP), seq=0, payload=payload)
    )


def _sample(counter: int, channels: tuple[int, ...]) -> bytes:
    payload = counter.to_bytes(4, "little") + b"".join(
        value.to_bytes(2, "little") for value in channels
    )
    return protocol.encode_frame(
        protocol.Frame(type=int(protocol.FrameType.SAMPLE), seq=0, payload=payload)
    )


def _configure_input(channels: list[str], rate: float = 100.0, count: int = 10) -> dict[str, Any]:
    return {
        "configuration_id": "cfg-1",
        "channels": [
            {
                "channel": channel,
                "quantity": "voltage",
                "unit": "V",
                "range": {"mode": "auto"},
            }
            for channel in channels
        ],
        "sample_rate_hz": rate,
        "sample_count": count,
        "sampling": "simultaneous",
        "trigger": {"kind": "immediate"},
    }


def _invoke_request(
    operation_id: str, action_id: str, action_input: dict[str, Any]
) -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "verb": "invoke",
        "arguments": {"action_id": action_id, "input": action_input},
    }


async def _open_adapter(host: MockHost) -> AdcAdapter:
    adapter = create_plugin()
    await adapter.open(DESCRIPTOR, host, MockContext("open", deadline_monotonic=10.0))
    return adapter


def test_descriptor_is_schema_valid() -> None:
    validate_descriptor(json.loads(DESCRIPTOR_PATH.read_text(encoding="utf-8")))


def test_lifecycle_is_quiet() -> None:
    asyncio.run(check_lifecycle(create_plugin, DESCRIPTOR))


def test_identify_round_trip() -> None:
    host = MockHost([(_exchange(protocol.FrameType.IDENTIFY, 0), {"data": _identify_rsp()})])

    async def scenario() -> dict[str, Any]:
        adapter = await _open_adapter(host)
        context = MockContext("op-identify", deadline_monotonic=10.0)
        request = {"operation_id": "op-identify", "verb": "identify", "arguments": {}}
        return await check_operation(adapter, request, context)

    result = asyncio.run(scenario())
    assert result["status"] == "ok"
    assert result["data"]["firmware"] == "0.2"
    host.assert_complete()


def test_capture_sequence_configure_arm_events_fetch_abort() -> None:
    averaging, achieved = _nearest_averaging(100.0, 2)
    host = MockHost(
        [
            (
                _exchange(
                    protocol.FrameType.SET_AVERAGING, 0, protocol.build_set_averaging(averaging)
                ),
                {"data": _ack(protocol.FrameType.SET_AVERAGING, averaging)},
            ),
            (
                _exchange(protocol.FrameType.SET_CHANNELS, 1, protocol.build_set_channels(0b11)),
                {"data": _ack(protocol.FrameType.SET_CHANNELS)},
            ),
            (
                _exchange(protocol.FrameType.START_STREAM, 2),
                {"data": _ack(protocol.FrameType.START_STREAM)},
            ),
            # One pull returns two samples; next_event consumes the first.
            (
                _receive(),
                {"data": _sample(7, (1, 2, 3, 4, 5, 6)) + _sample(8, (7, 8, 9, 10, 11, 12))},
            ),
            # fetch's pump finds the line quiet.
            (_receive(), {"data": b""}),
            (
                _exchange(protocol.FrameType.STOP_STREAM, 3),
                {"data": _ack(protocol.FrameType.STOP_STREAM)},
            ),
        ]
    )

    async def scenario() -> (
        tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any], dict[str, Any]]
    ):
        adapter = await _open_adapter(host)

        configure = await check_operation(
            adapter,
            _invoke_request("op-configure", CONFIGURE, _configure_input(["a0", "a1"])),
            MockContext("op-configure", deadline_monotonic=10.0),
        )
        arm = await check_operation(
            adapter,
            _invoke_request(
                "op-arm",
                ARM,
                {"configuration_id": "cfg-1", "acquisition_id": "acq-1", "max_duration_ms": 60000},
            ),
            MockContext("op-arm", deadline_monotonic=10.0),
        )
        assert arm["data"]["result"]["state"] == "running"

        event = await adapter.next_event(
            "acq-1", MockContext("op-event", deadline_monotonic=10.0)
        )
        fetch = await check_operation(
            adapter,
            _invoke_request(
                "op-fetch",
                FETCH,
                {"acquisition_id": "acq-1", "max_bytes": 1_000_000, "allow_partial": True},
            ),
            MockContext("op-fetch", deadline_monotonic=10.0),
        )
        abort = await check_operation(
            adapter,
            _invoke_request("op-abort", ABORT, {"acquisition_id": "acq-1"}),
            MockContext("op-abort", deadline_monotonic=10.0),
        )
        return configure, event, fetch, abort

    configure, event, fetch, abort = asyncio.run(scenario())
    host.assert_complete()

    effective = configure["data"]["result"]["effective_configuration"]
    assert effective["sample_rate_hz"] == achieved  # achieved, not requested
    assert configure["data"]["result"]["configuration_id"] == "cfg-1"

    assert event is not None
    validate(event, "otdp/0.1.0/otdp-runtime.schema.json", "event")
    assert event["kind"] == "telemetry"
    assert event["x-adc-sample"] == {
        "counter": 7,
        "channels": [1, 2, 3, 4, 5, 6],
        "averaged_n": averaging,
    }

    dataset = fetch["data"]["result"]
    validate(dataset, "otdp/0.1.0/otdp-measurement.schema.json", "dataset")
    assert dataset["status"] == "partial"  # acquisition still running at fetch
    values = {variable["id"]: variable["values"] for variable in dataset["variables"]}
    assert values == {"a0": [7.0], "a1": [8.0]}  # the second sample, first consumed

    assert abort["data"]["result"] == {"acquisition_id": "acq-1", "state": "aborted"}


def test_software_trigger_arms_without_wire_io() -> None:
    averaging, _ = _nearest_averaging(50.0, 1)
    configure_input = _configure_input(["a0"], rate=50.0, count=1)
    configure_input["trigger"] = {"kind": "software"}
    host = MockHost(
        [
            (
                _exchange(
                    protocol.FrameType.SET_AVERAGING, 0, protocol.build_set_averaging(averaging)
                ),
                {"data": _ack(protocol.FrameType.SET_AVERAGING, averaging)},
            ),
            (
                _exchange(protocol.FrameType.SET_CHANNELS, 1, protocol.build_set_channels(0b1)),
                {"data": _ack(protocol.FrameType.SET_CHANNELS)},
            ),
            # Trigger fires SAMPLE_ONCE because sample_count == 1.
            (
                _exchange(protocol.FrameType.SAMPLE_ONCE, 2),
                {"data": _ack(protocol.FrameType.SAMPLE_ONCE)},
            ),
        ]
    )

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        adapter = await _open_adapter(host)
        await check_operation(
            adapter,
            _invoke_request("op-c", CONFIGURE, configure_input),
            MockContext("op-c", deadline_monotonic=10.0),
        )
        arm = await check_operation(
            adapter,
            _invoke_request(
                "op-a",
                ARM,
                {"configuration_id": "cfg-1", "acquisition_id": "acq-s", "max_duration_ms": 1000},
            ),
            MockContext("op-a", deadline_monotonic=10.0),
        )
        trigger = await check_operation(
            adapter,
            _invoke_request("op-t", TRIGGER, {"acquisition_id": "acq-s"}),
            MockContext("op-t", deadline_monotonic=10.0),
        )
        return arm, trigger

    arm, trigger = asyncio.run(scenario())
    host.assert_complete()
    assert arm["data"]["result"]["state"] == "armed"  # no wire I/O at arm
    assert trigger["data"]["result"]["state"] == "running"


def test_nak_is_device_rejected_with_dispatched_state() -> None:
    host = MockHost(
        [
            (
                _exchange(protocol.FrameType.IDENTIFY, 0),
                {"data": _nak(protocol.FrameType.IDENTIFY, 0x03)},
            )
        ]
    )

    async def scenario() -> dict[str, Any]:
        adapter = await _open_adapter(host)
        return await check_operation(
            adapter,
            {"operation_id": "op-nak", "verb": "identify", "arguments": {}},
            MockContext("op-nak", deadline_monotonic=10.0),
        )

    result = asyncio.run(scenario())
    assert result["status"] == "error"
    assert result["error"]["code"] == "DEVICE_REJECTED"
    assert result["error"]["dispatch_state"] == "dispatched"


def test_post_dispatch_transport_loss_reports_unknown() -> None:
    host = MockHost(
        [(_exchange(protocol.FrameType.RESET, 0), ConnectionError("cable pulled"))]
    )

    async def scenario() -> dict[str, Any]:
        adapter = await _open_adapter(host)
        return await check_operation(
            adapter,
            {"operation_id": "op-lost", "verb": "reset", "arguments": {}},
            MockContext("op-lost", deadline_monotonic=10.0),
        )

    result = asyncio.run(scenario())
    assert result["status"] == "unknown"
    assert result["error"]["code"] == "TRANSPORT_ERROR"
    assert result["error"]["dispatch_state"] == "unknown"


def test_expired_context_never_transmits() -> None:
    host = MockHost([])
    host.advance(5.0)

    async def scenario() -> dict[str, Any]:
        adapter = await _open_adapter(host)
        return await adapter.execute(
            {"operation_id": "op-late", "verb": "identify", "arguments": {}},
            MockContext("op-late", deadline_monotonic=1.0),
        )

    result = asyncio.run(scenario())
    assert result["status"] == "error"
    assert result["error"]["code"] == "TIMEOUT"
    assert result["error"]["dispatch_state"] == "not_dispatched"
    assert host.transfers == []  # nothing reached the wire


def test_cancelled_context_reports_cancelled_without_transmit() -> None:
    host = MockHost([])

    async def scenario() -> dict[str, Any]:
        adapter = await _open_adapter(host)
        context = MockContext("op-cancel", deadline_monotonic=10.0)
        context.cancel()
        return await adapter.execute(
            {"operation_id": "op-cancel", "verb": "identify", "arguments": {}}, context
        )

    result = asyncio.run(scenario())
    assert result["status"] == "cancelled"
    assert result["error"]["code"] == "CANCELLED"
    assert host.transfers == []


def test_interleaved_samples_inside_a_reply_are_buffered() -> None:
    averaging, _ = _nearest_averaging(100.0, 1)
    host = MockHost(
        [
            (
                _exchange(
                    protocol.FrameType.SET_AVERAGING, 0, protocol.build_set_averaging(averaging)
                ),
                {"data": _ack(protocol.FrameType.SET_AVERAGING, averaging)},
            ),
            (
                _exchange(protocol.FrameType.SET_CHANNELS, 1, protocol.build_set_channels(0b1)),
                {"data": _ack(protocol.FrameType.SET_CHANNELS)},
            ),
            (
                _exchange(protocol.FrameType.START_STREAM, 2),
                {"data": _ack(protocol.FrameType.START_STREAM)},
            ),
            # The stop reply arrives BEHIND a straggling sample; the sample
            # must be buffered, not lost, and the ACK still matched.
            (
                _exchange(protocol.FrameType.STOP_STREAM, 3),
                {"data": _sample(42, (9, 0, 0, 0, 0, 0)) + _ack(protocol.FrameType.STOP_STREAM)},
            ),
            # Post-abort fetch pumps once; the line is quiet.
        ]
    )

    async def scenario() -> dict[str, Any]:
        adapter = await _open_adapter(host)
        await check_operation(
            adapter,
            _invoke_request("op-c", CONFIGURE, _configure_input(["a0"])),
            MockContext("op-c", deadline_monotonic=10.0),
        )
        await check_operation(
            adapter,
            _invoke_request(
                "op-a",
                ARM,
                {"configuration_id": "cfg-1", "acquisition_id": "acq-i", "max_duration_ms": 1000},
            ),
            MockContext("op-a", deadline_monotonic=10.0),
        )
        await check_operation(
            adapter,
            _invoke_request("op-x", ABORT, {"acquisition_id": "acq-i"}),
            MockContext("op-x", deadline_monotonic=10.0),
        )
        return await check_operation(
            adapter,
            _invoke_request(
                "op-f",
                FETCH,
                {"acquisition_id": "acq-i", "max_bytes": 1_000_000, "allow_partial": True},
            ),
            MockContext("op-f", deadline_monotonic=10.0),
        )

    fetch = asyncio.run(scenario())
    host.assert_complete()
    dataset = fetch["data"]["result"]
    assert dataset["variables"][0]["values"] == [9.0]
    assert dataset["status"] == "complete"  # aborted acquisition, full drain


def test_fetch_budget_truncates_or_refuses() -> None:
    averaging, _ = _nearest_averaging(100.0, 1)
    samples = b"".join(_sample(i, (i, 0, 0, 0, 0, 0)) for i in range(4))
    host = MockHost(
        [
            (
                _exchange(
                    protocol.FrameType.SET_AVERAGING, 0, protocol.build_set_averaging(averaging)
                ),
                {"data": _ack(protocol.FrameType.SET_AVERAGING, averaging)},
            ),
            (
                _exchange(protocol.FrameType.SET_CHANNELS, 1, protocol.build_set_channels(0b1)),
                {"data": _ack(protocol.FrameType.SET_CHANNELS)},
            ),
            (
                _exchange(protocol.FrameType.START_STREAM, 2),
                {"data": _ack(protocol.FrameType.START_STREAM)},
            ),
            (_receive(), {"data": samples}),
            (_receive(), {"data": b""}),  # refused fetch drains the line first
            (_receive(), {"data": b""}),  # partial fetch pump
            (_receive(), {"data": b""}),  # final fetch pump
        ]
    )

    async def scenario() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        adapter = await _open_adapter(host)
        await check_operation(
            adapter,
            _invoke_request("op-c", CONFIGURE, _configure_input(["a0"])),
            MockContext("op-c", deadline_monotonic=10.0),
        )
        await check_operation(
            adapter,
            _invoke_request(
                "op-a",
                ARM,
                {"configuration_id": "cfg-1", "acquisition_id": "acq-b", "max_duration_ms": 1000},
            ),
            MockContext("op-a", deadline_monotonic=10.0),
        )
        # Prime the buffer with four samples.
        await adapter.next_event("acq-b", MockContext("op-e", deadline_monotonic=10.0))
        refused = await check_operation(
            adapter,
            _invoke_request(
                "op-r",
                FETCH,
                {"acquisition_id": "acq-b", "max_bytes": 16, "allow_partial": False},
            ),
            MockContext("op-r", deadline_monotonic=10.0),
        )
        partial = await check_operation(
            adapter,
            _invoke_request(
                "op-p",
                FETCH,
                {"acquisition_id": "acq-b", "max_bytes": 16, "allow_partial": True},
            ),
            MockContext("op-p", deadline_monotonic=10.0),
        )
        rest = await check_operation(
            adapter,
            _invoke_request(
                "op-q",
                FETCH,
                {"acquisition_id": "acq-b", "max_bytes": 1_000_000, "allow_partial": True},
            ),
            MockContext("op-q", deadline_monotonic=10.0),
        )
        return refused, partial, rest

    refused, partial, rest = asyncio.run(scenario())
    host.assert_complete()

    assert refused["status"] == "error"
    assert refused["error"]["code"] == "RESOURCE_LIMIT"

    # 16 bytes / 8 per value => 2 samples per partial fetch; one was consumed
    # by next_event, so 3 remain: 2 now, 1 in the follow-up.
    assert partial["data"]["result"]["variables"][0]["values"] == [1.0, 2.0]
    assert partial["data"]["result"]["status"] == "partial"
    assert rest["data"]["result"]["variables"][0]["values"] == [3.0]


def test_unknown_subscription_is_silent() -> None:
    host = MockHost([])

    async def scenario() -> dict[str, Any] | None:
        adapter = await _open_adapter(host)
        return await adapter.next_event(
            "nobody", MockContext("op-n", deadline_monotonic=10.0)
        )

    assert asyncio.run(scenario()) is None
    assert host.transfers == []


def test_unknown_action_and_verb_are_unsupported() -> None:
    host = MockHost([])

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        adapter = await _open_adapter(host)
        bad_action = await adapter.execute(
            _invoke_request("op-1", "otdp.daq.telepathy/1.0.0", {}),
            MockContext("op-1", deadline_monotonic=10.0),
        )
        bad_verb = await adapter.execute(
            {"operation_id": "op-2", "verb": "self_test", "arguments": {}},
            MockContext("op-2", deadline_monotonic=10.0),
        )
        return bad_action, bad_verb

    bad_action, bad_verb = asyncio.run(scenario())
    assert bad_action["error"]["code"] == "UNSUPPORTED"
    assert bad_verb["error"]["code"] == "UNSUPPORTED"
    assert host.transfers == []


def test_external_trigger_is_refused_as_unsupported() -> None:
    host = MockHost([])
    configure_input = _configure_input(["a0"])
    configure_input["trigger"] = {"kind": "external", "source_channel": "a0"}

    async def scenario() -> dict[str, Any]:
        adapter = await _open_adapter(host)
        return await adapter.execute(
            _invoke_request("op-x", CONFIGURE, configure_input),
            MockContext("op-x", deadline_monotonic=10.0),
        )

    result = asyncio.run(scenario())
    assert result["error"]["code"] == "UNSUPPORTED"
    assert host.transfers == []


@pytest.mark.parametrize("requested,channels", [(100.0, 2), (3000.0, 6), (20.0, 1)])
def test_nearest_averaging_is_a_valid_choice(requested: float, channels: int) -> None:
    averaging, achieved = _nearest_averaging(requested, channels)
    assert averaging in protocol.AVERAGING_CHOICES
    assert achieved > 0
