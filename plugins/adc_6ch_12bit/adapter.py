"""OTDP adapter for the 6-channel ADC board (benchweave-sdk Adapter contract).

The adapter owns protocol framing and command semantics only. All transport
goes through the host's ``services.transfer``; construction and ``open`` do
no device I/O; every wire-writing operation marks dispatch immediately
before its first transmit and reports honest ``dispatch_state`` on failure
(``status: "unknown"`` after a transmit whose outcome is uncertain, per the
SDK's ``Adapter`` docstring). Raw ADC counts cross this boundary — gain,
offset and computed channels are product-side concerns (see
docs/feature-request-measurement-profiles.md for the upstream story).

The host side of the contract lives in ``benchweave.web.host`` — this
project implements both halves because no released BenchWeave gateway
implements capture/streaming host services yet.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import protocol

#: Transaction vocabulary this adapter emits, in OTDP section 8.1's grammar:
#: a command goes out as ``stream_send`` and every read is an exact-byte
#: ``stream_receive`` (a frame's header, then the rest of that frame), so the
#: binary protocol never depends on a line terminator. An empty receive means
#: the line is quiet.
SEND = "stream_send"
RECEIVE = "stream_receive"
#: Frames one fetch drains at most, so a live stream cannot hold it forever.
FETCH_DRAIN_FRAMES = 128
#: Descriptor channel ids in firmware SAMPLE order (CHANNEL_KEYS lowercased).
CHANNEL_IDS = ("a0", "a1", "a2", "a3", "a4", "a7")

_ACTION_CONFIGURE = "otdp.daq.configure/1.0.0"
_ACTION_ARM = "otdp.daq.arm/1.0.0"
_ACTION_TRIGGER = "otdp.daq.trigger/1.0.0"
_ACTION_FETCH = "otdp.daq.fetch/1.0.0"
_ACTION_ABORT = "otdp.daq.abort/1.0.0"

#: Bytes budgeted per sample value in a fetch dataset (JSON float text).
_VALUE_BYTES = 8


class _Context(Protocol):
    operation_id: str
    deadline_monotonic: float

    def is_cancelled(self) -> bool: ...

    async def mark_dispatch_started(self) -> None: ...


class _Services(Protocol):
    def monotonic(self) -> float: ...

    def utc_now(self) -> str: ...

    async def transfer(self, transaction: dict[str, Any], context: Any) -> dict[str, Any]: ...

    async def close_transport(self, context: Any) -> None: ...


class _OperationError(Exception):
    """Internal signal carrying a complete OTDP error result."""

    def __init__(self, status: str, code: str, message: str, dispatch_state: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.dispatch_state = dispatch_state


@dataclass
class _Configuration:
    channel_mask: int
    averaging: int
    sample_count: int
    trigger_kind: str
    effective: dict[str, Any]


@dataclass
class _Acquisition:
    configuration_id: str
    state: str  # "armed" | "running" | "aborted"
    started_at: str
    sequence: int = 0
    fetches: int = 0
    samples: deque[tuple[int, tuple[int, ...]]] = field(default_factory=deque)


def create_plugin() -> AdcAdapter:
    """No-argument factory named by the descriptor's adapter entry point."""
    return AdcAdapter()


class AdcAdapter:
    """Async OTDP adapter: identify/reset/invoke plus telemetry events.

    A fresh instance serves one session: ``open`` binds the descriptor and
    host services (no I/O), ``execute`` runs one operation, ``next_event``
    drains buffered samples for a known acquisition (reading one frame when
    the buffer runs dry), and ``close`` tolerates repeated calls.
    """

    def __init__(self) -> None:
        self._descriptor: dict[str, Any] | None = None
        self._services: _Services | None = None
        self._parser = protocol.FrameParser()
        self._seq = 0
        self._configurations: dict[str, _Configuration] = {}
        self._acquisitions: dict[str, _Acquisition] = {}
        self._active_acquisition: str | None = None
        self._closed = False

    # -- lifecycle -----------------------------------------------------------

    async def open(self, descriptor: dict[str, Any], services: Any, context: Any) -> None:
        """Bind descriptor and services; deliberately free of device I/O."""
        self._descriptor = descriptor
        self._services = services
        self._closed = False

    async def close(self, context: Any) -> None:
        """Release the session; safe to call repeatedly."""
        if self._closed:
            return
        self._closed = True
        if self._services is not None:
            await self._services.close_transport(context)

    # -- operations ----------------------------------------------------------

    async def execute(self, request: dict[str, Any], context: Any) -> dict[str, Any]:
        """Run one operation envelope and return its result envelope."""
        operation_id = str(request["operation_id"])
        verb = str(request["verb"])
        arguments = request.get("arguments") or {}
        try:
            if verb == "identify":
                data = await self._identify(context)
            elif verb == "reset":
                data = await self._reset(context)
            elif verb == "invoke":
                data = await self._invoke(arguments, context)
            else:
                raise _OperationError(
                    "error", "UNSUPPORTED", f"verb not supported: {verb}", "not_dispatched"
                )
        except _OperationError as failure:
            return {
                "operation_id": operation_id,
                "verb": verb,
                "status": failure.status,
                "error": {
                    "code": failure.code,
                    "message": failure.message,
                    "dispatch_state": failure.dispatch_state,
                },
            }
        return {"operation_id": operation_id, "verb": verb, "status": "ok", "data": data}

    async def next_event(self, subscription_id: str, context: Any) -> dict[str, Any] | None:
        """Return the next buffered sample event for a known acquisition.

        Unknown subscriptions answer ``None`` with no transport activity, so
        a quiet lifecycle stays quiet. When the buffer runs dry for a live
        acquisition, one frame is read from the wire; a quiet line answers
        ``None`` rather than raising.
        """
        acquisition = self._acquisitions.get(subscription_id)
        if acquisition is None or acquisition.state not in ("running", "armed"):
            return None
        if not acquisition.samples:
            await self._pump(context)
        if not acquisition.samples:
            return None
        counter, channels = acquisition.samples.popleft()
        configuration = self._configurations[acquisition.configuration_id]
        first_channel = self._active_channel_ids(configuration.channel_mask)[0]
        sequence = acquisition.sequence
        acquisition.sequence += 1
        return {
            "subscription_id": subscription_id,
            "sequence": sequence,
            "kind": "telemetry",
            "reading": {
                "parameter": first_channel,
                "value": channels[CHANNEL_IDS.index(first_channel)],
                "unit": "count",
                "observed_at": self._require_services().utc_now(),
                "age_ms": 0,
                "quality": "valid",
                "source": "device",
            },
            # Schema-sanctioned extension key: the full frame, so the host
            # never reassembles six per-channel events per sample.
            "x-adc-sample": {
                "counter": counter,
                "channels": list(channels),
                "averaged_n": configuration.averaging,
            },
        }

    # -- verbs ---------------------------------------------------------------

    async def _identify(self, context: Any) -> dict[str, Any]:
        response = await self._transact(protocol.FrameType.IDENTIFY, b"", context)
        if response.type == protocol.FrameType.NAK:
            try:
                _, error = protocol.parse_nak(response.payload)
                message = f"device NAK for IDENTIFY: error {error:#x}"
            except ValueError as exc:
                message = f"malformed NAK payload for IDENTIFY: {exc}"
            raise _OperationError("error", "DEVICE_REJECTED", message, "dispatched")
        if response.type != protocol.FrameType.IDENTIFY_RSP:
            raise _OperationError(
                "error",
                "PROTOCOL_ERROR",
                f"unexpected response type {response.type:#x}",
                "dispatched",
            )
        try:
            info = protocol.parse_identify(response.payload)
        except ValueError as exc:
            raise _OperationError(
                "error", "PROTOCOL_ERROR", f"malformed IDENTIFY payload: {exc}", "dispatched"
            ) from exc
        return {
            "manufacturer": "BenchWeave community",
            "model": "adc-6ch-12bit",
            "serial": None,
            "firmware": f"{info.fw_major}.{info.fw_minor}",
            "source": "device",
        }

    async def _reset(self, context: Any) -> dict[str, Any]:
        self._expect_ack(await self._transact(protocol.FrameType.RESET, b"", context), "RESET")
        self._acquisitions.clear()
        self._active_acquisition = None
        return {"acknowledged": True}

    async def _invoke(self, arguments: dict[str, Any], context: Any) -> dict[str, Any]:
        action_id = str(arguments.get("action_id", ""))
        action_input = arguments.get("input")
        if not isinstance(action_input, dict):
            raise _OperationError(
                "error", "INVALID_ARGUMENT", "invoke needs an object 'input'", "not_dispatched"
            )
        handlers = {
            _ACTION_CONFIGURE: self._configure,
            _ACTION_ARM: self._arm,
            _ACTION_TRIGGER: self._trigger,
            _ACTION_FETCH: self._fetch,
            _ACTION_ABORT: self._abort,
        }
        handler = handlers.get(action_id)
        if handler is None:
            raise _OperationError(
                "error", "UNSUPPORTED", f"unknown action: {action_id}", "not_dispatched"
            )
        result = await handler(action_input, context)
        return {"action_id": action_id, "result": result}

    async def _configure(self, action_input: dict[str, Any], context: Any) -> dict[str, Any]:
        configuration_id = str(action_input["configuration_id"])
        channels = action_input.get("channels")
        if not isinstance(channels, list) or not channels:
            raise _OperationError(
                "error", "INVALID_ARGUMENT", "configure needs channels[]", "not_dispatched"
            )
        mask = 0
        for entry in channels:
            channel = str(entry.get("channel", ""))
            if channel not in CHANNEL_IDS:
                raise _OperationError(
                    "error",
                    "INVALID_ARGUMENT",
                    f"unknown channel: {channel!r}",
                    "not_dispatched",
                )
            mask |= 1 << CHANNEL_IDS.index(channel)
        requested_rate = float(action_input["sample_rate_hz"])
        sample_count = int(action_input["sample_count"])
        trigger = action_input.get("trigger") or {}
        trigger_kind = str(trigger.get("kind", "immediate"))
        if trigger_kind not in ("immediate", "software"):
            # The firmware's external-trigger path is reserved, unimplemented.
            raise _OperationError(
                "error",
                "UNSUPPORTED",
                f"trigger kind not supported: {trigger_kind}",
                "not_dispatched",
            )
        averaging, achieved_rate = _nearest_averaging(requested_rate, mask.bit_count())

        self._expect_ack(
            await self._transact(
                protocol.FrameType.SET_AVERAGING,
                protocol.build_set_averaging(averaging),
                context,
            ),
            "SET_AVERAGING",
        )
        self._expect_ack(
            await self._transact(
                protocol.FrameType.SET_CHANNELS,
                protocol.build_set_channels(mask),
                context,
            ),
            "SET_CHANNELS",
        )

        effective = {
            **{k: v for k, v in action_input.items() if not k.startswith("x-")},
            "sample_rate_hz": achieved_rate,
        }
        self._configurations[configuration_id] = _Configuration(
            channel_mask=mask,
            averaging=averaging,
            sample_count=sample_count,
            trigger_kind=trigger_kind,
            effective=effective,
        )
        return {"configuration_id": configuration_id, "effective_configuration": effective}

    async def _arm(self, action_input: dict[str, Any], context: Any) -> dict[str, Any]:
        configuration_id = str(action_input["configuration_id"])
        acquisition_id = str(action_input["acquisition_id"])
        configuration = self._configurations.get(configuration_id)
        if configuration is None:
            raise _OperationError(
                "error",
                "INVALID_ARGUMENT",
                f"unknown configuration: {configuration_id}",
                "not_dispatched",
            )
        acquisition = _Acquisition(
            configuration_id=configuration_id,
            state="armed",
            started_at=self._require_services().utc_now(),
        )
        self._acquisitions[acquisition_id] = acquisition
        self._active_acquisition = acquisition_id
        if configuration.trigger_kind == "immediate":
            await self._start_acquisition(acquisition, configuration, context)
        return {"acquisition_id": acquisition_id, "state": acquisition.state}

    async def _trigger(self, action_input: dict[str, Any], context: Any) -> dict[str, Any]:
        acquisition_id = str(action_input["acquisition_id"])
        acquisition = self._acquisitions.get(acquisition_id)
        if acquisition is None:
            raise _OperationError(
                "error",
                "INVALID_ARGUMENT",
                f"unknown acquisition: {acquisition_id}",
                "not_dispatched",
            )
        if acquisition.state == "armed":
            configuration = self._configurations[acquisition.configuration_id]
            await self._start_acquisition(acquisition, configuration, context)
        return {"acquisition_id": acquisition_id, "state": "running"}

    async def _start_acquisition(
        self, acquisition: _Acquisition, configuration: _Configuration, context: Any
    ) -> None:
        if configuration.sample_count == 1:
            self._expect_ack(
                await self._transact(protocol.FrameType.SAMPLE_ONCE, b"", context),
                "SAMPLE_ONCE",
            )
        else:
            self._expect_ack(
                await self._transact(protocol.FrameType.START_STREAM, b"", context),
                "START_STREAM",
            )
        # The firmware answers a command before it samples again, so anything
        # buffered before this ACK predates the start: a board still streaming
        # from an earlier session would otherwise hand the acquisition its
        # stale backlog as fresh samples (#14).
        acquisition.samples.clear()
        acquisition.state = "running"

    async def _abort(self, action_input: dict[str, Any], context: Any) -> dict[str, Any]:
        acquisition_id = str(action_input["acquisition_id"])
        acquisition = self._acquisitions.get(acquisition_id)
        if acquisition is None:
            raise _OperationError(
                "error",
                "INVALID_ARGUMENT",
                f"unknown acquisition: {acquisition_id}",
                "not_dispatched",
            )
        if acquisition.state == "running":
            self._expect_ack(
                await self._transact(protocol.FrameType.STOP_STREAM, b"", context),
                "STOP_STREAM",
            )
        acquisition.state = "aborted"
        if self._active_acquisition == acquisition_id:
            self._active_acquisition = None
        return {"acquisition_id": acquisition_id, "state": "aborted"}

    async def _fetch(self, action_input: dict[str, Any], context: Any) -> dict[str, Any]:
        acquisition_id = str(action_input["acquisition_id"])
        max_bytes = int(action_input["max_bytes"])
        allow_partial = bool(action_input["allow_partial"])
        acquisition = self._acquisitions.get(acquisition_id)
        if acquisition is None:
            raise _OperationError(
                "error",
                "INVALID_ARGUMENT",
                f"unknown acquisition: {acquisition_id}",
                "not_dispatched",
            )
        configuration = self._configurations[acquisition.configuration_id]
        if acquisition.state == "running":
            # Take in what the board has sent so far: frames until the line
            # is quiet, bounded so a live stream cannot hold the fetch.
            for _ in range(FETCH_DRAIN_FRAMES):
                if not await self._pump(context):
                    break

        channel_ids = self._active_channel_ids(configuration.channel_mask)
        samples = list(acquisition.samples)
        acquisition.samples.clear()

        budget = max_bytes // (_VALUE_BYTES * max(len(channel_ids), 1))
        truncated = len(samples) > budget
        if truncated:
            if not allow_partial:
                # Nothing was consumed: everything stays fetchable.
                acquisition.samples.extend(samples)
                raise _OperationError(
                    "error",
                    "RESOURCE_LIMIT",
                    f"{len(samples)} samples exceed max_bytes={max_bytes}; "
                    "re-fetch with allow_partial or a larger budget",
                    "not_dispatched",
                )
            overflow = samples[budget:]
            samples = samples[:budget]
            # Undelivered samples stay fetchable next round, oldest first.
            acquisition.samples.extend(overflow)

        running = acquisition.state == "running"
        partial = truncated or running
        acquisition.fetches += 1
        channel_entries = {
            str(entry["channel"]): entry
            for entry in configuration.effective.get("channels", [])
            if isinstance(entry, dict)
        }
        variables = []
        for index, channel in enumerate(channel_ids):
            entry = channel_entries.get(channel, {})
            variables.append(
                {
                    "id": channel,
                    "quantity": str(entry.get("quantity", "voltage")),
                    # Raw counts cross the contract boundary; engineering
                    # conversion is product-side (measurement profiles).
                    "unit": "count",
                    "channel_ids": [channel],
                    "dtype": "float64",
                    "dimensions": ["sample_index"],
                    "values": [
                        float(channels[CHANNEL_IDS.index(channel)]) for _, channels in samples
                    ],
                    "uncertainty": {"status": "unknown"},
                    "calibration": {"status": "not_applied"},
                    "status": "partial" if partial else "valid",
                    **({"status_reason": "bounded fetch"} if partial else {}),
                }
            )
            del index
        axes = (
            [
                {
                    "id": "sample_index",
                    "quantity": "count",
                    "unit": "1",
                    "length": len(samples),
                    "coordinates": {"kind": "regular", "start": 0.0, "step": 1.0},
                }
            ]
            if samples
            else []
        )
        dataset: dict[str, Any] = {
            "dataset_id": f"{acquisition_id}-fetch-{acquisition.fetches}",
            "kind": "waveform",
            "configuration_id": acquisition.configuration_id,
            "acquisition_id": acquisition_id,
            "started_at": acquisition.started_at,
            "clock": {
                "domain_id": "host",
                "timestamp_source": "host",
                "synchronisation": "unsynchronised",
                "uncertainty_s": None,
            },
            "axes": axes,
            "variables": variables,
            "trigger": {"source": configuration.trigger_kind, "time_relative_s": None},
            "status": "partial" if partial else "complete",
            "context": {
                "averaged_n": configuration.averaging,
                "channel_mask": configuration.channel_mask,
            },
        }
        if partial:
            dataset["status_reason"] = (
                "fetch truncated by max_bytes" if truncated else "acquisition still running"
            )
        return dataset

    # -- wire helpers ----------------------------------------------------------

    def _require_services(self) -> _Services:
        if self._services is None:
            raise _OperationError(
                "error", "INTERNAL_ERROR", "adapter is not open", "not_dispatched"
            )
        return self._services

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFF
        return seq

    def _guard(self, context: Any) -> None:
        services = self._require_services()
        if context.is_cancelled():
            raise _OperationError("cancelled", "CANCELLED", "operation cancelled", "not_dispatched")
        if services.monotonic() >= context.deadline_monotonic:
            raise _OperationError(
                "error", "TIMEOUT", "deadline expired before dispatch", "not_dispatched"
            )

    async def _transact(
        self, command: protocol.FrameType, payload: bytes, context: Any
    ) -> protocol.Frame:
        """One command/response exchange; never transmits past the deadline."""
        services = self._require_services()
        self._guard(context)
        frame = protocol.Frame(type=int(command), seq=self._next_seq(), payload=payload)
        await context.mark_dispatch_started()
        try:
            sent = await services.transfer(
                {"kind": SEND, "data": protocol.encode_frame(frame)}, context
            )
            if sent != {}:
                raise ConnectionError(f"host answered a send with {sorted(sent)}")
            while True:
                # The reply can arrive interleaved behind SAMPLE frames; frames
                # are read until it surfaces or time runs out.
                reply = self._route_frames(await self._read_frames(context), command)
                if reply is not None:
                    return reply
                if services.monotonic() >= context.deadline_monotonic:
                    raise TimeoutError(f"no response to {command.name}")
        except (TimeoutError, ConnectionError) as exc:
            # After dispatch the outcome is uncertain — never claim "error".
            raise _OperationError(
                "unknown",
                "TIMEOUT" if isinstance(exc, TimeoutError) else "TRANSPORT_ERROR",
                f"{command.name}: {exc}",
                "unknown",
            ) from exc

    async def _receive_exact(self, size: int, context: Any) -> bytes:
        """One exact-byte receive: ``size`` bytes, or ``b""`` on a quiet line."""
        response = await self._require_services().transfer(
            {"kind": RECEIVE, "max_bytes": size, "termination": "lf", "exact_bytes": size},
            context,
        )
        data = response.get("data")
        if not isinstance(data, bytes) or len(data) not in (0, size):
            # A host that breaks the exact-bytes contract has lost the framing.
            raise ConnectionError(f"host answered an exact {size}-byte receive with {data!r:.40}")
        return data

    async def _read_frames(self, context: Any) -> list[protocol.Frame]:
        """Exact receives until the parser completes a frame; ``[]`` when quiet.

        A frame the line leaves unfinished stays in the parser, and the next
        read asks only for the bytes that complete it.
        """
        while True:
            data = await self._receive_exact(self._parser.bytes_wanted(), context)
            if not data:
                return []
            frames = self._parser.feed(data)
            if frames:
                return frames

    def _route_frames(
        self, frames: list[protocol.Frame], pending: protocol.FrameType
    ) -> protocol.Frame | None:
        """Buffer samples and pick out the reply to the pending command."""
        reply: protocol.Frame | None = None
        for frame in frames:
            if frame.type == protocol.FrameType.SAMPLE:
                self._buffer_sample(frame)
            elif reply is None and self._matches(frame, pending):
                reply = frame
        return reply

    @staticmethod
    def _matches(frame: protocol.Frame, pending: protocol.FrameType) -> bool:
        if pending is protocol.FrameType.IDENTIFY:
            return frame.type in (protocol.FrameType.IDENTIFY_RSP, protocol.FrameType.NAK)
        if frame.type == protocol.FrameType.IDENTIFY_RSP:
            return False
        # ACK and NAK both echo the command type as their first payload byte.
        return (
            frame.type in (protocol.FrameType.ACK, protocol.FrameType.NAK)
            and len(frame.payload) >= 1
            and frame.payload[0] == int(pending)
        )

    def _buffer_sample(self, frame: protocol.Frame) -> None:
        if self._active_acquisition is None:
            return
        acquisition = self._acquisitions.get(self._active_acquisition)
        if acquisition is None or acquisition.state not in ("armed", "running"):
            return
        try:
            acquisition.samples.append(protocol.parse_sample(frame.payload))
        except ValueError:
            # Malformed sample frame: skip it rather than faulting the link.
            return

    async def _pump(self, context: Any) -> bool:
        """Read one frame; a sample lands in the active acquisition.

        Returns ``False`` when nothing arrived: the line is quiet, or the
        deadline or a cancellation cut the read short (a partial frame stays
        buffered for the next read).
        """
        services = self._require_services()
        if context.is_cancelled() or services.monotonic() >= context.deadline_monotonic:
            return False
        try:
            frames = await self._read_frames(context)
        except TimeoutError:
            return False
        for frame in frames:
            if frame.type == protocol.FrameType.SAMPLE:
                self._buffer_sample(frame)
        return bool(frames)

    def _expect_ack(self, response: protocol.Frame, command: str) -> None:
        if response.type == protocol.FrameType.NAK:
            try:
                _, error = protocol.parse_nak(response.payload)
                message = f"device NAK for {command}: error {error:#x}"
            except ValueError as exc:
                message = f"malformed NAK payload for {command}: {exc}"
            raise _OperationError("error", "DEVICE_REJECTED", message, "dispatched")
        if response.type != protocol.FrameType.ACK:
            raise _OperationError(
                "error",
                "PROTOCOL_ERROR",
                f"unexpected response type {response.type:#x} for {command}",
                "dispatched",
            )

    @staticmethod
    def _active_channel_ids(mask: int) -> list[str]:
        active = [cid for index, cid in enumerate(CHANNEL_IDS) if mask & (1 << index)]
        return active or list(CHANNEL_IDS)


def _nearest_averaging(requested_rate: float, n_channels: int) -> tuple[int, float]:
    """The hardware averaging whose estimated rate best matches the request.

    ``otdp.daq.configure``'s input schema is closed (no extension fields for
    a raw averaging knob), so the requested ``sample_rate_hz`` is mapped onto
    the discrete averaging table and the ACHIEVED rate is reported back in
    ``effective_configuration.sample_rate_hz``.
    """
    from .config import estimate_max_sps

    best = protocol.AVERAGING_CHOICES[0]
    best_rate = estimate_max_sps(best, max(n_channels, 1))
    best_error = abs(best_rate - requested_rate)
    for choice in protocol.AVERAGING_CHOICES[1:]:
        rate = estimate_max_sps(choice, max(n_channels, 1))
        error = abs(rate - requested_rate)
        if error < best_error:
            best, best_rate, best_error = choice, rate, error
    return best, round(best_rate, 3)


__all__ = ["AdcAdapter", "create_plugin", "CHANNEL_IDS", "SEND", "RECEIVE"]
