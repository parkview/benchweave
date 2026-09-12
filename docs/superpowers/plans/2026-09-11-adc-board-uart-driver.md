# ADC Board UART Driver — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Python master-side serial driver for the 6-channel ADC board, with a pure binary protocol codec and a threaded state-machine driver, tested end-to-end against an in-memory loopback.

**Architecture:** A pure, I/O-free `protocol.py` encodes/decodes the binary wire frames (CRC-16/CCITT-FALSE, sync-framed). A `driver.py` owns the serial port and a background reader thread, exposes a command/response API and a streaming iterator, and is fully unit-testable via an injected `Transport`.

**Tech Stack:** Python 3.13, `pyserial>=3.5`, pytest, ruff, mypy (strict).

## Global Constraints

- Python 3.13; package source under `src/benchweave/`, tests under `tests/`.
- First runtime dependency is `pyserial>=3.5` — added to `[project] dependencies`.
- ruff: line-length 100, target py313, select `["E","F","I","UP","B","SIM"]`.
- mypy: `strict = true`, files `["src/benchweave", "tests"]`.
- Wire protocol is fixed by the spec `docs/superpowers/specs/2026-09-11-adc-board-uart-driver-design.md`: sync `0xAA 0x55`, CRC-16/CCITT-FALSE (check value `crc16(b"123456789") == 0x29B1`), little-endian, 6 channels, 12-bit `u16` per channel.
- Every git commit ends with the trailer `Co-Authored-By: Claude <noreply@anthropic.com>`.
- Run commands from the repository root; the `uv` environment is already set up (`uv sync --locked --dev`).

---

## File Structure

- `src/benchweave/adc/__init__.py` — public re-exports.
- `src/benchweave/adc/protocol.py` — pure codec: constants, enums, `crc16`, `Frame`, `encode_frame`, `FrameParser`, payload builders/parsers.
- `src/benchweave/adc/driver.py` — `Transport` protocol, exceptions, `Sample`, state machine, `AdcDriver` (reader thread, command/response, streaming).
- `tests/adc/test_protocol.py` — protocol unit tests.
- `tests/adc/test_driver.py` — driver tests with a fake transport.
- `pyproject.toml` + `uv.lock` — add `pyserial>=3.5`.

---

### Task 1: Protocol core — CRC, frames, parser

**Files:**
- Create: `src/benchweave/adc/__init__.py`
- Create: `src/benchweave/adc/protocol.py`
- Test: `tests/adc/test_protocol.py`

**Interfaces:**
- Produces: `crc16(data: bytes) -> int`, `Frame(type, seq, payload)`, `encode_frame(frame) -> bytes`, `FrameParser.feed(data) -> list[Frame]`, `FrameParser.error_count`, constants `SYNC`, `HEADER_LEN`, `CRC_LEN`, `MAX_PAYLOAD`, `FrameType`, `ErrorCode`.

- [ ] **Step 1: Write the failing tests**

Create `tests/adc/test_protocol.py`:

```python
from __future__ import annotations

import pytest

from benchweave.adc.protocol import Frame, FrameParser, FrameType, crc16, encode_frame


def test_crc16_check_vector() -> None:
    assert crc16(b"123456789") == 0x29B1


def test_encode_decode_round_trip() -> None:
    frame = Frame(type=FrameType.SAMPLE, seq=7, payload=bytes(range(16)))
    assert FrameParser().feed(encode_frame(frame)) == [frame]


def test_parser_survives_split_input() -> None:
    frame = Frame(type=FrameType.ACK, seq=0, payload=b"\x01\x02\x03")
    raw = encode_frame(frame)
    parser = FrameParser()
    out: list[Frame] = []
    for byte in raw:
        out += parser.feed(bytes([byte]))
    assert out == [frame]


def test_encode_rejects_long_payload() -> None:
    with pytest.raises(ValueError):
        encode_frame(Frame(type=FrameType.SAMPLE, seq=0, payload=bytes(256)))


def test_encode_rejects_bad_type() -> None:
    with pytest.raises(ValueError):
        encode_frame(Frame(type=256, seq=0, payload=b""))


def test_parser_recovers_after_garbage() -> None:
    frame = Frame(type=FrameType.SAMPLE, seq=1, payload=b"\x00" * 16)
    parser = FrameParser()
    assert parser.feed(b"\x13\x37\xff" + encode_frame(frame)) == [frame]
    assert parser.error_count >= 1


def test_parser_drops_corrupt_frame() -> None:
    # Hand-built frame with a wrong CRC; payload chosen to contain no sync bytes.
    bad = bytes([0xAA, 0x55, 0x90, 0x00, 0x01, 0x00, 0x00, 0x00])
    parser = FrameParser()
    assert parser.feed(bad) == []
    assert parser.error_count >= 1
    good = Frame(type=FrameType.SAMPLE, seq=2, payload=b"\x01" * 16)
    assert parser.feed(encode_frame(good)) == [good]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/adc/test_protocol.py -v`
Expected: FAIL (collection error — `benchweave.adc.protocol` does not exist).

- [ ] **Step 3: Create the package and implement the protocol core**

Create `src/benchweave/adc/__init__.py`:

```python
"""6-channel ADC board serial driver (master side)."""
```

Create `src/benchweave/adc/protocol.py`:

```python
"""Binary wire protocol for the 6-channel ADC board.

Pure codec with no I/O: frame encode/decode plus payload builders and parsers.
See docs/superpowers/specs/2026-09-11-adc-board-uart-driver-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

SYNC = b"\xaa\x55"
HEADER_LEN = 5  # sync(2) + type(1) + seq(1) + len(1)
CRC_LEN = 2
MAX_PAYLOAD = 255
MAX_FRAME = HEADER_LEN + MAX_PAYLOAD + CRC_LEN

N_CHANNELS = 6
AVERAGING_CHOICES = (0, 4, 8, 16, 32, 64, 128, 256)
CHANNEL_MASK_ALL = 0b0011_1111  # all six channels


class FrameType(IntEnum):
    SET_AVERAGING = 0x01
    SET_CHANNELS = 0x02
    SET_SAMPLE_MODE = 0x03
    START_STREAM = 0x04
    STOP_STREAM = 0x05
    SAMPLE_ONCE = 0x06
    RESET = 0x07
    IDENTIFY = 0x08
    ARM_TRIGGER = 0x09  # reserved: external trigger not implemented
    ACK = 0x81
    NAK = 0x82
    IDENTIFY_RSP = 0x83
    SAMPLE = 0x90


class ErrorCode(IntEnum):
    BAD_COMMAND = 0x01
    BAD_PARAMETER = 0x02
    BUSY = 0x03
    UNSUPPORTED = 0x04


class SampleMode(IntEnum):
    FREE_RUN = 0
    CYCLE = 1  # reserved


@dataclass(frozen=True)
class Frame:
    type: int
    seq: int
    payload: bytes


@dataclass(frozen=True)
class IdentifyInfo:
    proto_version: int
    fw_major: int
    fw_minor: int
    n_channels: int
    resolution: int


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflect, no xorout."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def encode_frame(frame: Frame) -> bytes:
    if not 0 <= frame.type <= 0xFF:
        raise ValueError(f"type out of range: {frame.type}")
    if not 0 <= frame.seq <= 0xFF:
        raise ValueError(f"seq out of range: {frame.seq}")
    if len(frame.payload) > MAX_PAYLOAD:
        raise ValueError(f"payload too long: {len(frame.payload)}")
    body = bytes((SYNC[0], SYNC[1], frame.type, frame.seq, len(frame.payload))) + frame.payload
    return body + crc16(body[2:]).to_bytes(CRC_LEN, "little")


class FrameParser:
    """Incremental decoder that resynchronises on corrupt bytes or a bad CRC."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.error_count = 0

    def feed(self, data: bytes) -> list[Frame]:
        self._buffer += data
        frames: list[Frame] = []
        while True:
            frame = self._extract()
            if frame is None:
                break
            frames.append(frame)
        return frames

    def _extract(self) -> Frame | None:
        buf = self._buffer
        start = buf.find(SYNC)
        if start < 0:
            keep = 1 if len(buf) > 1 else len(buf)
            self._buffer = buf[-keep:]
            return None
        if start > 0:
            self.error_count += 1
            del buf[:start]
        if len(buf) < HEADER_LEN:
            return None
        length = buf[4]
        total = HEADER_LEN + length + CRC_LEN
        if len(buf) < total:
            return None
        frame_bytes = bytes(buf[:total])
        if crc16(frame_bytes[2 : HEADER_LEN + length]) != int.from_bytes(frame_bytes[-CRC_LEN:], "little"):
            self.error_count += 1
            del buf[0]
            return None
        del buf[:total]
        return Frame(type=frame_bytes[2], seq=frame_bytes[3], payload=frame_bytes[5 : HEADER_LEN + length])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/adc/test_protocol.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/benchweave/adc/__init__.py src/benchweave/adc/protocol.py tests/adc/test_protocol.py
git commit -m "feat: add ADC binary protocol codec" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Protocol payload builders and parsers

**Files:**
- Modify: `src/benchweave/adc/protocol.py` (append the functions below)
- Test: `tests/adc/test_protocol.py` (append tests)

**Interfaces:**
- Consumes: `Frame`, `FrameType`, `SampleMode`, `IdentifyInfo` (Task 1).
- Produces: `build_set_averaging(n) -> bytes`, `build_set_channels(mask) -> bytes`, `build_set_sample_mode(mode) -> bytes`, `parse_ack(payload) -> tuple[int, int]`, `parse_nak(payload) -> tuple[int, int]`, `parse_identify(payload) -> IdentifyInfo`, `parse_sample(payload) -> tuple[int, tuple[int, ...]]`.

- [ ] **Step 1: Write the failing tests**

Merge the new imports into the existing top-of-file `from benchweave.adc.protocol import (...)` block (so imports stay at the top of the file), and append the test functions to the end of `tests/adc/test_protocol.py`:

```python
from benchweave.adc.protocol import (
    CHANNEL_MASK_ALL,
    IdentifyInfo,
    SampleMode,
    build_set_averaging,
    build_set_channels,
    build_set_sample_mode,
    parse_ack,
    parse_identify,
    parse_nak,
    parse_sample,
)


def test_build_set_averaging() -> None:
    assert build_set_averaging(64) == (64).to_bytes(2, "little")
    assert build_set_averaging(0) == b"\x00\x00"
    with pytest.raises(ValueError):
        build_set_averaging(3)


def test_build_set_channels() -> None:
    assert build_set_channels(CHANNEL_MASK_ALL) == b"\x3f"
    with pytest.raises(ValueError):
        build_set_channels(0x40)


def test_build_set_sample_mode() -> None:
    assert build_set_sample_mode(SampleMode.FREE_RUN) == b"\x00"
    with pytest.raises(ValueError):
        build_set_sample_mode(9)


def test_parse_ack() -> None:
    assert parse_ack(bytes([FrameType.SET_AVERAGING, 0x20, 0x00])) == (FrameType.SET_AVERAGING, 32)


def test_parse_nak() -> None:
    assert parse_nak(bytes([FrameType.START_STREAM, 0x03])) == (FrameType.START_STREAM, 3)


def test_parse_identify() -> None:
    assert parse_identify(bytes([1, 0, 2, 6, 12])) == IdentifyInfo(1, 0, 2, 6, 12)


def test_parse_sample() -> None:
    counter = 0x00010203
    channels = (0, 1, 2, 3, 4, 5)
    payload = counter.to_bytes(4, "little") + b"".join(c.to_bytes(2, "little") for c in channels)
    assert parse_sample(payload) == (counter, channels)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/adc/test_protocol.py -v`
Expected: FAIL with `ImportError` (the names do not exist yet).

- [ ] **Step 3: Append the payload functions to `protocol.py`**

Append to the end of `src/benchweave/adc/protocol.py`:

```python
def build_set_averaging(n: int) -> bytes:
    if n not in AVERAGING_CHOICES:
        raise ValueError(f"averaging must be one of {AVERAGING_CHOICES}")
    return n.to_bytes(2, "little")


def build_set_channels(mask: int) -> bytes:
    if not 0 <= mask <= CHANNEL_MASK_ALL:
        raise ValueError(f"channel mask out of range: {mask}")
    return bytes((mask,))


def build_set_sample_mode(mode: int) -> bytes:
    if mode not in (SampleMode.FREE_RUN, SampleMode.CYCLE):
        raise ValueError(f"invalid sample mode: {mode}")
    return bytes((mode,))


def parse_ack(payload: bytes) -> tuple[int, int]:
    if len(payload) < 3:
        raise ValueError("short ACK payload")
    return payload[0], int.from_bytes(payload[1:3], "little")


def parse_nak(payload: bytes) -> tuple[int, int]:
    if len(payload) < 2:
        raise ValueError("short NAK payload")
    return payload[0], payload[1]


def parse_identify(payload: bytes) -> IdentifyInfo:
    if len(payload) < 5:
        raise ValueError("short IDENTIFY payload")
    return IdentifyInfo(
        proto_version=payload[0],
        fw_major=payload[1],
        fw_minor=payload[2],
        n_channels=payload[3],
        resolution=payload[4],
    )


def parse_sample(payload: bytes) -> tuple[int, tuple[int, ...]]:
    if len(payload) < 4 + 2 * N_CHANNELS:
        raise ValueError("short SAMPLE payload")
    counter = int.from_bytes(payload[0:4], "little")
    channels = tuple(
        int.from_bytes(payload[4 + 2 * i : 6 + 2 * i], "little") for i in range(N_CHANNELS)
    )
    return counter, channels
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/adc/test_protocol.py -v`
Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
git add src/benchweave/adc/protocol.py tests/adc/test_protocol.py
git commit -m "feat: add ADC payload builders and parsers" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Driver core — transport, state machine, commands

**Files:**
- Create: `src/benchweave/adc/driver.py`
- Test: `tests/adc/test_driver.py`
- Modify: `pyproject.toml` (add `pyserial>=3.5`), regenerate `uv.lock`

**Interfaces:**
- Consumes: `protocol` module (FrameType, Frame, encode_frame, FrameParser, IdentifyInfo, builders/parsers, SampleMode, AVERAGING_CHOICES, CHANNEL_MASK_ALL).
- Produces: `Transport` protocol, `AdcError`/`AdcConnectionError`/`AdcProtocolError`/`AdcTimeout`/`AdcNotConnected`, `Sample`, `AdcDriver` with `open/close/identify/reset/set_averaging/set_channels/set_sample_mode/start_stream/stop_stream/sample_once/iter_samples` and `averaging` property.

- [ ] **Step 1: Add the `pyserial` dependency**

Modify `pyproject.toml` — change `dependencies = []` to:

```toml
dependencies = ["pyserial>=3.5"]
```

Then regenerate the lock and install:

```bash
uv lock
uv sync --locked --dev
```

Expected: `uv sync` completes and `uv run python -c "import serial"` succeeds.

- [ ] **Step 2: Write the failing tests**

Create `tests/adc/test_driver.py`:

```python
from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from benchweave.adc import protocol
from benchweave.adc.driver import (
    AdcDriver,
    AdcNotConnected,
    AdcProtocolError,
    AdcTimeout,
)
from benchweave.adc.protocol import Frame, FrameParser, FrameType, IdentifyInfo, encode_frame


class FakeTransport:
    """In-memory loopback that satisfies the Transport protocol."""

    def __init__(self, timeout: float = 0.05) -> None:
        self.timeout: float | None = timeout
        self._inbound = bytearray()
        self._outbound = bytearray()
        self._open = True
        self.responder: Callable[[bytes], None] | None = None

    @property
    def in_waiting(self) -> int:
        return len(self._inbound)

    @property
    def is_open(self) -> bool:
        return self._open

    def read(self, size: int = 1) -> bytes:
        if not self._inbound:
            time.sleep(0.001)
            return b""
        n = min(size, len(self._inbound))
        data = bytes(self._inbound[:n])
        del self._inbound[:n]
        return data

    def write(self, data: bytes) -> int:
        self._outbound += data
        if self.responder is not None:
            self.responder(data)
        return len(data)

    def close(self) -> None:
        self._open = False

    def push(self, data: bytes) -> None:
        self._inbound += data


def ack(cmd: int, value: int = 0) -> bytes:
    return encode_frame(Frame(type=FrameType.ACK, seq=0, payload=bytes([cmd]) + value.to_bytes(2, "little")))


def test_identify() -> None:
    fake = FakeTransport()

    def respond(data: bytes) -> None:
        for f in FrameParser().feed(data):
            if f.type == FrameType.IDENTIFY:
                fake.push(encode_frame(Frame(FrameType.IDENTIFY_RSP, 0, bytes([1, 0, 2, 6, 12]))))

    fake.responder = respond
    driver = AdcDriver(transport=fake)
    driver.open()
    assert driver.identify() == IdentifyInfo(1, 0, 2, 6, 12)
    driver.close()


def test_set_averaging_updates_readback() -> None:
    fake = FakeTransport()

    def respond(data: bytes) -> None:
        for f in FrameParser().feed(data):
            if f.type == FrameType.SET_AVERAGING:
                n = int.from_bytes(f.payload[:2], "little")
                fake.push(ack(FrameType.SET_AVERAGING, n))

    fake.responder = respond
    driver = AdcDriver(transport=fake)
    driver.open()
    driver.set_averaging(64)
    assert driver.averaging == 64
    with pytest.raises(ValueError):
        driver.set_averaging(3)
    driver.close()


def test_nak_raises_protocol_error() -> None:
    fake = FakeTransport()

    def respond(data: bytes) -> None:
        for f in FrameParser().feed(data):
            fake.push(encode_frame(Frame(FrameType.NAK, 0, bytes([f.type, 0x02]))))

    fake.responder = respond
    driver = AdcDriver(transport=fake)
    driver.open()
    with pytest.raises(AdcProtocolError):
        driver.set_channels(0x3F)
    driver.close()


def test_command_timeout() -> None:
    fake = FakeTransport()  # no responder -> no response ever
    driver = AdcDriver(transport=fake)
    driver.open()
    with pytest.raises(AdcTimeout):
        driver.identify(timeout=0.05)
    driver.close()


def test_requires_open() -> None:
    driver = AdcDriver(transport=FakeTransport())
    with pytest.raises(AdcNotConnected):
        driver.identify()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/adc/test_driver.py -v`
Expected: FAIL (collection error — `benchweave.adc.driver` does not exist).

- [ ] **Step 4: Implement the driver**

Create `src/benchweave/adc/driver.py`:

```python
"""Serial driver for the 6-channel ADC board (master side)."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol

from benchweave.adc import protocol

DEFAULT_BAUD = 2_000_000


class AdcError(Exception):
    """Base class for ADC driver errors."""


class AdcConnectionError(AdcError):
    """The serial transport is unavailable or faulted."""


class AdcProtocolError(AdcError):
    """The device returned an unexpected or NAK response."""


class AdcTimeout(AdcError):
    """No response arrived within the deadline."""


class AdcNotConnected(AdcError):
    """An operation was attempted before open()."""


class Transport(Protocol):
    timeout: float | None

    @property
    def in_waiting(self) -> int: ...

    def read(self, size: int = 1) -> bytes: ...

    def write(self, data: bytes) -> int | None: ...

    def close(self) -> None: ...

    @property
    def is_open(self) -> bool: ...


@dataclass(frozen=True)
class Sample:
    counter: int
    channels: tuple[int, ...]
    averaged_n: int


class _State(Enum):
    DISCONNECTED = auto()
    CONNECTED = auto()
    CONFIGURED = auto()
    STREAMING = auto()


class AdcDriver:
    """Master driver for the 6-channel ADC board."""

    def __init__(self, transport: Transport | None = None) -> None:
        self._transport = transport
        self._parser = protocol.FrameParser()
        self._state = _State.DISCONNECTED
        self._running = False
        self._faulted = False
        self._reader: threading.Thread | None = None
        self._cmd_lock = threading.Lock()
        self._response_cond = threading.Condition()
        self._pending_response: protocol.Frame | None = None
        self._sample_queue: queue.Queue[Sample] = queue.Queue()
        self._seq = 0
        self._averaged_n = 0
        self._timeout = 1.0
        self._awaiting_single = False
        self._single_sample: Sample | None = None
        self._single_event = threading.Event()

    # -- lifecycle ----------------------------------------------------------

    def open(self, port: str | None = None, *, baud: int = DEFAULT_BAUD, timeout: float = 1.0) -> None:
        if self._state is not _State.DISCONNECTED:
            raise AdcError("already open")
        if port is not None:
            import serial  # type: ignore[import-untyped]

            self._transport = serial.Serial(port=port, baudrate=baud, timeout=timeout)
        if self._transport is None:
            raise AdcConnectionError("no transport: pass a port or inject a transport")
        self._timeout = timeout
        self._running = True
        self._reader = threading.Thread(target=self._reader_loop, name="adc-reader", daemon=True)
        self._reader.start()
        self._state = _State.CONNECTED

    def close(self) -> None:
        self._running = False
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=2.0)
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
        self._reader = None
        self._state = _State.DISCONNECTED

    def __enter__(self) -> "AdcDriver":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- properties ---------------------------------------------------------

    @property
    def averaging(self) -> int:
        return self._averaged_n

    # -- commands -----------------------------------------------------------

    def identify(self, *, timeout: float | None = None) -> protocol.IdentifyInfo:
        self._require_state(_State.CONNECTED, _State.CONFIGURED)
        resp = self._transact(protocol.FrameType.IDENTIFY, b"", timeout=timeout)
        if resp.type != protocol.FrameType.IDENTIFY_RSP:
            raise AdcProtocolError(f"unexpected response type {resp.type:#x}")
        return protocol.parse_identify(resp.payload)

    def reset(self, *, timeout: float | None = None) -> None:
        self._require_state(_State.CONNECTED, _State.CONFIGURED, _State.STREAMING)
        self._command(protocol.FrameType.RESET, b"", timeout=timeout)
        self._state = _State.CONNECTED

    def set_averaging(self, n: int, *, timeout: float | None = None) -> None:
        if n not in protocol.AVERAGING_CHOICES:
            raise ValueError(f"averaging must be one of {protocol.AVERAGING_CHOICES}")
        self._require_state(_State.CONNECTED, _State.CONFIGURED)
        _, value = self._command(
            protocol.FrameType.SET_AVERAGING, protocol.build_set_averaging(n), timeout=timeout
        )
        self._averaged_n = value
        self._state = _State.CONFIGURED

    def set_channels(self, mask: int, *, timeout: float | None = None) -> None:
        if not 0 <= mask <= protocol.CHANNEL_MASK_ALL:
            raise ValueError(f"channel mask out of range: {mask}")
        self._require_state(_State.CONNECTED, _State.CONFIGURED)
        self._command(protocol.FrameType.SET_CHANNELS, protocol.build_set_channels(mask), timeout=timeout)
        self._state = _State.CONFIGURED

    def set_sample_mode(self, mode: int, *, timeout: float | None = None) -> None:
        if mode not in (protocol.SampleMode.FREE_RUN, protocol.SampleMode.CYCLE):
            raise ValueError(f"invalid sample mode: {mode}")
        self._require_state(_State.CONNECTED, _State.CONFIGURED)
        self._command(protocol.FrameType.SET_SAMPLE_MODE, protocol.build_set_sample_mode(mode), timeout=timeout)
        self._state = _State.CONFIGURED

    def start_stream(self, *, timeout: float | None = None) -> None:
        self._require_state(_State.CONNECTED, _State.CONFIGURED)
        self._command(protocol.FrameType.START_STREAM, b"", timeout=timeout)
        self._state = _State.STREAMING

    def stop_stream(self, *, timeout: float | None = None) -> None:
        self._require_state(_State.STREAMING)
        self._command(protocol.FrameType.STOP_STREAM, b"", timeout=timeout)
        self._state = _State.CONFIGURED

    def sample_once(self, *, timeout: float | None = None) -> Sample:
        self._require_state(_State.CONNECTED, _State.CONFIGURED)
        self._single_sample = None
        self._awaiting_single = True
        self._single_event.clear()
        try:
            self._transact(protocol.FrameType.SAMPLE_ONCE, b"", timeout=timeout)
            wait = timeout if timeout is not None else self._timeout
            if not self._single_event.wait(wait):
                raise AdcTimeout("timed out waiting for sample")
            assert self._single_sample is not None
            return self._single_sample
        finally:
            self._awaiting_single = False

    def iter_samples(self) -> Iterator[Sample]:
        while self._state is _State.STREAMING:
            try:
                yield self._sample_queue.get(timeout=0.5)
            except queue.Empty:
                continue

    # -- internals ----------------------------------------------------------

    def _require_state(self, *allowed: _State) -> None:
        if self._faulted:
            raise AdcConnectionError("transport faulted")
        if self._state is _State.DISCONNECTED:
            raise AdcNotConnected("not connected: call open() first")
        if self._state not in allowed:
            raise AdcError(f"invalid state {self._state.name} for this operation")

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFF
        return seq

    def _command(self, cmd: protocol.FrameType, payload: bytes, *, timeout: float | None = None) -> tuple[int, int]:
        resp = self._transact(cmd, payload, timeout=timeout)
        if resp.type == protocol.FrameType.NAK:
            echo, error = protocol.parse_nak(resp.payload)
            raise AdcProtocolError(f"device NAK for {cmd.name}: error {error:#x}")
        if resp.type != protocol.FrameType.ACK:
            raise AdcProtocolError(f"unexpected response type {resp.type:#x}")
        return protocol.parse_ack(resp.payload)

    def _transact(self, cmd: protocol.FrameType, payload: bytes, *, timeout: float | None = None) -> protocol.Frame:
        with self._cmd_lock:
            if self._transport is None:
                raise AdcNotConnected("no transport")
            frame = protocol.Frame(type=int(cmd), seq=self._next_seq(), payload=payload)
            self._transport.write(protocol.encode_frame(frame))
            wait = timeout if timeout is not None else self._timeout
            deadline = time.monotonic() + wait
            with self._response_cond:
                self._pending_response = None
                while self._pending_response is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AdcTimeout(f"no response to {cmd.name}")
                    self._response_cond.wait(remaining)
                resp = self._pending_response
                self._pending_response = None
            return resp

    def _reader_loop(self) -> None:
        while self._running:
            try:
                transport = self._transport
                if transport is None or not transport.is_open:
                    break
                n = transport.in_waiting or 1
                data = transport.read(n)
                if not data:
                    continue
                for frame in self._parser.feed(data):
                    self._handle_frame(frame)
            except Exception:
                if self._running:
                    self._faulted = True
                    with self._response_cond:
                        self._response_cond.notify_all()
                break
        self._running = False

    def _handle_frame(self, frame: protocol.Frame) -> None:
        if frame.type in (protocol.FrameType.ACK, protocol.FrameType.NAK, protocol.FrameType.IDENTIFY_RSP):
            with self._response_cond:
                self._pending_response = frame
                self._response_cond.notify_all()
        elif frame.type == protocol.FrameType.SAMPLE:
            counter, channels = protocol.parse_sample(frame.payload)
            sample = Sample(counter=counter, channels=channels, averaged_n=self._averaged_n)
            if self._state is _State.STREAMING:
                self._sample_queue.put(sample)
            if self._awaiting_single:
                self._single_sample = sample
                self._single_event.set()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/adc/test_driver.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/benchweave/adc/driver.py tests/adc/test_driver.py
git commit -m "feat: add ADC serial driver core" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Driver streaming — reader thread, sample delivery

**Files:**
- Modify: `src/benchweave/adc/driver.py` (already contains the streaming methods from Task 3; this task adds the tests that exercise them end-to-end)
- Test: `tests/adc/test_driver.py` (append tests)

**Interfaces:**
- Consumes: `AdcDriver`, `Sample`, `FakeTransport` (Task 3).
- Produces: verified behaviour of `start_stream`/`iter_samples`/`stop_stream`/`sample_once` under streaming and desync.

- [ ] **Step 1: Write the failing tests**

Append to `tests/adc/test_driver.py`:

```python
def sample_payload(counter: int, channels: tuple[int, ...]) -> bytes:
    return counter.to_bytes(4, "little") + b"".join(c.to_bytes(2, "little") for c in channels)


def test_sample_once() -> None:
    fake = FakeTransport()

    def respond(data: bytes) -> None:
        for f in FrameParser().feed(data):
            if f.type == FrameType.SAMPLE_ONCE:
                fake.push(ack(FrameType.SAMPLE_ONCE))
                fake.push(encode_frame(Frame(FrameType.SAMPLE, 0, sample_payload(42, (0, 2, 4, 6, 8, 10)))))

    fake.responder = respond
    driver = AdcDriver(transport=fake)
    driver.open()
    sample = driver.sample_once()
    assert sample.counter == 42
    assert sample.channels == (0, 2, 4, 6, 8, 10)
    assert sample.averaged_n == 0
    driver.close()


def test_stream_and_iter_samples() -> None:
    fake = FakeTransport()

    def respond(data: bytes) -> None:
        for f in FrameParser().feed(data):
            if f.type == FrameType.START_STREAM:
                fake.push(ack(FrameType.START_STREAM))
            elif f.type == FrameType.STOP_STREAM:
                fake.push(ack(FrameType.STOP_STREAM))

    fake.responder = respond
    driver = AdcDriver(transport=fake)
    driver.open()
    driver.start_stream()
    fake.push(encode_frame(Frame(FrameType.SAMPLE, 0, sample_payload(7, (0, 1, 2, 3, 4, 5)))))
    sample = next(driver.iter_samples())
    assert sample.counter == 7
    assert sample.channels == (0, 1, 2, 3, 4, 5)
    driver.stop_stream()
    driver.close()


def test_stream_recovers_after_garbage() -> None:
    fake = FakeTransport()

    def respond(data: bytes) -> None:
        for f in FrameParser().feed(data):
            if f.type == FrameType.START_STREAM:
                fake.push(ack(FrameType.START_STREAM))

    fake.responder = respond
    driver = AdcDriver(transport=fake)
    driver.open()
    driver.start_stream()
    fake.push(b"\x99\x88" + encode_frame(Frame(FrameType.SAMPLE, 0, sample_payload(1, (1, 2, 3, 4, 5, 6)))))
    sample = next(driver.iter_samples())
    assert sample.counter == 1
    assert sample.channels == (1, 2, 3, 4, 5, 6)
    driver.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/adc/test_driver.py -v`
Expected: 5 passed, 3 failed (the new streaming tests reference `sample_payload`, which is now defined; they fail on the first assertion because `sample_once`/`iter_samples` still raise — no, they should actually pass because the streaming methods already exist from Task 3).

> Note: the streaming methods were implemented in Task 3. If these tests pass immediately, that is acceptable — record it and proceed; the tests still lock in the behaviour. If any fails, fix the driver before committing.

- [ ] **Step 3: Run tests to verify they pass**

Run: `uv run pytest tests/adc/test_driver.py -v`
Expected: 8 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/adc/test_driver.py
git commit -m "test: cover ADC streaming and desync recovery" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Public API and final verification

**Files:**
- Modify: `src/benchweave/adc/__init__.py` (add re-exports)
- No new tests (imports verified by the existing suite).

**Interfaces:**
- Produces: public names `AdcDriver`, `Sample`, `IdentifyInfo`, `FrameType`, `SampleMode`, `AVERAGING_CHOICES`, `CHANNEL_MASK_ALL`, and the exception classes.

- [ ] **Step 1: Add the public re-exports**

Replace the contents of `src/benchweave/adc/__init__.py` with:

```python
"""6-channel ADC board serial driver (master side)."""

from benchweave.adc.driver import (
    AdcConnectionError,
    AdcDriver,
    AdcError,
    AdcNotConnected,
    AdcProtocolError,
    AdcTimeout,
    Sample,
)
from benchweave.adc.protocol import (
    AVERAGING_CHOICES,
    CHANNEL_MASK_ALL,
    FrameType,
    IdentifyInfo,
    SampleMode,
)

__all__ = [
    "AdcConnectionError",
    "AdcDriver",
    "AdcError",
    "AdcNotConnected",
    "AdcProtocolError",
    "AdcTimeout",
    "Sample",
    "AVERAGING_CHOICES",
    "CHANNEL_MASK_ALL",
    "FrameType",
    "IdentifyInfo",
    "SampleMode",
]
```

- [ ] **Step 2: Format, lint, and type-check**

Run:

```bash
uv run ruff format src/benchweave/adc tests/adc
uv run ruff check .
uv run mypy
```

Expected: all three pass with no errors. (If `ruff check` reports an issue in the new files, fix it and re-run.)

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest`

Expected: all tests pass (existing contract/package tests plus the new `tests/adc` tests).

- [ ] **Step 4: Commit**

```bash
git add src/benchweave/adc/__init__.py
git commit -m "feat: expose ADC driver public API" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review Notes

- **Spec coverage:** protocol frame layout + CRC → Task 1; command set + data frame + channel order → Task 2; state machine + API + concurrency → Task 3; streaming + sample delivery → Task 4; public API + lint/mypy → Task 5. Averaging readback, NAK/timeout/desync error paths all have tests. Firmware changes and OTDP plugin are correctly out of scope.
- **Placeholder scan:** none — every code step contains complete code.
- **Type consistency:** `parse_sample` returns `tuple[int, tuple[int, ...]]` and `Sample.channels` is `tuple[int, ...]`; `averaging` property matches `Sample.averaged_n`; `IdentifyInfo` is defined in `protocol` and re-exported consistently.
