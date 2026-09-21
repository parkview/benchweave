# EMeet SmartCam C960 4K — device-plugin design

**Date:** 2026-09-21
**Status:** approved design, pre-implementation
**Branch:** `plugins/emeet-c960` (off `upstream/main`, in the `benchweave-upstream` worktree)
**Issue:** [madeinoz67/benchweave#95](https://github.com/madeinoz67/benchweave/issues/95)
**Device evidence:** `docs/eMeet-4K.md` (in the `main` fork; carried into this work as a fact source)

## 1. Purpose

A BenchWeave OTDP device plugin for the **EMeet SmartCam C960 4K** UVC webcam,
used for bench/board inspection (photograph the DUT, verify power-on / LED state
visually). The camera is a plain USB Video Class device with no vendor protocol:
identity and control are exposed through V4L2 (`v4l2-ctl`), capture through
`ffmpeg`.

## 2. Scope

**In scope — a scaffold, mirroring `plugins/muselab/nanodla`:**

- A device plugin at `plugins/emeet/c960/` implementing adapter API 1.1 for the
  core verbs **`identify`, `read`, `write`**.
- A descriptor whose **identity and 16-control parameter surface are real**,
  traced to `docs/eMeet-4K.md`.
- **Synthetic** `protocol.py`/`protocol.md`/`vectors.json`/`adapter.py`, clearly
  labelled, not wired to `v4l2-ctl`/`ffmpeg`.
- A README documenting the working path (the `benchweave-webcam` MCP service),
  the LLM view loop, the capture-folder convention, and all limitations.
- Tests proving the adapter contract without hardware.

**Out of scope (explicitly deferred):**

- `capture`/`stream` — the host loader does not run external-plugin capture; the
  working capture path is the MCP service (see §7).
- Any `otdp.camera/*` class — no published OTDP version defines a camera class
  (`device-classes` §1 lists cameras as "not yet defined"). The plugin is an
  **unclassified core integration**.
- Hardware qualification — no device is contacted; the scaffold is labelled
  synthetic.

## 3. Location and naming

| Item | Value |
|---|---|
| Project root | `plugins/emeet/c960/` |
| Distribution | `benchweave-emeet-c960` |
| Import package | `benchweave_emeet_c960` |
| Adapter factory | `benchweave_emeet_c960.adapter:create_plugin` |

Mirrors `plugins/fnirsi/dps150` and `plugins/muselab/nanodla` layout:
`plugins/<manufacturer>/<name>/` with `src/<package>/` inside. The model directory
must build and test unchanged outside the core checkout.

## 4. Honesty split

Three distinct trust levels, kept separate throughout:

| Level | Content |
|---|---|
| **Real** | descriptor identity (manufacturer/model), the 16 V4L2 controls and their ranges, USB ID `328f:00ec`, the `/dev/v4l/by-id/…` addressing note. |
| **Synthetic** | the adapter's `identify`/`read`/`write` exchanges, `protocol.py`/`protocol.md`, `vectors.json`. These model a hypothetical control channel so the contract can be tested; no real device response is claimed. |
| **Deferred** | capture/stream, the custom-transport host-service extension, firmware version pinning, hardware qualification. |

## 5. Descriptor

`src/benchweave_emeet_c960/descriptor.json`, validated by
`benchweave_sdk.validation.validate_descriptor` against the SDK-bundled OTDP
**0.3.0** schema. (The authoring SDK ships 0.3.0; the repo's
`standards/otdp/0.2.0` corpus is behind the SDK and its schema would reject a
0.2.0 descriptor, so the plugin targets 0.3.0 to match `validate_descriptor` and
the nanoDLA scaffold.)

- `otdp_version`: `"0.3.0"` (matches the nanoDLA scaffold and the SDK's bundled
  contract, which is what `validate_descriptor` actually checks).
- `descriptor_version`: `"0.1.0"` (mirrors the nanoDLA 0.3.0 scaffold).
- `id`: `"dev.emeet.c960"`.
- `display_name`: `"EMeet SmartCam C960 4K"`.
- `description`: states scaffold status and that it is not hardware qualified.

**Identity** (`strategy: "adapter"`, `firmware_policy: "commissioned"`):

- `manufacturer`: `"EMeet"`, `model`: `"SmartCam C960 4K"`.
- `firmware_policy: "commissioned"` — the UVC firmware version is not captured in
  the evidence, so it is resolved at commissioning rather than fabricated as a
  `listed` entry. Schema-valid: `listed` is the only policy that forces
  `supported_firmware`.

**Integration:** `mode: "adapter"`, entry point as §3, `api_version: "1.1"`,
`permissions: ["scoped_transport"]` (no `artifact_writer` — capture is not declared).

**Transport** (`type: "custom"`, honest — UVC is not serial/i2c/spi):

- `connection_key: "emeet_c960"`.
- `settings.protocol_reference: "protocol.md"`.
- The descriptor does not grant a device path; `connection_key` resolves through
  commissioned gateway configuration. The README notes a custom-transport
  host-service extension (V4L2) does not yet exist, so this is a scaffold
  declaration, not a runnable transport.

**Capabilities:** `["identify", "read", "write"]`.

**Required features:** `["otdp.core/0.3.0", "otdp.adapter/1.1"]` (0.3.0 mandates
`otdp.core/0.3.0`; `otdp.adapter/1.1` reflects the adapter API).

**Provenance** (required in 0.3.0): `sources` traces the descriptor to
`docs/eMeet-4K.md` (revision `2026-09-21`); `test_vectors` points at
`vectors.json` as the synthetic identify/read/write exchanges.

**Operations** (policies per the schema):

| Operation | timeout_ms | side_effect | retry | cancellable | completion |
|---|---|---|---|---|---|
| `identify` | 1000 | none | never | true | acknowledged |
| `read` | 1000 | none | never | true | acknowledged |
| `write` | 1000 | state_change | never | true | acknowledged |

**Parameters — the 16 V4L2 controls** (real, from `docs/eMeet-4K.md` control
table). All are `access: "rw"`, `semantic: "configuration"`,
`hazard_class: "none"`, `binding: {"kind": "adapter", "key": "<v4l2 control name>"}`,
`read_policy: {"max_age_ms": 0, "destructive": false}`, and
`write_policy: {"effect": "setting", "retry": "never"}` with `completion` set per
the control (below). Integer controls carry `unit: "1"` (dimensionless — required
for `int`/`float` in 0.3.0); `bool` and `enum` controls carry no `unit`.

| Control | type | range / enum | gated by |
|---|---|---|---|
| `brightness` | int | −64…64 | — |
| `contrast` | int | 0…100 | — |
| `saturation` | int | 0…128 | — |
| `hue` | int | −40…40 | — |
| `white_balance_automatic` | bool | — | — |
| `white_balance_temperature` | int | 2300…6500 | WB auto on |
| `gamma` | int | 72…255 | — |
| `gain` | int | 0…100 | — |
| `power_line_frequency` | enum | Disabled / 50 Hz / 60 Hz | — |
| `sharpness` | int | 1…64 | — |
| `backlight_compensation` | int | 1…2 | — |
| `auto_exposure` | enum | 1 Manual / 3 Aperture-Priority | — |
| `exposure_time_absolute` | int | 1…5000 | exposure != manual |
| `focus_automatic_continuous` | bool | — | — |
| `focus_absolute` | int | 0…1023 | AF on |
| `zoom_absolute` | int | 0…100 | — |

Gating (the "auto" switch controls the underlying parameter) is a documented
constraint in each gated parameter's `description` and in `protocol.md`; OTDP has
no dedicated gating field, so it is not invented. `write_policy.completion` is
`acknowledged` for all controls (a V4L2 set that returns success is acknowledged);
it is not upgraded to `readback`/`physical` for the scaffold.

## 6. Adapter (`adapter.py`, API 1.1)

Structurally identical to `plugins/muselab/nanodla/.../adapter.py`, extended for
`write`:

- `create_plugin()` — no-argument factory returning a fresh `Plugin`.
- `open(descriptor, services, context)` — attaches scoped services; **no device
  I/O**; rejects reuse after close.
- `execute(request, context)` — validates the envelope (`operation_id`, `verb`,
  `arguments`), checks context identity, rejects unsupported verbs before I/O,
  then checks deadline/cancellation **before** `mark_dispatch_started()`, performs
  the synthetic transfer, parses, and returns the result. Failures map to
  `INVALID_ARGUMENT`, `UNSUPPORTED`, `TIMEOUT`, `TRANSPORT_ERROR`,
  `PROTOCOL_ERROR`, `INTERNAL_ERROR` with correct `dispatch_state`
  (`not_dispatched` vs `unknown`). No automatic replay, reconnect, or hidden work.
- `next_event(...)` → `None` (no subscriptions).
- `close(context)` — idempotent, bounded, releases transport; failed open permits
  close; reopen requires a fresh instance.

`read` resolves a parameter `key` against the declared parameter list; `write`
validates the value against the parameter's type/range before dispatch. Read
results are validated for frame shape/type only — not int range or enum
membership — so the real V4L2 path must decide how to treat an out-of-range or
off-enum read (degrade `quality`, clamp, or error).

## 7. Capture / view story (documented, not implemented here)

The controlling LLM views a photo through the **working path**, not the OTDP
adapter (which cannot run capture yet). The loop, documented in the README:

1. The `benchweave-webcam` MCP service (separate TODO, in the `main` fork) runs
   `ffmpeg -f v4l2 -input_format mjpeg …` to grab a still.
2. The still is written to `plugins/emeet/c960/captures/` as
   `<YYYY-MM-DDTHH-MM-SS>_<WxH>.jpg` (timestamp stem + resolution suffix, matching
   nanoDLA's timestamp stem and the `plugins/adc_6ch_12bit/captures/` convention).
3. The tool returns the **path** (not frame bytes); the LLM Reads the file, and the
   image is rendered for inspection.

Captured image files are runtime data and **must never be committed**: the
plugin's `.gitignore` excludes the entire `captures/` directory, so no capture
output can be swept into version control by a stray `git add`. The OTDP descriptor
deliberately omits `capture`; when host capture support lands, this is a follow-up
revision, not part of this scaffold.

## 8. Testing (`tests/test_plugin.py`)

Using `benchweave_sdk.testing.MockHost`/`MockContext`,
`benchweave_sdk.validation.validate_descriptor`/`validate_result`, and
`benchweave_sdk.conformance.check_lifecycle`, mirroring the nanoDLA suite:

- `validate_descriptor` passes against the SDK-bundled 0.3.0 schema.
- Happy path: `identify`, `read` (a representative control), `write` (set + read
  back).
- No-transmit-before-dispatch: cancelled, expired, bad arguments, wrong context —
  each returns `error` with `dispatch_state: "not_dispatched"` and zero transfers.
- Uncertain-outcome-after-dispatch: malformed/NaN/oversized response, transport
  loss, deadline, host failure — each returns `unknown` with `dispatch_state:
  "unknown"`.
- `write` rejects an out-of-range / unknown parameter before dispatch.
- Lifecycle conformance and double-close idempotence.

## 9. Files

```
plugins/emeet/c960/
    .gitignore            # captures/, dist/, __pycache__, .venv
    pyproject.toml        # hatchling; test extra: benchweave-sdk==0.1.0, pytest
    README.md             # scaffold status, hardware facts, LLM view loop, limitations
    AI-GUIDE.md           # 5-step build prompts (camera-adapted)
    src/benchweave_emeet_c960/
        __init__.py
        descriptor.json
        adapter.py
        protocol.py
        protocol.md
        vectors.json
    tests/
        test_plugin.py
```

## 10. Contract references

- `benchweave-sdk` bundled contracts (`otdp-v0.3.0/otdp-device-descriptor.schema.json`,
  `otdp-v0.3.0/otdp-runtime.schema.json`) — the authoritative descriptor/runtime
  contracts the SDK's `validate_descriptor`/`validate_result` actually check.
- `standards/otdp/0.2.0/otdp-specification.md` — core operations, adapter ABI 1.1,
  error codes, transport (§6.4 adapter transports); the repo's published standard
  (0.2.0) lags the authoring SDK (0.3.0), so field-level detail defers to the SDK
  contracts above.
- `standards/otdp/0.2.0/device-classes.md` — §1 establishes no camera class exists.
- `docs/device-developer-guide.md`, `docs/develop-your-device.md` — P1–P5 workflow.
