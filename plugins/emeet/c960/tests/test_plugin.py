import asyncio
import json
from importlib.resources import files
from benchweave_sdk.testing import MockContext, MockHost
from benchweave_sdk.validation import validate_descriptor, validate_result
from benchweave_emeet_c960.adapter import create_plugin
from benchweave_emeet_c960.protocol import transaction


def _descriptor():
    return json.loads(files("benchweave_emeet_c960").joinpath("descriptor.json").read_text())


def test_identify_read_write():
    async def run():
        descriptor = _descriptor()
        validate_descriptor(descriptor)
        host = MockHost([
            (transaction("identify"), {"data": b"EMeet,SmartCam C960 4K\n"}),
            (transaction("read", "brightness"), {"data": b"0\n"}),
            (transaction("read", "white_balance_automatic"), {"data": b"1\n"}),
            (transaction("read", "power_line_frequency"), {"data": b"50 Hz\n"}),
            (transaction("write", "brightness", 32), {"data": b"OK\n"}),
        ])
        plugin = create_plugin()
        context = MockContext("op-1", deadline_monotonic=1.0)
        await plugin.open(descriptor, host, context)
        cases = [
            ("identify", {}),
            ("read", {"parameter": "brightness"}),
            ("read", {"parameter": "white_balance_automatic"}),
            ("read", {"parameter": "power_line_frequency"}),
            ("write", {"parameter": "brightness", "value": 32}),
        ]
        results = []
        for verb, args in cases:
            request = {"operation_id": "op-1", "verb": verb, "arguments": args}
            result = await plugin.execute(request, context)
            validate_result(result, request)
            assert result["status"] == "ok"
            results.append(result)
        assert results[0]["data"]["manufacturer"] == "EMeet"
        assert results[1]["data"]["value"] == 0
        assert results[2]["data"]["value"] is True
        assert results[3]["data"]["value"] == "50 Hz"
        assert results[4]["data"]["assurance"] == "acknowledged"
        host.assert_complete()
        await plugin.close(context)
        await plugin.close(context)
    asyncio.run(run())


def test_quiet_lifecycle():
    from benchweave_sdk.conformance import check_lifecycle
    descriptor = _descriptor()
    asyncio.run(check_lifecycle(create_plugin, descriptor))


def test_no_transmit_before_dispatch():
    async def run():
        descriptor = _descriptor()
        for reason in ("cancelled", "expired", "bad_arguments", "wrong_context"):
            host = MockHost([])
            plugin = create_plugin()
            context = MockContext("op", deadline_monotonic=1.0)
            await plugin.open(descriptor, host, context)
            request = {"operation_id": "op", "verb": "read",
                       "arguments": {"parameter": "brightness"}}
            if reason == "cancelled":
                context.cancel()
            elif reason == "expired":
                host.advance(1.0)
            elif reason == "bad_arguments":
                request["arguments"]["parameter"] = "unknown"
            else:
                context.operation_id = "other"
            result = await plugin.execute(request, context)
            validate_result(result, request)
            assert result["status"] == "error"
            assert result["error"]["dispatch_state"] == "not_dispatched"
            assert not context.dispatched and not host.transfers
            await plugin.close(MockContext("cleanup", deadline_monotonic=2.0))
    asyncio.run(run())


def test_write_rejects_bad_values_before_dispatch():
    async def run():
        descriptor = _descriptor()
        host = MockHost([])
        plugin = create_plugin()
        context = MockContext("op", deadline_monotonic=1.0)
        await plugin.open(descriptor, host, context)
        for value in (65, -65, 3.5, "32", True):
            request = {"operation_id": "op", "verb": "write",
                       "arguments": {"parameter": "brightness", "value": value}}
            result = await plugin.execute(request, context)
            validate_result(result, request)
            assert result["status"] == "error"
            assert result["error"]["dispatch_state"] == "not_dispatched"
        request = {"operation_id": "op", "verb": "write",
                   "arguments": {"parameter": "nope", "value": 1}}
        result = await plugin.execute(request, context)
        assert result["status"] == "error"
        assert not context.dispatched and not host.transfers
        await plugin.close(context)
    asyncio.run(run())


def test_uncertain_response_after_dispatch():
    async def run():
        descriptor = _descriptor()
        responses = (
            {"data": b"nan\n"},
            {"data": b"32"},
            {"data": b"\xff\n"},
            ConnectionError("lost"),
            TimeoutError("expired"),
            RuntimeError("host"),
        )
        for response in responses:
            host = MockHost([(transaction("read", "brightness"), response)])
            plugin = create_plugin()
            context = MockContext("op", deadline_monotonic=1.0)
            await plugin.open(descriptor, host, context)
            request = {"operation_id": "op", "verb": "read",
                       "arguments": {"parameter": "brightness"}}
            result = await plugin.execute(request, context)
            validate_result(result, request)
            assert result["status"] == "unknown"
            assert result["error"]["dispatch_state"] == "unknown"
            assert context.dispatched
            host.assert_complete()
            await plugin.close(context)
    asyncio.run(run())
