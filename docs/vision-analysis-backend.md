# Vision-analysis backend for board monitoring

**Status:** design note — the analysis half of the *annotate-and-monitor* feature
(user draws labelled bounding boxes on a board photo, later asks "is the power
LED on?", "is the cable plugged in?", "what colour is the RGB LED?").

This doc covers **how a crop gets analysed**, local-only (no cloud/external
LLMs). It deliberately splits into two tiers because the two query kinds have
very different needs:

- **Tier 0 — non-LLM deterministic image processing.** Try this first. Covers
  the structured queries that make up most monitoring.
- **Tier 1 — small local VLMs via Ollama.** Only for the open-ended queries
  Tier 0 cannot answer.

Key design property of both tiers: **the orchestrating LLM never needs to see
the photo.** Tier 0 emits facts as data; Tier 1 returns text. Both are
model-agnostic, so the feature works even from a session whose model has no
vision input.

---

## Tier 0 — Non-LLM deterministic techniques

No model, no GPU, no network. Deterministic, free, instant (µs–ms per box),
offline, unit-testable, and it cannot hallucinate.

**Deps** (driven via uv, shared root venv):

```bash
uv pip install opencv-python-headless numpy
```

Each technique below maps to a query type.

### 1. Brightness threshold → "is the LED on/off?"

Crop the box → convert to HSV → take the **V (value) channel** → mean or max →
compare against a threshold.

Make it robust to exposure drift (the C960's auto-exposure shifts global
brightness) by **normalising against a reference**: capture once with the LED
known-off as a baseline, and report "on" only when value rises significantly
*above* that baseline. A bare absolute threshold will drift.

Gotcha: a lit white/green LED has high **value**, but so can a reflective LED
body. Combine channels — an unlit coloured LED is high-saturation + low-value;
a lit one is high-saturation + high-value.

### 2. Colour classification → "what colour is the LED?"

Crop → HSV → **hue histogram** → find dominant hue peak(s) → map to named
colours. Hue is brightness-independent, so this is more robust than raw RGB.

For an RGB LED, count distinct hue clusters and report which are present
("red + green", "blue only"). This is the same data you'd show a human, but
as numbers.

### 3. Frame differencing / change detection → "is the cable plugged in?"

Two approaches, pick per feature:

- **Reference differencing:** store a baseline crop (known "unplugged" or
  known-good). Compare live vs baseline with mean-squared error, SSIM, or a
  plain abs-diff pixel count. Below a threshold = unchanged state.
- **Edge density:** a seated connector has more structured edges (cable +
  connector body) than an empty socket. Count Canny edges in the box and
  threshold the count.

### 4. Template matching → "is this the same component / is it present?"

`cv2.matchTemplate` (normalised cross-correlation) of a saved reference crop
against the live frame. Returns confidence **and a location**, so it doubles
as a gentle re-finder if the camera drifts a few pixels between captures.

### 5. Blob / circle detection → "is the round power LED lit?"

Threshold → `findContours` / `SimpleBlobDetector` → detect a bright circular
blob at the expected spot. More shape-aware than raw brightness, so it rejects
specular streaks a plain threshold would count.

### 6. OCR (Tesseract — still non-LLM) → "read the silkscreen text on U4"

```bash
sudo apt install tesseract-ocr
uv pip install pytesseract
```

Classic OCR, not a VLM. Good for printed silkscreen labels; bad at arbitrary
scenes and handwriting. Use it for the narrow "read this label" query rather
than reaching for a VLM.

### 7. Blink detection (temporal brightness) → "is the LED blinking / how fast?"

The one technique that needs *time* rather than a single frame. A photo misses
a blink; a short burst of frames turns the box into a 1-D brightness signal
whose periodicity is the blink rate.

**Lock exposure first** — auto-exposure will fight the blink and smear the
signal, so pin it:

```bash
v4l2-ctl -d /dev/video4 --set-ctrl=auto_exposure=1          # manual
v4l2-ctl -d /dev/video4 --set-ctrl=exposure_time_absolute=100
```

**Capture ~2 s at native fps** (the C960 does 30 fps at 1080p → ~60 frames):

```bash
ffmpeg -y -f v4l2 -input_format mjpeg -video_size 1920x1080 -framerate 30 \
  -i /dev/video4 -t 2 -q:v 3 captures/led_%03d.jpg
```

**Measure the box per frame, then classify** (flat → steady on/off; alternating
→ blinking, rate from threshold crossings):

```python
import glob
import numpy as np
from PIL import Image

x, y, w, h = (1200, 800, 60, 60)          # the LED box
series = np.array([
    np.asarray(Image.open(f).convert('L'), dtype=np.float32)[y:y+h, x:x+w].mean()
    for f in sorted(glob.glob('captures/led_*.jpg'))
])

lo, hi = series.min(), series.max()
if hi - lo < 8:                            # effectively flat
    state = "on" if series.mean() > 128 else "off"
else:
    thr = (lo + hi) / 2
    above = series > thr
    switches = int(np.count_nonzero(above[1:] != above[:-1]))
    print(f"blinking at ~{switches / 2 / 2.0:.1f} Hz")   # 2 s of video
```

For the exact rate, FFT the mean-subtracted series and take the dominant peak
above ~0.5 Hz.

**Nyquist limit.** Sampling at 30 fps resolves blinks up to ~15 Hz. Status LEDs
(1–10 Hz) sit well inside; anything faster is aliased into a wrong number, and
kHz PWM dimming reads as steadily-on-but-dim to camera and eye alike. For a
>15 Hz signal, probe the LED pin with a nanoDLA/scope instead of the camera.

**Where a VLM still fits.** Run this Tier 0 path for state/rate, then send one
representative still to a Tier 1 model only for the semantic label ("green
LED", "power LED").

### Tier 0 trade-offs

| Pro | Con |
|---|---|
| Deterministic, no hallucination | Needs stable geometry (fixed camera, or re-identified box) |
| Free, offline, instant | Thresholds need calibration against a reference capture |
| Unit-testable (pure functions on arrays) | Lighting/exposure changes need normalisation |
| No GPU, no model download | Cannot answer open-ended questions |

---

## Tier 1 — Small local VLMs (Ollama, 12 GB RTX 3060)

For open-ended queries ("what is this component?", "describe what's in this
region", "read any text you can see"). All run at Q4 in ≤12 GB VRAM, zero
marginal cost, offline, private.

**Setup:**

```bash
# install ollama, then pull a model once
ollama pull moondream            # smallest / fastest
ollama pull minicpm-v4.5         # strongest small option
```

**Options (all fit a 12 GB card with headroom):**

| Model | Params | ~VRAM (Q4) | `ollama run` | Notes |
|---|---|---|---|---|
| moondream2 | 1.9B | ~2 GB | `moondream` | fastest; enough for "is the LED lit?"; weak on fine detail |
| smolvlm2 | 2B | ~2–3 GB | `smolvlm` | tiny, fast |
| qwen2.5-vl | 7B | 8–10 GB | `qwen2.5vl:7b` | all-rounder, clean JSON output; wants Ollama 0.7.0 |
| minicpm-v 2.6 | 8B | 8–9 GB | `minicpm-v` | state-of-the-art OCR |
| minicpm-v 4.5 | 8B | ~6 GB download | `minicpm-v4.5` | newest; strong OCR; strong OpenCompass result |

An 8B Q4 + vision encoder leaves comfortable headroom and returns ~30–60 tok/s,
which is plenty for occasional queries.

**Interface — keep it pluggable.** The model is an install-time choice, not a
design-time one, so the feature calls through one seam:

```python
def analyze_crop(image_bytes: bytes, prompt: str) -> str: ...
```

**Prompting tips:**

- Constrain the answer: *"Answer with exactly one of: on / off. Do not
  elaborate."*
- Ask for JSON for structured queries: `{"state": "on", "color": "green"}`.
- Send **the crop**, not the full frame — fewer tokens, faster, less
  distraction.

---

## Recommendation

Route each query by kind:

1. **Structured** — LED state/colour, blink rate, cable presence, change
   detection → **Tier 0 deterministic** (default). Free, testable, never wrong
   in the "confident nonsense" sense.
2. **Open-ended** — identify a component, read text, "what's wrong here" →
   **Tier 1 small VLM**.
3. **Tier 1 default model** — `moondream` for speed, `minicpm-v4.5` for
   accuracy/OCR; expose it as a setting.

This three-way split (deterministic first, local VLM second, cloud explicitly
out of scope) is the shape the feature's analysis backend should take.
