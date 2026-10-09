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
from typing import Callable, Optional

import sympy as sp
from PIL import ImageFont

from maths.geometry.constructions import exact_value
from maths.geometry.errors import GeometryRefusal
from maths.geometry.properties import grid_symmetry_flags
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
    tag: Optional[str] = None   # what it depicts: line:<id> ray:<id> seg:<a>|<b> angle:<key> mark


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
    tag: Optional[str] = None   # point:<id> anglelabel:<key> seglabel:<a>|<b>


@dataclass
class Fill:
    points: list[Vec]
    colour: str                 # a palette name; the renderer maps it
    tag: Optional[str] = None   # cell:<grid id>


@dataclass
class Dot:
    x: float
    y: float
    r: float = 0.06
    solid: bool = False         # a disc of ink (a closed bound), not the board's mist-filled ring


@dataclass
class Drawing:
    strokes: list[Stroke] = field(default_factory=list)
    texts: list[Text] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    dots: list[Dot] = field(default_factory=list)
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    notes: list[str] = field(default_factory=list)
    # clearance around every label box; the video board asks for more than
    # print (its handwriting face runs wider than the measuring font and its
    # audit counts touching as overlap)
    label_pad: float = LABEL_PAD
    # (text, size) -> (w, h) in figure units with the face that will draw
    # the labels; None measures with the print font at cap height
    measure: Optional[Callable[[str, float], tuple[float, float]]] = None


# ── text metrics ──────────────────────────────────────────────────────────

def text_box(x: float, y: float, text: str, size: float, anchor: str, pad: float = LABEL_PAD,
             measure: Optional[Callable[[str, float], tuple[float, float]]] = None
             ) -> tuple[float, float, float, float]:
    """The box a string occupies at (x, y) — y is the vertical centre —
    measured with the font the PNG uses. Figure units."""
    if measure is not None:
        w, h = measure(text, size)
    else:
        w = _MEASURE_FONT.getlength(text) / 100.0 * size
        h = 0.78 * size
    if anchor == "middle":
        x0 = x - w / 2
    elif anchor == "end":
        x0 = x - w
    else:
        x0 = x
    return (x0 - pad, y - h / 2 - pad, x0 + w + pad, y + h / 2 + pad)


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
        if s.role in ("hidden", "grid", "back") or s.tag == "tick" or (s.tag or "").startswith(("rim:", "outline:")):
            # v3: a solid's rim is an outline a label may cross (a radius
            # written inside the top ellipse, as a textbook does)
            # a grid line is background, not an obstacle (v2: every label
            # on a gridded figure crosses one); a tick mark is too small to
            # block a label (the (1, 1) tag by the origin, 2026-10-08)
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
    # coordinate geometry's unit is the grid's own: "d", never "d units"
    if (unit or "").strip().lower() in ("", "unit", "units"):
        unit = None
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

def grid_origin(m: Model) -> Vec:
    """Where a grid pattern sits in figure units: at the origin, or one
    unit to the right of the figure's points. Unit cells, rows from the
    top (row 0 is the highest)."""
    if m.points:
        _x0, _y0, x1, _y1 = m.bbox()
        return (x1 + 1.0, 0.0)
    return (0.0, 0.0)


def grid_axes(m: Model) -> list[tuple[Vec, Vec]]:
    """The mirror lines of the figure's one grid pattern as DRAWN (a blank
    cell is a blank), as anchor pairs across the grid in figure units —
    what a board draws when it says how many lines of symmetry the
    pattern has. Empty when the figure has no grid, or more than one."""
    if len(m.grids) != 1:
        return []
    g = next(iter(m.grids.values()))
    ox, oy = grid_origin(m)
    r, c = g.rows, g.cols
    vertical, horizontal, main, anti = grid_symmetry_flags(g)
    out: list[tuple[Vec, Vec]] = []
    if vertical:
        out.append(((ox + c / 2, oy), (ox + c / 2, oy + r)))
    if horizontal:
        out.append(((ox, oy + r / 2), (ox + c, oy + r / 2)))
    if main:        # top-left corner to bottom-right
        out.append(((ox, oy + r), (ox + c, oy)))
    if anti:        # bottom-left corner to top-right
        out.append(((ox, oy), (ox + c, oy + r)))
    return out


def build_drawing(m: Model, spec: FigureSpec, *, show_hidden: bool = False, label_size: float = 0.42,
                  label_pad: Optional[float] = None,
                  measure: Optional[Callable[[str, float], tuple[float, float]]] = None,
                  policy: str = "instructional_metric", note: Optional[str] = None) -> Drawing:
    d = Drawing()
    if label_pad is not None:
        d.label_pad = label_pad
    d.measure = measure
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
                                dashed=ln.hidden, role="hidden" if ln.hidden else "ink", tag=f"line:{ln.id}"))
        for a, b in zip(ln.points, ln.points[1:]):
            drawn_segments.add(seg_key(a, b))
    for r in m.rays.values():
        p0, p1 = m.xy(r.vertex), m.xy(r.through)
        u = _unit(_sub(p1, p0))
        d.strokes.append(Stroke([p0, _add(p1, _mul(u, RAY_OVERHANG))], tag=f"ray:{r.id}"))
        drawn_segments.add(seg_key(r.vertex, r.through))
    # segments (polygon sides, radii, chords, plain segments)
    # v3: a solid draws its own edges — the seen ones in ink, the hidden
    # ones dashed (role `back`: drawn, muted, never a reveal) — and its rims
    for sd in m.solids.values():
        for a, b, hidden in sd.edges:
            d.strokes.append(Stroke([m.xy(a), m.xy(b)], dashed=hidden, role="back" if hidden else "ink",
                                    tag=f"seg:{min(a, b)}|{max(a, b)}"))
            drawn_segments.add(seg_key(a, b))
        for pts, dashed, tag in sd.curves:
            d.strokes.append(Stroke(list(pts), dashed=dashed, role="back" if dashed else "ink", tag=f"{tag}:{sd.id}"))
    for nt in m.nets.values():
        _draw_net(d, nt)
    for key, sg in m.segments.items():
        if key in drawn_segments or sg.hidden:
            continue
        a, b = sorted(key)
        on_line = any(a in ln.points and b in ln.points for ln in m.lines.values() if ln.hidden and not show_hidden)
        if on_line:
            # a side of a shape that also carries a hidden line fact (parallel-side bookkeeping)
            pass
        d.strokes.append(Stroke([m.xy(a), m.xy(b)], tag=f"seg:{a}|{b}"))
        drawn_segments.add(key)
    # circles
    for c in m.circles.values():
        d.strokes.append(Stroke(_arc_points(m.xy(c.centre), c.radius, 0.0, 360.0, 72)))
        d.dots.append(Dot(*m.xy(c.centre)))
    # grids
    for gid, g in m.grids.items():
        ox, oy = grid_origin(m)
        for i in range(g.rows):
            for j in range(g.cols):
                x, y = ox + j, oy + (g.rows - 1 - i)
                cell = g.cells[i][j]
                pts = [(x, y), (x + 1, y), (x + 1, y + 1), (x, y + 1)]
                if cell != g.blank:
                    d.fills.append(Fill(pts, cell, tag=f"cell:{gid}"))
                else:
                    d.texts.append(Text(x + 0.5, y + 0.5, "★", 0.5, role="note"))
                d.strokes.append(Stroke(pts + [pts[0]], width=0.8, role="mark", tag=f"cell:{gid}"))
        # the pattern's border in ink: what "highlight this figure" sweeps (a
        # band along every cell outline would tint the cells themselves)
        r, c = g.rows, g.cols
        border = [(ox, oy), (ox + c, oy), (ox + c, oy + r), (ox, oy + r), (ox, oy)]
        d.strokes.append(Stroke(border, width=1.0, role="ink", tag=f"grid:{gid}"))

    boxes: list = []
    if m.axes is not None:
        _draw_axes(d, m, label_size, boxes)
    if m.number_line is not None:
        _draw_number_line(d, m)
    if m.bar_chart is not None:
        _draw_bar_chart(d, m)
    # a curve from its equation: one polyline per run inside the box — drawn
    # BEFORE any label is placed, so a coordinate tag (a parabola's vertex)
    # keeps clear of it (frame review, 2026-10-09)
    for cid, (runs, _ctext) in m.curves.items():
        for run in runs:
            d.strokes.append(Stroke(list(run), width=1.1, tag=f"curve:{cid}"))
    # angle marks and labels: every measured angle, plus explicit angle_arc marks
    labelled: set = set()
    arcs_drawn: set = set()
    coord_tags: list[tuple[str, str]] = []   # v2: placed after the point labels
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
        elif ms.target in m.points and m.axes is not None:
            # v2: the coordinate tag "(3, -2)" beside a plotted point; an
            # unknown's "(a, b)" the same way (it is what the question asks)
            if ms.role == "derived":
                continue
            coord_tags.append((ms.target, _fmt_pair(ms.value)))
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
            # v4: an arc with an id and `hidden` waits for a step's reveal_object
            # (the compass set at A, the arc drawn as the teacher says so)
            hidden_mark = bool(extra.get("hidden")) and bool(mk.id)
            d.strokes.append(Stroke(_arc_points(c, r, a - 12, a + 12, 10), width=0.7,
                                    role="hidden" if hidden_mark else "mark",
                                    tag=f"mark:{mk.id}" if mk.id else None))
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
    # v2: the coordinate tag beside a plotted point gives way to the point's
    # name — placed after every name, it takes the next clear side
    for pid, text in coord_tags:
        _place_coord_tag(d, m, pid, text, label_size, boxes)
    # a curve's label beside the end of its longest run, else by its
    # highest point; a crowded curve goes unlabelled
    for cid, (runs, ctext) in m.curves.items():
        if ctext:
            longest = max(runs, key=len)
            first, last = longest[0], longest[-1]
            top = max(longest, key=lambda pt: pt[1])
            size = label_size * 0.8
            cands = [(last[0] - label_size * 0.4, last[1] - label_size * 0.9, "end"),
                     (first[0] + label_size * 0.4, first[1] - label_size * 0.9, "start"),
                     (last[0] + label_size * 0.4, last[1] + label_size * 0.6, "start"),
                     (first[0] - label_size * 0.4, first[1] + label_size * 0.6, "end"),
                     (top[0] + label_size * 0.6, top[1] + label_size * 0.6, "start"),
                     (top[0] - label_size * 0.6, top[1] + label_size * 0.6, "end")]
            _place_or_skip(d, cands, ctext, size, boxes, tag=f"curvelabel:{cid}")
    # a line drawn from its equation carries the equation beside it, near
    # its far end, on whichever side is clear; a crowded line goes unlabelled
    for ln in m.lines.values():
        text = m.line_labels.get(ln.id)
        if not text or len(ln.points) < 2:
            continue
        p0, p1 = m.xy(ln.points[0]), m.xy(ln.points[-1])
        u = _unit(_sub(p1, p0))
        nrm = (-u[1], u[0])
        size = label_size * 0.8
        # the label's axis-aligned box must clear its own slanted line: the
        # offset along the normal is half the box's extent across that normal
        bx0, by0, bx1, by1 = text_box(0.0, 0.0, text, size, "middle", d.label_pad, d.measure)
        bw, bh = bx1 - bx0, by1 - by0
        off = (bw * abs(nrm[0]) + bh * abs(nrm[1])) / 2 + label_size * 0.3
        # either end first (one end is usually in the axes' corner), then the
        # middle; close to the line, then a little further out
        cands = []
        for k in (1.0, 1.5):
            for t in (0.8, 0.2, 0.62, 0.38, 0.9, 0.1, 0.5):
                base = _add(p0, _mul(_sub(p1, p0), t))
                cands += [(base[0] + nrm[0] * off * k, base[1] + nrm[1] * off * k, "middle"),
                          (base[0] - nrm[0] * off * k, base[1] - nrm[1] * off * k, "middle")]
        _place_or_skip(d, cands, text, size, boxes, tag=f"linelabel:{ln.id}")
    if m.axes is not None:
        _number_axes(d, m, label_size, boxes)
    if m.number_line is not None:
        _number_line_numbers(d, m, label_size, boxes)
    if m.bar_chart is not None:
        _bar_chart_labels(d, m, label_size, boxes)
    if m.nets:
        _net_labels(d, m, label_size, boxes)
    if (policy == "assessment_schematic" and m.axes is None) or m.solids:
        # v3: a solid's picture is a view — always captioned
        d.notes.append(note or "Not drawn to scale")
    d.bbox = _drawing_bbox(d, m)
    return d


# ── v2: the coordinate grid ───────────────────────────────────────────────

AXIS_OVERHANG = 0.6          # past the range, before the arrowhead
TICK = 0.12                  # half-length of a tick, figure units


def _fmt_pair(value) -> str:
    text = str(value).strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    parts = [t.strip() for t in text.split(",")]
    return "(" + ", ".join(parts) + ")" if len(parts) == 2 else str(value)


def _fmt_tick(v: float) -> str:
    return str(int(round(v))) if abs(v - round(v)) < 1e-9 else f"{v:g}"


def _tick_numbers(ax, label_size: float) -> tuple[float, int]:
    """(number size, every n-th tick numbered). Sized to the step's pitch,
    not only to the figure's labels: on the video board a 24 px label beside
    a 29 px grid pitch ran the tick numbers into each other (chapter-17
    probe, 2026-10-08). When the pitch is too tight for every tick, every
    second one is numbered."""
    step = ax.step
    num_size = min(label_size * 0.8, step * 0.42)
    every = 1
    if num_size < label_size * 0.5:
        every = 2
        num_size = min(label_size * 0.8, step * 0.84)
    return num_size, every


def _number_axes(d: Drawing, m: Model, label_size: float, boxes: list) -> None:
    """The tick numbers and O, written LAST — after every label of the
    question's data — and skipped where one of those already sits: a point
    by the origin keeps its name and its coordinates, the axis loses a
    number it can spare (its tick mark stays)."""
    ax = m.axes
    step = ax.step
    num_size, every = _tick_numbers(ax, label_size)
    y_axis_x = 0.0 if ax.x0 <= 0 <= ax.x1 else ax.x0
    x_axis_y = 0.0 if ax.y0 <= 0 <= ax.y1 else ax.y0

    def numbered(v: float) -> bool:
        return abs(v) > 1e-9 and round(v / step) % every == 0

    # the y-axis numbers first, then O, then the x-axis numbers: the "-1"
    # under the x-axis and the "-1" beside the y-axis meet at the corner
    y = ax.y0
    while y <= ax.y1 + 1e-9:
        if numbered(y):
            _place_or_skip(d, [(y_axis_x - TICK - num_size * 0.3, y - num_size * 0.35, "end")], _fmt_tick(y),
                           num_size, boxes, strokes=False)
        y += step
    if ax.x0 <= 0 <= ax.x1 and ax.y0 <= 0 <= ax.y1:
        _place_or_skip(d, [(-num_size * 0.6, -num_size * 1.1, "middle")], "O", num_size, boxes, strokes=False)
    x = ax.x0
    while x <= ax.x1 + 1e-9:
        if numbered(x):
            _place_or_skip(d, [(x, x_axis_y - TICK - num_size * 0.9, "middle")], _fmt_tick(x), num_size, boxes,
                           strokes=False)
        x += step


def _draw_axes(d: Drawing, m: Model, label_size: float, boxes: list) -> None:
    """Axes with arrowheads and names, tick marks every step, a light grid
    when the axes ask for one (and the step is 1 or more — finer grids
    are noise at board size), dots for the plotted points. The numbers
    come last, from _number_axes, once the question's labels are placed."""
    ax = m.axes
    step = ax.step
    num_size, _every = _tick_numbers(ax, label_size)
    if ax.grid and step >= 1.0:
        x = ax.x0
        while x <= ax.x1 + 1e-9:
            if abs(x) > 1e-9:
                d.strokes.append(Stroke([(x, ax.y0), (x, ax.y1)], width=0.35, role="grid", tag="grid"))
            x += step
        y = ax.y0
        while y <= ax.y1 + 1e-9:
            if abs(y) > 1e-9:
                d.strokes.append(Stroke([(ax.x0, y), (ax.x1, y)], width=0.35, role="grid", tag="grid"))
            y += step
    # the axes themselves: through the origin when it is inside the range,
    # else along the range's edge
    y_axis_x = 0.0 if ax.x0 <= 0 <= ax.x1 else ax.x0
    x_axis_y = 0.0 if ax.y0 <= 0 <= ax.y1 else ax.y0
    d.strokes.append(Stroke([(ax.x0 - AXIS_OVERHANG * 0.5, x_axis_y), (ax.x1 + AXIS_OVERHANG, x_axis_y)],
                            width=1.1, arrow=True, tag="axis:x"))
    d.strokes.append(Stroke([(y_axis_x, ax.y0 - AXIS_OVERHANG * 0.5), (y_axis_x, ax.y1 + AXIS_OVERHANG)],
                            width=1.1, arrow=True, tag="axis:y"))
    # ticks on both axes
    x = ax.x0
    while x <= ax.x1 + 1e-9:
        if abs(x) > 1e-9:
            d.strokes.append(Stroke([(x, x_axis_y - TICK), (x, x_axis_y + TICK)], width=0.8, role="mark", tag="tick"))
        x += step
    y = ax.y0
    while y <= ax.y1 + 1e-9:
        if abs(y) > 1e-9:
            d.strokes.append(Stroke([(y_axis_x - TICK, y), (y_axis_x + TICK, y)], width=0.8, role="mark", tag="tick"))
        y += step
    # the axis names
    for (tx, ty, name) in ((ax.x1 + AXIS_OVERHANG + num_size * 0.9, x_axis_y - num_size * 0.35, "x"),
                           (y_axis_x + num_size * 0.9, ax.y1 + AXIS_OVERHANG, "y")):
        t = Text(tx, ty, name, label_size, role="label", anchor="middle", italic=True)
        t.box = text_box(t.x, t.y, t.text, t.size, t.anchor, d.label_pad, d.measure)
        d.texts.append(t)
        boxes.append(t.box)
    for pid, pt in m.points.items():
        if pt.hidden:
            continue            # a line's own end, nobody's point
        x, y = m.xy(pid)
        d.dots.append(Dot(x, y, r=0.09))


NL_OVERHANG = 0.8            # a number line runs past its range to its arrowheads
BOUND_R = 0.13               # the circle at an interval's bound


def _arrowhead(d: Drawing, tip: Vec, direction: Vec, size: float, width: float, tag: Optional[str]) -> None:
    """Two barbs meeting at `tip`, drawn as strokes: no renderer draws
    a Stroke's `arrow` flag, and a number line's unbounded side must be
    SEEN to go on."""
    u = _unit(direction)
    n = (-u[1], u[0])
    base = _sub(tip, _mul(u, size))
    for sgn in (1.0, -1.0):
        d.strokes.append(Stroke([_add(base, _mul(n, sgn * size * 0.5)), tip], width=width, tag=tag))


def _draw_number_line(d: Drawing, m: Model) -> None:
    """The line with arrowheads both ways, a tick every step, then each
    interval: a heavy bar along the line, a filled circle at a closed
    bound, a hollow one at an open bound, an arrowhead where it goes on
    for ever. The numbers come last (_number_line_numbers)."""
    nl = m.number_line
    assert nl is not None
    x0, x1 = nl.lo - NL_OVERHANG, nl.hi + NL_OVERHANG
    d.strokes.append(Stroke([(x0, 0.0), (x1, 0.0)], width=1.1, tag="numberline"))
    _arrowhead(d, (x1, 0.0), (1.0, 0.0), 0.3, 1.1, "numberline")
    _arrowhead(d, (x0, 0.0), (-1.0, 0.0), 0.3, 1.1, "numberline")
    x = nl.lo
    while x <= nl.hi + 1e-9:
        d.strokes.append(Stroke([(x, -TICK), (x, TICK)], width=0.8, role="mark", tag="tick"))
        x += nl.step
    for iv in m.intervals.values():
        a = nl.lo - NL_OVERHANG * 0.6 if iv.lo is None else iv.lo
        b = nl.hi + NL_OVERHANG * 0.6 if iv.hi is None else iv.hi
        d.strokes.append(Stroke([(a, 0.0), (b, 0.0)], width=1.4, tag=f"interval:{iv.id}"))
        if iv.lo is None:
            _arrowhead(d, (a, 0.0), (-1.0, 0.0), 0.36, 1.4, f"interval:{iv.id}")
        if iv.hi is None:
            _arrowhead(d, (b, 0.0), (1.0, 0.0), 0.36, 1.4, f"interval:{iv.id}")
        for bound, closed in ((iv.lo, iv.lo_closed), (iv.hi, iv.hi_closed)):
            if bound is None:
                continue
            if closed:
                d.dots.append(Dot(bound, 0.0, r=BOUND_R, solid=True))
            else:
                ring = [(bound + BOUND_R * math.cos(2 * math.pi * k / 20), BOUND_R * math.sin(2 * math.pi * k / 20))
                        for k in range(21)]
                d.strokes.append(Stroke(ring, width=1.1, tag=f"bound:{iv.id}"))


BAR_AXIS_X = -0.3            # the value axis, left of the first bar
BAR_IN, BAR_OUT = 0.15, 0.85 # a bar spans i + BAR_IN .. i + BAR_OUT


def _draw_bar_chart(d: Drawing, m: Model) -> None:
    """The baseline and the value axis (ticks every step, an arrowhead),
    a filled bar per value — the highlighted ones in yellow, the rest in
    blue, each outlined — then the mean line and the range bracket. The
    numbers come last (_bar_chart_labels)."""
    bc = m.bar_chart
    assert bc is not None
    n = len(bc.values)
    top = bc.steps + 0.5
    d.strokes.append(Stroke([(BAR_AXIS_X, 0.0), (n + 0.3, 0.0)], width=1.1, tag="axis:x"))
    d.strokes.append(Stroke([(BAR_AXIS_X, 0.0), (BAR_AXIS_X, top)], width=1.1, tag="axis:y"))
    _arrowhead(d, (BAR_AXIS_X, top), (0.0, 1.0), 0.25, 1.1, "axis:y")
    for k in range(1, bc.steps + 1):
        d.strokes.append(Stroke([(BAR_AXIS_X - TICK, float(k)), (BAR_AXIS_X + TICK, float(k))], width=0.8,
                                role="mark", tag="tick"))
    for i, v in enumerate(bc.values):
        h = v / bc.step
        pts = [(i + BAR_IN, 0.0), (i + BAR_OUT, 0.0), (i + BAR_OUT, h), (i + BAR_IN, h)]
        if h > 1e-9:
            d.fills.append(Fill(pts, "Y" if i in bc.highlight else "B", tag=f"bar:{i}"))
        d.strokes.append(Stroke(pts + [pts[0]], width=0.7, tag=f"bar:{i}"))
    if bc.mean is not None:
        y = bc.mean / bc.step
        d.strokes.append(Stroke([(BAR_AXIS_X, y), (n + 0.3, y)], width=0.9, dashed=True, tag="mean"))
    if bc.range_bracket:
        x = n + 0.55
        lo, hi = min(bc.values) / bc.step, max(bc.values) / bc.step
        d.strokes.append(Stroke([(x - 0.15, lo), (x, lo), (x, hi), (x - 0.15, hi)], width=0.9, tag="range"))


def _bar_chart_labels(d: Drawing, m: Model, label_size: float, boxes: list) -> None:
    """The tick numbers up the value axis, each value under its bar, the
    mean beside its line and the range beside its bracket — written last,
    skipped where a label already sits."""
    bc = m.bar_chart
    assert bc is not None
    n = len(bc.values)
    num = label_size * 0.8
    for k in range(1, bc.steps + 1):
        _place_or_skip(d, [(BAR_AXIS_X - TICK - num * 0.3, k - num * 0.35, "end")], _fmt_tick(k * bc.step), num,
                       boxes, strokes=False)
    _place_or_skip(d, [(BAR_AXIS_X - TICK - num * 0.3, -num * 0.35, "end")], "0", num, boxes, strokes=False)
    for i, text in enumerate(bc.labels):
        _place_or_skip(d, [(i + 0.5, -num * 1.0, "middle")], text, num, boxes, strokes=False)
    if bc.mean is not None and bc.mean_label:
        y = bc.mean / bc.step
        _place_or_skip(d, [(n + 0.45, y, "start"), (n + 0.45, y + num * 0.8, "start"), (n + 0.45, y - num * 0.8, "start")],
                       bc.mean_label, num, boxes, tag="meanlabel")
    if bc.range_bracket and bc.range_label:
        lo, hi = min(bc.values) / bc.step, max(bc.values) / bc.step
        _place_or_skip(d, [(n + 0.8, (lo + hi) / 2, "start")], bc.range_label, num, boxes, tag="rangelabel")


def _number_line_numbers(d: Drawing, m: Model, label_size: float, boxes: list) -> None:
    """Every tick's number under the line (0 included), written last and
    skipped where a label already sits."""
    nl = m.number_line
    assert nl is not None
    num_size, every = _tick_numbers(nl, label_size)
    x = nl.lo
    k = 0
    while x <= nl.hi + 1e-9:
        if k % every == 0:
            _place_or_skip(d, [(x, -TICK - num_size * 1.0, "middle")], _fmt_tick(x), num_size, boxes, strokes=False)
        x += nl.step
        k += 1


def _draw_net(d: Drawing, nt) -> None:
    """v3: a net's cells as squares (or rectangles), the shaded one filled."""
    for cell in nt.cells:
        x0, y0, x1, y1 = cell.box
        pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        if cell.shaded:
            d.fills.append(Fill(pts, "Y", tag=f"cell:{nt.id}:{cell.label}"))
        d.strokes.append(Stroke(pts + [pts[0]], width=1.0, tag=f"net:{nt.id}:{cell.label}"))


def _net_labels(d: Drawing, m: Model, label_size: float, boxes: list) -> None:
    for nt in m.nets.values():
        for cell in nt.cells:
            x0, y0, x1, y1 = cell.box
            _place_or_skip(d, [((x0 + x1) / 2, (y0 + y1) / 2 - label_size * 0.35, "middle")], cell.label,
                           label_size * 0.9, boxes, strokes=False, tag=f"netlabel:{nt.id}:{cell.label}")


def _place_or_skip(d: Drawing, candidates: list[tuple[float, float, str]], text: str, size: float,
                   boxes: list, *, strokes: bool = True, tag: Optional[str] = None) -> bool:
    """A label that may be left out: placed at the first clear candidate,
    else not written (a tick number at a crowded corner). ``strokes=False``
    checks other labels only — a tick number sits against its own tick
    and axis by construction."""
    for (x, y, anchor) in candidates:
        box = text_box(x, y, text, size, anchor, d.label_pad, d.measure)
        if _box_clear(box, d.strokes if strokes else [], boxes):
            d.texts.append(Text(x, y, text, size, role="label", anchor=anchor, box=box, tag=tag))
            boxes.append(box)
            return True
    return False


def _place_coord_tag(d: Drawing, m: Model, pid: str, text: str, size: float, boxes: list) -> None:
    x, y = m.xy(pid)
    off = size * 0.9
    cands = [(x + off, y + off, "start"), (x + off, y - off * 1.3, "start"), (x - off, y + off, "end"),
             (x - off, y - off * 1.3, "end"), (x, y + off * 1.4, "middle"), (x, y - off * 1.8, "middle")]
    # a second ring, further out: the name took the near side, and a point
    # by the origin sits among the tick numbers (C5's A at (1, 1))
    far = off * 1.8
    cands += [(x + far, y + far, "start"), (x + far, y - far, "start"), (x - far, y + far, "end"),
              (x - far, y - far, "end"), (x, y + far * 1.3, "middle"), (x, y - far * 1.3, "middle")]
    # a point by the axes (C4's A at (1, 1)) has no room for a full-size tag
    # between its segment and the axis lines: a smaller tag before none —
    # the way a textbook writes small coordinates by a crowded origin
    last: Optional[GeometryRefusal] = None
    for k in (0.85, 0.7, 0.55):
        try:
            _try_place(d, cands, text, size * k, boxes, "label", pid, tag=f"coord:{pid}")
            return
        except GeometryRefusal as exc:
            if exc.code != "layout_collision":
                raise
            last = exc
    assert last is not None
    raise last


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


def angle_tag(key) -> str:
    v, arms, region = key
    return f"{v}|{'|'.join(sorted(arms))}|{region}"


def _angle_arc(d: Drawing, m: Model, key, drawn: set) -> None:
    if key in drawn:
        return
    drawn.add(key)
    v, a0, a1, _ = _angle_geometry(m, key)
    r = ARC_RADIUS_REFLEX if key[2] == "reflex" else ARC_RADIUS
    # nested arcs at the same vertex step outward so they stay distinct
    n_here = sum(1 for k in drawn if k[0] == key[0]) - 1
    r += 0.12 * n_here
    d.strokes.append(Stroke(_arc_points(v, r, a0, a1), width=0.8, role="mark", tag=f"angle:{angle_tag(key)}"))


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
               role: str, what: str, tag: Optional[str] = None) -> None:
    for (x, y, anchor) in candidates:
        box = text_box(x, y, text, size, anchor, d.label_pad, d.measure)
        if _box_clear(box, d.strokes, boxes):
            t = Text(x, y, text, size, role=role, anchor=anchor, box=box, tag=tag)
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
    _try_place(d, cands, text, size, boxes, "label", what, tag=f"anglelabel:{angle_tag(key)}")


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
    # the label's axis-aligned box must clear its OWN slanted segment: on a
    # solid's board (a cylinder's radius PR, 2026-10-09) the handwriting
    # face's box was wider than the segment and every near offset crossed
    # it — the offset along the normal is at least half the box's extent
    # across that normal (as a line's equation label is placed, phase 1)
    bx0, by0, bx1, by1 = text_box(0.0, 0.0, text, size, "middle", d.label_pad, d.measure)
    bw, bh = bx1 - bx0, by1 - by0
    need = (bw * abs(n[0]) + bh * abs(n[1])) / 2 + 0.08
    cands = []
    for off in (0.38, 0.6, 0.85):
        off = max(off, need)
        cands.append((mid[0] + n[0] * off, mid[1] + n[1] * off, "middle"))
        cands.append((mid[0] - n[0] * off, mid[1] - n[1] * off, "middle"))
    for off in (need + 0.3, need + 0.6):
        cands.append((mid[0] + n[0] * off, mid[1] + n[1] * off, "middle"))
        cands.append((mid[0] - n[0] * off, mid[1] - n[1] * off, "middle"))
    _try_place(d, cands, text, size, boxes, "label", what, tag=f"seglabel:{a}|{b}")


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
    _try_place(d, cands, text, size, boxes, "point", pid, tag=f"point:{pid}")


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
