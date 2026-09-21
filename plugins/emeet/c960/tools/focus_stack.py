"""Focus-stack a set of aligned frames into one all-in-focus image.

Usage (from ``plugins/emeet/c960/``, after ``align_image_stack -a aligned_ -m -C``)::

    uv run --no-project python tools/focus_stack.py

Reads ``aligned_*.tif``, computes a per-pixel focus map (local Laplacian
variance), softmax-weights each frame per pixel, then blends through a Laplacian
pyramid so fine detail stays from a single frame while depth transitions stay
seamless. Writes ``captures/focused_pyramid.jpg``.
"""
import glob

import numpy as np
from PIL import Image
from scipy import ndimage

LEVELS = 7
TEMPERATURE = 0.10   # lower = sharper per-pixel selection
SMOOTH_SIGMA = 3.0   # px; just enough to kill speckle, not seam-blend


def blur_spatial(a, sigma=1.0):
    if a.ndim == 3:
        return ndimage.gaussian_filter(a, sigma=(sigma, sigma, 0.0))
    return ndimage.gaussian_filter(a, sigma=sigma)


def zoom2(a):
    if a.ndim == 3:
        return ndimage.zoom(a, (2.0, 2.0, 1.0), order=1)
    return ndimage.zoom(a, 2.0, order=1)


def match(a, shape):
    out = np.zeros(shape, dtype=a.dtype)
    h = min(a.shape[0], shape[0])
    w = min(a.shape[1], shape[1])
    out[:h, :w] = a[:h, :w]
    return out


def gaussian_pyramid(img, levels):
    pyr = [img]
    cur = img
    for _ in range(levels - 1):
        cur = blur_spatial(cur, 1.0)[::2, ::2]
        pyr.append(cur)
    return pyr


def laplacian_pyramid(img, levels):
    gp = gaussian_pyramid(img, levels)
    lp = []
    for i in range(levels - 1):
        up = match(zoom2(gp[i + 1]), gp[i].shape)
        lp.append(gp[i] - up)
    lp.append(gp[-1])
    return lp


def reconstruct(lp):
    cur = lp[-1]
    for i in range(len(lp) - 2, -1, -1):
        up = match(zoom2(cur), lp[i].shape)
        cur = up + lp[i]
    return cur


def focus_measure(g):
    lap = ndimage.laplace(g)
    m = ndimage.uniform_filter(lap, size=13)
    v = ndimage.uniform_filter(lap * lap, size=13) - m * m
    return v.astype(np.float32)


def main():
    files = sorted(glob.glob('aligned_*.tif'))
    if not files:
        raise SystemExit('no aligned_*.tif — run align_image_stack -a aligned_ -m -C first')
    rgb, gray = [], []
    for f in files:
        a = np.asarray(Image.open(f).convert('RGB'), dtype=np.float32) / 255.0
        rgb.append(a)
        gray.append(a @ np.array([0.299, 0.587, 0.114], dtype=np.float32))
    rgb = np.stack(rgb).astype(np.float32)
    gray = np.stack(gray).astype(np.float32)
    n = len(files)

    fm = np.stack([focus_measure(g) for g in gray])
    fm_norm = fm / (fm.max(axis=0, keepdims=True) + 1e-8)
    w = np.exp(fm_norm / TEMPERATURE).astype(np.float32)
    w = w / w.sum(axis=0, keepdims=True)
    w = ndimage.gaussian_filter(w, sigma=(0, SMOOTH_SIGMA, SMOOTH_SIGMA))
    w = w / w.sum(axis=0, keepdims=True)

    acc = [np.zeros((*gaussian_pyramid(rgb[0], LEVELS)[l].shape[:2], 3), np.float32)
           for l in range(LEVELS)]
    for i in range(n):
        lp = laplacian_pyramid(rgb[i], LEVELS)
        mp = gaussian_pyramid(w[i], LEVELS)
        for l in range(LEVELS):
            acc[l] += lp[l] * mp[l][..., None]

    out = np.clip(reconstruct(acc), 0, 1)
    Image.fromarray((out * 255.0).astype(np.uint8), 'RGB').save(
        'captures/focused_pyramid.jpg', quality=95)
    print(f'fused {n} frames -> captures/focused_pyramid.jpg '
          f'{out.shape[1]}x{out.shape[0]}')


if __name__ == '__main__':
    main()
