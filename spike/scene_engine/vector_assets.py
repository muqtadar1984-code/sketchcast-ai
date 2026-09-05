"""Authored layered vector illustrations — the deterministic asset tier.

An asset is an ordered list of LAYERS (cell wall, membrane, nucleus, ...),
each a list of strokes in draw order. That ordering is the whole point: the
renderer draws outline -> internal structure -> details, layer by layer,
stroke by stroke, so the diagram is CONSTRUCTED in front of the student —
never swept in with a mask (the prototype's §26 requirement).

These are also the guaranteed fallback tier: when the AI raster path is
unavailable (no key, network down, refused output), scenes referencing these
keys still render, and a lesson never fails because an asset did (§20).

Coordinates are in each asset's own viewbox; IllustrationElement.at/scale
places it in the world. Color roles ("ink"/"accent"/"muted") resolve to the
scene palette at render time so branding threads through like everywhere else.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .geometry import (Point, blob_path, catmull_rom, ellipse_path, path_length,
                       resample, roughen)


@dataclass(frozen=True)
class VStroke:
    pts: tuple[Point, ...]
    width: float = 3.0
    color: str = "ink"          # role: ink | accent | muted
    fill: str | None = None     # role to fill the closed path with once drawn


@dataclass(frozen=True)
class VLayer:
    id: str
    strokes: tuple[VStroke, ...]
    label_anchor: Point | None = None


@dataclass(frozen=True)
class VectorAsset:
    key: str
    w: float
    h: float
    layers: tuple[VLayer, ...]
    # a stand-in frame, not the diagram (see `placeholder_asset`). The renderer
    # reports it so the acceptance gate keeps counting the board as unresolved.
    placeholder: bool = False

    def layer_ids(self) -> list[str]:
        return [l.id for l in self.layers]

    def subset(self, ids: list[str] | None) -> tuple[VLayer, ...]:
        """Layers matching `ids` — via the ONE shared matcher every consumer
        uses (draw distribution, carried-state reveal, workloads). Divergent
        matchers once made a draw reveal nothing while the carry showed it."""
        if not ids or self.placeholder:
            # A placeholder has ONE layer, named "frame", which is not the
            # nucleus or the cilia the scene asked to draw. Matched normally
            # it answers nothing, so the frame that exists to show the board
            # is missing was itself invisible on every scene whose draw names
            # layers -- and the director writes those for exactly the detailed
            # diagrams most likely to be the ones that failed. It has no parts
            # to distinguish, so it answers to all of them.
            return self.layers
        matched = set(match_layer_ids([l.id for l in self.layers], ids))
        return tuple(l for l in self.layers if l.id in matched)

    def ink_length(self, ids: list[str] | None = None) -> float:
        """Total stroke arc length — the draw-duration workload hint."""
        return sum(path_length(s.pts) for l in self.subset(ids) for s in l.strokes)


def match_layer_ids(available: list[str], want: list[str]) -> list[str]:
    """THE layer matcher — used by asset subsetting, carried-state reveal and
    draw distribution alike, so 'wall' means the same strokes everywhere.
    Exact (case-insensitive) wins outright; only when nothing matches exactly
    does substring containment apply — which keeps 'membrane' from bleeding
    into 'nucleus_membrane' when a literal 'membrane' layer exists."""
    wl = [w.lower() for w in want]
    by_lower = {a.lower(): a for a in available}
    exact = [by_lower[w] for w in wl if w in by_lower]
    if exact:
        return exact
    return [a for a in available
            if any(w in a.lower() or a.lower() in w for w in wl)]


# ── path helpers ─────────────────────────────────────────────────────────────

def _rot(pts: list[Point], cx: float, cy: float, deg: float) -> list[Point]:
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    return [(cx + (x - cx) * ca - (y - cy) * sa, cy + (x - cx) * sa + (y - cy) * ca)
            for x, y in pts]


def rrect_path(x0: float, y0: float, x1: float, y1: float, r: float,
               seed: int = 0, rough: float = 2.2) -> list[Point]:
    """A hand-drawn rounded rectangle (the classic plant-cell silhouette)."""
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    pts: list[Point] = []

    def arc(cx: float, cy: float, a0: float, a1: float) -> None:
        steps = 14
        for i in range(steps + 1):
            a = math.radians(a0 + (a1 - a0) * i / steps)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))

    pts.append((x0 + r, y0))
    pts.append((x1 - r, y0)); arc(x1 - r, y0 + r, -90, 0)
    pts.append((x1, y1 - r)); arc(x1 - r, y1 - r, 0, 90)
    pts.append((x0 + r, y1)); arc(x0 + r, y1 - r, 90, 180)
    pts.append((x0, y0 + r)); arc(x0 + r, y0 + r, 180, 270)
    dense = resample(pts, 7.0)
    return roughen(dense, amplitude=1.2, wobble=rough, seed=seed)


def _wavy_hline(x0: float, x1: float, y: float, seed: int, amp: float = 2.5) -> list[Point]:
    base = resample([(x0, y), (x1, y)], 8.0)
    return roughen(base, amplitude=1.0, wobble=amp, seed=seed)


def _smooth_blob(cx: float, cy: float, r: float, seed: int, irr: float = 0.1) -> list[Point]:
    return resample(blob_path(cx, cy, r, irregularity=irr, seed=seed), 6.0)


def _org_ellipse(cx: float, cy: float, rx: float, ry: float, deg: float,
                 seed: int) -> list[Point]:
    pts = ellipse_path(cx, cy, rx, ry, overshoot_deg=10.0, seed=seed, rough=1.5)
    return _rot(resample(pts, 5.0), cx, cy, deg)


# ── plant cell (scene 1: construction) ───────────────────────────────────────

def _plant_cell() -> VectorAsset:
    W, H = 800.0, 540.0
    layers: list[VLayer] = []

    # cell wall: double boundary — the rigid rectangular silhouette that makes
    # a plant cell read as a plant cell
    layers.append(VLayer("wall", (
        VStroke(tuple(rrect_path(22, 22, 778, 518, 92, seed=11)), 5.0),
        VStroke(tuple(rrect_path(40, 40, 760, 500, 80, seed=12)), 3.2),
    ), label_anchor=(120, 30)))

    # membrane: a thinner line just inside the wall
    layers.append(VLayer("membrane", (
        VStroke(tuple(rrect_path(58, 58, 742, 482, 68, seed=13, rough=3.0)), 2.2, "muted"),
    ), label_anchor=(150, 74)))

    # cytoplasm: sparse ribosome stipple — quick marks, not a texture
    dots: list[VStroke] = []
    for i, (dx, dy) in enumerate([(150, 250), (215, 120), (700, 260), (620, 470),
                                  (390, 470), (110, 400), (480, 105), (720, 400)]):
        dots.append(VStroke(tuple(ellipse_path(dx, dy, 3.5, 3.5, steps=12, seed=40 + i)), 2.0, "muted"))
    layers.append(VLayer("cytoplasm", tuple(dots), label_anchor=(480, 120)))

    # central vacuole: the large organelle that dominates a plant cell
    layers.append(VLayer("vacuole", (
        VStroke(tuple(_smooth_blob(330, 280, 152, seed=21, irr=0.12)), 3.0),
    ), label_anchor=(330, 280)))

    # nucleus: envelope + nucleolus + chromatin marks
    layers.append(VLayer("nucleus", (
        VStroke(tuple(_smooth_blob(600, 175, 64, seed=31, irr=0.06)), 3.2),
        VStroke(tuple(_smooth_blob(586, 168, 20, seed=32, irr=0.12)), 2.4, "ink", fill="accent_mist"),
        VStroke(tuple(catmull_rom([(618, 150), (634, 162), (626, 178)])), 2.0, "muted"),
        VStroke(tuple(catmull_rom([(590, 205), (606, 198), (616, 208)])), 2.0, "muted"),
    ), label_anchor=(600, 175)))

    # chloroplasts: lens shapes with internal grana lines — the accent organelle
    chloro: list[VStroke] = []
    for j, (cx, cy, deg) in enumerate([(545, 408, 14), (665, 330, -22), (172, 152, 8)]):
        s = 51 + j * 3
        chloro.append(VStroke(tuple(_org_ellipse(cx, cy, 42, 22, deg, seed=s)), 3.0, "accent"))
        for k in (-7, 3, 12):
            g = _rot([(cx - 24, cy + k), (cx + 24, cy + k)], cx, cy, deg)
            chloro.append(VStroke(tuple(resample(g, 5.0)), 2.0, "accent"))
    layers.append(VLayer("chloroplasts", tuple(chloro), label_anchor=(545, 408)))

    # mitochondria: small ellipses with a folded cristae line
    mito: list[VStroke] = []
    for j, (cx, cy, deg) in enumerate([(205, 432, -12), (688, 118, 28)]):
        mito.append(VStroke(tuple(_org_ellipse(cx, cy, 31, 16, deg, seed=71 + j)), 2.6, "muted"))
        wave = [(cx - 20 + i * 8, cy + (5 if i % 2 else -5)) for i in range(6)]
        mito.append(VStroke(tuple(resample(_rot(catmull_rom(wave), cx, cy, deg), 4.0)), 1.8, "muted"))
    layers.append(VLayer("mitochondria", tuple(mito), label_anchor=(205, 432)))

    return VectorAsset("plant_cell", W, H, tuple(layers))


# ── membrane cross-section (scene 2: process) ────────────────────────────────

def _membrane_section() -> VectorAsset:
    W, H = 1000.0, 560.0
    top, bot = 250.0, 318.0
    gap0, gap1 = 425.0, 575.0
    layers: list[VLayer] = []

    # the bilayer: two parallel lines with a gap where the channel sits,
    # plus end caps so it reads as a slab of membrane, not two stray lines
    layers.append(VLayer("bilayer", (
        VStroke(tuple(_wavy_hline(45, gap0, top, seed=81)), 4.0),
        VStroke(tuple(_wavy_hline(gap1, 955, top, seed=82)), 4.0),
        VStroke(tuple(_wavy_hline(45, gap0, bot, seed=83)), 4.0),
        VStroke(tuple(_wavy_hline(gap1, 955, bot, seed=84)), 4.0),
        VStroke(((45.0, top), (45.0, bot)), 3.0, "muted"),
        VStroke(((955.0, top), (955.0, bot)), 3.0, "muted"),
    ), label_anchor=(150, (top + bot) / 2)))

    # the channel protein: two rounded pillars framing an open pore
    def pillar(x0: float, x1: float, seed: int) -> list[Point]:
        return rrect_path(x0, top - 22, x1, bot + 22, 24, seed=seed, rough=1.8)

    layers.append(VLayer("channel", (
        VStroke(tuple(pillar(gap0, gap0 + 58, 91)), 3.4, "accent"),
        VStroke(tuple(pillar(gap1 - 58, gap1, 92)), 3.4, "accent"),
    ), label_anchor=(500, (top + bot) / 2)))

    return VectorAsset("membrane_section", W, H, tuple(layers))


# ── the placeholder tier ─────────────────────────────────────────────────────

def placeholder_asset(key: str) -> VectorAsset:
    """A dashed frame standing where a picture should have been.

    Dropping the element entirely left `b.box` a ZERO-SIZE point at the
    element's nominal centre (render.py), so every label and leader anchored to
    the missing diagram was laid out around one pixel -- a fan of arrows
    converging on nothing, which reads as a rendering bug rather than a missing
    picture. A real box gives them somewhere honest to point.

    Deliberately NOT in `_BUILDERS`: `vector_asset("volcano")` must still be
    None, so this can only be reached through the resolver's last rung, and
    only for a key that HAD a prompt.
    """
    W, H = 800.0, 540.0
    # a dashed border: short arcs of the rounded rect, drawn as separate
    # strokes, so it never reads as a real drawn outline
    ring = rrect_path(30, 30, W - 30, H - 30, 46, seed=7, rough=1.4)
    dashes: list[VStroke] = []
    step, gap = 9, 5
    for i in range(0, len(ring) - step, step + gap):
        seg = tuple(ring[i:i + step])
        if len(seg) >= 2:
            dashes.append(VStroke(seg, 2.4, "muted"))
    return VectorAsset(f"placeholder:{key}", W, H,
                       (VLayer("frame", tuple(dashes),
                               label_anchor=(W / 2, H / 2)),),
                       placeholder=True)


# ── registry ─────────────────────────────────────────────────────────────────

_BUILDERS = {
    "plant_cell": _plant_cell,
    "membrane_section": _membrane_section,
}
_CACHE: dict[str, VectorAsset] = {}


def vector_asset(key: str) -> VectorAsset | None:
    """The authored vector asset for `key`, or None (caller decides fallback)."""
    if key not in _CACHE:
        builder = _BUILDERS.get(key)
        if builder is None:
            return None
        _CACHE[key] = builder()
    return _CACHE[key]


def known_vector_assets() -> list[str]:
    return sorted(_BUILDERS)
