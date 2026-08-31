"""Polyline geometry for the scene engine.

Everything the renderer draws is ultimately a polyline (a stroke): smooth
curves are sampled Catmull-Rom splines, circles/ellipses are sampled arcs, and
the hand-drawn look comes from `roughen` — deterministic low-frequency wobble
plus fine jitter applied along a path. Arc-length tables make progressive
reveal exact: "reveal 43% of this stroke" cuts at 43% of its *length*, not 43%
of its points, so the pen moves at constant speed regardless of point spacing.

All functions are pure and seeded — same inputs, same output, every render.
"""

from __future__ import annotations

import math
import random
from typing import Sequence

Point = tuple[float, float]


# ── easing ────────────────────────────────────────────────────────────────────

def ease(name: str, t: float) -> float:
    """Easing value for t in [0,1]. Unknown names fall back to linear."""
    t = min(1.0, max(0.0, t))
    if name == "ease_in_out":
        return t * t * (3.0 - 2.0 * t)  # smoothstep
    if name == "ease_out":
        return 1.0 - (1.0 - t) ** 2
    if name == "ease_in":
        return t * t
    return t


# ── basic ops ────────────────────────────────────────────────────────────────

def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_pt(a: Point, b: Point, t: float) -> Point:
    return (lerp(a[0], b[0], t), lerp(a[1], b[1], t))


def dist(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def path_length(pts: Sequence[Point]) -> float:
    return sum(dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def bbox(pts: Sequence[Point]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


# ── splines & resampling ─────────────────────────────────────────────────────

def catmull_rom(pts: Sequence[Point], samples_per_seg: int = 12, closed: bool = False) -> list[Point]:
    """Sample a Catmull-Rom spline through `pts` (centripetal-ish, alpha=0.5
    approximated with uniform parameterization — fine at drawing scale)."""
    if len(pts) < 3:
        return list(pts)
    p = list(pts)
    if closed:
        p = [p[-1]] + p + [p[0], p[1]]
    else:
        p = [p[0]] + p + [p[-1]]
    out: list[Point] = []
    for i in range(1, len(p) - 2):
        p0, p1, p2, p3 = p[i - 1], p[i], p[i + 1], p[i + 2]
        for s in range(samples_per_seg):
            t = s / samples_per_seg
            t2, t3 = t * t, t * t * t
            x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t
                       + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                       + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t
                       + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                       + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            out.append((x, y))
    out.append(p[-2])
    return out


def resample(pts: Sequence[Point], spacing: float) -> list[Point]:
    """Resample a polyline at (approximately) uniform arc-length spacing."""
    if len(pts) < 2 or spacing <= 0:
        return list(pts)
    out = [pts[0]]
    carry = 0.0
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        seg = dist(a, b)
        if seg <= 1e-9:
            continue
        pos = spacing - carry
        while pos <= seg:
            out.append(lerp_pt(a, b, pos / seg))
            pos += spacing
        carry = seg - (pos - spacing)
    if out[-1] != tuple(pts[-1]):
        out.append(tuple(pts[-1]))
    return out


# ── arc-length reveal ────────────────────────────────────────────────────────

def cut_at_fraction(pts: Sequence[Point], frac: float) -> list[Point]:
    """The prefix of a polyline up to `frac` of its total arc length.

    This is what makes reveal look *drawn*: the endpoint of the returned prefix
    is exactly where the pen tip is."""
    frac = min(1.0, max(0.0, frac))
    if frac >= 1.0 or len(pts) < 2:
        return list(pts)
    if frac <= 0.0:
        return [tuple(pts[0])]
    target = path_length(pts) * frac
    out = [tuple(pts[0])]
    walked = 0.0
    for i in range(len(pts) - 1):
        seg = dist(pts[i], pts[i + 1])
        if walked + seg >= target:
            remain = target - walked
            out.append(lerp_pt(pts[i], pts[i + 1], remain / seg if seg > 0 else 0.0))
            return out
        walked += seg
        out.append(tuple(pts[i + 1]))
    return out


# ── hand-drawn roughening ────────────────────────────────────────────────────

def roughen(pts: Sequence[Point], amplitude: float = 1.6, wobble: float = 3.5,
            seed: int = 0) -> list[Point]:
    """Deterministic hand wobble: a low-frequency sinusoidal drift (the wrist)
    plus fine per-point jitter (the ink), both perpendicular to the path.
    Endpoints are pinned so joints between strokes stay closed."""
    if len(pts) < 3:
        return list(pts)
    rng = random.Random(seed)
    ph1, ph2 = rng.uniform(0, math.tau), rng.uniform(0, math.tau)
    f1, f2 = rng.uniform(1.5, 2.5), rng.uniform(4.0, 6.0)
    total = path_length(pts) or 1.0
    out: list[Point] = []
    walked = 0.0
    for i, p in enumerate(pts):
        if i > 0:
            walked += dist(pts[i - 1], p)
        u = walked / total
        # perpendicular direction from the local tangent
        a = pts[max(0, i - 1)]
        b = pts[min(len(pts) - 1, i + 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / n, dx / n
        # pin endpoints (fade the offset in/out over the first/last 8%)
        pin = min(1.0, u / 0.08, (1.0 - u) / 0.08) if 0 < u < 1 else 0.0
        off = (wobble * math.sin(math.tau * f1 * u + ph1)
               + wobble * 0.4 * math.sin(math.tau * f2 * u + ph2)
               + rng.uniform(-amplitude, amplitude)) * pin
        out.append((p[0] + nx * off, p[1] + ny * off))
    return out


# ── shape generators (all return sampled polylines) ──────────────────────────

def ellipse_path(cx: float, cy: float, rx: float, ry: float, *, start_deg: float = -60.0,
                 overshoot_deg: float = 18.0, steps: int = 72, seed: int = 0,
                 rough: float = 2.0) -> list[Point]:
    """A hand-drawn ellipse: starts at an off-axis angle, overshoots slightly
    past closure (the way a circled word overlaps itself), with radius noise.
    Degenerate radii are floored so a zero-size target still yields a mark."""
    rx, ry = max(rx, 3.0), max(ry, 3.0)
    rng = random.Random(seed)
    ph = rng.uniform(0, math.tau)
    pts: list[Point] = []
    sweep = 360.0 + overshoot_deg
    for i in range(steps + 1):
        a = math.radians(start_deg + sweep * i / steps)
        wob = 1.0 + (rough / max(rx, ry)) * math.sin(3 * a + ph) if rough else 1.0
        pts.append((cx + rx * wob * math.cos(a), cy + ry * wob * math.sin(a)))
    return pts


def arc_path(cx: float, cy: float, rx: float, ry: float, a0_deg: float, a1_deg: float,
             steps: int = 48) -> list[Point]:
    pts: list[Point] = []
    for i in range(steps + 1):
        a = math.radians(lerp(a0_deg, a1_deg, i / steps))
        pts.append((cx + rx * math.cos(a), cy + ry * math.sin(a)))
    return pts


def blob_path(cx: float, cy: float, r: float, *, irregularity: float = 0.18,
              lobes: int = 7, steps: int = 90, seed: int = 0) -> list[Point]:
    """An organic closed blob (cytoplasm boundary, nucleus, organelles):
    an ellipse-ish base radius modulated by a few random low-order harmonics."""
    rng = random.Random(seed)
    amps = [rng.uniform(-irregularity, irregularity) * r for _ in range(lobes)]
    phs = [rng.uniform(0, math.tau) for _ in range(lobes)]
    pts: list[Point] = []
    for i in range(steps + 1):
        a = math.tau * i / steps
        rr = r + sum(amp * math.sin((k + 2) * a + ph) for k, (amp, ph) in enumerate(zip(amps, phs)))
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    pts[-1] = pts[0]
    return pts


def arrow_paths(tail: Point, head: Point, *, curve: float = 0.0, head_len: float = 16.0,
                seed: int = 0) -> list[list[Point]]:
    """An arrow as polylines: shaft (optionally curved via a control point
    offset perpendicular to the line) + two head barbs. Returned in draw order."""
    if math.hypot(head[0] - tail[0], head[1] - tail[1]) < 1e-6:
        head = (tail[0] + 1.0, tail[1])  # zero-length arrow degrades to a tick
    dx, dy = head[0] - tail[0], head[1] - tail[1]
    n = math.hypot(dx, dy) or 1.0
    ux, uy = dx / n, dy / n
    px, py = -uy, ux
    if abs(curve) > 1e-6:
        mid = (lerp(tail[0], head[0], 0.5) + px * curve, lerp(tail[1], head[1], 0.5) + py * curve)
        shaft = catmull_rom([tail, mid, head], samples_per_seg=16)
    else:
        shaft = [tail, head]
    shaft = roughen(resample(shaft, 6.0), amplitude=0.8, wobble=1.2, seed=seed)
    if len(shaft) < 2:
        shaft = [tail, head]
    # barbs angle back from the head along the *final* shaft direction
    fx, fy = shaft[-1][0] - shaft[-2][0], shaft[-1][1] - shaft[-2][1]
    fn = math.hypot(fx, fy) or 1.0
    fx, fy = fx / fn, fy / fn
    bpx, bpy = -fy, fx
    b1 = (head[0] - fx * head_len + bpx * head_len * 0.55,
          head[1] - fy * head_len + bpy * head_len * 0.55)
    b2 = (head[0] - fx * head_len - bpx * head_len * 0.55,
          head[1] - fy * head_len - bpy * head_len * 0.55)
    return [shaft, [b1, head], [b2, head]]


def underline_path(x0: float, x1: float, y: float, *, seed: int = 0) -> list[Point]:
    base = [(x0, y), ((x0 + x1) / 2, y), (x1, y)]
    return roughen(resample(catmull_rom(base), 5.0), amplitude=1.0, wobble=2.2, seed=seed)
