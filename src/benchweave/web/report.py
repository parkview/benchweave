"""Self-contained HTML report for a capture.

Renders a capture's series as an inline SVG chart (matching the Analyse tab's
dual-axis layout), overlays the A-Z letter markers and their notes, and — when a
region is supplied — shades that region and appends the same per-channel
statistics table the live viewer shows.

The output is a single ``.html`` file with no external assets, so it can be
saved next to the CSV and shared as-is.
"""

from __future__ import annotations

from typing import Any, cast

# Mirrors the COLORS palette in static/app.js.
_COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#f032e6"]

WIDTH = 900
HEIGHT = 420
_M = {"left": 60, "right": 60, "top": 42, "bottom": 44}

_STAT_COLS = ["Min", "Mean", "Max", "RMS", "Pk-Pk"]

Series = dict[str, Any]
Points = list[list[float | None]]


def _pts(s: Series) -> Points:
    return cast(Points, s["points"])


def _fmt(v: float) -> str:
    a = abs(v)
    if a >= 100:
        s = f"{a:.1f}"
    elif a >= 1:
        s = f"{a:.3f}"
    else:
        s = f"{a:.3g}"
    return ("-" if v < 0 else "") + s


def _fmt_signed(v: float) -> str:
    return ("+" if v >= 0 else "-") + _fmt(abs(v))


_MODE_LABELS = {
    "battery": "Battery drain",
    "dc-dc": "DC-DC efficiency",
    "sleep": "Sleep / wake",
    "load-step": "Load step (R)",
}


def _fmt_time(t: float) -> str:
    if t < 0.001:
        return f"{t * 1e6:.0f} µs"
    if t < 1:
        return f"{t * 1e3:.2f} ms"
    return f"{t:.3f} s"


def _region_stats(points: Points, lo: float, hi: float) -> dict[str, float] | None:
    count = 0
    mn = float("inf")
    mx = float("-inf")
    total = 0.0
    total_sq = 0.0
    for t, v in points:
        if t is None or v is None or t < lo or t > hi:
            continue
        count += 1
        mn = min(mn, v)
        mx = max(mx, v)
        total += v
        total_sq += v * v
    if count == 0:
        return None
    mean = total / count
    return {
        "count": float(count),
        "min": mn,
        "max": mx,
        "mean": mean,
        "rms": (total_sq / count) ** 0.5,
        "pp": mx - mn,
    }


def _trapezoid(points: Points, lo: float, hi: float) -> float:
    total = 0.0
    for k in range(len(points) - 1):
        t0, v0 = points[k]
        t1, v1 = points[k + 1]
        if t0 is None or t1 is None or v0 is None or v1 is None:
            continue
        a = max(t0, lo)
        b = min(t1, hi)
        if b <= a:
            continue
        span = t1 - t0 or 1.0
        va = v0 + (v1 - v0) * ((a - t0) / span)
        vb = v0 + (v1 - v0) * ((b - t0) / span)
        total += ((va + vb) / 2.0) * (b - a)
    return total


def _power_points(volts: Points, amps: Points) -> Points:
    n = min(len(volts), len(amps))
    out: Points = []
    for k in range(n):
        t = volts[k][0]
        if t is None:
            continue
        v_val = volts[k][1]
        a_val = amps[k][1]
        v = 0.0 if v_val is None else v_val
        a = 0.0 if a_val is None else a_val
        out.append([t, v * a])
    return out


def _power_region_stats(
    volts: Points, amps: Points, lo: float, hi: float
) -> tuple[float, float] | None:
    """Return (mean, peak) power over the region, or None if empty."""
    count = 0
    total = 0.0
    peak = float("-inf")
    for k in range(min(len(volts), len(amps))):
        t = volts[k][0]
        if t is None or t < lo or t > hi:
            continue
        v_val = volts[k][1]
        a_val = amps[k][1]
        v = 0.0 if v_val is None else v_val
        a = 0.0 if a_val is None else a_val
        p = v * a
        count += 1
        total += p
        peak = max(peak, p)
    if count == 0:
        return None
    return total / count, peak


def _series_axis(series: list[Series]) -> tuple[list[int], list[int]]:
    """Split series indices into (left-axis, right-axis) on the A unit."""
    left: list[int] = []
    right: list[int] = []
    for i, s in enumerate(series):
        unit = str(s.get("unit", "")).upper()
        (right if unit == "A" else left).append(i)
    return left, right


def _path(points: Points, xmax: float, ylo: float, yhi: float) -> str:
    plot_x = _M["left"]
    plot_w = WIDTH - _M["left"] - _M["right"]
    plot_y = _M["top"]
    plot_h = HEIGHT - _M["top"] - _M["bottom"]
    parts: list[str] = []
    pen_down = False
    for t, v in points:
        if t is None or v is None:
            pen_down = False
            continue
        px = plot_x + (t / xmax) * plot_w if xmax else plot_x
        py = plot_y + (1.0 - (v - ylo) / (yhi - ylo)) * plot_h
        parts.append(f"{'L' if pen_down else 'M'}{px:.2f},{py:.2f}")
        pen_down = True
    return " ".join(parts)


def build_report(
    data: dict[str, object],
    markers: list[dict[str, Any]],
    lo: float | None,
    hi: float | None,
    power: dict[str, Any] | None = None,
    assertions: list[dict[str, Any]] | None = None,
) -> str:
    """Return a complete HTML document for the capture."""
    name = str(data.get("name", "capture"))
    meta = cast(dict[str, Any], data.get("meta") or {})
    series = cast(list[Series], data.get("series") or [])
    duration = float(cast(Any, data.get("duration_s", 0.0)) or 0.0) or 1.0
    sample_count = int(cast(Any, data.get("sample_count", 0)))

    left_idx, right_idx = _series_axis(series)

    def _yrange(idxs: list[int]) -> tuple[float, float]:
        vals: list[float] = []
        for i in idxs:
            vals.extend(v for _, v in _pts(series[i]) if v is not None)
        if not vals:
            return 0.0, 1.0
        lo_v, hi_v = min(vals), max(vals)
        if lo_v >= 0:
            lo_v = 0.0
        if hi_v == lo_v:
            hi_v = lo_v + 1.0
        return lo_v, hi_v

    y_left = _yrange(left_idx)
    y_right = _yrange(right_idx)
    y_left = _pad(y_left)
    y_right = _pad(y_right)

    plo = phi = None
    if power:
        raw_lo = power.get("lo")
        raw_hi = power.get("hi")
        if raw_lo is not None and raw_hi is not None:
            plo = min(float(cast(Any, raw_lo)), float(cast(Any, raw_hi)))
            phi = max(float(cast(Any, raw_lo)), float(cast(Any, raw_hi)))

    svg = _render_svg(
        name, series, left_idx, right_idx, duration,
        y_left, y_right, lo, hi, markers, plo, phi,
    )

    notes_rows = ""
    for m in markers:
        label = str(m.get("label", ""))
        t = float(cast(Any, m.get("t", 0.0)))
        note = str(m.get("note", "")).strip()
        note_html = _esc(note).replace("\n", "<br>") if note else "—"
        notes_rows += (
            f'<div class="note"><span class="badge">{_esc(label)}</span>'
            f'<span class="ntime">{_fmt_time(t)}</span>'
            f'<span class="ntext">{note_html}</span></div>'
        )
    notes_block = (
        f'<section class="notes"><h2>Notes</h2>{notes_rows}</section>'
        if markers
        else ""
    )

    stats_block = ""
    if lo is not None and hi is not None:
        stats_block = _render_region(series, lo, hi)

    power_block = ""
    if power:
        power_block = _render_power(series, power, duration)

    assertions_block = ""
    if assertions:
        assertions_block = _render_assertions(assertions)

    meta_bits = []
    if meta.get("note"):
        meta_bits.append(str(meta["note"]))
    if meta.get("sample_rate_hz"):
        meta_bits.append(f'{meta["sample_rate_hz"]} Hz')
    meta_bits.append(f"{sample_count:,} samples")
    meta_bits.append(f"{duration:.1f} s")
    meta_line = " · ".join(meta_bits)

    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(name)}</title>\n<style>{_css()}</style>\n</head>\n<body>\n"
        f'<header><h1>{_esc(name)}</h1><p class="meta">{_esc(meta_line)}</p></header>\n'
        f'<div class="chart">{svg}</div>\n{notes_block}{stats_block}'
        f'{power_block}{assertions_block}\n'
        "</body>\n</html>\n"
    )


def _pad(rng: tuple[float, float]) -> tuple[float, float]:
    span = rng[1] - rng[0] or 1.0
    return (rng[0] - 0.05 * span, rng[1] + 0.05 * span)


def _render_svg(
    name: str,
    series: list[Series],
    left_idx: list[int],
    right_idx: list[int],
    xmax: float,
    y_left: tuple[float, float],
    y_right: tuple[float, float],
    lo: float | None,
    hi: float | None,
    markers: list[dict[str, Any]],
    plo: float | None = None,
    phi: float | None = None,
) -> str:
    plot_x = _M["left"]
    plot_y = _M["top"]
    plot_w = WIDTH - _M["left"] - _M["right"]
    plot_h = HEIGHT - _M["top"] - _M["bottom"]

    def px(t: float) -> float:
        return plot_x + (t / xmax) * plot_w if xmax else plot_x

    def py(v: float, ylo: float, yhi: float) -> float:
        return plot_y + (1.0 - (v - ylo) / (yhi - ylo)) * plot_h

    parts: list[str] = [
        f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="{_esc(name)}">',
        f'<rect x="{plot_x}" y="{plot_y}" width="{plot_w}" height="{plot_h}" '
        f'fill="#ffffff" stroke="#cccccc"/>',
    ]

    for k in range(6):
        frac = k / 5
        v = y_left[0] + frac * (y_left[1] - y_left[0])
        gy = py(v, *y_left)
        parts.append(
            f'<line x1="{plot_x}" y1="{gy:.2f}" x2="{plot_x + plot_w}" y2="{gy:.2f}" '
            f'stroke="#eeeeee"/>'
        )
        parts.append(
            f'<text x="{plot_x - 6}" y="{gy:.2f}" text-anchor="end" '
            f'dominant-baseline="middle" class="tick">{_fmt(v)}</text>'
        )

    for k in range(6):
        frac = k / 5
        v = y_right[0] + frac * (y_right[1] - y_right[0])
        gy = py(v, *y_right)
        parts.append(
            f'<text x="{plot_x + plot_w + 6}" y="{gy:.2f}" text-anchor="start" '
            f'dominant-baseline="middle" class="tick">{_fmt(v)}</text>'
        )

    for k in range(6):
        t = (k / 5) * xmax
        gx = px(t)
        parts.append(
            f'<text x="{gx:.2f}" y="{plot_y + plot_h + 16}" text-anchor="middle" '
            f'class="tick">{_fmt(t)}</text>'
        )
    parts.append(
        f'<text x="{(plot_x + plot_w) / 2:.2f}" y="{HEIGHT - 8}" text-anchor="middle" '
        f'class="axis">elapsed (s)</text>'
    )
    left_unit = str(series[left_idx[0]].get("unit", "") or "") if left_idx else ""
    right_unit = str(series[right_idx[0]].get("unit", "") or "") if right_idx else ""
    parts.append(
        f'<text x="{plot_x}" y="{plot_y - 10}" text-anchor="start" class="axis">'
        f'{_esc(left_unit)}</text>'
    )
    parts.append(
        f'<text x="{plot_x + plot_w}" y="{plot_y - 10}" text-anchor="end" class="axis">'
        f'{_esc(right_unit)}</text>'
    )

    if lo is not None and hi is not None:
        x1 = px(max(0.0, lo))
        x2 = px(min(xmax, hi))
        parts.append(
            f'<rect x="{x1:.2f}" y="{plot_y}" width="{x2 - x1:.2f}" height="{plot_h}" '
            f'fill="rgba(67, 99, 216, 0.14)"/>'
        )
        for xx in (x1, x2):
            parts.append(
                f'<line x1="{xx:.2f}" y1="{plot_y}" x2="{xx:.2f}" '
                f'y2="{plot_y + plot_h}" stroke="rgba(67, 99, 216, 0.85)" '
                f'stroke-dasharray="4,3"/>'
            )

    if plo is not None and phi is not None:
        gx1 = px(max(0.0, plo))
        gx2 = px(min(xmax, phi))
        parts.append(
            f'<rect x="{gx1:.2f}" y="{plot_y}" width="{gx2 - gx1:.2f}" height="{plot_h}" '
            f'fill="rgba(60, 180, 75, 0.12)"/>'
        )
        for xx in (gx1, gx2):
            parts.append(
                f'<line x1="{xx:.2f}" y1="{plot_y}" x2="{xx:.2f}" '
                f'y2="{plot_y + plot_h}" stroke="rgba(60, 180, 75, 0.9)" '
                f'stroke-dasharray="4,3"/>'
            )

    for i, s in enumerate(series):
        color = _COLORS[i % len(_COLORS)]
        ylo, yhi = y_right if i in right_idx else y_left
        parts.append(
            f'<path d="{_path(_pts(s), xmax, ylo, yhi)}" fill="none" '
            f'stroke="{color}" stroke-width="1.5"/>'
        )

    for m in markers:
        label = str(m.get("label", ""))
        t = float(cast(Any, m.get("t", 0.0)))
        gx = px(t)
        if gx < plot_x or gx > plot_x + plot_w:
            continue
        parts.append(
            f'<line x1="{gx:.2f}" y1="{plot_y}" x2="{gx:.2f}" y2="{plot_y + plot_h}" '
            f'stroke="#333333" stroke-dasharray="3,3" stroke-opacity="0.45"/>'
        )
        cx, cy = gx, plot_y + 11
        parts.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="9" fill="#333333"/>')
        parts.append(
            f'<text x="{cx:.2f}" y="{cy + 0.5:.2f}" text-anchor="middle" '
            f'dominant-baseline="middle" class="marker">{_esc(label)}</text>'
        )

    parts.append("</svg>")

    legend_items = "".join(
        f'<span class="lg"><span class="swatch" '
        f'style="background:{_COLORS[i % len(_COLORS)]}"></span>'
        f'{_esc(str(s.get("name", "")))} ({_esc(str(s.get("unit", "")))})</span>'
        for i, s in enumerate(series)
    )
    return f'<div class="svg-wrap">{"".join(parts)}</div><div class="legend">{legend_items}</div>'


def _render_region(series: list[Series], lo: float, hi: float) -> str:
    rows = ""
    for i, s in enumerate(series):
        st = _region_stats(_pts(s), lo, hi)
        if st is None:
            continue
        color = _COLORS[i % len(_COLORS)]
        name = str(s.get("name", ""))
        unit = str(s.get("unit", "") or "")
        cells = "".join(
            f"<td>{_fmt(st[k])}</td>" for k in ("min", "mean", "max", "rms", "pp")
        )
        rows += (
            f'<tr><td style="color:{color}">{_esc(name)}</td><td>{_esc(unit)}</td>'
            f"{cells}</tr>"
        )

    left_idx, right_idx = _series_axis(series)
    summary = ""
    if right_idx:
        amps = _pts(series[right_idx[0]])
        ah = _trapezoid(amps, lo, hi) / 3600.0
        bits = [f"{_fmt(ah)} Ah"]
        if left_idx:
            volts = _pts(series[left_idx[0]])
            wh = _trapezoid(_power_points(volts, amps), lo, hi) / 3600.0
            bits.append(f"{_fmt(wh)} Wh")
            pw = _power_region_stats(volts, amps, lo, hi)
            if pw is not None:
                bits.append(f"{_fmt(pw[0])} W avg")
                bits.append(f"{_fmt(pw[1])} W pk")
        summary = "∫ " + "  ·  ".join(bits)

    header = "".join(f"<th>{c}</th>" for c in ["Channel", "Unit", *_STAT_COLS])
    return (
        f'<section class="region"><h2>Selected region '
        f'{_fmt_time(lo)} → {_fmt_time(hi)} (Δ {_fmt_time(hi - lo)})</h2>'
        f'<p class="summary">{summary}</p>'
        f'<table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table></section>'
    )


def _index_of_name(series: list[Series], name: object) -> int:
    if not name:
        return -1
    wanted = str(name)
    for i, s in enumerate(series):
        if str(s.get("name", "")) == wanted:
            return i
    return -1


def _region_points(points: Points, lo: float, hi: float) -> Points:
    return [
        [t, v]
        for t, v in points
        if t is not None and v is not None and lo <= t <= hi
    ]


def _mean(pts: Points) -> float | None:
    vals = [v for _, v in pts if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _rail_stats(
    series: list[Series], v_idx: int, i_idx: int, lo: float, hi: float
) -> dict[str, Any] | None:
    if i_idx < 0 or i_idx >= len(series):
        return None
    amps = _pts(series[i_idx])
    i_stats = _region_stats(amps, lo, hi)
    if i_stats is None:
        return None
    out: dict[str, Any] = {
        "i": i_stats,
        "v": None,
        "p": None,
        "ah": _trapezoid(amps, lo, hi) / 3600.0,
        "wh": None,
    }
    if 0 <= v_idx < len(series):
        volts = _pts(series[v_idx])
        out["v"] = _region_stats(volts, lo, hi)
        out["wh"] = _trapezoid(_power_points(volts, amps), lo, hi) / 3600.0
        out["p"] = _power_region_stats(volts, amps, lo, hi)
    return out


def _battery_bits(
    series: list[Series],
    rail: dict[str, Any],
    lo: float,
    hi: float,
    capacity: float | None,
) -> str | None:
    s = _rail_stats(
        series,
        _index_of_name(series, rail.get("v")),
        _index_of_name(series, rail.get("i")),
        lo,
        hi,
    )
    if s is None:
        return None
    i_stats = s["i"]
    bits = [f"∫ {_fmt_signed(s['ah'])} Ah"]
    if s["wh"] is not None:
        bits.append(f"{_fmt_signed(s['wh'])} Wh")
    bits.append(f"{_fmt(i_stats['mean'])} A avg · {_fmt(i_stats['max'])} A pk")
    if s["v"] is not None:
        bits.append(f"{_fmt(s['v']['mean'])} V avg (min {_fmt(s['v']['min'])})")
    if s["p"] is not None:
        bits.append(f"{_fmt(s['p'][0])} W avg · {_fmt(s['p'][1])} W pk")
    if capacity is not None and i_stats["mean"] > 1e-9:
        bits.append(f"~{_fmt(capacity / i_stats['mean'])} h @ {_fmt(capacity)} Ah")
    return "  ·  ".join(bits)


def _dcdc_bits(
    series: list[Series], rails: list[dict[str, Any]], lo: float, hi: float
) -> str | None:
    if len(rails) < 2:
        return None
    in_s = _rail_stats(
        series,
        _index_of_name(series, rails[0].get("v")),
        _index_of_name(series, rails[0].get("i")),
        lo,
        hi,
    )
    out_s = _rail_stats(
        series,
        _index_of_name(series, rails[1].get("v")),
        _index_of_name(series, rails[1].get("i")),
        lo,
        hi,
    )
    if (
        in_s is None
        or out_s is None
        or in_s["v"] is None
        or out_s["v"] is None
        or in_s["p"] is None
        or out_s["p"] is None
    ):
        return None
    pin = in_s["p"][0]
    pout = out_s["p"][0]
    bits = [
        f"Vin {_fmt(in_s['v']['mean'])} V · Iin {_fmt(in_s['i']['mean'])} A "
        f"· Pin {_fmt(pin)} W",
        f"Vout {_fmt(out_s['v']['mean'])} V · Iout {_fmt(out_s['i']['mean'])} A "
        f"· Pout {_fmt(pout)} W",
    ]
    if abs(pin) > 1e-9:
        bits.append(f"η {_fmt((pout / pin) * 100)} %")
    bits.append(f"∫ in {_fmt_signed(in_s['wh'])} Wh · out {_fmt_signed(out_s['wh'])} Wh")
    return "  ·  ".join(bits)


def _sleep_bits(
    series: list[Series],
    rail: dict[str, Any],
    lo: float,
    hi: float,
    threshold: float | None,
) -> str | None:
    i_idx = _index_of_name(series, rail.get("i"))
    if i_idx < 0:
        return None
    amps = _pts(series[i_idx])
    i_stats = _region_stats(amps, lo, hi)
    if i_stats is None:
        return None
    thr = threshold if threshold is not None else (i_stats["min"] + i_stats["max"]) / 2.0
    active_n = 0
    sleep_n = 0
    active_sum = 0.0
    sleep_sum = 0.0
    for t, v in amps:
        if t is None or v is None or t < lo or t > hi:
            continue
        if v > thr:
            active_n += 1
            active_sum += v
        else:
            sleep_n += 1
            sleep_sum += v
    total = active_n + sleep_n
    if total == 0:
        return None
    duty = active_n / total * 100.0
    active_avg = active_sum / active_n if active_n else 0.0
    sleep_avg = sleep_sum / sleep_n if sleep_n else 0.0
    return "  ·  ".join(
        [
            f"active {_fmt(duty)} % · sleep {_fmt(100.0 - duty)} %",
            f"I active {_fmt(active_avg)} A · sleep {_fmt(sleep_avg)} A",
            f"∫ {_fmt_signed(_trapezoid(amps, lo, hi) / 3600.0)} Ah",
        ]
    )


def _load_step_stats(
    series: list[Series], v_idx: int, i_idx: int, lo: float, hi: float
) -> dict[str, Any] | None:
    volts = _region_points(_pts(series[v_idx]), lo, hi)
    amps = _region_points(_pts(series[i_idx]), lo, hi)
    n = min(len(volts), len(amps))
    if n < 3:
        return None
    head = max(1, int(n * 0.15))
    v0 = _mean(volts[:head])
    v1 = _mean(volts[-head:])
    i0 = _mean(amps[:head])
    i1 = _mean(amps[-head:])
    if v0 is None or v1 is None or i0 is None or i1 is None:
        return None
    dv = v1 - v0
    di = i1 - i0
    r = -dv / di if abs(di) > 1e-9 else None
    return {"v0": v0, "v1": v1, "i0": i0, "i1": i1, "dv": dv, "di": di, "r": r}


def _load_step_bits(
    series: list[Series], rail: dict[str, Any], lo: float, hi: float
) -> str | None:
    v_idx = _index_of_name(series, rail.get("v"))
    i_idx = _index_of_name(series, rail.get("i"))
    if v_idx < 0 or i_idx < 0:
        return None
    s = _load_step_stats(series, v_idx, i_idx, lo, hi)
    if s is None:
        return None
    bits = [f"ΔV {_fmt(s['dv'])} V · ΔI {_fmt(s['di'])} A"]
    if s["r"] is not None:
        bits.append(f"R {_fmt(s['r'])} Ω")
    bits.append(
        f"V {_fmt(s['v0'])}→{_fmt(s['v1'])} V · I {_fmt(s['i0'])}→{_fmt(s['i1'])} A"
    )
    return "  ·  ".join(bits)


def _render_power(series: list[Series], power: dict[str, Any], duration: float) -> str:
    mode = str(power.get("mode", "battery"))
    rails = cast(list[dict[str, Any]], power.get("rails") or [])
    capacity = cast(float | None, power.get("capacity_ah"))
    threshold = cast(float | None, power.get("threshold"))

    raw_lo = power.get("lo")
    raw_hi = power.get("hi")
    region = raw_lo is not None and raw_hi is not None
    if region:
        a = float(cast(Any, raw_lo))
        b = float(cast(Any, raw_hi))
        lo, hi = min(a, b), max(a, b)
    else:
        lo, hi = 0.0, duration

    if not rails:
        return ""
    rail = rails[0]
    if mode == "battery":
        bits = _battery_bits(series, rail, lo, hi, capacity)
    elif mode == "dc-dc":
        bits = _dcdc_bits(series, rails, lo, hi)
    elif mode == "sleep":
        bits = _sleep_bits(series, rail, lo, hi, threshold)
    elif mode == "load-step":
        bits = _load_step_bits(series, rail, lo, hi)
    else:
        return ""
    if not bits:
        return ""

    label = _MODE_LABELS.get(mode, mode)
    scope = f"region {_fmt_time(lo)} → {_fmt_time(hi)}" if region else f"full {_fmt_time(duration)}"
    return (
        f'<section class="power"><h2>Power analysis — {_esc(label)}</h2>'
        f'<p class="summary">{_esc(scope)}  ·  {bits}</p></section>'
    )


def _bounds_text(r: dict[str, Any], unit: str) -> str:
    lo = r.get("min")
    hi = r.get("max")
    if lo is not None and hi is not None:
        return f" (limit {_fmt(lo)} … {_fmt(hi)}{unit})"
    if lo is not None:
        return f" (min {_fmt(lo)}{unit})"
    if hi is not None:
        return f" (max {_fmt(hi)}{unit})"
    return ""


def _render_assertions(results: list[dict[str, Any]]) -> str:
    if not results:
        return ""
    passed = sum(1 for r in results if r.get("pass"))
    rows = ""
    for r in results:
        name = str(r.get("name", ""))
        unit = (" " + str(r.get("unit"))) if r.get("unit") else ""
        ok = bool(r.get("pass"))
        mark = "✓" if ok else "✗"
        cls = "pass" if ok else "fail"
        if not r.get("found"):
            text = "no matching channel"
        else:
            amin = cast(float, r.get("actual_min"))
            amax = cast(float, r.get("actual_max"))
            actual = f"{_fmt(amin)} … {_fmt(amax)}{unit}"
            if ok:
                text = f"{actual}{_bounds_text(r, unit)}"
            else:
                reasons = []
                if r.get("min") is not None and amin < r["min"]:
                    reasons.append(f"min {_fmt(amin)}{unit} below {_fmt(r['min'])}{unit}")
                if r.get("max") is not None and amax > r["max"]:
                    reasons.append(f"max {_fmt(amax)}{unit} above {_fmt(r['max'])}{unit}")
                text = f"{actual}; {'; '.join(reasons)}"
        rows += (
            f'<div class="check {cls}"><span class="mark">{mark}</span>'
            f'<span class="cname">{_esc(name)}</span>'
            f'<span class="ctext">{_esc(text)}</span></div>'
        )
    return (
        f'<section class="assertions"><h2>Checks ({passed}/{len(results)} passed)</h2>'
        f'{rows}</section>'
    )


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _css() -> str:
    return """
:root { color-scheme: light dark; }
body {
  font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 960px;
  padding: 0 1rem; color: #1a1a1a; background: #ffffff;
}
h1 { margin: 0 0 0.25rem; font-size: 1.4rem; }
header .meta { color: #666; margin: 0 0 1rem; }
.svg-wrap svg { width: 100%; height: auto; }
.legend { display: flex; flex-wrap: wrap; gap: 0.5rem 1.25rem; margin: 0.5rem 0 1rem; }
.lg { display: inline-flex; align-items: center; gap: 0.4rem; font-size: 0.9rem; }
.swatch { display: inline-block; width: 0.85rem; height: 0.85rem; border-radius: 2px; }
.tick { font: 10px system-ui, sans-serif; fill: #666; }
.axis { font: 11px system-ui, sans-serif; fill: #333; }
.marker { font: bold 11px system-ui, sans-serif; fill: #ffffff; }
section { margin: 1.25rem 0; }
.notes h2, .region h2 { font-size: 1.05rem; margin: 0 0 0.5rem; }
.note { display: flex; align-items: baseline; gap: 0.6rem; padding: 0.25rem 0; }
.badge {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 1.4rem; height: 1.4rem; padding: 0 0.2rem; border-radius: 4px;
  background: #333; color: #fff; font-weight: 600; font-size: 0.85rem;
}
.ntime { color: #888; font-size: 0.85rem; font-variant-numeric: tabular-nums; }
.ntext { white-space: pre-wrap; }
.summary { color: #555; font-variant-numeric: tabular-nums; }
.assertions h2 { font-size: 1.05rem; margin: 0 0 0.5rem; }
.check { display: flex; align-items: baseline; gap: 0.6rem; padding: 0.2rem 0; }
.mark { font-weight: 700; width: 1.1rem; }
.check.pass .mark { color: #2e7d32; }
.check.fail .mark { color: #b3261e; }
.cname { font-weight: 600; }
.ctext { color: #555; font-variant-numeric: tabular-nums; }
table { border-collapse: collapse; font-size: 0.9rem; }
th, td { border: 1px solid #ddd; padding: 0.25rem 0.6rem; text-align: right; }
th:nth-child(-n+2), td:nth-child(-n+2) { text-align: left; }
td { font-variant-numeric: tabular-nums; }
@media (prefers-color-scheme: dark) {
  body { background: #121212; color: #e6e6e6; }
  .meta { color: #aaa; }
  .tick { fill: #999; }
  .axis { fill: #ccc; }
  th, td { border-color: #333; }
  .ctext { color: #aaa; }
}
"""
