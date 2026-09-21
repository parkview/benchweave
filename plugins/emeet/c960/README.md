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
  parameter list traces each to `src/benchweave_emeet_c960/docs/eMeet-4K.md`.

## Operating this plugin

The plugin is developed with **uv**. The uv project lives at the worktree root
(`benchweave-upstream/` — where `.venv`, `uv.lock` and `.python-version` sit);
the plugin is installed **editable** into that shared venv and has no venv of its
own. Every command below runs from this directory (`plugins/emeet/c960/`); the
`--no-project` flag tells uv to use the discovered root venv rather than create a
plugin-local one.

**Run the tests:**

```bash
uv run --no-project pytest -q
```

**Drive the adapter** against a mock host (no hardware):

```bash
uv run --no-project python - <<'EOF'
import asyncio, json
from importlib.resources import files
from benchweave_sdk.testing import MockContext, MockHost
from benchweave_sdk.validation import validate_descriptor, validate_result
from benchweave_emeet_c960.adapter import create_plugin
from benchweave_emeet_c960.protocol import transaction

descriptor = json.loads(files("benchweave_emeet_c960")
                        .joinpath("descriptor.json").read_text())
validate_descriptor(descriptor)

async def run():
    host = MockHost([
        (transaction("identify"), {"data": b"EMeet,SmartCam C960 4K\n"}),
        (transaction("read", "brightness"), {"data": b"32\n"}),
        (transaction("write", "brightness", 32), {"data": b"OK\n"}),
    ])
    plugin = create_plugin()
    ctx = MockContext("op-1", deadline_monotonic=1.0)
    await plugin.open(descriptor, host, ctx)
    for verb, args in [("identify", {}),
                       ("read", {"parameter": "brightness"}),
                       ("write", {"parameter": "brightness", "value": 32})]:
        req = {"operation_id": "op-1", "verb": verb, "arguments": args}
        res = await plugin.execute(req, ctx)
        validate_result(res, req)
        print(f"{verb:9} -> {json.dumps(res, sort_keys=True)}")
    host.assert_complete()
    await plugin.close(ctx)

asyncio.run(run())
EOF
```

**Build the wheel** (`dist/benchweave_emeet_c960-0.1.0-py3-none-any.whl`):

```bash
uv build
```

**(Re)install the plugin's test extra** into the root venv:

```bash
uv pip install -e ".[test]"
```

**Browse the files** in Dolphin:

```bash
dolphin plugins/emeet/c960
```

Captures are excluded from git (`.gitignore` covers `captures/`); see "Working
tooling" below for the capture convention.

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

Gotchas (from `src/benchweave_emeet_c960/docs/eMeet-4K.md`): the camera is single-client ("Device or
resource busy" if a browser holds it); USB 2.0 caps 4K at ~5–15 fps; the first
frame can be dark (auto-exposure hasn't converged — grab a few frames and keep
the last); `focus_absolute` is inactive until continuous AF is switched off.
