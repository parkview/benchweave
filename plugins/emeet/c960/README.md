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
