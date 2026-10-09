"""From a figure spec to a canonical model: run the constructions, name
the facts the spec refers to, and check the spec's claims against them.

Order matters and is fixed: constructions build (and may refuse), then
the spec's angle and segment ids are bound to facts, then GIVEN measures
are checked against the realisation (a given the figure does not have is
``given_not_realised``), then claimed relations are checked against the
facts graph (``relation_not_implied``). Closure rules run before the
relation check so a parallel derived by transitivity counts.

The policy decides the numbers a construction draws with (``resolve``);
the metric policy draws exact values, the schematic one draws jittered
ones and skips the given/relation checks, which are about truth and are
run on the metric model.
"""

from __future__ import annotations

import math
from typing import Callable, Optional

import sympy as sp

from maths.geometry.constructions import BuildContext, exact_value, run, exact_pair
from maths.geometry.errors import GeometryRefusal
from maths.geometry.model import (in_degrees, ANGLE_TOL_DEG, LENGTH_REL_TOL, Model, SegKey, angle_key, is_exact_number,
                                  seg_key, to_float)
from maths.geometry.spec import FigureSpec, RelationSpec

Resolver = Callable[[sp.Expr, str, bool, str], float]


def metric_resolver(bind: dict[str, sp.Expr]) -> Resolver:
    """Draw every measurement at its exact value."""
    def resolve(exact: sp.Expr, kind: str, free: bool, where: str) -> float:
        return to_float(exact, bind)
    return resolve


def parse_bind(spec: FigureSpec) -> dict[str, sp.Expr]:
    out = {}
    for k, v in spec.bind.items():
        e = in_degrees(exact_value(v, where=f"bind.{k}"))      # v4: bind o = 10*sin(40) means 40°
        if e.free_symbols:
            raise GeometryRefusal("bad_schema", f"bind.{k}: a bound value is a number", f"bind.{k}")
        out[k] = e
    return out


def compile_figure(spec: FigureSpec, resolve: Optional[Resolver] = None, *,
                   check_givens: bool = True, check_relations: bool = True) -> Model:
    m = Model()
    m.units = spec.units
    m.bind = parse_bind(spec)
    if resolve is None:
        resolve = metric_resolver(m.bind)
    ctx = BuildContext(m, spec, resolve, labels={p.id: p.label for p in spec.points if p.label},
                       strict=check_givens)
    # angle ids are bound to their keys BEFORE building: a construction may
    # take an angle by id (angle_bisector of angle_avb, B15) once an earlier
    # construction has made it a fact
    for a in spec.angles:
        if a.id in m.angle_ids:
            raise GeometryRefusal("bad_reference", f"angle {a.id!r} is defined twice", a.id)
        m.angle_ids[a.id] = angle_key(a.vertex, a.arms[0], a.arms[1], a.region)
    for obj in spec.objects:
        run(ctx, obj)
    for p in spec.points:
        if not m.has_point(p.id):
            raise GeometryRefusal("bad_reference", f"point {p.id!r} is listed but no construction places it", p.id)
    _implicit_angle_ids(m)
    for a in spec.angles:
        for pid in (a.vertex, *a.arms):
            if not m.has_point(pid):
                raise GeometryRefusal("bad_reference", f"angle {a.id!r} uses point {pid!r}, which does not exist", a.id)
        m.angle(a.vertex, a.arms[0], a.arms[1], a.region)
    for s in spec.segments:
        for pid in s.points:
            if not m.has_point(pid):
                raise GeometryRefusal("bad_reference", f"segment {s.id!r} uses point {pid!r}, which does not exist", s.id)
        if s.id in m.segment_ids:
            raise GeometryRefusal("bad_reference", f"segment {s.id!r} is defined twice", s.id)
        sg = m.segment(s.points[0], s.points[1])
        m.segment_ids[s.id] = sg.key
        m.object_ids[s.id] = ("segment", s.id)
        sg.hidden = False          # named by the question: drawn
    for mk in spec.marks:
        if mk.id:
            m.object_ids[mk.id] = ("mark", mk.id)   # v4: a compass arc a step reveals
    for ms in spec.measures:
        if ms.target in m.points:
            # v2: a point's coordinates, given "(3, -2)" or asked "(a, b)"
            exact_pair(ms.value, where=f"measure.{ms.target}")
            continue
        if ms.target in m.solids:
            # v3: the solid's volume or surface area, given (a number the
            # chain reads as V or S); never realised — a picture is a view
            if ms.kind not in ("volume", "surface_area"):
                raise GeometryRefusal("bad_schema", f"measure of solid {ms.target!r}: say kind: volume or surface_area", ms.target)
            if ms.role != "given":
                raise GeometryRefusal("bad_schema", f"measure of solid {ms.target!r}: a volume is given; the unknown is an edge", ms.target)
            exact_value(ms.value, where=f"measure.{ms.target}")
            continue
        if ms.target not in m.angle_ids and ms.target not in m.segment_ids:
            raise GeometryRefusal("bad_reference", f"measure of {ms.target!r}: no such angle, segment or point", ms.target)
        exact_value(ms.value, where=f"measure.{ms.target}")
    m.close()
    if check_givens:
        check_given_measures(m, spec)
    if check_relations:
        for r in spec.relations:
            check_relation(m, r)
    if spec.orientation:
        if m.axes is not None:
            raise GeometryRefusal("bad_schema", "a coordinate figure is not rotated: the axes fix its orientation")
        if m.solids or m.nets:
            raise GeometryRefusal("bad_schema", "a solid is not rotated: one projection draws every solid")
        _rotate(m, spec.orientation)
    return m


def _label_of(m: Model, pid: str) -> str:
    lab = (m.points[pid].label or pid.removeprefix("p_")).lower()
    return "".join(ch for ch in lab if ch.isalnum()) or pid


def _implicit_angle_ids(m: Model) -> None:
    """Every interior angle of a closed shape gets ids from its vertex
    labels — angle_bac, angle_cab and the short angle_a — unless the spec
    already names that angle. A step may cite them and a measure target
    them without the model declaring an `angles` entry; the proof sees
    one symbol per angle through Model.canonical_angle_id."""
    for pg in m.polygons.values():
        if not pg.closed:
            continue
        L = len(pg.vertices)
        for i, v in enumerate(pg.vertices):
            pv, nv = pg.vertices[i - 1], pg.vertices[(i + 1) % L]
            k = angle_key(v, pv, nv)
            m.angle(v, pv, nv)
            lp, lv, ln_ = _label_of(m, pv), _label_of(m, v), _label_of(m, nv)
            for name in (f"angle_{lp}{lv}{ln_}", f"angle_{ln_}{lv}{lp}", f"angle_{lv}"):
                if name not in m.angle_ids:
                    m.angle_ids[name] = k


def _rotate(m: Model, deg: float) -> None:
    x0, y0, x1, y1 = m.bbox()
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))

    def fn(x, y):
        dx, dy = x - cx, y - cy
        return (cx + dx * c - dy * s, cy + dx * s + dy * c)

    m.transform(fn)


# ── measures ──────────────────────────────────────────────────────────────

def measure_exact(spec: FigureSpec, target: str) -> Optional[sp.Expr]:
    ms = spec.measure(target)
    return exact_value(ms.value, where=f"measure.{target}") if ms is not None else None


def realised(m: Model, target: str) -> float:
    """The drawn value of an angle (degrees) or segment (length)."""
    if target in m.angle_ids:
        return m.angle_float(m.angle_ids[target])
    if target in m.segment_ids:
        return m.length_float(m.segment_ids[target])
    raise GeometryRefusal("bad_reference", f"{target!r} is not an angle or segment", target)


def check_given_measures(m: Model, spec: FigureSpec) -> None:
    for ms in spec.measures:
        if ms.role != "given" or ms.target in m.solids:
            continue
        if ms.target in m.points:
            ex, ey = exact_pair(ms.value, where=f"measure.{ms.target}")
            wx, wy = to_float(ex, m.bind), to_float(ey, m.bind)
            hx, hy = m.xy(ms.target)
            if abs(hx - wx) > LENGTH_REL_TOL * max(1.0, abs(wx)) or abs(hy - wy) > LENGTH_REL_TOL * max(1.0, abs(wy)):
                raise GeometryRefusal("given_not_realised",
                                      f"{ms.target} is given at ({wx:g}, {wy:g}) but the figure places it at ({hx:.6g}, {hy:.6g})",
                                      ms.target)
            pt = m.points[ms.target]
            if pt.exact is None:
                pt.exact = (ex, ey)
            continue
        e = exact_value(ms.value, where=f"measure.{ms.target}")
        want = to_float(e, m.bind)
        have = realised(m, ms.target)
        if ms.target in m.angle_ids:
            if abs(have - want) > ANGLE_TOL_DEG:
                # six significant digits: a 59.996° built from rounded sides
                # must not read as "60° but the figure has 60°"
                raise GeometryRefusal("given_not_realised",
                                      f"{ms.target} is given as {ms.value} = {want:g}° but the figure has {have:.6g}°",
                                      ms.target)
            an = m.angles[m.angle_ids[ms.target]]
            if an.exact is None:
                an.exact = e
        else:
            if abs(have - want) > LENGTH_REL_TOL * max(1.0, abs(want)):
                raise GeometryRefusal("given_not_realised",
                                      f"{ms.target} is given as {ms.value} = {want:g} but the figure has {have:.6g}",
                                      ms.target)
            sg = m.segments[m.segment_ids[ms.target]]
            if sg.exact is None:
                sg.exact = e


# ── relations ─────────────────────────────────────────────────────────────

_RELATION_ALIASES = {
    "equal_segments": "equal_length", "equal_sides": "equal_length", "equal_lengths": "equal_length",
    "same_length": "equal_length", "congruent_segments": "equal_length",
    "equal_angles": "equal_angle", "same_angle": "equal_angle", "congruent_angles": "equal_angle",
    "parallel_lines": "parallel", "perpendicular_lines": "perpendicular", "straight_line": "collinear",
    "right_angled": "right_angle",
}

def _seg_ref(m: Model, ref, where: str) -> SegKey:
    if isinstance(ref, str):
        if ref in m.segment_ids:
            return m.segment_ids[ref]
        raise GeometryRefusal("bad_reference", f"{where}: segment {ref!r} is not defined", where)
    if isinstance(ref, list) and len(ref) == 2 and all(isinstance(x, str) for x in ref):
        for pid in ref:
            if not m.has_point(pid):
                raise GeometryRefusal("bad_reference", f"{where}: point {pid!r} does not exist", where)
        k = seg_key(*ref)
        m.segment(ref[0], ref[1])
        return k
    raise GeometryRefusal("bad_schema", f"{where}: a segment is an id or [point, point]", where)


def _line_ref(m: Model, ref, where: str) -> str:
    """The id of the line fact a reference names: a line id, or a segment
    (id or point pair) that lies on a known line."""
    if isinstance(ref, str) and ref in m.lines:
        return ref
    k = _seg_ref(m, ref, where)
    a, b = sorted(k)
    ln = m.line_through(a, b)
    if ln is None:
        # a segment with no line fact: give it one (two points make a line)
        ln = m.add_line(m.new_point_id("line_"), [a, b], hidden=True)
    return ln.id


def _float_parallel(m: Model, l1: str, l2: str) -> bool:
    from maths.geometry.constructions import _cross, _line_direction  # noqa: PLC0415
    return abs(_cross(_line_direction(m, l1), _line_direction(m, l2))) < 1e-9


def _float_perpendicular(m: Model, l1: str, l2: str) -> bool:
    from maths.geometry.constructions import _line_direction  # noqa: PLC0415
    d1, d2 = _line_direction(m, l1), _line_direction(m, l2)
    return abs(d1[0] * d2[0] + d1[1] * d2[1]) < 1e-9


def check_relation(m: Model, r: RelationSpec) -> None:
    """A claim holds when the facts imply it AND the realisation agrees;
    the first failure is the refusal."""
    where = r.id or r.kind
    extra = r.model_extra or {}
    # a model's synonyms for the relation kinds (equal_segments for
    # equal_length, 2026-10-07): the registry's name is the one checked
    kind = _RELATION_ALIASES.get(r.kind, r.kind)

    def fail(msg: str):
        raise GeometryRefusal("relation_not_implied", f"{where} ({kind}): {msg}", where)

    if kind == "collinear":
        pts = extra.get("points")
        if not isinstance(pts, list) or len(pts) < 3:
            raise GeometryRefusal("bad_schema", f"{where}: collinear needs at least three points", where)
        if not m.collinear(pts):
            fail("no construction put these points on one line")
        from maths.geometry.constructions import _cross, _sub  # noqa: PLC0415
        p0 = m.xy(pts[0])
        d = _sub(m.xy(pts[1]), p0)
        for p in pts[2:]:
            if abs(_cross(d, _sub(m.xy(p), p0))) > 1e-9 * max(1.0, math.hypot(*d)):
                fail("the drawn points are not on one line")
    elif kind in ("parallel", "perpendicular"):
        refs = extra.get("lines") or extra.get("segments")
        if not isinstance(refs, list) or len(refs) != 2:
            raise GeometryRefusal("bad_schema", f"{where}: {kind} needs two lines or two segments", where)
        l1, l2 = (_line_ref(m, x, where) for x in refs)
        if kind == "parallel":
            if not m.are_parallel(l1, l2):
                fail(f"no construction makes {refs[0]} parallel to {refs[1]}")
            if not _float_parallel(m, l1, l2):
                fail("the drawn lines are not parallel")
        else:
            if not m.are_perpendicular(l1, l2):
                fail(f"no construction makes {refs[0]} perpendicular to {refs[1]}")
            if not _float_perpendicular(m, l1, l2):
                fail("the drawn lines are not perpendicular")
    elif kind == "equal_length":
        refs = extra.get("segments")
        if not isinstance(refs, list) or len(refs) != 2:
            raise GeometryRefusal("bad_schema", f"{where}: equal_length needs two segments", where)
        k1, k2 = (_seg_ref(m, x, where) for x in refs)
        if not m.lengths_equal(k1, k2):
            fail(f"no construction makes {refs[0]} equal to {refs[1]}")
        a, b = m.length_float(k1), m.length_float(k2)
        if abs(a - b) > LENGTH_REL_TOL * max(1.0, a, b):
            fail(f"the drawn lengths differ ({a:.4g} and {b:.4g})")
    elif kind == "equal_angle":
        refs = extra.get("angles")
        if not isinstance(refs, list) or len(refs) != 2:
            raise GeometryRefusal("bad_schema", f"{where}: equal_angle needs two angle ids", where)
        k1, k2 = (m.angle_by_id(x).key for x in refs)
        if not m.angles_equal(k1, k2):
            fail(f"no construction makes {refs[0]} equal to {refs[1]}")
        if abs(m.angle_float(k1) - m.angle_float(k2)) > ANGLE_TOL_DEG:
            fail("the drawn angles differ")
    elif kind in ("midpoint", "on_segment"):
        p, seg = extra.get("point"), extra.get("segment")
        k = _seg_ref(m, seg, where)
        a, b = sorted(k)
        if not isinstance(p, str) or not m.has_point(p):
            raise GeometryRefusal("bad_reference", f"{where}: point {p!r} does not exist", where)
        if not m.between(a, p, b):
            fail(f"{p} is not between {a} and {b} on a known line")
        if kind == "midpoint" and not m.lengths_equal(seg_key(a, p), seg_key(p, b)):
            fail(f"{p} is on the segment but not at its middle")
    elif kind == "on_circle":
        p, cid = extra.get("point"), extra.get("circle")
        c = m.circles.get(cid)
        if c is None or not isinstance(p, str) or not m.has_point(p):
            raise GeometryRefusal("bad_reference", f"{where}: unknown point or circle", where)
        sg = m.segments.get(seg_key(c.centre, p))
        if sg is None or (sg.exact is None and not any(seg_key(c.centre, p) in pair for pair in m.equal_lengths)):
            fail(f"no construction put {p} on {cid}")
        if abs(m.length_float(seg_key(c.centre, p)) - c.radius) > LENGTH_REL_TOL * max(1.0, c.radius):
            fail(f"{p} is drawn off the circle")
    elif kind == "right_angle":
        an = m.angle_by_id(extra.get("angle"))
        if an.exact is None or sp.simplify(an.exact - 90) != 0:
            fail("no construction makes this angle a right angle")
    elif kind in ("angle_value", "length_value"):
        tid = extra.get("angle") if kind == "angle_value" else extra.get("segment")
        val = exact_value(extra.get("value"), where=where)
        obj = m.angle_by_id(tid) if kind == "angle_value" else m.segment_by_id(tid)
        if obj.exact is None or sp.simplify(obj.exact - val) != 0:
            have = obj.exact if obj.exact is not None else "nothing exact"
            fail(f"the construction gives {have}, not {val}")
    else:
        raise GeometryRefusal("bad_schema", f"{where}: {kind!r} is not a relation v1 knows", where)
