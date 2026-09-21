# EMeet SmartCam C960 4K — Device Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scaffold a BenchWeave OTDP device plugin for the EMeet SmartCam C960 4K UVC webcam that declares a real 16-control surface and implements the `identify`/`read`/`write` verbs over a clearly-labelled synthetic control channel.

**Architecture:** A hatchling-built Python package at `plugins/emeet/c960/` exposing a single zero-argument factory `benchweave_emeet_c960.adapter:create_plugin` (adapter API 1.1). The descriptor (OTDP 0.3.0) is the single source of truth for the parameter surface; `protocol.py` models a hypothetical text control channel (the camera has no vendor protocol) so the adapter contract is testable with the SDK's `MockHost`/`MockContext`, `validate_descriptor`/`validate_result`, and `check_lifecycle`.

**Tech Stack:** Python ≥3.13, hatchling (build), benchweave-sdk==0.1.0 + pytest (test extra), OTDP 0.3.0, adapter API 1.1.

**Spec:** `docs/superpowers/specs/2026-09-21-emeet-c960-plugin-design.md`

## Global Constraints

- **OTDP `0.3.0`, adapter API `1.1`.** The descriptor is validated by `benchweave_sdk.validation.validate_descriptor`, which checks the SDK-bundled **0.3.0** schema (not the repo's `standards/otdp/0.2.0` corpus — that lags the SDK and would reject a 0.2.0 descriptor). Required-feature floor: `otdp.core/0.3.0`.
- **Working directory:** every command below runs from the plugin root `plugins/emeet/c960/`.
- **Environment:** reuse the worktree-root venv three levels up. It already contains `benchweave-sdk==0.1.0`, `pytest`, and (in uv's cache) `hatchling`, so no network is needed.
  - Install: `uv pip install -e ".[test]"`
  - Test: `../../../.venv/bin/python -m pytest -q`
  - Build: `uv build`
- **Captures never committed.** Captured stills live under `captures/` and the plugin `.gitignore` excludes the whole directory.
- **Commit messages** end with the line `Co-Authored-By: Claude Code <noreply@anthropic.com>`.
- **Plans/specs are gitignored** at `docs/superpowers/`, so commit them with `git add -f`.

## File Structure

```
plugins/emeet/c960/
    .gitignore                        # captures/ + standard python/build ignores
    pyproject.toml                    # hatchling; test extra: benchweave-sdk==0.1.0, pytest
    README.md                         # scaffold status, hardware facts, LLM view loop, limitations
    AI-GUIDE.md                       # 5-step build prompts (camera-adapted)
    src/benchweave_emeet_c960/
        __init__.py                   # package marker
        descriptor.json               # OTDP 0.3.0 descriptor — real 16-control surface
        adapter.py                    # async API 1.1 adapter (identify/read/write)
        protocol.py                   # synthetic exchange model + parsers
        protocol.md                   # synthetic protocol prose
        vectors.json                  # synthetic exchange vectors
    tests/
        test_descriptor.py            # descriptor validity + accuracy
        test_protocol.py              # pure parse/transaction helpers
        test_plugin.py                # adapter happy path, lifecycle, error matrix
```

Each file has one responsibility: `descriptor.json` is the contract, `protocol.py` turns a verb into bytes and parses bytes back, `adapter.py` maps OTDP requests → `protocol.py` exchanges → OTDP results, tests pin each boundary.

---

### Task 1: Package skeleton + descriptor

**Files:**
- Create: `plugins/emeet/c960/pyproject.toml`
- Create: `plugins/emeet/c960/.gitignore`
- Create: `plugins/emeet/c960/src/benchweave_emeet_c960/__init__.py`
- Create: `plugins/emeet/c960/src/benchweave_emeet_c960/descriptor.json`
- Test: `plugins/emeet/c960/tests/test_descriptor.py`

**Interfaces:**
- Produces: package `benchweave_emeet_c960` with data file `descriptor.json` (read via `importlib.resources.files("benchweave_emeet_c960")`), and `descriptor.json` fields Tasks 3 uses: `parameters[].name`/`type`/`unit`/`range`/`enum_values`/`access`/`semantic`/`binding`.

- [ ] **Step 1: Write the package skeleton**

`plugins/emeet/c960/pyproject.toml`:

```toml
[build-system]
requires = ["hatchling>=1.26"]
build-backend = "hatchling.build"

[project]
name = "benchweave-emeet-c960"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = []

[project.optional-dependencies]
test = ["benchweave-sdk==0.1.0", "pytest>=8.0"]

[tool.hatch.build.targets.wheel]
packages = ["src/benchweave_emeet_c960"]
```

`plugins/emeet/c960/.gitignore`:

```gitignore
.venv/
venv/
dist/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.mypy_cache/
# Captured stills are runtime data — never committed.
captures/
```

`plugins/emeet/c960/src/benchweave_emeet_c960/__init__.py`:

```python
"""Synthetic device plugin for the EMeet SmartCam C960 4K UVC webcam."""
```

- [ ] **Step 2: Install the package editable**

Run (from `plugins/emeet/c960/`):

```bash
uv pip install -e ".[test]"
```

Expected: success. (hatchling resolves from uv's cache; sdk/pytest already installed.)

- [ ] **Step 3: Write the failing descriptor test**

`plugins/emeet/c960/tests/test_descriptor.py`:

```python
import json
from importlib.resources import files
from benchweave_sdk.validation import validate_descriptor


def test_descriptor_valid_and_accurate():
    descriptor = json.loads(files("benchweave_emeet_c960").joinpath("descriptor.json").read_text())
    validate_descriptor(descriptor)
    assert descriptor["otdp_version"] == "0.3.0"
    assert descriptor["id"] == "dev.emeet.c960"
    assert descriptor["capabilities"] == ["identify", "read", "write"]
    assert "otdp.core/0.3.0" in descriptor["required_features"]
    names = [p["name"] for p in descriptor["parameters"]]
    assert len(names) == 16
    assert names == [
        "brightness", "contrast", "saturation", "hue", "white_balance_automatic",
        "white_balance_temperature", "gamma", "gain", "power_line_frequency",
        "sharpness", "backlight_compensation", "auto_exposure",
        "exposure_time_absolute", "focus_automatic_continuous", "focus_absolute",
        "zoom_absolute",
    ]
    by_name = {p["name"]: p for p in descriptor["parameters"]}
    assert by_name["brightness"]["range"] == [-64, 64]
    assert by_name["power_line_frequency"]["enum_values"] == ["Disabled", "50 Hz", "60 Hz"]
    assert by_name["auto_exposure"]["enum_values"] == ["Manual", "Aperture-Priority"]
    assert by_name["white_balance_automatic"]["type"] == "bool"
    assert all(p["access"] == "rw" for p in descriptor["parameters"])
    assert all("hazard_class" in p for p in descriptor["parameters"])
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `../../../.venv/bin/python -m pytest tests/test_descriptor.py -q`
Expected: FAIL with `FileNotFoundError` (`descriptor.json` does not exist).

- [ ] **Step 5: Write the descriptor**

`plugins/emeet/c960/src/benchweave_emeet_c960/descriptor.json`:

```json
{
  "otdp_version": "0.3.0",
  "descriptor_version": "0.1.0",
  "id": "dev.emeet.c960",
  "display_name": "EMeet SmartCam C960 4K",
  "description": "Synthetic OTDP scaffold for the EMeet SmartCam C960 4K UVC webcam; not hardware qualified. Real V4L2 control surface; capture is documented but not declared.",
  "identity": {
    "strategy": "adapter",
    "manufacturer": "EMeet",
    "model": "SmartCam C960 4K",
    "firmware_policy": "commissioned"
  },
  "integration": {
    "mode": "adapter",
    "adapter": {
      "entry_point": "benchweave_emeet_c960.adapter:create_plugin",
      "api_version": "1.1",
      "version": "0.1.0",
      "dependencies": [],
      "permissions": ["scoped_transport"]
    }
  },
  "transport": {
    "type": "custom",
    "connection_key": "emeet_c960",
    "settings": {
      "protocol_reference": "protocol.md"
    }
  },
  "capabilities": ["identify", "read", "write"],
  "operations": {
    "identify": {"timeout_ms": 1000, "side_effect": "none", "retry": "never", "cancellable": true, "completion": "acknowledged"},
    "read": {"timeout_ms": 1000, "side_effect": "none", "retry": "never", "cancellable": true, "completion": "acknowledged"},
    "write": {"timeout_ms": 1000, "side_effect": "state_change", "retry": "never", "cancellable": true, "completion": "acknowledged"}
  },
  "parameters": [
    {"name": "brightness", "description": "Image brightness (-64..64).", "type": "int", "access": "rw", "semantic": "configuration", "unit": "1", "range": [-64, 64], "binding": {"kind": "adapter", "key": "brightness"}, "read_policy": {"max_age_ms": 0, "destructive": false}, "write_policy": {"effect": "setting", "completion": "acknowledged", "retry": "never"}, "hazard_class": "none"},
    {"name": "contrast", "description": "Image contrast (0..100).", "type": "int", "access": "rw", "semantic": "configuration", "unit": "1", "range": [0, 100], "binding": {"kind": "adapter", "key": "contrast"}, "read_policy": {"max_age_ms": 0, "destructive": false}, "write_policy": {"effect": "setting", "completion": "acknowledged", "retry": "never"}, "hazard_class": "none"},
    {"name": "saturation", "description": "Colour saturation (0..128).", "type": "int", "access": "rw", "semantic": "configuration", "unit": "1", "range": [0, 128], "binding": {"kind": "adapter", "key": "saturation"}, "read_policy": {"max_age_ms": 0, "destructive": false}, "write_policy": {"effect": "setting", "completion": "acknowledged", "retry": "never"}, "hazard_class": "none"},
    {"name": "hue", "description": "Hue adjustment (-40..40).", "type": "int", "access": "rw", "semantic": "configuration", "unit": "1", "range": [-40, 40], "binding": {"kind": "adapter", "key": "hue"}, "read_policy": {"max_age_ms": 0, "destructive": false}, "write_policy": {"effect": "setting", "completion": "acknowledged", "retry": "never"}, "hazard_class": "none"},
    {"name": "white_balance_automatic", "description": "Automatic white balance on/off; gates white_balance_temperature.", "type": "bool", "access": "rw", "semantic": "configuration", "binding": {"kind": "adapter", "key": "white_balance_automatic"}, "read_policy": {"max_age_ms": 0, "destructive": false}, "write_policy": {"effect": "setting", "completion": "acknowledged", "retry": "never"}, "hazard_class": "none"},
    {"name": "white_balance_temperature", "description": "Colour temperature in kelvin (2300..6500); inactive while white_balance_automatic is on.", "type": "int", "access": "rw", "semantic": "configuration", "unit": "1", "range": [2300, 6500], "binding": {"kind": "adapter", "key": "white_balance_temperature"}, "read_policy": {"max_age_ms": 0, "destructive": false}, "write_policy": {"effect": "setting", "completion": "acknowledged", "retry": "never"}, "hazard_class": "none"},
    {"name": "gamma", "description": "Gamma correction (72..255).", "type": "int", "access": "rw", "semantic": "configuration", "unit": "1", "range": [72, 255], "binding": {"kind": "adapter", "key": "gamma"}, "read_policy": {"max_age_ms": 0, "destructive": false}, "write_policy": {"effect": "setting", "completion": "acknowledged", "retry": "never"}, "hazard_class": "none"},
    {"name": "gain", "description": "Analogue gain (0..100).", "type": "int", "access": "rw", "semantic": "configuration", "unit": "1", "range": [0, 100], "binding": {"kind": "adapter", "key": "gain"}, "read_policy": {"max_age_ms": 0, "destructive": false}, "write_policy": {"effect": "setting", "completion": "acknowledged", "retry": "never"}, "hazard_class": "none"},
    {"name": "power_line_frequency", "description": "Anti-flicker mains frequency (V4L2 menu: 0=Disabled, 1=50 Hz, 2=60 Hz).", "type": "enum", "access": "rw", "semantic": "configuration", "enum_values": ["Disabled", "50 Hz", "60 Hz"], "binding": {"kind": "adapter", "key": "power_line_frequency"}, "read_policy": {"max_age_ms": 0, "destructive": false}, "write_policy": {"effect": "setting", "completion": "acknowledged", "retry": "never"}, "hazard_class": "none"},
    {"name": "sharpness", "description": "Sharpness (1..64).", "type": "int", "access": "rw", "semantic": "configuration", "unit": "1", "range": [1, 64], "binding": {"kind": "adapter", "key": "sharpness"}, "read_policy": {"max_age_ms": 0, "destructive": false}, "write_policy": {"effect": "setting", "completion": "acknowledged", "retry": "never"}, "hazard_class": "none"},
    {"name": "backlight_compensation", "description": "Backlight compensation (1..2).", "type": "int", "access": "rw", "semantic": "configuration", "unit": "1", "range": [1, 2], "binding": {"kind": "adapter", "key": "backlight_compensation"}, "read_policy": {"max_age_ms": 0, "destructive": false}, "write_policy": {"effect": "setting", "completion": "acknowledged", "retry": "never"}, "hazard_class": "none"},
    {"name": "auto_exposure", "description": "Exposure mode (V4L2 menu: 1=Manual, 3=Aperture-Priority); gates exposure_time_absolute.", "type": "enum", "access": "rw", "semantic": "configuration", "enum_values": ["Manual", "Aperture-Priority"], "binding": {"kind": "adapter", "key": "auto_exposure"}, "read_policy": {"max_age_ms": 0, "destructive": false}, "write_policy": {"effect": "setting", "completion": "acknowledged", "retry": "never"}, "hazard_class": "none"},
    {"name": "exposure_time_absolute", "description": "Exposure time (1..5000); inactive unless auto_exposure is Manual.", "type": "int", "access": "rw", "semantic": "configuration", "unit": "1", "range": [1, 5000], "binding": {"kind": "adapter", "key": "exposure_time_absolute"}, "read_policy": {"max_age_ms": 0, "destructive": false}, "write_policy": {"effect": "setting", "completion": "acknowledged", "retry": "never"}, "hazard_class": "none"},
    {"name": "focus_automatic_continuous", "description": "Continuous autofocus on/off; gates focus_absolute.", "type": "bool", "access": "rw", "semantic": "configuration", "binding": {"kind": "adapter", "key": "focus_automatic_continuous"}, "read_policy": {"max_age_ms": 0, "destructive": false}, "write_policy": {"effect": "setting", "completion": "acknowledged", "retry": "never"}, "hazard_class": "none"},
    {"name": "focus_absolute", "description": "Lens focus position (0..1023); inactive while focus_automatic_continuous is on.", "type": "int", "access": "rw", "semantic": "configuration", "unit": "1", "range": [0, 1023], "binding": {"kind": "adapter", "key": "focus_absolute"}, "read_policy": {"max_age_ms": 0, "destructive": false}, "write_policy": {"effect": "setting", "completion": "acknowledged", "retry": "never"}, "hazard_class": "none"},
    {"name": "zoom_absolute", "description": "Digital zoom (0..100).", "type": "int", "access": "rw", "semantic": "configuration", "unit": "1", "range": [0, 100], "binding": {"kind": "adapter", "key": "zoom_absolute"}, "read_policy": {"max_age_ms": 0, "destructive": false}, "write_policy": {"effect": "setting", "completion": "acknowledged", "retry": "never"}, "hazard_class": "none"}
  ],
  "required_features": ["otdp.core/0.3.0", "otdp.adapter/1.1"],
  "provenance": {
    "sources": [
      {"title": "EMeet SmartCam C960 4K - V4L2 control reference", "reference": "docs/eMeet-4K.md", "revision": "2026-09-21"},
      {"title": "Synthetic control protocol", "reference": "protocol.md", "revision": "0.1.0"}
    ],
    "test_vectors": [
      {"id": "c960", "path": "vectors.json", "purpose": "Synthetic identify/read/write exchanges"}
    ]
  }
}
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `../../../.venv/bin/python -m pytest tests/test_descriptor.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add plugins/emeet/c960/pyproject.toml plugins/emeet/c960/.gitignore \
        plugins/emeet/c960/src/benchweave_emeet_c960/__init__.py \
        plugins/emeet/c960/src/benchweave_emeet_c960/descriptor.json \
        plugins/emeet/c960/tests/test_descriptor.py
git commit -m "feat(emeet-c960): add OTDP 0.3.0 descriptor for the 16 V4L2 controls

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: Synthetic protocol

**Files:**
- Create: `plugins/emeet/c960/src/benchweave_emeet_c960/protocol.py`
- Create: `plugins/emeet/c960/src/benchweave_emeet_c960/vectors.json`
- Create: `plugins/emeet/c960/src/benchweave_emeet_c960/protocol.md`
- Test: `plugins/emeet/c960/tests/test_protocol.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure module).
- Produces (Task 3 depends on these exact names): `transaction(verb, parameter=None, value=None) -> dict`, `parse_identity(raw: bytes) -> dict`, `parse_value(ptype: str, raw: bytes) -> int | bool | str` (raises `ValueError` on bad frames).

- [ ] **Step 1: Write the failing protocol test**

`plugins/emeet/c960/tests/test_protocol.py`:

```python
import pytest
from benchweave_emeet_c960.protocol import transaction, parse_identity, parse_value


def test_transaction_shapes():
    assert transaction("identify") == {"kind": "stream_exchange", "data": b"ID?\n",
                                       "max_bytes": 256, "termination": "lf", "exact_bytes": None}
    assert transaction("read", "brightness")["data"] == b"GET brightness\n"
    assert transaction("write", "brightness", 32)["data"] == b"SET brightness=32\n"


def test_parse_identity():
    assert parse_identity(b"EMeet,SmartCam C960 4K\n") == {
        "manufacturer": "EMeet", "model": "SmartCam C960 4K",
        "serial": None, "firmware": None, "source": "commissioned"}


def test_parse_value_int():
    assert parse_value("int", b"0\n") == 0
    assert parse_value("int", b"-64\n") == -64


def test_parse_value_bool():
    assert parse_value("bool", b"1\n") is True
    assert parse_value("bool", b"0\n") is False


def test_parse_value_enum():
    assert parse_value("enum", b"50 Hz\n") == "50 Hz"


def test_parse_value_rejects_bad_frames():
    with pytest.raises(ValueError):
        parse_value("int", b"nan\n")
    with pytest.raises(ValueError):
        parse_value("int", b"3.3")  # no terminating newline
    with pytest.raises(ValueError):
        parse_value("bool", b"2\n")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `../../../.venv/bin/python -m pytest tests/test_protocol.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'benchweave_emeet_c960.protocol'`.

- [ ] **Step 3: Write the protocol module and evidence**

`plugins/emeet/c960/src/benchweave_emeet_c960/protocol.py`:

```python
"""Synthetic V4L2-style control exchanges only; this is not a camera driver.

The EMeet C960 is a plain UVC device with no vendor protocol, so this module
models a hypothetical text control channel purely so the adapter contract can be
exercised without hardware. Every exchange here is synthetic evidence, not a
claimed device response.
"""


def transaction(verb, parameter=None, value=None):
    """Build the synthetic exchange envelope for a verb."""
    if verb == "identify":
        data = b"ID?\n"
    elif verb == "read":
        data = f"GET {parameter}\n".encode("ascii")
    else:  # write
        data = f"SET {parameter}={value}\n".encode("ascii")
    return {"kind": "stream_exchange", "data": data, "max_bytes": 256,
            "termination": "lf", "exact_bytes": None}


def parse_identity(raw):
    """Parse the synthetic identify response into an OTDP identity object.

    The camera has no device-readable identity string, so serial/firmware are
    unknown and the source is commissioned (the descriptor declares
    firmware_policy "commissioned").
    """
    if raw != b"EMeet,SmartCam C960 4K\n":
        raise ValueError("Unexpected identity")
    return {"manufacturer": "EMeet", "model": "SmartCam C960 4K",
            "serial": None, "firmware": None, "source": "commissioned"}


def parse_value(ptype, raw):
    """Parse a synthetic GET response into a Python value of the declared type."""
    if not isinstance(raw, bytes) or len(raw) > 256 or not raw.endswith(b"\n"):
        raise ValueError("Incomplete frame")
    text = raw[:-1].decode("ascii")
    if ptype == "int":
        return int(text)
    if ptype == "bool":
        if text not in ("0", "1"):
            raise ValueError("Bad boolean")
        return text == "1"
    # enum
    return text
```

`plugins/emeet/c960/src/benchweave_emeet_c960/vectors.json`:

```json
{
  "evidence": "synthetic",
  "exchanges": [
    {"request": "ID?\n", "response": "EMeet,SmartCam C960 4K\n"},
    {"request": "GET brightness\n", "response": "0\n"},
    {"request": "GET white_balance_automatic\n", "response": "1\n"},
    {"request": "GET power_line_frequency\n", "response": "50 Hz\n"},
    {"request": "SET brightness=32\n", "response": "OK\n"}
  ]
}
```

`plugins/emeet/c960/src/benchweave_emeet_c960/protocol.md`:

```markdown
# Synthetic control protocol

The EMeet C960 is a plain UVC device with no vendor text protocol. This file
describes a **hypothetical** text control channel, used only to exercise the
adapter contract without hardware. No real device is claimed.

- `ID?` + LF → `EMeet,SmartCam C960 4K` + LF (serial/firmware are unknown).
- `GET <control>` + LF → the control's current value as text + LF (int as decimal,
  bool as `0`/`1`, enum as its label).
- `SET <control>=<value>` + LF → `OK` + LF (value already range-checked).

Real control happens through V4L2 (`v4l2-ctl`); capture through `ffmpeg`. See
`docs/eMeet-4K.md` for the device facts these synthetic exchanges are traced to.
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `../../../.venv/bin/python -m pytest tests/test_protocol.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/emeet/c960/src/benchweave_emeet_c960/protocol.py \
        plugins/emeet/c960/src/benchweave_emeet_c960/vectors.json \
        plugins/emeet/c960/src/benchweave_emeet_c960/protocol.md \
        plugins/emeet/c960/tests/test_protocol.py
git commit -m "feat(emeet-c960): add synthetic control protocol + vectors

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: Adapter (identify / read / write)

**Files:**
- Create: `plugins/emeet/c960/src/benchweave_emeet_c960/adapter.py`
- Test: `plugins/emeet/c960/tests/test_plugin.py`

**Interfaces:**
- Consumes: `transaction`, `parse_identity`, `parse_value` from Task 2; `descriptor.json` fields from Task 1 (`parameters[].name/type/unit/range/enum_values`).
- Produces: `create_plugin() -> Plugin` (the factory named by the descriptor's `integration.adapter.entry_point`). `Plugin` exposes `async open(descriptor, services, context)`, `async execute(request, context)`, `async next_event(subscription_id, context)`, `async close(context)`.

- [ ] **Step 1: Write the failing adapter test**

`plugins/emeet/c960/tests/test_plugin.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `../../../.venv/bin/python -m pytest tests/test_plugin.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'benchweave_emeet_c960.adapter'`.

- [ ] **Step 3: Write the adapter**

`plugins/emeet/c960/src/benchweave_emeet_c960/adapter.py`:

```python
"""Synthetic OTDP adapter for the EMeet SmartCam C960 4K UVC webcam.

Reads the declared parameter list from the descriptor and models identify/read/
write over a synthetic text control channel. No device I/O; not hardware
qualified. A real integration would replace protocol.py with V4L2 control
(``v4l2-ctl``) and capture (``ffmpeg``) via the working MCP service.
"""
import math
from .protocol import transaction, parse_identity, parse_value


def create_plugin():
    return Plugin()


class Plugin:
    def __init__(self):
        self.services = None
        self.closed = False
        self._params = {}

    async def open(self, descriptor, services, context):
        if self.services is not None or self.closed:
            raise RuntimeError("Use a fresh plugin instance")
        self.services = services
        self._params = {p["name"]: p for p in descriptor.get("parameters", [])}

    async def execute(self, request, context):
        verb = request.get("verb")
        operation_id = request.get("operation_id")
        dispatched = False

        def failure(code, message, uncertain=False):
            return {"operation_id": operation_id, "verb": verb,
                    "status": "unknown" if uncertain else "error",
                    "error": {"code": code, "message": message,
                              "dispatch_state": "unknown" if uncertain else "not_dispatched"}}

        def remaining():
            deadline = context.deadline_monotonic
            if (not math.isfinite(deadline) or context.is_cancelled()
                    or self.services.monotonic() >= deadline):
                raise TimeoutError("Cancelled or expired")

        if self.services is None or self.closed:
            return failure("INTERNAL_ERROR", "Plugin is not open")
        if operation_id != context.operation_id:
            return failure("INVALID_ARGUMENT", "Context identity mismatch")
        if verb not in ("identify", "read", "write"):
            return failure("UNSUPPORTED", "Only identify, read and write are supported")
        if (set(request) != {"operation_id", "verb", "arguments"}
                or not isinstance(operation_id, str) or not operation_id):
            return failure("INVALID_ARGUMENT", "Invalid envelope")

        if verb == "identify":
            if request["arguments"] != {}:
                return failure("INVALID_ARGUMENT", "Identify takes no arguments")
            exchange = transaction("identify")
        elif verb == "read":
            if set(request["arguments"]) != {"parameter"}:
                return failure("INVALID_ARGUMENT", "Read requires a single parameter")
            name = request["arguments"]["parameter"]
            param = self._params.get(name)
            if param is None:
                return failure("INVALID_ARGUMENT", "Unknown parameter %r" % name)
            exchange = transaction("read", name)
        else:  # write
            if set(request["arguments"]) != {"parameter", "value"}:
                return failure("INVALID_ARGUMENT", "Write requires parameter and value")
            name = request["arguments"]["parameter"]
            value = request["arguments"]["value"]
            param = self._params.get(name)
            if param is None:
                return failure("INVALID_ARGUMENT", "Unknown parameter %r" % name)
            if not self._check_value(param, value):
                return failure("INVALID_ARGUMENT", "Value %r invalid for %s" % (value, name))
            exchange = transaction("write", name, value)

        try:
            remaining()
            await context.mark_dispatch_started()
            dispatched = True
            response = await self.services.transfer(exchange, context)
            remaining()
            if verb == "identify":
                data = parse_identity(response["data"])
            elif verb == "read":
                data = {"parameter": name, "value": parse_value(param["type"], response["data"]),
                        "unit": param.get("unit"), "observed_at": self.services.utc_now(),
                        "age_ms": 0, "quality": "valid", "source": "device"}
            else:
                data = {"parameter": name, "requested_value": value,
                        "effective_value": value, "assurance": "acknowledged",
                        "verification": None}
            return {"operation_id": operation_id, "verb": verb, "status": "ok", "data": data}
        except TimeoutError:
            return failure("TIMEOUT", "Deadline or cancellation", dispatched)
        except ConnectionError:
            return failure("TRANSPORT_ERROR", "Connection lost", dispatched)
        except (ValueError, KeyError, TypeError):
            return failure("PROTOCOL_ERROR", "Invalid device response", dispatched)
        except RuntimeError:
            return failure("INTERNAL_ERROR", "Host resource or internal failure", dispatched)

    async def next_event(self, subscription_id, context):
        return None

    async def close(self, context):
        if self.closed:
            return
        if self.services is not None:
            await self.services.close_transport(context)
        self.closed = True

    @staticmethod
    def _check_value(param, value):
        ptype = param["type"]
        if ptype == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                return False
            lo, hi = param["range"]
            return lo <= value <= hi
        if ptype == "bool":
            return isinstance(value, bool)
        # enum
        return isinstance(value, str) and value in param["enum_values"]
```

- [ ] **Step 4: Run the full suite to verify it passes**

Run: `../../../.venv/bin/python -m pytest -q`
Expected: `12 passed`.

- [ ] **Step 5: Commit**

```bash
git add plugins/emeet/c960/src/benchweave_emeet_c960/adapter.py \
        plugins/emeet/c960/tests/test_plugin.py
git commit -m "feat(emeet-c960): add identify/read/write adapter over synthetic control channel

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: Documentation (README + AI-GUIDE)

**Files:**
- Create: `plugins/emeet/c960/README.md`
- Create: `plugins/emeet/c960/AI-GUIDE.md`

**Interfaces:**
- Consumes: the finished plugin from Tasks 1–3 (describes it).
- Produces: no code; these are the human/agent-facing docs.

- [ ] **Step 1: Write the README**

`plugins/emeet/c960/README.md`:

```markdown
# EMeet SmartCam C960 4K — BenchWeave device plugin

OTDP device plugin for the **EMeet SmartCam C960 4K** UVC webcam (USB
`328f:00ec`), used for bench/board inspection — photograph the DUT and verify
power-on / LED state visually.

> **Status: scaffold.** The adapter here models a hypothetical control channel so
> the OTDP contract is testable without hardware; it is **not** a qualified
> integration and does not contact the camera. Control and capture today go
> through V4L2 (`v4l2-ctl`) and `ffmpeg` directly — see below.

## Hardware facts

- Plain USB Video Class device (USB 2.0); no vendor protocol, no PTZ motor.
- On the bench it enumerates as `/dev/video4` (capture) + `/dev/video5` (UVC
  metadata; ignore). Address it by its stable `by-id` path, not the node number:
  `/dev/v4l/by-id/usb-EMEET_EMEET_SmartCam_C960_4K_*-video-index0`.
- Formats: MJPG up to 3840×2160; YUYV only 640×480 (anything above 640×480 is
  MJPG — pass `-input_format mjpeg` to ffmpeg).
- 16 V4L2 controls (brightness, contrast, saturation, hue, WB auto/temperature,
  gamma, gain, power-line frequency, sharpness, backlight compensation,
  auto-exposure, exposure time, continuous AF, focus, zoom) — the descriptor's
  parameter list traces each to `docs/eMeet-4K.md`.

## Working tooling — how the controlling LLM views a photo

Until this adapter is qualified, capture goes through the `benchweave-webcam` MCP
service (a separate TODO), not the OTDP adapter. The loop:

1. The MCP service runs
   `ffmpeg -y -f v4l2 -input_format mjpeg -video_size 3840x2160 -i /dev/video4 -frames:v 3 -q:v 3 -update 1 <path>`.
2. The still is written to `plugins/emeet/c960/captures/` as
   `<YYYY-MM-DDTHH-MM-SS>_<WxH>.jpg` (timestamp stem + resolution suffix).
3. The tool returns the **path** (not frame bytes); the LLM Reads the file, and
   the image is rendered for inspection.

Captured image files are runtime data and **must never be committed**: the
plugin `.gitignore` excludes the entire `captures/` directory. The descriptor
deliberately omits `capture` (the host loader does not run external-plugin
capture today); when host capture support lands, this is a follow-up revision.

## Building this plugin

This directory follows the SDK plugin layout. See `AI-GUIDE.md` for the
prompt-by-prompt build process and the OTDP 0.3.0 / adapter API 1.1 contracts.
To turn the scaffold into a real integration: replace the synthetic
`descriptor.json`, `protocol.py`/`protocol.md` and `vectors.json` with V4L2
facts, implement the adapter against `v4l2-ctl`/`ffmpeg` (or the MCP service),
then qualify independently (AI-GUIDE steps 1–5).

Gotchas (from `docs/eMeet-4K.md`): the camera is single-client ("Device or
resource busy" if a browser holds it); USB 2.0 caps 4K at ~5–15 fps; the first
frame can be dark (auto-exposure hasn't converged — grab a few frames and keep
the last); `focus_absolute` is inactive until continuous AF is switched off.
```

- [ ] **Step 2: Write the AI-GUIDE**

`plugins/emeet/c960/AI-GUIDE.md`:

```markdown
# Build a BenchWeave device plugin with AI (EMeet C960 4K)

Use one prompt at a time and review its result. This project is a synthetic
scaffold for a UVC webcam. The hardware is the device; this Python package is its
plugin. Use `plugins/<manufacturer>/<name>/` as the project root, with
`src/<package>/` inside; that project can become its own external repository.

## 1. Establish the facts

> Inspect this project and my supplied device evidence (`docs/eMeet-4K.md`: USB
> identity, V4L2 formats, the 16 controls, capture/stream commands). List exact
> model support, intended operations, control ranges/gating, and unknowns. Map
> them to OTDP 0.3.0 and adapter API 1.1. Do not invent controls. Propose a small
> plan before editing. Do not contact hardware or publish.

## 2. Implement against mocks

> Implement the protocol in protocol.py and async adapter.py. Trace every
> control to the V4L2 evidence. Keep create_plugin no-argument, construction/open
> free of device I/O, transport behind the supplied scoped services, mark
> dispatch before transmit, honour deadlines and cancellation, never retry
> silently, preserve uncertain outcomes. Run the synthetic identify/read/write
> tests before replacing them.

## 3. Demonstrate behaviour

> Extend the exact-exchange tests: supported operations, wrong correlation,
> invalid arguments (out-of-range and unknown parameter), expiry/cancellation
> before and after dispatch, malformed/truncated responses, transport loss,
> repeated close. Use SDK validation and conformance helpers. Label synthetic
> evidence separately from device captures. Do not claim hardware qualification.

## 4. Review and qualify separately

> Prepare this exact revision, descriptor, compatibility claims and evidence for
> an independent review using BenchWeave's AI device integration reviewer role.
> Draft a supervised hardware qualification plan (focus sweep, exposure, a real
> still); do not execute it without separate authority.

## 5. Prepare a release for owner review

> Build the wheel and sdist. Prepare the registry manifest, payload
> inventory/hashes, dependency locks, licence, provenance and evidence status.
> A Python wheel is not a registry admission bundle. Show artefacts and remaining
> gaps before publishing.
```

- [ ] **Step 3: Verify the suite still passes and the wheel builds**

Run:

```bash
../../../.venv/bin/python -m pytest -q
uv build
```

Expected: `12 passed`; `dist/` contains `benchweave_emeet_c960-0.1.0-py3-none-any.whl` and a `.tar.gz` (both gitignored).

- [ ] **Step 4: Commit**

```bash
git add plugins/emeet/c960/README.md plugins/emeet/c960/AI-GUIDE.md
git commit -m "docs(emeet-c960): README + AI-GUIDE (scaffold status, LLM view loop)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```
