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
from typing import Callable, Optional

from maths.geometry.errors import GeometryRefusal
from maths.geometry.layout import (LINE_OVERHANG, Drawing, Stroke, _add, _mul, _sub, _unit, angle_tag,
                                  build_drawing, grid_axes)
from maths.geometry.model import Model
from maths.geometry.properties import symmetry_axes
from maths.geometry.spec import FigureSpec

Rect = tuple[float, float, float, float]   # x0, y0, x1, y1 in board pixels

MAX_PX_PER_UNIT = 70.0      # a 3-unit figure is not blown up to fill a panel
MIN_PX_PER_UNIT = 18.0
# An UNGRIDDED figure's units are only its side lengths — 7, 24 and 25 cm
# draw the same triangle as 0.7, 2.4 and 2.5 — so its legibility is its DRAWN
# size, not pixels per unit: its longest side must reach this many pixels.
MIN_FIGURE_PX = 240.0
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
GRID_PX = 1.0               # v2: a grid line, faint and quick
GRID_SECS = 0.05
CELL_SECS = 0.15            # a coloured cell of a grid pattern: outlined, then filled
# the worksheet's palette letters as the scene engine's fill names
# (spike/scene_engine/paper.py CELL_FILLS; the two are pinned equal by a test)
CELL_COLOURS = {"R": "red", "Y": "yellow", "G": "green", "B": "blue", "O": "orange", "P": "purple",
                "W": "white", "K": "black"}
LABEL_PX = 24.0             # the figure's labels, board pixels (em)
MIN_TEXT_PX = 10.0          # the scene schema's floor for a text element
DIM = 0.42
_ANCHOR = {"start": "lm", "middle": "mm", "end": "rm"}   # layout anchors -> scene anchors


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
    # (text, size px) -> (w, h) px with the face the board draws, when known
    text_metric: Optional[Callable[[str, float], tuple[float, float]]] = None

    def to_board(self, pt) -> list[float]:
        return [round(self.origin[0] + pt[0] * self.scale, 1), round(self.origin[1] - pt[1] * self.scale, 1)]


def _dash_pieces(points: list, on: float, off: float) -> list[list]:
    """A polyline cut into dashes of `on` figure units with `off` gaps."""
    out: list[list] = []
    pos, drawing = 0.0, True        # distance into the current dash or gap
    cur: list = []
    for p, q in zip(points, points[1:]):
        seg = _sub(q, p)
        length = (seg[0] ** 2 + seg[1] ** 2) ** 0.5
        if length < 1e-9:
            continue
        u = _unit(seg)
        t = 0.0
        if drawing and not cur:
            cur = [p]
        while t < length:
            room = (on if drawing else off) - pos
            step = min(room, length - t)
            t += step
            pos += step
            here = _add(p, _mul(u, t))
            if drawing:
                cur.append(here)
            if pos >= (on if drawing else off) - 1e-9:
                if drawing and len(cur) >= 2:
                    out.append(cur)
                cur = [] if drawing else [here]
                drawing, pos = not drawing, 0.0
    if drawing and len(cur) >= 2:
        out.append(cur)
    return out


def _stroke_secs(points: list, role: str) -> float:
    if role == "grid":
        return GRID_SECS
    if role == "mark":
        return MARK_SECS
    length = sum(((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 for (x0, y0), (x1, y1) in zip(points, points[1:]))
    return round(max(STROKE_MIN_SECS, length / PEN_PX_PER_SEC), 2)


def _gridded(m: Model) -> bool:
    """Axes or a number line: figure units are numbers, labels crowd."""
    return m.axes is not None or m.number_line is not None or m.bar_chart is not None or bool(m.solids) or bool(m.nets)


def _min_px(m: Model) -> float:
    """The smallest scale a figure may draw at: MIN_PX_PER_UNIT — per grid
    STEP on axes, where the step is the pitch the eye reads (a parabola over
    y in [-8, 8] at step 2 is as legible as a line over [-4, 4] at step 1;
    the labels are sized in board pixels, not units, either way)."""
    step = m.axes.step if m.axes is not None else (m.number_line.step if m.number_line is not None else 1.0)
    if float(step) > 1.0:
        return MIN_PX_PER_UNIT / float(step)
    if _gridded(m):
        # axes, a number line, a chart, a solid: units are numbers the eye
        # reads off the grid, so the floor stays per unit
        return MIN_PX_PER_UNIT
    floor = MIN_PX_PER_UNIT
    if m.circles and not m.polygons and not m.angle_ids:
        # a circle alone (its centre, a radius or a diameter): nothing in it
        # is finer than its outline, so it may draw at half the floor — a
        # radius-10 circle is 20 units across, which no panel holds at
        # 18 px/unit ("the figure (20.0×20.0 units) does not fit the board
        # panel", the circle kit's try-it round, 2026-10-15)
        floor = MIN_PX_PER_UNIT / 2
    # ungridded: legible when its longest side draws MIN_FIGURE_PX, however
    # many units that is. A 7-24-25 right triangle is 27.5 units wide with
    # its labels and needed 495 px at 18 px/unit, so it never fitted the
    # 410 px try-it panel (the first English trigonometry kit, 2026-10-18,
    # failed twice on it); at the drawn-size floor it draws ~400 px wide.
    # Small figures keep the per-unit floor: it only ever LOWERS.
    x0, y0, x1, y1 = m.bbox()
    longest = max(x1 - x0, y1 - y0)
    if longest > 0:
        floor = min(floor, MIN_FIGURE_PX / longest)
    return floor


def _fit(d: Drawing, panel: Rect, max_scale: Optional[float] = None,
         min_px: float = MIN_PX_PER_UNIT) -> tuple[float, float, float]:
    x0, y0, x1, y1 = d.bbox
    w, h = max(1e-6, x1 - x0), max(1e-6, y1 - y0)
    pw, ph = panel[2] - panel[0], panel[3] - panel[1]
    s = min(pw / w, ph / h, MAX_PX_PER_UNIT, max_scale or MAX_PX_PER_UNIT)
    if s < min_px:
        raise GeometryRefusal("layout_collision", f"the figure ({w:.1f}×{h:.1f} units) does not fit the board panel")
    # centred in the panel
    ox = panel[0] + (pw - w * s) / 2 - x0 * s
    oy = panel[1] + (ph - h * s) / 2 + y1 * s
    return s, ox, oy


def _declared_hidden(spec: FigureSpec) -> set[str]:
    """The lines the AUTHOR hid (to reveal in a step), as opposed to the
    bookkeeping lines constructions hide for themselves, which never draw."""
    return {str(o.get("id")) for o in spec.objects if o.get("hidden") and o.get("id")}


def _with_hidden(m: Model, spec: FigureSpec, label_size: float, px_per_unit: Optional[float] = None,
                 text_metric: Optional[Callable[[str, float], tuple[float, float]]] = None) -> Drawing:
    # a quarter of a label around every label, measured with the board's
    # own face: the handwriting face runs wider than the measuring font and
    # the audit counts a touch as an overlap (a coordinate tag against its
    # point's name, 2026-10-08; four measure labels against the vertex O
    # of a fan of angles, 2026-10-15 — print's clearance had passed them
    # all). A figure the larger clearance cannot lay out (a narrow wedge,
    # B8's 30°) falls back to print's, as it was laid out before; a
    # coordinate figure keeps the board's, as since 2026-10-08.
    measure = None
    if text_metric is not None and px_per_unit:
        # the board's own face, in figure units: the print font's cap height
        # under-measured the handwriting face by half, and a point's name
        # was placed touching its coordinate tag
        def measure(text: str, size: float, _k: float = px_per_unit) -> tuple[float, float]:
            w, h = text_metric(text, size * _k)
            return (w / _k, h / _k)
    try:
        d = build_drawing(m, spec, policy="instructional_metric", label_size=label_size,
                          label_pad=label_size * 0.25 if measure is not None else None, measure=measure)
    except GeometryRefusal as exc:
        if exc.code != "layout_collision" or measure is None or _gridded(m):
            raise
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


def _turn_to_fit(m: Model, spec: FigureSpec, panel: Rect) -> bool:
    """Lay a figure on its side when it is too tall for the panel and its
    orientation carries no meaning: no axes, number line or chart (they fix
    the orientation), no solid or net (one projection draws every solid),
    and no orientation the author chose. The first English trigonometry kit
    (2026-10-18) lost its try-it to "the figure (5.0×12.0 units) does not
    fit the board panel": a right triangle standing on its 5 cm leg under a
    two-line question, while the same triangle on its 12 cm leg fits at
    29 px/unit. The rotation is IN PLACE because the board's later steps
    reveal labels from this same model, and it happens once per model (the
    evidence row calls figure_board twice). True when the model was turned."""
    if getattr(m, "_turned_to_fit", False) or spec.orientation:
        return False
    if _gridded(m) or m.solids or m.nets:
        return False
    x0, y0, x1, y1 = m.bbox()
    w, h = x1 - x0, y1 - y0
    pw, ph = panel[2] - panel[0], panel[3] - panel[1]
    # only when turning makes the figure's shape match the panel's: a tall
    # figure in a landscape panel
    if h <= w or ph >= pw:
        return False
    from maths.geometry.compiler import _rotate  # noqa: PLC0415 — compiler imports the drawing side

    _rotate(m, -90.0)
    m._turned_to_fit = True  # type: ignore[attr-defined]
    return True


def figure_board(m: Model, spec: FigureSpec, *, panel: Rect, prefix: str = "fig",
                 cue: Optional[dict] = None, label_px: float = LABEL_PX,
                 max_scale: Optional[float] = None,
                 text_metric: Optional[Callable[[str, float], tuple[float, float]]] = None) -> FigureBoard:
    """The figure's elements and its initial draw/write actions. ``cue``
    times the first stroke to the narration; the rest follow in sequence.
    ``max_scale`` (px per unit) lets a row of figures share one scale."""
    fb = FigureBoard(prefix=prefix, text_metric=text_metric)
    floor = _min_px(m)

    def first_pass():
        # the label size is chosen in board pixels, so it is converted to figure
        # units with a provisional scale from the bare model, then the drawing
        # (labels included) is fitted
        bx0, by0, bx1, by1 = m.bbox()
        prov = min((panel[2] - panel[0]) / max(1e-6, bx1 - bx0), (panel[3] - panel[1]) / max(1e-6, by1 - by0),
                   MAX_PX_PER_UNIT)
        dd = _with_hidden(m, spec, label_px / max(prov, floor), max(prov, floor), text_metric)
        return (dd, *_fit(dd, panel, max_scale, floor))

    try:
        d, s, ox, oy = first_pass()
    except GeometryRefusal as exc:
        if exc.code != "layout_collision" or not _turn_to_fit(m, spec, panel):
            raise
        d, s, ox, oy = first_pass()
    # the labels widen the box, so the fitted scale is smaller than the
    # provisional one and the labels come out small: one more pass at the
    # true scale settles them within a pixel
    try:
        d2 = _with_hidden(m, spec, label_px / s, s, text_metric)
        s, ox, oy = _fit(d2, panel, max_scale, floor)
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
    # a dashed stroke that is DRAWN (a bar chart's mean line) becomes its
    # dashes: the scene schema has no dash style, and the teacher calls it
    # "the dashed line" (2026-10-09). A hidden dashed line stays one element
    # for reveal_object.
    strokes: list[Stroke] = []
    for st in d.strokes:
        if st.dashed and st.role != "hidden":
            for piece in _dash_pieces(st.points, 0.28, 0.18):
                strokes.append(Stroke(piece, width=st.width, role=st.role, tag=st.tag))
        else:
            strokes.append(st)
    for st in strokes:
        eid = uid("s")
        # exact: a geometric figure is never hand-wobbled — a triangle with
        # equal sides must look it (founder direction 2026-10-08)
        width = GRID_PX if st.role == "grid" else (MARK_PX if st.role == "mark" else STROKE_PX) * max(0.6, min(1.4, st.width))
        # the axes in their own colour: on the simultaneous-equations video the
        # x- and y-axes read as a third and fourth line (founder, 2026-10-09)
        colour = "muted" if st.role in ("hidden", "mark", "grid", "back") else (
            "accent2" if (st.tag or "").startswith("axis:") else "ink")
        el = {"id": eid, "type": "shape", "shape": "path", "points": [P(p) for p in st.points],
              "width": width, "color": colour, "exact": True}
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
    for f in d.fills:
        # a coloured cell of a grid pattern: the pen outlines it, then the
        # colour lands — solid, exact, the worksheet's own colour
        name = CELL_COLOURS.get(f.colour)
        if name is None:
            raise GeometryRefusal("unsupported_feature",
                                  f"the video board has no fill for the palette colour {f.colour!r}")
        eid = uid("c")
        # a tint is the renderer's translucent wash (the lines show through
        # a shaded half-plane); a cell is the worksheet's own solid colour
        fb.elements.append({"id": eid, "type": "shape", "shape": "path", "points": [P(p) for p in f.points],
                            "closed": True, "width": MARK_PX, "color": "muted", "fill": True if f.tint else name,
                            "exact": True})
        fb.actions.append({"verb": "draw", "target": eid, "duration": CELL_SECS})
        if f.tag:
            fb.targets.setdefault(f.tag, []).append(eid)
    for t in d.texts:
        eid = uid("t")
        x, y = P((t.x, t.y))
        # the layout's anchor, kept: a start-anchored tag written centred sat
        # half its width to the left — into its point's name (chapter-17
        # probe, 2026-10-08: TEXT_OVERLAP B + "(7, 6)")
        # never under the schema's floor: a tick number sized in the first fit
        # pass and kept after the second failed came out at 9.9 px in a short
        # panel, and the whole example was lost to a validation error
        fb.elements.append({"id": eid, "type": "text", "text": t.text,
                            "size": max(MIN_TEXT_PX, round(t.size * s, 1)),
                            "at": [x, y], "anchor": _ANCHOR.get(t.anchor, "mm"), "role": "label", "fixed": True})
        fb.actions.append({"verb": "write", "target": eid, "duration": LABEL_SECS})
        if t.tag:
            fb.targets.setdefault(t.tag, []).append(eid)
            if t.tag.startswith(("anglelabel:", "seglabel:")):
                fb.labels[t.tag] = eid
                fb.label_at[t.tag] = (x, y)
    for dot in d.dots:
        eid = uid("d")
        c = P((dot.x, dot.y))
        if dot.solid:
            # the renderer fills an ellipse with mist, never ink; a stroke as
            # wide as the dot, one pixel long, lands as a solid disc through
            # its round caps (a number line's closed bound, 2026-10-09)
            fb.elements.append({"id": eid, "type": "shape", "shape": "path",
                                "points": [[round(c[0] - 0.5, 1), c[1]], [round(c[0] + 0.5, 1), c[1]]],
                                "width": round(max(3.0, 2 * dot.r * s), 1), "color": "ink", "exact": True})
        else:
            fb.elements.append({"id": eid, "type": "shape", "shape": "ellipse", "center": c,
                                "rx": max(1.5, dot.r * s), "ry": max(1.5, dot.r * s), "fill": True, "color": "ink",
                                "exact": True})
        fb.actions.append({"verb": "draw", "target": eid, "duration": 0.2})
        if dot.tag:
            fb.targets.setdefault(dot.tag, []).append(eid)
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
    return [eid for tag, ids in fb.targets.items() if tag.startswith(("line:", "ray:", "seg:", "grid:", "rim:", "outline:", "net:"))
            for eid in ids]


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
    if target in m.grids:
        return fb.targets.get(f"grid:{target}", [])      # the border, never the cells
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
            anchor = next((e.get("anchor", "mm") for e in fb.elements if e["id"] == old), "mm")
            # the value is wider than the symbol it replaces ("126°" over
            # "z"): written at the symbol's spot it ran into the vertex O
            # (2026-10-15). It steps away from the vertex (or its segment)
            # until its box clears every other label on the figure.
            x, y = _clear_spot(fb, m, target, (x, y), text, anchor, skip=(old,) + tuple(e["id"] for e in elements))
            elements.append({"id": eid, "type": "text", "text": text, "size": LABEL_PX, "at": [x, y],
                             "anchor": anchor, "role": "label", "color": "accent", "fixed": True})
            if old:
                actions.append({"verb": "fade", "target": old, "to": 0.0, "duration": 0.3, **at})
                at = {}
            actions.append({"verb": "write", "target": eid, **at})
    elif verb == "show_symmetry":
        # the mirror lines, one after another, each across the whole shape
        # (a polygon's, else the one grid pattern's as coloured)
        for i, (a, b) in enumerate(symmetry_axes(m) or grid_axes(m)):
            dx, dy = b[0] - a[0], b[1] - a[1]
            p0 = (a[0] - dx * AXIS_OVERHANG, a[1] - dy * AXIS_OVERHANG)
            p1 = (b[0] + dx * AXIS_OVERHANG, b[1] + dy * AXIS_OVERHANG)
            eid = f"{prefix}_sym{i + 1}"
            elements.append({"id": eid, "type": "shape", "shape": "line", "points": [fb.to_board(p0), fb.to_board(p1)],
                             "width": AXIS_PX, "color": "accent2", "exact": True})
            actions.append({"verb": "draw", "target": eid, "duration": 0.5, **(at if i == 0 else {})})
    # unhighlight: a highlight is a sweep, nothing persists to undo
    return elements, actions


TOUCH_PX = 6.0      # the renderer counts boxes within 4 px as one word; keep further


def _px_box(fb: FigureBoard, text: str, size: float, at: tuple[float, float], anchor: str) -> Rect:
    if fb.text_metric is not None:
        w, h = fb.text_metric(text, size)
    else:
        w, h = len(text) * size * 0.6, size * 1.3
    ax, ay = at
    x0 = ax - (w if anchor[0] == "r" else w / 2 if anchor[0] == "m" else 0)
    y0 = ay - (h if anchor[1] == "b" else h / 2 if anchor[1] == "m" else 0)
    return (x0, y0, x0 + w, y0 + h)


def _clear_spot(fb: FigureBoard, m: Model, target: str, at: tuple[float, float], text: str, anchor: str,
                skip: tuple[str, ...] = ()) -> tuple[float, float]:
    """Where a label of ``text`` goes so its box clears the figure's other
    labels: ``at`` when it already does, else stepped outward from the
    angle's vertex (or the segment's midpoint) along the line through
    ``at``. The last step is kept when none clears."""
    others = [_px_box(fb, e["text"], float(e["size"]), tuple(e["at"]), e.get("anchor", "mm"))
              for e in fb.elements if e["type"] == "text" and e["id"] not in skip]

    def clear(p):
        x0, y0, x1, y1 = _px_box(fb, text, LABEL_PX, p, anchor)
        g = TOUCH_PX
        return not any(x0 - g < b[2] and x1 + g > b[0] and y0 - g < b[3] and y1 + g > b[1] for b in others)

    if clear(at):
        return at
    key = m.angle_ids.get(target)
    if key is not None:
        c = fb.to_board(m.xy(key[0]))
    else:
        seg = m.segment_ids.get(target)
        if seg is None:
            return at
        a, b = tuple(seg)
        (ax, ay), (bx, by) = m.xy(a), m.xy(b)
        c = fb.to_board(((ax + bx) / 2, (ay + by) / 2))
    dx, dy = at[0] - c[0], at[1] - c[1]
    L = (dx * dx + dy * dy) ** 0.5
    ux, uy = (dx / L, dy / L) if L > 1e-6 else (0.0, -1.0)
    nx, ny = -uy, ux
    p = at
    # outward a quarter-label at a time, a little to either side at each
    # step; the nearest clear spot wins
    for k in range(1, 13):
        step = LABEL_PX * 0.25 * k
        for side in (0.0, 0.5, -0.5):
            sx, sy = nx * LABEL_PX * side, ny * LABEL_PX * side
            p = (round(at[0] + ux * step + sx, 1), round(at[1] + uy * step + sy, 1))
            if clear(p):
                return p
    return p


__all__ = ["FigureBoard", "figure_board", "figure_targets", "op_actions", "targets_for", "LABEL_PX"]
