"""A verified figure on the video board: the engine's drawing primitives
as scene elements inside a panel, and a step's figure_ops as scene
actions.

The board (maths/board.py) decides WHERE the panel is and WHEN each step
happens; this module decides what a figure's strokes and labels become
and which element a highlight aims at. Nothing here re-decides geometry:
the primitives come from maths.geometry.layout (the same placement the
worksheet prints), scaled into the panel and flipped (figure units are y
up; the board is y down). Every stroke draws with the hand pen; labels
write; the hidden construction line (reveal_object) exists from the
start and draws when its step says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from maths.geometry.errors import GeometryRefusal
from maths.geometry.layout import LINE_OVERHANG, Drawing, Stroke, _add, _mul, _sub, _unit, angle_tag, build_drawing
from maths.geometry.model import Model
from maths.geometry.properties import symmetry_axes
from maths.geometry.spec import FigureSpec

Rect = tuple[float, float, float, float]   # x0, y0, x1, y1 in board pixels

MAX_PX_PER_UNIT = 70.0      # a 3-unit figure is not blown up to fill a panel
MIN_PX_PER_UNIT = 18.0
STROKE_PX = 3.2
MARK_PX = 2.2
AXIS_PX = 2.0               # a symmetry axis drawn across the figure
AXIS_OVERHANG = 0.15        # past the shape, as a fraction of the axis length
# A figure is many short strokes. At the timeline's defaults every `draw` is
# at least 0.8 s and every label `write` 0.6 s, so a hexagon took ~5 s and
# five triangles ~15 s before the first observation could start — the
# timeline then compressed to fit the audio and the highlight landed on a
# half-drawn figure. Explicit durations: a brisk pen, a short floor.
PEN_PX_PER_SEC = 900.0
STROKE_MIN_SECS = 0.25
MARK_SECS = 0.3
LABEL_SECS = 0.4
LABEL_PX = 24.0             # the figure's labels, board pixels (em)
DIM = 0.42


@dataclass
class FigureBoard:
    elements: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    # what a step may cite -> the element ids that depict it
    targets: dict[str, list[str]] = field(default_factory=dict)
    hidden: dict[str, list[str]] = field(default_factory=dict)   # object id -> stroke ids not drawn yet
    labels: dict[str, str] = field(default_factory=dict)          # label tag -> its measure label element
    label_at: dict[str, tuple[float, float]] = field(default_factory=dict)
    box: Rect = (0.0, 0.0, 0.0, 0.0)
    scale: float = 1.0
    prefix: str = "fig"
    origin: tuple[float, float] = (0.0, 0.0)   # board = origin + (x, -y) * scale

    def to_board(self, pt) -> list[float]:
        return [round(self.origin[0] + pt[0] * self.scale, 1), round(self.origin[1] - pt[1] * self.scale, 1)]


def _stroke_secs(points: list, role: str) -> float:
    if role == "mark":
        return MARK_SECS
    length = sum(((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 for (x0, y0), (x1, y1) in zip(points, points[1:]))
    return round(max(STROKE_MIN_SECS, length / PEN_PX_PER_SEC), 2)


def _fit(d: Drawing, panel: Rect, max_scale: Optional[float] = None) -> tuple[float, float, float]:
    x0, y0, x1, y1 = d.bbox
    w, h = max(1e-6, x1 - x0), max(1e-6, y1 - y0)
    pw, ph = panel[2] - panel[0], panel[3] - panel[1]
    s = min(pw / w, ph / h, MAX_PX_PER_UNIT, max_scale or MAX_PX_PER_UNIT)
    if s < MIN_PX_PER_UNIT:
        raise GeometryRefusal("layout_collision", f"the figure ({w:.1f}×{h:.1f} units) does not fit the board panel")
    # centred in the panel
    ox = panel[0] + (pw - w * s) / 2 - x0 * s
    oy = panel[1] + (ph - h * s) / 2 + y1 * s
    return s, ox, oy


def _declared_hidden(spec: FigureSpec) -> set[str]:
    """The lines the AUTHOR hid (to reveal in a step), as opposed to the
    bookkeeping lines constructions hide for themselves, which never draw."""
    return {str(o.get("id")) for o in spec.objects if o.get("hidden") and o.get("id")}


def _with_hidden(m: Model, spec: FigureSpec, label_size: float) -> Drawing:
    d = build_drawing(m, spec, policy="instructional_metric", label_size=label_size)
    x0, y0, x1, y1 = d.bbox
    for lid in sorted(_declared_hidden(spec)):
        ln = m.lines.get(lid)
        if ln is None or len(ln.points) < 2:
            continue
        p0, p1 = m.xy(ln.points[0]), m.xy(ln.points[-1])
        u = _unit(_sub(p1, p0))
        pts = [_sub(p0, _mul(u, LINE_OVERHANG)), _add(p1, _mul(u, LINE_OVERHANG))]
        d.strokes.append(Stroke(pts, dashed=True, role="hidden", tag=f"line:{lid}"))
        for x, y in pts:
            x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
    d.bbox = (x0, y0, x1, y1)
    return d


def figure_board(m: Model, spec: FigureSpec, *, panel: Rect, prefix: str = "fig",
                 cue: Optional[dict] = None, label_px: float = LABEL_PX,
                 max_scale: Optional[float] = None) -> FigureBoard:
    """The figure's elements and its initial draw/write actions. ``cue``
    times the first stroke to the narration; the rest follow in sequence.
    ``max_scale`` (px per unit) lets a row of figures share one scale."""
    fb = FigureBoard(prefix=prefix)
    # the label size is chosen in board pixels, so it is converted to figure
    # units with a provisional scale from the bare model, then the drawing
    # (labels included) is fitted
    bx0, by0, bx1, by1 = m.bbox()
    prov = min((panel[2] - panel[0]) / max(1e-6, bx1 - bx0), (panel[3] - panel[1]) / max(1e-6, by1 - by0),
               MAX_PX_PER_UNIT)
    d = _with_hidden(m, spec, label_px / max(prov, MIN_PX_PER_UNIT))
    if d.fills:
        raise GeometryRefusal("unsupported_feature", "a coloured grid pattern is not drawn on the video board")
    s, ox, oy = _fit(d, panel, max_scale)
    # the labels widen the box, so the fitted scale is smaller than the
    # provisional one and the labels come out small: one more pass at the
    # true scale settles them within a pixel
    try:
        d2 = _with_hidden(m, spec, label_px / s)
        s, ox, oy = _fit(d2, panel, max_scale)
        d = d2
    except GeometryRefusal as exc:
        if exc.code != "layout_collision":
            raise
        # the larger labels do not fit this figure: keep the first pass,
        # whose labels are a little smaller and already placed
    fb.scale = s

    fb.origin = (ox, oy)
    P = fb.to_board

    n = 0

    def uid(kind: str) -> str:
        nonlocal n
        n += 1
        return f"{prefix}_{kind}{n}"

    first_cue = cue
    for st in d.strokes:
        eid = uid("s")
        # exact: a geometric figure is never hand-wobbled — a triangle with
        # equal sides must look it (founder direction 2026-10-08)
        el = {"id": eid, "type": "shape", "shape": "path", "points": [P(p) for p in st.points],
              "width": (MARK_PX if st.role == "mark" else STROKE_PX) * max(0.6, min(1.4, st.width)),
              "color": "muted" if st.role in ("hidden", "mark") else "ink", "exact": True}
        fb.elements.append(el)
        if st.tag:
            fb.targets.setdefault(st.tag, []).append(eid)
        if st.role == "hidden":
            lid = st.tag.split(":", 1)[1] if st.tag else eid
            fb.hidden.setdefault(lid, []).append(eid)
            continue   # exists, not drawn: a reveal_object step draws it
        act = {"verb": "draw", "target": eid, "duration": _stroke_secs(el["points"], st.role)}
        if first_cue:
            act["at"] = first_cue
            first_cue = None
        fb.actions.append(act)
    for t in d.texts:
        eid = uid("t")
        x, y = P((t.x, t.y))
        fb.elements.append({"id": eid, "type": "text", "text": t.text, "size": round(t.size * s, 1),
                            "at": [x, y], "anchor": "mm", "role": "label", "fixed": True})
        fb.actions.append({"verb": "write", "target": eid, "duration": LABEL_SECS})
        if t.tag:
            fb.targets.setdefault(t.tag, []).append(eid)
            if t.tag.startswith(("anglelabel:", "seglabel:")):
                fb.labels[t.tag] = eid
                fb.label_at[t.tag] = (x, y)
    for dot in d.dots:
        eid = uid("d")
        c = P((dot.x, dot.y))
        fb.elements.append({"id": eid, "type": "shape", "shape": "ellipse", "center": c,
                            "rx": max(1.5, dot.r * s), "ry": max(1.5, dot.r * s), "fill": True, "color": "ink",
                            "exact": True})
        fb.actions.append({"verb": "draw", "target": eid, "duration": 0.2})
    x0, y0, x1, y1 = d.bbox
    fb.box = (ox + x0 * s, oy - y1 * s, ox + x1 * s, oy - y0 * s)
    return fb


def _angle_targets(fb: FigureBoard, m: Model, aid: str) -> list[str]:
    key = m.angle_ids.get(aid)
    if key is None:
        return []
    tag = angle_tag(key)
    return fb.targets.get(f"angle:{tag}", []) + fb.targets.get(f"anglelabel:{tag}", [])


def _segment_targets(fb: FigureBoard, m: Model, sid: str) -> list[str]:
    key = m.segment_ids.get(sid)
    if key is None:
        return []
    a, b = sorted(key)
    out = fb.targets.get(f"seg:{a}|{b}", []) + fb.targets.get(f"seg:{b}|{a}", [])
    if not out:
        # the segment is part of a line fact: highlight that line's stroke
        ln = m.line_through(a, b)
        if ln is not None:
            out = fb.targets.get(f"line:{ln.id}", [])
    return out


def figure_targets(fb: FigureBoard) -> list[str]:
    """Every ink stroke of the figure — what 'highlight this figure' means."""
    return [eid for tag, ids in fb.targets.items() if tag.startswith(("line:", "ray:", "seg:")) for eid in ids]


def _label_tag(fb: FigureBoard, m: Model, target: str) -> Optional[str]:
    """The tag of the label that shows ``target``'s measure, if one was placed."""
    key = m.angle_ids.get(target)
    if key is not None:
        return f"anglelabel:{angle_tag(key)}"
    seg = m.segment_ids.get(target)
    if seg is not None:
        a, b = tuple(seg)
        for t in (f"seglabel:{a}|{b}", f"seglabel:{b}|{a}"):
            if t in fb.labels:
                return t
        return f"seglabel:{a}|{b}"
    return None


def targets_for(fb: FigureBoard, m: Model, target: str) -> list[str]:
    """The element ids a step's op means by an id: an angle (its arc and
    label), a segment, a line, a ray, a point's label, a shape (its sides)."""
    if target in m.angle_ids:
        return _angle_targets(fb, m, target)
    if target in m.segment_ids:
        return _segment_targets(fb, m, target)
    if target in m.lines:
        return fb.targets.get(f"line:{target}", [])
    if target in m.rays:
        return fb.targets.get(f"ray:{target}", [])
    if target in m.points:
        return fb.targets.get(f"point:{target}", [])
    if target in m.polygons:
        pg = m.polygons[target]
        out: list[str] = []
        L = len(pg.vertices)
        for i in range(L if pg.closed else L - 1):
            a, b = pg.vertices[i], pg.vertices[(i + 1) % L]
            out += fb.targets.get(f"seg:{a}|{b}", []) + fb.targets.get(f"seg:{b}|{a}", [])
        return out
    return []


def op_actions(fb: FigureBoard, m: Model, op: dict, *, cue: Optional[dict] = None,
               prefix: Optional[str] = None) -> tuple[list[dict], list[dict]]:
    """A figure_op as (new elements, actions). Unknown targets are ignored
    here — the verifier refused them before a board was ever built."""
    verb = str(op.get("op") or "")
    target = str(op.get("target") or "")
    prefix = prefix or fb.prefix
    elements: list[dict] = []
    actions: list[dict] = []
    at = {"at": cue} if cue else {}
    if verb in ("highlight", "tag_equal"):
        ids = targets_for(fb, m, target)
        for i, eid in enumerate(ids):
            actions.append({"verb": "highlight", "target": eid, **(at if i == 0 else {})})
    elif verb == "reveal_object":
        for i, eid in enumerate(fb.hidden.get(target, [])):
            actions.append({"verb": "draw", "target": eid, **(at if i == 0 else {})})
    elif verb == "reveal_measure":
        tag = _label_tag(fb, m, target)
        old = fb.labels.get(tag) if tag else None
        value = str(op.get("value") or "").strip()
        if value and tag and tag in fb.label_at:
            x, y = fb.label_at[tag]
            is_angle = tag.startswith("anglelabel:")
            text = f"{value}°" if is_angle and value.replace(".", "").isdigit() else value
            eid = f"{prefix}_m{len(fb.elements) + len(elements) + 1}"
            elements.append({"id": eid, "type": "text", "text": text, "size": LABEL_PX, "at": [x, y],
                             "anchor": "mm", "role": "label", "color": "accent", "fixed": True})
            if old:
                actions.append({"verb": "fade", "target": old, "to": 0.0, "duration": 0.3, **at})
                at = {}
            actions.append({"verb": "write", "target": eid, **at})
    elif verb == "show_symmetry":
        # the mirror lines, one after another, each across the whole shape
        for i, (a, b) in enumerate(symmetry_axes(m)):
            dx, dy = b[0] - a[0], b[1] - a[1]
            p0 = (a[0] - dx * AXIS_OVERHANG, a[1] - dy * AXIS_OVERHANG)
            p1 = (b[0] + dx * AXIS_OVERHANG, b[1] + dy * AXIS_OVERHANG)
            eid = f"{prefix}_sym{i + 1}"
            elements.append({"id": eid, "type": "shape", "shape": "line", "points": [fb.to_board(p0), fb.to_board(p1)],
                             "width": AXIS_PX, "color": "accent2", "exact": True})
            actions.append({"verb": "draw", "target": eid, "duration": 0.5, **(at if i == 0 else {})})
    # unhighlight: a highlight is a sweep, nothing persists to undo
    return elements, actions


__all__ = ["FigureBoard", "figure_board", "figure_targets", "op_actions", "targets_for", "LABEL_PX"]
