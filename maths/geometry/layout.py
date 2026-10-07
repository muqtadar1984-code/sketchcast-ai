"""From a realised model to drawing primitives, with labels placed.

Everything a renderer draws is one of four primitives in FIGURE UNITS
with y up: a Stroke (polyline), a Text, a Fill (a grid cell), a Dot. The
renderers (maths.geometry.render_static) only map units to millimetres
and flip y; nothing here knows about SVG or pixels.

What gets drawn comes from the facts and the spec: every segment and
visible line/ray, every circle and grid, an arc and label for every
measured angle, a length label for every measured segment, the spec's
marks, and a label for every named point. Labels are PLACED: each tries
a list of positions and takes the first whose box crosses no stroke and
no other label; a label with nowhere to go is ``layout_collision``. The
box is measured with the real font, so what is checked is what prints.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import sympy as sp
from PIL import ImageFont

from maths.geometry.constructions import exact_value
from maths.geometry.errors import GeometryRefusal
from maths.geometry.model import Line, Model, angle_key, seg_key
from maths.geometry.spec import FigureSpec

Vec = tuple[float, float]

_FONT_PATH = Path(__file__).resolve().parents[2] / "agent5_slides" / "fonts" / "DejaVuSans.ttf"
_MEASURE_FONT = ImageFont.truetype(str(_FONT_PATH), 100)

LINE_OVERHANG = 0.7        # a line is drawn past its last named point
RAY_OVERHANG = 0.5
ARC_RADIUS = 0.55          # angle arc, figure units
ARC_RADIUS_REFLEX = 0.45
TICK_LEN = 0.22
SQUARE_SIDE = 0.32
LABEL_PAD = 0.08


@dataclass
class Stroke:
    points: list[Vec]
    width: float = 1.0          # relative line width
    dashed: bool = False
    arrow: bool = False         # arrowhead at the end (parallel marks)
    role: str = "ink"           # ink | mark | hidden


@dataclass
class Text:
    x: float
    y: float
    text: str
    size: float                 # em height in figure units
    role: str = "label"         # label | point | note
    anchor: str = "middle"      # middle | start | end
    italic: bool = False
    # the placed box, figure units (x0, y0, x1, y1), filled by place()
    box: Optional[tuple[float, float, float, float]] = None


@dataclass
class Fill:
    points: list[Vec]
    colour: str                 # a palette name; the renderer maps it


@dataclass
class Dot:
    x: float
    y: float
    r: float = 0.06


@dataclass
class Drawing:
    strokes: list[Stroke] = field(default_factory=list)
    texts: list[Text] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    dots: list[Dot] = field(default_factory=list)
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    notes: list[str] = field(default_factory=list)


# ── text metrics ──────────────────────────────────────────────────────────

def text_box(x: float, y: float, text: str, size: float, anchor: str) -> tuple[float, float, float, float]:
    """The box a string occupies at (x, y) — y is the vertical centre —
    measured with the font the PNG uses. Figure units."""
    w = _MEASURE_FONT.getlength(text) / 100.0 * size
    h = 0.78 * size
    if anchor == "middle":
        x0 = x - w / 2
    elif anchor == "end":
        x0 = x - w
    else:
        x0 = x
    return (x0 - LABEL_PAD, y - h / 2 - LABEL_PAD, x0 + w + LABEL_PAD, y + h / 2 + LABEL_PAD)


def _boxes_overlap(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _seg_hits_box(p: Vec, q: Vec, box) -> bool:
    """Liang–Barsky: does the segment pq cross the box."""
    x0, y0, x1, y1 = box
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for pk, qk in ((-dx, p[0] - x0), (dx, x1 - p[0]), (-dy, p[1] - y0), (dy, y1 - p[1])):
        if abs(pk) < 1e-12:
            if qk < 0:
                return False
            continue
        t = qk / pk
        if pk < 0:
            if t > t1:
                return False
            t0 = max(t0, t)
        else:
            if t < t0:
                return False
            t1 = min(t1, t)
    return t0 <= t1


def _box_clear(box, strokes: list[Stroke], boxes: list) -> bool:
    for b in boxes:
        if _boxes_overlap(box, b):
            return False
    for s in strokes:
        if s.role == "hidden":
            continue
        for p, q in zip(s.points, s.points[1:]):
            if _seg_hits_box(p, q, box):
                return False
    return True


# ── geometry helpers ──────────────────────────────────────────────────────

def _unit(v: Vec) -> Vec:
    n = math.hypot(*v)
    return (v[0] / n, v[1] / n) if n > 1e-12 else (1.0, 0.0)


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1])


def _add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1])


def _mul(a: Vec, k: float) -> Vec:
    return (a[0] * k, a[1] * k)


def _arc_points(c: Vec, r: float, a0: float, a1: float, n: int = 24) -> list[Vec]:
    """Counter-clockwise from a0 to a1 (degrees), sampled."""
    while a1 < a0:
        a1 += 360.0
    out = []
    for i in range(n + 1):
        a = math.radians(a0 + (a1 - a0) * i / n)
        out.append((c[0] + r * math.cos(a), c[1] + r * math.sin(a)))
    return out


def _angle_geometry(m: Model, key) -> tuple[Vec, float, float, Vec]:
    """(vertex, start deg, end deg, bisector unit) of an angle's arc,
    counter-clockwise from start to end through the angle's region."""
    v, arms, region = key
    a, b = sorted(arms)
    vx, vy = m.xy(v)
    da = math.degrees(math.atan2(m.xy(a)[1] - vy, m.xy(a)[0] - vx))
    db = math.degrees(math.atan2(m.xy(b)[1] - vy, m.xy(b)[0] - vx))
    ccw = (db - da) % 360.0           # from a to b counter-clockwise
    if region == "interior":
        if ccw <= 180.0:
            start, sweep = da, ccw
        else:
            start, sweep = db, 360.0 - ccw
    else:
        if ccw <= 180.0:
            start, sweep = db, 360.0 - ccw
        else:
            start, sweep = da, ccw
    mid = math.radians(start + sweep / 2)
    return (vx, vy), start, start + sweep, (math.cos(mid), math.sin(mid))


def _fmt_value(value, unit: Optional[str], is_angle: bool) -> str:
    s = str(value).strip()
    e = exact_value(value, where="label")
    if not e.free_symbols:
        if e.is_Integer:
            s = str(int(e))
        else:
            f = float(e)
            s = f"{f:g}"
        return f"{s}°" if is_angle else (f"{s} {unit}" if unit else s)
    s = s.replace("*", "")
    return s if is_angle else (f"{s} {unit}" if unit else s)


# ── the drawing ───────────────────────────────────────────────────────────

def build_drawing(m: Model, spec: FigureSpec, *, show_hidden: bool = False, label_size: float = 0.42,
                  policy: str = "instructional_metric", note: Optional[str] = None) -> Drawing:
    d = Drawing()
    drawn_segments: set = set()

    # lines, rays
    for ln in m.lines.values():
        if ln.hidden and not show_hidden:
            continue
        if len(ln.points) < 2:
            continue
        p0, p1 = m.xy(ln.points[0]), m.xy(ln.points[-1])
        u = _unit(_sub(p1, p0))
        d.strokes.append(Stroke([_sub(p0, _mul(u, LINE_OVERHANG)), _add(p1, _mul(u, LINE_OVERHANG))],
                                dashed=ln.hidden, role="hidden" if ln.hidden else "ink"))
        for a, b in zip(ln.points, ln.points[1:]):
            drawn_segments.add(seg_key(a, b))
    for r in m.rays.values():
        p0, p1 = m.xy(r.vertex), m.xy(r.through)
        u = _unit(_sub(p1, p0))
        d.strokes.append(Stroke([p0, _add(p1, _mul(u, RAY_OVERHANG))]))
        drawn_segments.add(seg_key(r.vertex, r.through))
    # segments (polygon sides, radii, chords, plain segments)
    for key in m.segments:
        if key in drawn_segments:
            continue
        a, b = sorted(key)
        on_line = any(a in ln.points and b in ln.points for ln in m.lines.values() if ln.hidden and not show_hidden)
        if on_line:
            # a side of a shape that also carries a hidden line fact (parallel-side bookkeeping)
            pass
        d.strokes.append(Stroke([m.xy(a), m.xy(b)]))
        drawn_segments.add(key)
    # circles
    for c in m.circles.values():
        d.strokes.append(Stroke(_arc_points(m.xy(c.centre), c.radius, 0.0, 360.0, 72)))
        d.dots.append(Dot(*m.xy(c.centre)))
    # grids
    for g in m.grids.values():
        ox, oy = 0.0, 0.0
        if m.points:
            x0, y0, x1, y1 = m.bbox()
            ox = x1 + 1.0
        for i in range(g.rows):
            for j in range(g.cols):
                x, y = ox + j, oy + (g.rows - 1 - i)
                cell = g.cells[i][j]
                pts = [(x, y), (x + 1, y), (x + 1, y + 1), (x, y + 1)]
                if cell != g.blank:
                    d.fills.append(Fill(pts, cell))
                else:
                    d.texts.append(Text(x + 0.5, y + 0.5, "★", 0.5, role="note"))
                d.strokes.append(Stroke(pts + [pts[0]], width=0.8, role="mark"))

    boxes: list = []
    # angle marks and labels: every measured angle, plus explicit angle_arc marks
    labelled: set = set()
    arcs_drawn: set = set()
    for ms in spec.measures:
        if ms.target in m.angle_ids:
            key = m.angle_ids[ms.target]
            if ms.role == "derived":
                continue
            _angle_arc(d, m, key, arcs_drawn)
            text = _fmt_value(ms.value, None, True)
            _place_angle_label(d, m, key, text, label_size, boxes, ms.target)
            labelled.add(ms.target)
        elif ms.target in m.segment_ids:
            if ms.role == "derived":
                continue
            key = m.segment_ids[ms.target]
            text = _fmt_value(ms.value, ms.unit or m.units, False)
            _place_segment_label(d, m, key, text, label_size, boxes, ms.target)
    for mk in spec.marks:
        extra = mk.model_extra or {}
        kind = mk.kind
        if kind == "angle_arc":
            t = extra.get("target")
            if t not in m.angle_ids:
                raise GeometryRefusal("bad_reference", f"mark {mk.id or kind}: angle {t!r} is not defined")
            _angle_arc(d, m, m.angle_ids[t], arcs_drawn)
            if extra.get("label") and t not in labelled:
                _place_angle_label(d, m, m.angle_ids[t], str(extra["label"]), label_size, boxes, t)
        elif kind == "right_angle_square":
            t = extra.get("target")
            if t not in m.angle_ids:
                raise GeometryRefusal("bad_reference", f"mark {mk.id or kind}: angle {t!r} is not defined")
            _right_angle_square(d, m, m.angle_ids[t])
        elif kind == "equal_ticks":
            refs = extra.get("targets") or extra.get("segments") or []
            for n, ref in enumerate(refs if isinstance(refs, list) else [refs]):
                key = _seg_from_ref(m, ref)
                _ticks(d, m, key, int(extra.get("count", 1)))
        elif kind == "parallel_arrows":
            refs = extra.get("targets") or extra.get("lines") or []
            for ref in refs:
                _parallel_arrow(d, m, ref, int(extra.get("count", 1)))
        elif kind == "point_dot":
            t = extra.get("target")
            if t in m.points:
                d.dots.append(Dot(*m.xy(t)))
        elif kind == "construction_arc":
            centre, through = extra.get("centre"), extra.get("through")
            if centre not in m.points or through not in m.points:
                raise GeometryRefusal("bad_reference", f"mark {mk.id or kind}: needs centre and through points")
            c, p = m.xy(centre), m.xy(through)
            r = math.dist(c, p)
            a = math.degrees(math.atan2(p[1] - c[1], p[0] - c[0]))
            d.strokes.append(Stroke(_arc_points(c, r, a - 12, a + 12, 10), width=0.7, role="mark"))
        else:
            raise GeometryRefusal("bad_schema", f"mark kind {kind!r} is not one v1 draws")
    # right-angle squares the facts guarantee, when the policy shows them
    # (a schematic figure keeps given perpendiculars; an evidence figure's
    # giveaway rule is enforced by the verifier before we get here)
    # point labels
    cx, cy = _centroid(m)
    for p in m.points.values():
        if not p.label:
            continue
        _place_point_label(d, m, p.id, p.label, label_size, boxes, (cx, cy))
    if policy == "assessment_schematic":
        d.notes.append(note or "Not drawn to scale")
    d.bbox = _drawing_bbox(d, m)
    return d


def _centroid(m: Model) -> Vec:
    if not m.points:
        return (0.0, 0.0)
    xs = [p.x for p in m.points.values()]
    ys = [p.y for p in m.points.values()]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def _seg_from_ref(m: Model, ref):
    if isinstance(ref, str) and ref in m.segment_ids:
        return m.segment_ids[ref]
    if isinstance(ref, list) and len(ref) == 2:
        return seg_key(*ref)
    raise GeometryRefusal("bad_reference", f"mark: segment {ref!r} is not defined")


def _angle_arc(d: Drawing, m: Model, key, drawn: set) -> None:
    if key in drawn:
        return
    drawn.add(key)
    v, a0, a1, _ = _angle_geometry(m, key)
    r = ARC_RADIUS_REFLEX if key[2] == "reflex" else ARC_RADIUS
    # nested arcs at the same vertex step outward so they stay distinct
    n_here = sum(1 for k in drawn if k[0] == key[0]) - 1
    r += 0.12 * n_here
    d.strokes.append(Stroke(_arc_points(v, r, a0, a1), width=0.8, role="mark"))


def _right_angle_square(d: Drawing, m: Model, key) -> None:
    v, arms, _ = key
    a, b = sorted(arms)
    vx, vy = m.xy(v)
    ua, ub = _unit(_sub(m.xy(a), (vx, vy))), _unit(_sub(m.xy(b), (vx, vy)))
    s = SQUARE_SIDE
    p1 = (vx + ua[0] * s, vy + ua[1] * s)
    p2 = (vx + (ua[0] + ub[0]) * s, vy + (ua[1] + ub[1]) * s)
    p3 = (vx + ub[0] * s, vy + ub[1] * s)
    d.strokes.append(Stroke([p1, p2, p3], width=0.8, role="mark"))


def _ticks(d: Drawing, m: Model, key, count: int) -> None:
    a, b = sorted(key)
    p, q = m.xy(a), m.xy(b)
    mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
    u = _unit(_sub(q, p))
    n = (-u[1], u[0])
    for i in range(count):
        off = (i - (count - 1) / 2) * 0.14
        c = _add(mid, _mul(u, off))
        d.strokes.append(Stroke([_sub(c, _mul(n, TICK_LEN / 2)), _add(c, _mul(n, TICK_LEN / 2))], width=0.9, role="mark"))


def _parallel_arrow(d: Drawing, m: Model, ref, count: int) -> None:
    if isinstance(ref, str) and ref in m.lines:
        ln = m.lines[ref]
        p, q = m.xy(ln.points[0]), m.xy(ln.points[-1])
    else:
        key = _seg_from_ref(m, ref)
        a, b = sorted(key)
        p, q = m.xy(a), m.xy(b)
    mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
    u = _unit(_sub(q, p))
    for i in range(count):
        c = _add(mid, _mul(u, (i - (count - 1) / 2) * 0.18))
        tip = _add(c, _mul(u, 0.12))
        n = (-u[1], u[0])
        back = _sub(tip, _mul(u, 0.2))
        d.strokes.append(Stroke([_add(back, _mul(n, 0.12)), tip, _sub(back, _mul(n, 0.12))], width=0.8, role="mark"))


# ── label placement ───────────────────────────────────────────────────────

def _try_place(d: Drawing, candidates: list[tuple[float, float, str]], text: str, size: float, boxes: list,
               role: str, what: str) -> None:
    for (x, y, anchor) in candidates:
        box = text_box(x, y, text, size, anchor)
        if _box_clear(box, d.strokes, boxes):
            t = Text(x, y, text, size, role=role, anchor=anchor, box=box)
            d.texts.append(t)
            boxes.append(box)
            return
    raise GeometryRefusal("layout_collision", f"no room for the label {text!r} on {what}", what)


def _place_angle_label(d: Drawing, m: Model, key, text: str, size: float, boxes: list, what: str) -> None:
    """Along the bisector first, then a little either side of it, further
    out each time: a narrow wedge (30°, B8) only has room far from the
    vertex, and a crowded vertex (four rays, B3) only off the bisector."""
    v, a0, a1, bis = _angle_geometry(m, key)
    base = (ARC_RADIUS_REFLEX if key[2] == "reflex" else ARC_RADIUS) + 0.12 * max(0, sum(1 for k in m.angle_ids.values() if k[0] == key[0]) - 1)
    bis_deg = math.degrees(math.atan2(bis[1], bis[0]))
    cands = []
    for extra in (0.42, 0.62, 0.85, 1.1, 1.4, 1.75, 2.1):
        r = base + extra
        for off in (0.0, 14.0, -14.0):
            a = math.radians(bis_deg + off)
            cands.append((v[0] + math.cos(a) * r, v[1] + math.sin(a) * r, "middle"))
    _try_place(d, cands, text, size, boxes, "label", what)


def _place_segment_label(d: Drawing, m: Model, key, text: str, size: float, boxes: list, what: str) -> None:
    a, b = sorted(key)
    p, q = m.xy(a), m.xy(b)
    mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
    u = _unit(_sub(q, p))
    n = (-u[1], u[0])
    # away from the figure's centroid first
    cx, cy = _centroid(m)
    if (mid[0] - cx) * n[0] + (mid[1] - cy) * n[1] < 0:
        n = (-n[0], -n[1])
    cands = []
    for off in (0.38, 0.6, 0.85):
        cands.append((mid[0] + n[0] * off, mid[1] + n[1] * off, "middle"))
        cands.append((mid[0] - n[0] * off, mid[1] - n[1] * off, "middle"))
    _try_place(d, cands, text, size, boxes, "label", what)


def _place_point_label(d: Drawing, m: Model, pid: str, text: str, size: float, boxes: list, centroid: Vec) -> None:
    x, y = m.xy(pid)
    away = _unit(_sub((x, y), centroid))
    if math.hypot(x - centroid[0], y - centroid[1]) < 1e-9:
        away = (0.0, 1.0)
    dirs = [away]
    for a in range(0, 360, 22):
        r = math.radians(a)
        dirs.append((math.cos(r), math.sin(r)))
    cands = []
    # a vertex ringed by arcs (a reflex angle and its interior, E3) needs
    # its label beyond the arcs
    for off in (0.45, 0.7, 0.95, 1.2):
        for ux, uy in dirs:
            cands.append((x + ux * off, y + uy * off, "middle"))
    _try_place(d, cands, text, size, boxes, "point", pid)


def _drawing_bbox(d: Drawing, m: Model) -> tuple[float, float, float, float]:
    xs, ys = [], []
    for s in d.strokes:
        for p in s.points:
            xs.append(p[0])
            ys.append(p[1])
    for t in d.texts:
        if t.box:
            xs += [t.box[0], t.box[2]]
            ys += [t.box[1], t.box[3]]
    for f in d.fills:
        for p in f.points:
            xs.append(p[0])
            ys.append(p[1])
    if not xs:
        return m.bbox()
    return (min(xs), min(ys), max(xs), max(ys))
