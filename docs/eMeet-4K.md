# eMeet C960 4K webcam — CLI reference

Reference for driving the **EMeet SmartCam C960 4K** UVC webcam from the Linux
command line: identity, supported formats, control list, capture/stream commands,
and measured focus behaviour. Tracked as a BenchWeave device-plugin proposal in
[madeinoz67/benchweave#95](https://github.com/madeinoz67/benchweave/issues/95).

## Identity

- USB ID: `328f:00ec` — `EMEET EMEET SmartCam C960 4K`
- Class: UVC (USB Video Class), USB 2.0 (no vendor protocol, no PTZ motor).
- On the current bench it enumerates as:
  - `/dev/video4` — capture node (index 0)
  - `/dev/video5` — UVC metadata node (index 1; ignore)
- The built-in laptop webcam (Sonix/Microdia) occupies `/dev/video0`–`video3`.

## Supported formats (on `/dev/video4`)

| Type | Pixel format | Resolutions |
|---|---|---|
| Compressed | MJPG (Motion-JPEG) | 3840×2160, 2560×1440, 1920×1080, 1280×960, 1280×720, 1024×576, 960×720, 800×600, 640×480, 640×360 |
| Raw | YUYV 4:2:2 | 640×480, 640×360 only |

**Anything above 640×480 is MJPG only** — pass `-input_format mjpeg` to ffmpeg.

## CLI setup

```bash
sudo apt install v4l-utils     # v4l2-ctl (controls); ffmpeg/ffplay already present
v4l2-ctl --list-devices        # enumerate cameras and their /dev/videoN nodes
v4l2-ctl -d /dev/video4 --list-ctrls        # list controls + current values
v4l2-ctl -d /dev/video4 --list-ctrls-menus  # menu labels (e.g. exposure modes)
```

## Control reference

`inactive` controls are gated by a matching "auto" switch — flip the switch off
to make the underlying control settable.

| Control | Type | Range | Default | Notes |
|---|---|---|---|---|
| `brightness` | int | −64…64 | 0 | |
| `contrast` | int | 0…100 | 57 | |
| `saturation` | int | 0…128 | 80 | |
| `hue` | int | −40…40 | 0 | |
| `white_balance_automatic` | bool | 0/1 | 1 | gate for `white_balance_temperature` |
| `white_balance_temperature` | int | 2300…6500 | 5000 | `inactive` while WB auto |
| `gamma` | int | 72…255 | 214 | |
| `gain` | int | 0…100 | 0 | |
| `power_line_frequency` | menu | Disabled / 50 Hz / 60 Hz | 50 Hz | |
| `sharpness` | int | 1…64 | 32 | |
| `backlight_compensation` | int | 1…2 | 1 | |
| `auto_exposure` | menu | 1 Manual / 3 Aperture-Priority | 3 | gate for `exposure_time_absolute` |
| `exposure_time_absolute` | int | 1…5000 | 300 | `inactive` in aperture-priority |
| `focus_automatic_continuous` | bool | 0/1 | 1 | **continuous autofocus** (gate for focus) |
| `focus_absolute` | int | 0…1023 | 192 | `inactive` while AF on |
| `zoom_absolute` | int | 0…100 | 0 | digital zoom |

## Commands

### Focus

```bash
# manual focus: turn continuous autofocus OFF, then set the lens
v4l2-ctl -d /dev/video4 --set-ctrl=focus_automatic_continuous=0
v4l2-ctl -d /dev/video4 --set-ctrl=focus_absolute=512        # 0…1023

# back to autofocus
v4l2-ctl -d /dev/video4 --set-ctrl=focus_automatic_continuous=1
```

This unit uses `focus_automatic_continuous` (always-on AF), **not** a one-shot
`focus_auto` trigger. `focus_absolute` only becomes active once AF is off.

### Zoom (digital)

```bash
v4l2-ctl -d /dev/video4 --set-ctrl=zoom_absolute=50          # 0…100
```

### Exposure (manual)

```bash
v4l2-ctl -d /dev/video4 --set-ctrl=auto_exposure=1           # Manual Mode
v4l2-ctl -d /dev/video4 --set-ctrl=exposure_time_absolute=100   # 1…5000
```

### Photo

```bash
# single 4K still
ffmpeg -y -f v4l2 -input_format mjpeg -video_size 3840x2160 \
  -i /dev/video4 -frames:v 1 -q:v 2 photo.jpg

# grab a few frames and keep the last (auto-exposure has time to settle)
ffmpeg -y -f v4l2 -input_format mjpeg -video_size 3840x2160 -framerate 30 \
  -i /dev/video4 -frames:v 3 -q:v 3 -update 1 photo.jpg
```

### Stream

```bash
# live preview
ffplay -f v4l2 -input_format mjpeg -video_size 1920x1080 -framerate 30 /dev/video4

# record (re-encode to H.264 MP4)
ffmpeg -y -f v4l2 -input_format mjpeg -video_size 1920x1080 -framerate 30 \
  -i /dev/video4 -c:v libx264 -pix_fmt yuv420p out.mp4
```

## Focus behaviour (measured 2026-09-21)

10-shot sweep over the 0…1023 focus range (10% steps), each scored with a
Laplacian standard-deviation sharpness metric (×1000, higher = sharper):

| Focus % | value | sharpness |
|---:|---:|---:|
| 10 | 102 | 14.1 |
| 20 | 205 | **21.6** |
| 30 | 307 | 5.9 |
| 40 | 410 | 17.2 |
| 50 | 512 | 15.1 |
| 60 | 614 | 2.6 |
| 70 | 717 | 6.9 |
| 80 | 819 | 16.4 |
| 90 | 922 | **19.0** |
| 100 | 1023 | 7.2 |
| (AF on) | — | 15.5 |

Metric command (ImageMagick):

```bash
convert photo.jpg -colorspace Gray -convolve '0,-1,0,-1,4,-1,0,-1,0' \
  -format "%[fx:standard_deviation*1000]" info:
```

**Interpretation.** The focus control demonstrably works (≈8× sharpness spread
across the range), but the curve is multi-peaked (≈20%, ≈40–50%, ≈90%) rather
than a single peak. That is expected when the frame contains objects at several
depths — a whole-frame metric spikes wherever *any* region is in focus. The
autofocus reference (15.5) lands near the mid-range (512), consistent with AF
settling around mid focus for this scene.

To map the **near↔far direction** of the 0…1023 axis, re-run the sweep against a
single subject at a known distance with a plain background (e.g. the DUT at a
fixed 300 mm), so exactly one peak appears and its position names the direction.

## Gotchas

- **"Device or resource busy"** — another app (browser, Cheese, Zoom) holds the
  camera. UVC is single-client: close it and retry.
- **4K frame rate** — USB 2.0, so 3840×2160 MJPG runs at low fps (~5–15). Use
  1920×1080 (30 fps) for smooth video; reserve 4K for stills.
- **First frame can be dark** — auto-exposure hasn't converged on device open;
  grab a few frames and keep the last (`-update 1`).
- **Node changes on replug** — `/dev/videoN` is assigned by enumeration order.
  Address the camera by its stable `by-id` path instead:
  `/dev/v4l/by-id/usb-EMEET_EMEET_SmartCam_C960_4K_*-video-index0`.
- No `sudo` needed to capture — the node is owned by the `video` group and the
  current user has access.
