"""The closed construction library: every way a figure can be built.

A construction is DETERMINED by its inputs — no solver, no search, no
randomness. It places points (the realisation) and registers what it
guarantees (the facts): the line a `line_through` makes, the parallel a
`parallel_through` makes, the equal legs an isosceles triangle has, the
exact size of every angle it fixes. A construction that has no figure
(angles summing past 180°) refuses with ``construction_impossible``; one
with more than one figure (two sides and a non-included angle) refuses
with ``construction_ambiguous``. It never picks.

Numbers reach a construction through ``ctx.num``/``ctx.angle_num``, so the
render policy (maths.geometry.realise) can hand a schematic figure jittered
values while the facts — registered from the EXACT expressions — stay the
truth. A construction asks for its free measurements with ``free=True``;
anything derived by closure is computed, never jittered.

Conventions: angles in degrees, counter-clockwise positive (y up; the
renderer flips). ``side: "left"`` turns counter-clockwise from the base
ray, ``"right"`` clockwise. The first object of a figure starts at the
origin heading +x; later objects anchor on points that already exist.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import sympy as sp

from maths.geometry.errors import GeometryRefusal
from maths.geometry.model import Circle, GridPattern, Model, angle_key, seg_key
from maths.geometry.spec import FigureSpec
from maths.notation import NotationError, parse_relation

DEFAULT_LEN = 3.5          # a ray, an extension, a line's point spacing
LINE_GAP = 3.0             # spacing of named points along a line
PARALLEL_GAP = 2.5         # distance between a line and a parallel through nothing in particular
FREE_GAP = 2.0             # gap before an object that anchors on no existing point
_MIN_ANGLE = 0.5           # a construction angle below this is degenerate
_EPS = 1e-9

Vec = tuple[float, float]


# ── values ────────────────────────────────────────────────────────────────

def exact_value(v: Any, *, where: str) -> sp.Expr:
    """A parameter as an exact expression: an int, a float (as the
    rational it prints as), or notation (``3x + 10``)."""
    if isinstance(v, bool):
        raise GeometryRefusal("bad_schema", f"{where}: a boolean is not a measurement", where)
    if isinstance(v, int):
        return sp.Integer(v)
    if isinstance(v, float):
        return sp.nsimplify(sp.Float(v), rational=True)
    if isinstance(v, str):
        try:
            rel = parse_relation(v)
        except NotationError as exc:
            raise GeometryRefusal("bad_schema", f"{where}: {exc}", where) from exc
        if not rel.is_expression:
            raise GeometryRefusal("bad_schema", f"{where}: {v!r} is not a single expression", where)
        return rel.lhs
    raise GeometryRefusal("bad_schema", f"{where}: {v!r} is not a measurement", where)


@dataclass
class BuildContext:
    """What a construction may read: the model it adds to, the spec it
    came from, and the policy's number resolver."""
    model: Model
    spec: FigureSpec
    resolve: Callable[[sp.Expr, str, bool, str], float]   # (exact, kind, free, where) -> float
    labels: dict[str, str] = field(default_factory=dict)

    def num(self, v: Any, kind: str, *, where: str, free: bool = True) -> float:
        e = exact_value(v, where=where)
        return self.resolve(e, kind, free, where)

    def exact(self, v: Any, *, where: str) -> sp.Expr:
        return exact_value(v, where=where)

    def angle_measure(self, angle_id: str, *, where: str) -> tuple[sp.Expr, float, str]:
        """(exact, float, region) of a spec angle used as a construction
        input. The float honours the policy (a schematic figure may draw
        70° at 55°); the exact value is the fact."""
        a = self.spec.angle(angle_id)
        if a is None:
            raise GeometryRefusal("bad_reference", f"{where}: angle {angle_id!r} is not defined", where)
        m = self.spec.measure(angle_id)
        if m is None:
            raise GeometryRefusal("construction_impossible",
                                  f"{where}: angle {angle_id!r} has no measure to build from", where)
        e = exact_value(m.value, where=f"{where}.{angle_id}")
        return e, self.resolve(e, "angle", True, where), a.region

    def place(self, pid: str, x: float, y: float):
        return self.model.place(pid, x, y, self.labels.get(pid))

    def free_origin(self) -> Vec:
        if not self.model.points and not self.model.grids:
            return (0.0, 0.0)
        x0, y0, x1, y1 = self.model.bbox()
        return (x1 + FREE_GAP, y0)


# ── small geometry ────────────────────────────────────────────────────────

def _add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1])


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1])


def _mul(a: Vec, k: float) -> Vec:
    return (a[0] * k, a[1] * k)


def _norm(a: Vec) -> float:
    return math.hypot(a[0], a[1])


def _unit(a: Vec) -> Vec:
    n = _norm(a)
    if n < _EPS:
        raise GeometryRefusal("construction_impossible", "two points coincide")
    return (a[0] / n, a[1] / n)


def _rot(a: Vec, deg: float) -> Vec:
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return (a[0] * c - a[1] * s, a[0] * s + a[1] * c)


def _perp(a: Vec) -> Vec:
    return (-a[1], a[0])


def _cross(a: Vec, b: Vec) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _req(obj: dict, key: str, where: str):
    if key not in obj:
        raise GeometryRefusal("bad_schema", f"{where}: `{key}` is required", where)
    return obj[key]


def _ids(v, n: Optional[int], where: str, what: str) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise GeometryRefusal("bad_schema", f"{where}: `{what}` is a list of point ids", where)
    if n is not None and len(v) != n:
        raise GeometryRefusal("bad_schema", f"{where}: `{what}` needs exactly {n} ids", where)
    if len(set(v)) != len(v):
        raise GeometryRefusal("bad_schema", f"{where}: `{what}` repeats an id", where)
    return v


def _where(obj: dict) -> str:
    return str(obj.get("id") or obj.get("make"))


def _direction_deg(v: Vec) -> float:
    return math.degrees(math.atan2(v[1], v[0]))


# ── line helpers ──────────────────────────────────────────────────────────

def _place_along(ctx: BuildContext, origin: Vec, direction: Vec, pids: list[str], *, gap: float = LINE_GAP) -> None:
    """Points on the line origin + t·direction. Existing ones fix their
    parameter t (and must lie on the line); missing ones are spaced by
    order — between two known, evenly; outside, `gap` apart."""
    m = ctx.model
    d = _unit(direction)
    t_known: dict[str, float] = {}
    for pid in pids:
        if m.has_point(pid):
            rel = _sub(m.xy(pid), origin)
            off = abs(_cross(d, rel))
            if off > 1e-7 * max(1.0, _norm(rel)):
                raise GeometryRefusal("construction_impossible", f"point {pid!r} is not on this line", pid)
            t_known[pid] = rel[0] * d[0] + rel[1] * d[1]
    known_idx = [i for i, p in enumerate(pids) if p in t_known]
    for i in range(1, len(known_idx)):
        if t_known[pids[known_idx[i]]] <= t_known[pids[known_idx[i - 1]]]:
            raise GeometryRefusal("construction_impossible",
                                  f"points {pids[known_idx[i - 1]]!r}, {pids[known_idx[i]]!r} are not in that order on the line")
    t: dict[str, float] = dict(t_known)
    if not known_idx:
        for i, p in enumerate(pids):
            t[p] = i * gap
    else:
        first, last = known_idx[0], known_idx[-1]
        for i in range(first - 1, -1, -1):
            t[pids[i]] = t[pids[i + 1]] - gap
        for i in range(last + 1, len(pids)):
            t[pids[i]] = t[pids[i - 1]] + gap
        for a, b in zip(known_idx, known_idx[1:]):
            n = b - a
            for k in range(1, n):
                t[pids[a + k]] = t[pids[a]] + (t[pids[b]] - t[pids[a]]) * k / n
    for pid in pids:
        if not m.has_point(pid):
            x, y = _add(origin, _mul(d, t[pid]))
            ctx.place(pid, x, y)


def _line_direction(m: Model, lid: str) -> Vec:
    ln = m.lines.get(lid)
    if ln is None:
        raise GeometryRefusal("bad_reference", f"line {lid!r} is not defined", lid)
    return _unit(_sub(m.xy(ln.points[-1]), m.xy(ln.points[0])))


def _order_on_line(m: Model, pids: list[str]) -> list[str]:
    """Collinear points sorted along their common direction."""
    if len(pids) < 2:
        return list(pids)
    origin = m.xy(pids[0])
    far = max(pids[1:], key=lambda p: math.dist(origin, m.xy(p)))
    d = _unit(_sub(m.xy(far), origin))
    return sorted(pids, key=lambda p: (m.xy(p)[0] - origin[0]) * d[0] + (m.xy(p)[1] - origin[1]) * d[1])


def _extend_line_fact(m: Model, through: list[str], new_pid: str, lid: Optional[str] = None, hidden: bool = False) -> None:
    """The new point joins the line fact these points share, re-ordered
    along the line; or a new line fact is made of them."""
    ln = m.line_through(*through)
    if ln is not None:
        if new_pid not in ln.points:
            ln.points = _order_on_line(m, ln.points + [new_pid])
        return
    m.add_line(lid or m.new_point_id("line_"), _order_on_line(m, list(through) + [new_pid]), hidden)


def _supplement_angles_at(m: Model, v: str, new_arm: str) -> None:
    """A new ray from V on a known line: the angle it makes with one side
    of the line fixes the angle with the other (angles on a line), and a
    ray extended through V fixes the vertically opposite angle. Facts the
    construction guarantees; the proof must still cite the theorem."""
    for ln in m.lines_through(v):
        i = ln.points.index(v)
        left, right = ln.points[:i], ln.points[i + 1:]
        if not left or not right:
            continue
        for a in left:
            for b in right:
                ka, kb = angle_key(v, a, new_arm), angle_key(v, b, new_arm)
                ea = m.angles[ka].exact if ka in m.angles else None
                eb = m.angles[kb].exact if kb in m.angles else None
                if ea is not None and eb is None:
                    m.angle(v, b, new_arm, exact=180 - ea)
                elif eb is not None and ea is None:
                    m.angle(v, a, new_arm, exact=180 - eb)


# ── constructions ─────────────────────────────────────────────────────────

def c_segment(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    a, b = _ids(_req(obj, "points", w), 2, w, "points")
    m = ctx.model
    exact = ctx.exact(obj["length"], where=w) if "length" in obj else None
    if m.has_point(a) and m.has_point(b):
        pass
    elif m.has_point(a) or m.has_point(b):
        known, new = (a, b) if m.has_point(a) else (b, a)
        length = ctx.num(obj["length"], "length", where=w) if "length" in obj else DEFAULT_LEN
        x, y = m.xy(known)
        ctx.place(new, x + length, y)
    else:
        length = ctx.num(obj["length"], "length", where=w) if "length" in obj else DEFAULT_LEN
        ox, oy = ctx.free_origin()
        ctx.place(a, ox, oy)
        ctx.place(b, ox + length, oy)
    s = m.segment(a, b, exact)
    if "id" in obj:
        m.segment_ids[obj["id"]] = s.key
        m.object_ids[obj["id"]] = ("segment", obj["id"])


def c_line_through(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    pids = _ids(_req(obj, "points", w), None, w, "points")
    if len(pids) < 2:
        raise GeometryRefusal("bad_schema", f"{w}: a line needs at least two points", w)
    m = ctx.model
    known = [p for p in pids if m.has_point(p)]
    if len(known) >= 2:
        origin, direction = m.xy(known[0]), _sub(m.xy(known[1]), m.xy(known[0]))
    elif len(known) == 1:
        origin, direction = m.xy(known[0]), (1.0, 0.0)
    else:
        origin, direction = ctx.free_origin(), (1.0, 0.0)
    _place_along(ctx, origin, direction, pids)
    m.add_line(obj.get("id") or m.new_point_id("line_"), pids, bool(obj.get("hidden")))
    for i in range(len(pids) - 1):
        m.segment(pids[i], pids[i + 1])


def c_ray(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    v, p = _req(obj, "vertex", w), _req(obj, "through", w)
    m = ctx.model
    if not m.has_point(v):
        raise GeometryRefusal("bad_reference", f"{w}: vertex {v!r} does not exist", w)
    if not m.has_point(p):
        x, y = m.xy(v)
        ctx.place(p, x + DEFAULT_LEN, y)
    m.add_ray(obj.get("id") or m.new_point_id("ray_"), v, p)


def c_extend(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    a, b = _ids(_req(obj, "segment", w), 2, w, "segment")
    beyond, to = _req(obj, "beyond", w), _req(obj, "to", w)
    if beyond not in (a, b):
        raise GeometryRefusal("bad_schema", f"{w}: `beyond` must be one end of the segment", w)
    m = ctx.model
    tail = a if beyond == b else b
    length = ctx.num(obj["length"], "length", where=w) if "length" in obj else DEFAULT_LEN
    d = _unit(_sub(m.xy(beyond), m.xy(tail)))
    x, y = _add(m.xy(beyond), _mul(d, length))
    ctx.place(to, x, y)
    _extend_line_fact(m, [tail, beyond], to, obj.get("id"))
    m.segment(beyond, to)
    # the ray through `beyond` continues: angles on one side fix the other
    # side, and vertically opposite pairs are equal
    for key, an in list(m.angles.items()):
        if key[0] != beyond or key[2] != "interior" or tail not in key[1]:
            continue
        (other,) = key[1] - {tail}
        if other == to:
            continue
        m.angle(beyond, other, to, exact=(180 - an.exact) if an.exact is not None else None)
    for ln in m.lines_through(beyond):
        if tail in ln.points:
            continue
        i = ln.points.index(beyond)
        for p in ln.points[:i]:
            for q in ln.points[i + 1:]:
                k1, k2 = angle_key(beyond, p, tail), angle_key(beyond, q, to)
                e = m.angles[k1].exact if k1 in m.angles else None
                m.angle(beyond, q, to, exact=e)
                m.set_equal_angles(k1, k2)
                k3, k4 = angle_key(beyond, q, tail), angle_key(beyond, p, to)
                e2 = m.angles[k3].exact if k3 in m.angles else None
                m.angle(beyond, p, to, exact=e2)
                m.set_equal_angles(k3, k4)


def c_point_on_segment(ctx: BuildContext, obj: dict, ratio: Optional[float] = None) -> None:
    w = _where(obj)
    a, b = _ids(_req(obj, "segment", w), 2, w, "segment")
    pid = _req(obj, "id", w)
    m = ctx.model
    if ratio is None:
        ratio = ctx.num(_req(obj, "ratio", w), "ratio", where=w, free=False)
    if not (0.0 < ratio < 1.0):
        raise GeometryRefusal("construction_impossible", f"{w}: ratio must be strictly between 0 and 1", w)
    ax, ay = m.xy(a)
    bx, by = m.xy(b)
    ctx.place(pid, ax + (bx - ax) * ratio, ay + (by - ay) * ratio)
    _extend_line_fact(m, [a, b], pid)
    whole = m.segments.get(seg_key(a, b))
    e = whole.exact if whole else None
    r = sp.Rational(str(ratio)) if ratio == 0.5 else None
    m.segment(a, pid, (e * r) if (e is not None and r is not None) else None)
    m.segment(pid, b, (e * (1 - r)) if (e is not None and r is not None) else None)
    m.angle(pid, a, b, exact=sp.Integer(180))


def c_midpoint(ctx: BuildContext, obj: dict) -> None:
    c_point_on_segment(ctx, obj, ratio=0.5)
    a, b = obj["segment"]
    ctx.model.set_equal_lengths(seg_key(a, obj["id"]), seg_key(obj["id"], b))


def _carrier(m: Model, oid: str) -> tuple[Vec, Vec, bool, str, str]:
    """(origin, direction, is_ray, vertex, through) of a line or ray."""
    kind, key = m.object_ids.get(oid, (None, None))
    if kind == "line":
        ln = m.lines[key]
        return m.xy(ln.points[0]), _sub(m.xy(ln.points[-1]), m.xy(ln.points[0])), False, ln.points[0], ln.points[-1]
    if kind == "ray":
        r = m.rays[key]
        return m.xy(r.vertex), _sub(m.xy(r.through), m.xy(r.vertex)), True, r.vertex, r.through
    raise GeometryRefusal("bad_reference", f"{oid!r} is not a line or ray", oid)


def c_intersection(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    pid = _req(obj, "id", w)
    o1, o2 = _ids(_req(obj, "of", w), 2, w, "of")
    m = ctx.model
    p, d1, ray1, v1, t1 = _carrier(m, o1)
    q, d2, ray2, v2, t2 = _carrier(m, o2)
    den = _cross(d1, d2)
    if abs(den) < 1e-9 * max(1.0, _norm(d1) * _norm(d2)):
        raise GeometryRefusal("construction_impossible", f"{w}: {o1!r} and {o2!r} are parallel", w)
    pq = _sub(q, p)
    s = _cross(pq, d2) / den
    t = _cross(pq, d1) / den
    if (ray1 and s < -1e-9) or (ray2 and t < -1e-9):
        raise GeometryRefusal("construction_impossible", f"{w}: the rays {o1!r} and {o2!r} do not meet", w)
    x, y = _add(p, _mul(d1, s))
    ctx.place(pid, x, y)
    for (oid, vtx, thr, is_ray) in ((o1, v1, t1, ray1), (o2, v2, t2, ray2)):
        if is_ray:
            # the new point is on the ray: every angle at its vertex that
            # used the ray's defining point holds for the new point too
            for key, an in list(m.angles.items()):
                if key[0] == vtx and thr in key[1]:
                    (other,) = key[1] - {thr}
                    if other != pid:
                        m.angle(vtx, other, pid, key[2], exact=an.exact)
            _extend_line_fact(m, [vtx, thr], pid, hidden=True)
            m.segment(vtx, pid)
        else:
            _extend_line_fact(m, [vtx, thr], pid)


def c_ray_at_angle(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    v = _req(obj, "vertex", w)
    fv, fa = _ids(_req(obj, "from_ray", w), 2, w, "from_ray")
    if fv != v:
        raise GeometryRefusal("bad_schema", f"{w}: `from_ray` must start at the vertex", w)
    side = obj.get("side", "left")
    if side not in ("left", "right"):
        raise GeometryRefusal("bad_schema", f"{w}: side is 'left' or 'right'", w)
    m = ctx.model
    if not (m.has_point(v) and m.has_point(fa)):
        raise GeometryRefusal("bad_reference", f"{w}: the base ray's points must exist first", w)
    exact, theta, region = ctx.angle_measure(_req(obj, "angle", w), where=w)
    turn = theta if region == "interior" else 360.0 - theta
    if not (_MIN_ANGLE <= turn <= 360.0 - _MIN_ANGLE):
        raise GeometryRefusal("construction_impossible", f"{w}: an angle of {theta:g}° cannot be drawn", w)
    length = ctx.num(obj["length"], "length", where=w) if "length" in obj else DEFAULT_LEN
    d = _rot(_unit(_sub(m.xy(fa), m.xy(v))), turn if side == "left" else -turn)
    to = obj.get("to") or m.new_point_id("r")
    if m.has_point(to):
        raise GeometryRefusal("bad_reference", f"{w}: {to!r} already exists", w)
    x, y = _add(m.xy(v), _mul(d, length))
    ctx.place(to, x, y)
    m.add_ray(obj.get("id") or m.new_point_id("ray_"), v, to)
    m.segment(v, to)
    m.angle(v, fa, to, region, exact=exact)
    _supplement_angles_at(m, v, to)


def c_angles_at_point(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    v = _req(obj, "vertex", w)
    pts = _ids(_req(obj, "points", w), None, w, "points")
    if len(pts) < 2:
        raise GeometryRefusal("bad_schema", f"{w}: at least two rays", w)
    m = ctx.model
    if not m.has_point(v):
        ox, oy = ctx.free_origin()
        ctx.place(v, ox + DEFAULT_LEN, oy)
    n = len(pts)
    exacts: list[Optional[sp.Expr]] = []
    floats: list[Optional[float]] = []
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        spec_angle = next((s for s in ctx.spec.angles if s.vertex == v and set(s.arms) == {a, b}), None)
        if spec_angle is not None and ctx.spec.measure(spec_angle.id) is not None:
            e, f, region = ctx.angle_measure(spec_angle.id, where=w)
            if region == "reflex":
                e, f = 360 - e, 360.0 - f
            exacts.append(e)
            floats.append(f)
        else:
            exacts.append(None)
            floats.append(None)
    missing = [i for i, e in enumerate(exacts) if e is None]
    if len(missing) > 1:
        raise GeometryRefusal("construction_impossible",
                              f"{w}: all angles round the point but one need a measure", w)
    if missing:
        i = missing[0]
        exacts[i] = 360 - sum(e for e in exacts if e is not None)
        floats[i] = 360.0 - sum(f for f in floats if f is not None)
    else:
        total = sp.simplify(sum(exacts))
        total_f = sum(floats)
        if abs(total_f - 360.0) > 1e-6:
            raise GeometryRefusal("closure_failed",
                                  f"{w}: the angles round {v!r} add to {total} = {total_f:g}°, not 360°", w)
    if any(f <= 0 for f in floats):
        raise GeometryRefusal("construction_impossible", f"{w}: an angle round {v!r} is not positive", w)
    heading = 0.0
    vx, vy = m.xy(v)
    for i, p in enumerate(pts):
        if m.has_point(p):
            if i == 0:
                heading = _direction_deg(_sub(m.xy(p), (vx, vy)))
            continue
        d = _rot((1.0, 0.0), heading)
        ctx.place(p, vx + d[0] * DEFAULT_LEN, vy + d[1] * DEFAULT_LEN)
        if i + 1 < n:
            heading += floats[i]
        m.add_ray(f"ray_{v}_{p}", v, p)
        m.segment(v, p)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        f = floats[i]
        m.angle(v, a, b, "reflex" if f > 180 else "interior", exact=exacts[i])


def c_parallel_through(ctx: BuildContext, obj: dict, *, perpendicular: bool = False) -> None:
    w = _where(obj)
    base = _req(obj, "line", w)
    m = ctx.model
    if base not in m.lines:
        raise GeometryRefusal("bad_reference", f"{w}: line {base!r} is not defined", w)
    d = _line_direction(m, base)
    if perpendicular:
        d = _perp(d)
    pids = _ids(obj.get("points", []), None, w, "points")
    through = obj.get("point")
    if through is not None and through not in pids:
        pids = [through] + pids
    if through is not None and m.has_point(through):
        origin = m.xy(through)
    elif any(m.has_point(p) for p in pids):
        origin = m.xy(next(p for p in pids if m.has_point(p)))
    else:
        base_ln = m.lines[base]
        anchor = m.xy(base_ln.points[0])
        off = _perp(_line_direction(m, base))
        gap = ctx.num(obj["gap"], "length", where=w) if "gap" in obj else PARALLEL_GAP
        origin = _sub(anchor, _mul(off, gap)) if not perpendicular else anchor
        if perpendicular and through is None:
            raise GeometryRefusal("bad_schema", f"{w}: a perpendicular needs the point it passes through", w)
        if through is not None:
            ctx.place(through, *origin)
    if len(pids) < 2:
        # a line needs two points to be a line: add a direction point
        pids = pids + [m.new_point_id("l")]
    _place_along(ctx, origin, d, pids)
    lid = obj.get("id") or m.new_point_id("line_")
    m.add_line(lid, pids, bool(obj.get("hidden")))
    if perpendicular:
        m.set_perpendicular(lid, base)
        foot = through if (through is not None and through in m.lines[base].points) else None
        if foot is not None:
            i = m.lines[base].points.index(foot)
            for q in m.lines[base].points[:i] + m.lines[base].points[i + 1:]:
                for p in pids:
                    if p != foot:
                        m.angle(foot, q, p, exact=sp.Integer(90))
    else:
        m.set_parallel(lid, base)


def c_perpendicular_through(ctx: BuildContext, obj: dict) -> None:
    c_parallel_through(ctx, obj, perpendicular=True)


_P_ROLES = {"a": 0, "b": 1, "c": 2, "d": 3, "e": 4, "f": 5, "p": 6, "q": 7}


def _transversal_angles(m: Model, a, b, c, d, e, f, p, q, theta: sp.Expr) -> None:
    """The eight angles two parallels make with a transversal, as facts:
    theta is the angle EPB (upper-right at P)."""
    t = theta
    s = 180 - theta
    at_p = {(e, b): t, (e, a): s, (a, q): t, (b, q): s}
    at_q = {(p, d): t, (p, c): s, (c, f): t, (d, f): s}
    for (x, y), val in at_p.items():
        m.angle(p, x, y, exact=val)
    for (x, y), val in at_q.items():
        m.angle(q, x, y, exact=val)
    for group in (
        [angle_key(p, e, b), angle_key(p, a, q), angle_key(q, p, d), angle_key(q, c, f)],
        [angle_key(p, e, a), angle_key(p, b, q), angle_key(q, p, c), angle_key(q, d, f)],
    ):
        m.set_equal_angles(*group)


def c_parallels_transversal(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    pts = _ids(_req(obj, "points", w), 8, w, "points")
    a, b, c, d, e, f, p, q = pts
    m = ctx.model
    if any(m.has_point(x) for x in pts):
        raise GeometryRefusal("bad_schema", f"{w}: parallels_transversal builds all eight points itself", w)
    aid = _req(obj, "angle", w)
    spec_angle = ctx.spec.angle(aid)
    if spec_angle is None:
        raise GeometryRefusal("bad_reference", f"{w}: angle {aid!r} is not defined", w)
    exact, val, region = ctx.angle_measure(aid, where=w)
    if region != "interior":
        raise GeometryRefusal("construction_impossible", f"{w}: the defining angle must be interior", w)
    arms = set(spec_angle.arms)
    theta_roles = {frozenset((e, b)), frozenset((a, q)), frozenset((p, d)), frozenset((c, f))}
    supp_roles = {frozenset((e, a)), frozenset((b, q)), frozenset((p, c)), frozenset((d, f))}
    if spec_angle.vertex == p and frozenset(arms) in theta_roles or spec_angle.vertex == q and frozenset(arms) in theta_roles:
        theta_e, theta = exact, val
    elif frozenset(arms) in supp_roles and spec_angle.vertex in (p, q):
        theta_e, theta = 180 - exact, 180.0 - val
    else:
        raise GeometryRefusal("bad_schema", f"{w}: {aid!r} is not one of the eight angles of the configuration", w)
    if not (_MIN_ANGLE <= theta <= 180.0 - _MIN_ANGLE):
        raise GeometryRefusal("construction_impossible", f"{w}: a transversal at {val:g}° cannot be drawn", w)
    gap = ctx.num(obj["gap"], "length", where=w) if "gap" in obj else PARALLEL_GAP
    ox, oy = ctx.free_origin()
    px, py = ox + 1.5, oy + gap
    dvec = _rot((1.0, 0.0), theta)
    s = gap / dvec[1]
    qx, qy = px - s * dvec[0], oy
    reach = 2.0
    ctx.place(p, px, py)
    ctx.place(q, qx, qy)
    ctx.place(a, px - 3.0, py)
    ctx.place(b, px + 3.0, py)
    ctx.place(c, qx - 3.0, qy)
    ctx.place(d, qx + 3.0, qy)
    ctx.place(e, px + reach * dvec[0], py + reach * dvec[1])
    ctx.place(f, qx - reach * dvec[0], qy - reach * dvec[1])
    base = obj.get("id") or "cfg"
    m.add_line(f"{base}_l1", [a, p, b])
    m.add_line(f"{base}_l2", [c, q, d])
    m.add_line(f"{base}_t", [e, p, q, f])
    m.set_parallel(f"{base}_l1", f"{base}_l2")
    m.object_ids[base] = ("config", base)
    for x, y in ((a, b), (c, d), (e, p), (p, q), (q, f)):
        m.segment(x, y)
    _transversal_angles(m, a, b, c, d, e, f, p, q, theta_e)


def c_transversal(ctx: BuildContext, obj: dict) -> None:
    """A line through a point of one line, crossing a second: named
    E (before the first line), P (on it), Q (on the second), F (beyond)."""
    w = _where(obj)
    l1, l2 = _ids(_req(obj, "lines", w), 2, w, "lines")
    e, p, q, f = _ids(_req(obj, "points", w), 4, w, "points")
    m = ctx.model
    if l1 not in m.lines or not m.has_point(p) or p not in m.lines[l1].points:
        raise GeometryRefusal("bad_reference", f"{w}: {p!r} must already lie on {l1!r}", w)
    aid = _req(obj, "angle", w)
    spec_angle = ctx.spec.angle(aid)
    if spec_angle is None or spec_angle.vertex != p:
        raise GeometryRefusal("bad_reference", f"{w}: angle {aid!r} must be at {p!r}", w)
    exact, theta, region = ctx.angle_measure(aid, where=w)
    ln1 = m.lines[l1]
    i = ln1.points.index(p)
    arms = set(spec_angle.arms)
    base_pt = next((x for x in ln1.points if x in arms), None)
    if base_pt is None or (arms - {base_pt}) != {e}:
        raise GeometryRefusal("bad_schema", f"{w}: the angle is between a point of {l1!r} and {e!r}", w)
    base_dir = _unit(_sub(m.xy(base_pt), m.xy(p)))
    # E is on the side away from l2
    d2 = m.lines.get(l2)
    if d2 is None:
        raise GeometryRefusal("bad_reference", f"{w}: line {l2!r} is not defined", w)
    far = m.xy(d2.points[0])
    cand = [_rot(base_dir, theta), _rot(base_dir, -theta)]
    away = max(cand, key=lambda v: -(v[0] * (far[0] - m.xy(p)[0]) + v[1] * (far[1] - m.xy(p)[1])))
    ctx.place(e, *(_add(m.xy(p), _mul(away, 2.0))))
    # Q: where the line through P opposite to E meets l2
    origin2, dir2 = m.xy(d2.points[0]), _sub(m.xy(d2.points[-1]), m.xy(d2.points[0]))
    den = _cross(_mul(away, -1.0), dir2)
    if abs(den) < 1e-9:
        raise GeometryRefusal("construction_impossible", f"{w}: the transversal never meets {l2!r}", w)
    s = _cross(_sub(origin2, m.xy(p)), dir2) / den
    if s <= 0:
        raise GeometryRefusal("construction_impossible", f"{w}: {l2!r} is on the wrong side", w)
    qxy = _add(m.xy(p), _mul(away, -s))
    ctx.place(q, *qxy)
    ctx.place(f, *(_add(qxy, _mul(away, -2.0))))
    d2.points = _order_on_line(m, d2.points + [q])
    lid = obj.get("id") or m.new_point_id("line_")
    m.add_line(lid, [e, p, q, f])
    for x, y in ((e, p), (p, q), (q, f)):
        m.segment(x, y)
    m.angle(p, base_pt, e, exact=exact)
    _supplement_angles_at(m, p, e)
    if m.are_parallel(l1, l2):
        # the full eight-angle fact set, with roles read off the realisation
        left1 = [x for x in ln1.points if x != p and m.side_of_line(m.lines[lid], x) > 0]
        right1 = [x for x in ln1.points if x != p and m.side_of_line(m.lines[lid], x) < 0]
        left2 = [x for x in d2.points if x != q and m.side_of_line(m.lines[lid], x) > 0]
        right2 = [x for x in d2.points if x != q and m.side_of_line(m.lines[lid], x) < 0]
        if left1 and right1 and left2 and right2:
            b_like = right1[0] if base_pt in right1 else left1[0]
            a_like = left1[0] if b_like in right1 else right1[0]
            d_like = right2[0] if b_like in right1 else left2[0]
            c_like = left2[0] if b_like in right1 else right2[0]
            _transversal_angles(m, a_like, b_like, c_like, d_like, e, f, p, q, exact)


def c_angle_bisector(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    aid = _req(obj, "angle", w)
    m = ctx.model
    an = m.angle_by_id(aid)
    v = an.vertex
    a, b = an.arms
    f = m.angle_float(an.key)
    da, db = _unit(_sub(m.xy(a), m.xy(v))), _unit(_sub(m.xy(b), m.xy(v)))
    # bisector direction: rotate arm a halfway toward b, through the region
    sign = 1.0 if _cross(da, db) >= 0 else -1.0
    half = f / 2.0
    d = _rot(da, sign * half) if an.region == "interior" else _rot(da, -sign * half)
    to = obj.get("to") or m.new_point_id("bis")
    length = ctx.num(obj["length"], "length", where=w) if "length" in obj else DEFAULT_LEN * 0.8
    ctx.place(to, *(_add(m.xy(v), _mul(d, length))))
    m.add_ray(obj.get("id") or m.new_point_id("ray_"), v, to)
    m.segment(v, to)
    e = (an.exact / 2) if an.exact is not None else None
    m.angle(v, a, to, exact=e)
    m.angle(v, b, to, exact=e)
    m.set_equal_angles(angle_key(v, a, to), angle_key(v, b, to))


def c_perpendicular_bisector(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    a, b = _ids(_req(obj, "segment", w), 2, w, "segment")
    m = ctx.model
    mid = obj.get("midpoint") or m.new_point_id("m")
    c_midpoint(ctx, {"id": mid, "segment": [a, b]})
    base = m.line_through(a, b)
    if base is None:
        base = m.add_line(m.new_point_id("line_"), _order_on_line(m, [a, mid, b]), hidden=True)
    pids = _ids(obj.get("points", []), None, w, "points")
    c_parallel_through(ctx, {"id": obj.get("id"), "line": base.id, "point": mid, "points": pids,
                             "hidden": obj.get("hidden", False)}, perpendicular=True)


# ── polygons ──────────────────────────────────────────────────────────────

def _finish_polygon(ctx: BuildContext, obj: dict, coords: list[Vec], kind: str, *,
                    exact_sides: list[Optional[sp.Expr]], exact_angles: list[Optional[sp.Expr]],
                    closed: bool = True, regular: bool = False) -> list[str]:
    """Place the vertices (at the free origin unless named points already
    exist — then refuse, a polygon is built whole), register the shape,
    its sides and its angles."""
    w = _where(obj)
    m = ctx.model
    n = len(coords)
    vids = obj.get("vertices")
    if vids is not None:
        vids = _ids(vids, n, w, "vertices")
    else:
        vids = [m.new_point_id("v") for _ in range(n)]
    for v in vids:
        if m.has_point(v):
            raise GeometryRefusal("bad_reference", f"{w}: vertex {v!r} already exists; a shape is built whole", w)
    for i in range(n):
        if not closed and i == n - 1:
            break
        d = _sub(coords[(i + 1) % n], coords[i])
        if _norm(d) < 1e-9:
            raise GeometryRefusal("construction_impossible", f"{w}: two vertices coincide", w)
    ox, oy = ctx.free_origin()
    for v, (x, y) in zip(vids, coords):
        ctx.place(v, ox + x, oy + y)
    pid = obj.get("id") or m.new_point_id("shape_")
    m.add_polygon(pid, vids, kind, closed)
    for i in range(n if closed else n - 1):
        m.segment(vids[i], vids[(i + 1) % n], exact_sides[i] if i < len(exact_sides) else None)
    if closed:
        for i in range(n):
            prev_v, v, next_v = vids[i - 1], vids[i], vids[(i + 1) % n]
            e = exact_angles[i] if i < len(exact_angles) else None
            region = "interior"
            f = m.angle_float(angle_key(v, prev_v, next_v))
            # the interior angle at a reflex corner is the reflex one
            if e is not None and _is_number(e) and float(e) > 180.0:
                region = "reflex"
            elif e is None and _interior_is_reflex(coords, i):
                region = "reflex"
            m.angle(v, prev_v, next_v, region, exact=e)
        # equal sides / equal angles the construction guarantees
        keys = [seg_key(vids[i], vids[(i + 1) % n]) for i in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                ei, ej = exact_sides[i] if i < len(exact_sides) else None, exact_sides[j] if j < len(exact_sides) else None
                if ei is not None and ej is not None and sp.simplify(ei - ej) == 0:
                    m.set_equal_lengths(keys[i], keys[j])
        akeys = [angle_key(vids[i], vids[i - 1], vids[(i + 1) % n], m.angles[angle_key(vids[i], vids[i - 1], vids[(i + 1) % n])].region
                           if angle_key(vids[i], vids[i - 1], vids[(i + 1) % n]) in m.angles else "interior") for i in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                ei, ej = exact_angles[i] if i < len(exact_angles) else None, exact_angles[j] if j < len(exact_angles) else None
                if ei is not None and ej is not None and sp.simplify(ei - ej) == 0:
                    m.set_equal_angles(akeys[i], akeys[j])
        if regular:
            m.set_equal_lengths(*keys)
            m.set_equal_angles(*akeys)
    return vids


def _is_number(e: sp.Expr) -> bool:
    return not sp.sympify(e).free_symbols


def _interior_is_reflex(coords: list[Vec], i: int) -> bool:
    """For a simple polygon traversed counter-clockwise, a right turn at a
    vertex is a reflex interior angle."""
    n = len(coords)
    area2 = sum(_cross(coords[k], coords[(k + 1) % n]) for k in range(n))
    ccw = area2 > 0
    d1 = _sub(coords[i], coords[i - 1])
    d2 = _sub(coords[(i + 1) % n], coords[i])
    turn = _cross(d1, d2)
    return (turn < 0) if ccw else (turn > 0)


def _triangle_from_sides(ctx: BuildContext, obj: dict, ab: float, bc: float, ca: float, w: str) -> list[Vec]:
    for x, y, z in ((ab, bc, ca), (bc, ca, ab), (ca, ab, bc)):
        if x + y <= z + 1e-9:
            raise GeometryRefusal("construction_impossible",
                                  f"{w}: sides {ab:g}, {bc:g}, {ca:g} break the triangle inequality", w)
    cx = (ab * ab + ca * ca - bc * bc) / (2 * ab)
    cy = math.sqrt(max(0.0, ca * ca - cx * cx))
    return [(0.0, 0.0), (ab, 0.0), (cx, cy)]


def _angle_exact_from_sides(e_ab, e_bc, e_ca) -> list[Optional[sp.Expr]]:
    """Angles at A, B, C of a triangle with exact sides: exact only when
    the sides make it so (none in general — acos is not a fact)."""
    return [None, None, None]


def c_triangle_sss(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    sides = _req(obj, "sides", w)
    if not isinstance(sides, list) or len(sides) != 3:
        raise GeometryRefusal("bad_schema", f"{w}: `sides` is [AB, BC, CA]", w)
    ex = [ctx.exact(s, where=w) for s in sides]
    ab, bc, ca = (ctx.num(s, "length", where=w) for s in sides)
    coords = _triangle_from_sides(ctx, obj, ab, bc, ca, w)
    _finish_polygon(ctx, obj, coords, "triangle", exact_sides=ex, exact_angles=_angle_exact_from_sides(*ex))


def c_triangle_sas(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    sides = _req(obj, "sides", w)
    if not isinstance(sides, list) or len(sides) != 2:
        raise GeometryRefusal("bad_schema", f"{w}: `sides` is [AB, BC]; `angle` is at B", w)
    e_ab, e_bc = (ctx.exact(s, where=w) for s in sides)
    e_b = ctx.exact(_req(obj, "angle", w), where=w)
    ab, bc = (ctx.num(s, "length", where=w) for s in sides)
    b_deg = ctx.num(obj["angle"], "angle", where=w)
    if not (_MIN_ANGLE <= b_deg <= 180.0 - _MIN_ANGLE):
        raise GeometryRefusal("construction_impossible", f"{w}: an angle of {b_deg:g}° makes no triangle", w)
    bxy = (ab, 0.0)
    d = _rot((-1.0, 0.0), -b_deg)   # from B, the side BC turns inward from BA
    cxy = _add(bxy, _mul(d, bc))
    if cxy[1] < 0:
        cxy = (cxy[0], -cxy[1])
    _finish_polygon(ctx, obj, [(0.0, 0.0), bxy, cxy], "triangle",
                    exact_sides=[e_ab, e_bc, None], exact_angles=[None, e_b, None])


def c_triangle_asa(ctx: BuildContext, obj: dict, *, aas: bool = False) -> None:
    w = _where(obj)
    angles = _req(obj, "angles", w)
    if not isinstance(angles, list) or len(angles) != 2:
        raise GeometryRefusal("bad_schema", f"{w}: `angles` is [at A, at B]", w)
    e_a, e_b = (ctx.exact(x, where=w) for x in angles)
    e_c = 180 - e_a - e_b
    a_deg, b_deg = (ctx.num(x, "angle", where=w) for x in angles)
    c_deg = 180.0 - a_deg - b_deg
    if min(a_deg, b_deg, c_deg) < _MIN_ANGLE:
        raise GeometryRefusal("construction_impossible",
                              f"{w}: angles {a_deg:g}° and {b_deg:g}° leave {c_deg:g}° for the third", w)
    side_e = ctx.exact(_req(obj, "side", w), where=w)
    side = ctx.num(obj["side"], "length", where=w)
    if aas:
        # the side is BC (opposite A); scale from the AB = 1 triangle
        ab = side * math.sin(math.radians(c_deg)) / math.sin(math.radians(a_deg))
        sides_e = [None, side_e, None]
    else:
        ab = side
        sides_e = [side_e, None, None]
    ca = ab * math.sin(math.radians(b_deg)) / math.sin(math.radians(c_deg))
    cxy = _rot((ca, 0.0), a_deg)
    _finish_polygon(ctx, obj, [(0.0, 0.0), (ab, 0.0), cxy], "triangle",
                    exact_sides=sides_e, exact_angles=[e_a, e_b, e_c])


def c_triangle_aas(ctx: BuildContext, obj: dict) -> None:
    c_triangle_asa(ctx, obj, aas=True)


def c_triangle_rhs(ctx: BuildContext, obj: dict) -> None:
    """Right angle at B. Either `hyp` (AC) and `side` (AB), or `legs` [AB, BC]."""
    w = _where(obj)
    ninety = sp.Integer(90)
    if "legs" in obj:
        legs = obj["legs"]
        if not isinstance(legs, list) or len(legs) != 2:
            raise GeometryRefusal("bad_schema", f"{w}: `legs` is [AB, BC]", w)
        e_ab, e_bc = (ctx.exact(x, where=w) for x in legs)
        ab, bc = (ctx.num(x, "length", where=w) for x in legs)
        angles = [None, ninety, None]
        if sp.simplify(e_ab - e_bc) == 0:
            angles = [sp.Integer(45), ninety, sp.Integer(45)]
        _finish_polygon(ctx, obj, [(0.0, 0.0), (ab, 0.0), (ab, bc)], "triangle",
                        exact_sides=[e_ab, e_bc, None], exact_angles=angles)
        return
    e_hyp, e_side = ctx.exact(_req(obj, "hyp", w), where=w), ctx.exact(_req(obj, "side", w), where=w)
    hyp, side = ctx.num(obj["hyp"], "length", where=w), ctx.num(obj["side"], "length", where=w)
    if side >= hyp - 1e-9:
        raise GeometryRefusal("construction_impossible", f"{w}: the hypotenuse must be the longest side", w)
    bc = math.sqrt(hyp * hyp - side * side)
    e_bc = sp.sqrt(e_hyp ** 2 - e_side ** 2)
    _finish_polygon(ctx, obj, [(0.0, 0.0), (side, 0.0), (side, bc)], "triangle",
                    exact_sides=[e_side, e_bc, e_hyp], exact_angles=[None, ninety, None])


def c_triangle_isosceles(ctx: BuildContext, obj: dict) -> None:
    """Apex A; AB = AC = legs; plus exactly one of base / apex_angle / base_angle."""
    w = _where(obj)
    e_leg = ctx.exact(_req(obj, "legs", w), where=w)
    leg = ctx.num(obj["legs"], "length", where=w)
    given = [k for k in ("base", "apex_angle", "base_angle") if k in obj]
    if len(given) != 1:
        raise GeometryRefusal("bad_schema", f"{w}: give exactly one of base, apex_angle, base_angle", w)
    k = given[0]
    if k == "base":
        e_base, base = ctx.exact(obj["base"], where=w), ctx.num(obj["base"], "length", where=w)
        if base >= 2 * leg - 1e-9:
            raise GeometryRefusal("construction_impossible", f"{w}: base {base:g} is too long for legs {leg:g}", w)
        apex = 2 * math.degrees(math.asin(base / (2 * leg)))
        e_angles: list = [None, None, None]
    else:
        e_ang = ctx.exact(obj[k], where=w)
        ang = ctx.num(obj[k], "angle", where=w)
        if k == "apex_angle":
            apex = ang
            e_angles = [e_ang, (180 - e_ang) / 2, (180 - e_ang) / 2]
        else:
            apex = 180.0 - 2 * ang
            e_angles = [180 - 2 * e_ang, e_ang, e_ang]
        if not (_MIN_ANGLE <= apex <= 180.0 - _MIN_ANGLE):
            raise GeometryRefusal("construction_impossible", f"{w}: an apex of {apex:g}° makes no triangle", w)
        base = 2 * leg * math.sin(math.radians(apex / 2))
        e_base = None
    half = math.radians(apex / 2)
    bxy = (leg * math.sin(half), -leg * math.cos(half))
    cxy = (-leg * math.sin(half), -leg * math.cos(half))
    # apex up: A at the top, base below
    coords = [(0.0, leg * math.cos(half)), (bxy[0], 0.0), (cxy[0], 0.0)]
    vids = _finish_polygon(ctx, obj, coords, "triangle", exact_sides=[e_leg, e_base, e_leg], exact_angles=e_angles)
    m = ctx.model
    m.set_equal_lengths(seg_key(vids[0], vids[1]), seg_key(vids[0], vids[2]))
    m.set_equal_angles(angle_key(vids[1], vids[0], vids[2]), angle_key(vids[2], vids[0], vids[1]))


def c_triangle_equilateral(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    e_s = ctx.exact(_req(obj, "side", w), where=w)
    s = ctx.num(obj["side"], "length", where=w)
    sixty = sp.Integer(60)
    _finish_polygon(ctx, obj, [(0.0, 0.0), (s, 0.0), (s / 2, s * math.sqrt(3) / 2)], "triangle",
                    exact_sides=[e_s, e_s, e_s], exact_angles=[sixty, sixty, sixty], regular=True)


def c_square(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    e_s = ctx.exact(_req(obj, "side", w), where=w)
    s = ctx.num(obj["side"], "length", where=w)
    ninety = sp.Integer(90)
    _finish_polygon(ctx, obj, [(0.0, 0.0), (s, 0.0), (s, s), (0.0, s)], "quadrilateral",
                    exact_sides=[e_s] * 4, exact_angles=[ninety] * 4, regular=True)
    _mark_parallel_sides(ctx, obj)


def c_rectangle(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    e_w, e_h = ctx.exact(_req(obj, "width", w), where=w), ctx.exact(_req(obj, "height", w), where=w)
    wd, ht = ctx.num(obj["width"], "length", where=w), ctx.num(obj["height"], "length", where=w)
    ninety = sp.Integer(90)
    _finish_polygon(ctx, obj, [(0.0, 0.0), (wd, 0.0), (wd, ht), (0.0, ht)], "quadrilateral",
                    exact_sides=[e_w, e_h, e_w, e_h], exact_angles=[ninety] * 4)
    _mark_parallel_sides(ctx, obj)


def c_parallelogram(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    sides = _req(obj, "sides", w)
    if not isinstance(sides, list) or len(sides) != 2:
        raise GeometryRefusal("bad_schema", f"{w}: `sides` is [AB, BC]; `angle` is at B", w)
    e_a, e_b = (ctx.exact(s, where=w) for s in sides)
    a, b = (ctx.num(s, "length", where=w) for s in sides)
    e_ang = ctx.exact(_req(obj, "angle", w), where=w)
    ang = ctx.num(obj["angle"], "angle", where=w)
    if not (_MIN_ANGLE <= ang <= 180.0 - _MIN_ANGLE):
        raise GeometryRefusal("construction_impossible", f"{w}: an angle of {ang:g}° makes no parallelogram", w)
    d = _rot((-1.0, 0.0), -ang)
    bxy = (a, 0.0)
    cxy = _add(bxy, _mul(d, b))
    if cxy[1] < 0:
        cxy = (cxy[0], -cxy[1])
    dxy = _sub(cxy, bxy)
    _finish_polygon(ctx, obj, [(0.0, 0.0), bxy, cxy, dxy], "quadrilateral",
                    exact_sides=[e_a, e_b, e_a, e_b], exact_angles=[180 - e_ang, e_ang, 180 - e_ang, e_ang])
    _mark_parallel_sides(ctx, obj)


def c_rhombus(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    c_parallelogram(ctx, {**obj, "sides": [obj.get("side"), obj.get("side")]} if "side" in obj else obj)


def c_trapezium(ctx: BuildContext, obj: dict) -> None:
    """AB ∥ DC: `parallel_sides` [AB, DC], `height`, `offset` of D along AB from A."""
    w = _where(obj)
    ps = _req(obj, "parallel_sides", w)
    if not isinstance(ps, list) or len(ps) != 2:
        raise GeometryRefusal("bad_schema", f"{w}: `parallel_sides` is [AB, DC]", w)
    e_ab, e_dc = (ctx.exact(s, where=w) for s in ps)
    ab, dc = (ctx.num(s, "length", where=w) for s in ps)
    h = ctx.num(_req(obj, "height", w), "length", where=w)
    off = ctx.num(obj.get("offset", 1), "length", where=w)
    vids = _finish_polygon(ctx, obj, [(0.0, 0.0), (ab, 0.0), (off + dc, h), (off, h)], "quadrilateral",
                           exact_sides=[e_ab, None, e_dc, None], exact_angles=[None] * 4)
    m = ctx.model
    l1 = m.add_line(f"{obj.get('id', 'trap')}_ab", [vids[0], vids[1]], hidden=True)
    l2 = m.add_line(f"{obj.get('id', 'trap')}_dc", [vids[3], vids[2]], hidden=True)
    m.set_parallel(l1.id, l2.id)


def c_kite(ctx: BuildContext, obj: dict) -> None:
    """A at the top; AB = AD = `sides[0]`, CB = CD = `sides[1]`, `angle` at B."""
    w = _where(obj)
    sides = _req(obj, "sides", w)
    if not isinstance(sides, list) or len(sides) != 2:
        raise GeometryRefusal("bad_schema", f"{w}: `sides` is [AB, BC]", w)
    e1, e2 = (ctx.exact(s, where=w) for s in sides)
    s1, s2 = (ctx.num(s, "length", where=w) for s in sides)
    e_ang = ctx.exact(_req(obj, "angle", w), where=w)
    ang = ctx.num(obj["angle"], "angle", where=w)
    bxy = (s1, 0.0)
    cxy = _add(bxy, _mul(_rot((-1.0, 0.0), -ang), s2))
    # the kite is symmetric about AC: D is B reflected in AC
    axis = _unit(cxy)
    proj = bxy[0] * axis[0] + bxy[1] * axis[1]
    foot = _mul(axis, proj)
    dxy = _sub(_mul(foot, 2.0), bxy)
    vids = _finish_polygon(ctx, obj, [(0.0, 0.0), bxy, cxy, dxy], "quadrilateral",
                           exact_sides=[e1, e2, e2, e1], exact_angles=[None, e_ang, None, e_ang])
    m = ctx.model
    m.set_equal_lengths(seg_key(vids[0], vids[1]), seg_key(vids[0], vids[3]))
    m.set_equal_lengths(seg_key(vids[2], vids[1]), seg_key(vids[2], vids[3]))


def _mark_parallel_sides(ctx: BuildContext, obj: dict) -> None:
    m = ctx.model
    pg = m.polygons[obj.get("id") or max(m.polygons)]
    v = pg.vertices
    base = pg.id
    l1 = m.add_line(f"{base}_s0", [v[0], v[1]], hidden=True)
    l2 = m.add_line(f"{base}_s2", [v[3], v[2]], hidden=True)
    l3 = m.add_line(f"{base}_s1", [v[1], v[2]], hidden=True)
    l4 = m.add_line(f"{base}_s3", [v[0], v[3]], hidden=True)
    m.set_parallel(l1.id, l2.id)
    m.set_parallel(l3.id, l4.id)
    if pg.kind == "quadrilateral" and all(
        m.angles.get(angle_key(v[i], v[i - 1], v[(i + 1) % 4])) and
        m.angles[angle_key(v[i], v[i - 1], v[(i + 1) % 4])].exact == 90 for i in range(4)
    ):
        m.set_perpendicular(l1.id, l3.id)
        m.set_perpendicular(l2.id, l4.id)


def c_regular_polygon(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    n = _req(obj, "n", w)
    if not isinstance(n, int) or n < 3 or n > 20:
        raise GeometryRefusal("bad_schema", f"{w}: n is a whole number from 3 to 20", w)
    e_s = ctx.exact(_req(obj, "side", w), where=w)
    s = ctx.num(obj["side"], "length", where=w)
    turn = 360.0 / n
    coords: list[Vec] = [(0.0, 0.0)]
    heading = 0.0
    for _ in range(n - 1):
        d = _rot((1.0, 0.0), heading)
        coords.append(_add(coords[-1], _mul(d, s)))
        heading += turn
    interior = sp.Rational((n - 2) * 180, n)
    _finish_polygon(ctx, obj, coords, "triangle" if n == 3 else ("quadrilateral" if n == 4 else "regular"),
                    exact_sides=[e_s] * n, exact_angles=[interior] * n, regular=True)


def c_turtle_polygon(ctx: BuildContext, obj: dict, *, closed: bool = True) -> None:
    w = _where(obj)
    sides, turns = _req(obj, "sides", w), _req(obj, "turns", w)
    if not (isinstance(sides, list) and isinstance(turns, list) and len(sides) == len(turns) and len(sides) >= 2):
        raise GeometryRefusal("bad_schema", f"{w}: `sides` and `turns` are lists of the same length", w)
    e_sides = [ctx.exact(s, where=w) for s in sides]
    e_turns = [ctx.exact(t, where=w) for t in turns]
    # a drawn shape is not jittered: its closure is the construction
    f_sides = [ctx.num(s, "length", where=w, free=False) for s in sides]
    f_turns = [ctx.num(t, "angle", where=w, free=False) for t in turns]
    coords: list[Vec] = [(0.0, 0.0)]
    heading = 0.0
    for s, t in zip(f_sides, f_turns):
        d = _rot((1.0, 0.0), heading)
        coords.append(_add(coords[-1], _mul(d, s)))
        heading += t
    if closed:
        end = coords.pop()
        if math.dist(end, coords[0]) > 1e-6 * max(1.0, sum(f_sides)):
            raise GeometryRefusal("closure_failed", f"{w}: the sides and turns do not return to the start", w)
        if abs((sum(f_turns) % 360.0)) > 1e-6 and abs((sum(f_turns) % 360.0) - 360.0) > 1e-6:
            raise GeometryRefusal("closure_failed", f"{w}: the turns add to {sum(f_turns):g}°, not 360°", w)
        # interior angle at the vertex AFTER side i is 180 - turn i; a right
        # turn (negative) makes a reflex corner. Vertex k's angle is turn k-1.
        n = len(coords)
        ccw = sum(f_turns) > 0
        e_angles = []
        for k in range(n):
            t = e_turns[k - 1]
            e_angles.append((180 - t) if ccw else (180 + t))
        _finish_polygon(ctx, obj, coords, "triangle" if n == 3 else ("quadrilateral" if n == 4 else "polygon"),
                        exact_sides=e_sides, exact_angles=e_angles)
    else:
        _finish_polygon(ctx, obj, coords, "open", exact_sides=e_sides, exact_angles=[], closed=False)


def c_polyline_open(ctx: BuildContext, obj: dict) -> None:
    c_turtle_polygon(ctx, obj, closed=False)


def c_quadrilateral_by_angles(ctx: BuildContext, obj: dict) -> None:
    """Angles at A, B, C, D (sum 360) and the two sides AB, BC; CD and DA
    follow from closure — a 2×2 linear solve with one answer."""
    w = _where(obj)
    angles = _req(obj, "angles", w)
    sides = _req(obj, "sides", w)
    if not (isinstance(angles, list) and len(angles) == 4 and isinstance(sides, list) and len(sides) == 2):
        raise GeometryRefusal("bad_schema", f"{w}: `angles` is [A, B, C, D], `sides` is [AB, BC]", w)
    e_ang = [ctx.exact(a, where=w) for a in angles]
    total = sp.simplify(sum(e_ang))
    f_ang = [ctx.num(a, "angle", where=w) for a in angles]
    if abs(sum(f_ang) - 360.0) > 1e-6:
        raise GeometryRefusal("closure_failed", f"{w}: the angles add to {total} = {sum(f_ang):g}°, not 360°", w)
    if min(f_ang) < _MIN_ANGLE or max(f_ang) > 360.0 - _MIN_ANGLE:
        raise GeometryRefusal("construction_impossible", f"{w}: an angle is degenerate", w)
    e_ab, e_bc = (ctx.exact(s, where=w) for s in sides)
    ab, bc = (ctx.num(s, "length", where=w) for s in sides)
    # walk: A(0,0) -> B(ab,0); at B turn left by 180-B; C = B + bc·dir; at C
    # turn left by 180-C: D = C + cd·dir2; at D turn 180-D: back to A along dir3.
    a_deg, b_deg, c_deg, d_deg = f_ang
    dir1 = _rot((1.0, 0.0), 180.0 - b_deg)
    cxy = _add((ab, 0.0), _mul(dir1, bc))
    dir2 = _rot(dir1, 180.0 - c_deg)
    dir3 = _rot(dir2, 180.0 - d_deg)
    # C + s·dir2 + t·dir3 = A  → solve for s (= CD) and t (= DA)
    den = _cross(dir2, dir3)
    if abs(den) < 1e-9:
        raise GeometryRefusal("construction_impossible", f"{w}: these angles do not close", w)
    rhs = _mul(cxy, -1.0)
    s = _cross(rhs, dir3) / den
    t = _cross(dir2, rhs) / den
    if s <= 1e-9 or t <= 1e-9:
        raise GeometryRefusal("construction_impossible",
                              f"{w}: with AB = {ab:g} and BC = {bc:g} the other sides come out {s:.3g} and {t:.3g}", w)
    dxy = _add(cxy, _mul(dir2, s))
    _finish_polygon(ctx, obj, [(0.0, 0.0), (ab, 0.0), cxy, dxy], "quadrilateral",
                    exact_sides=[e_ab, e_bc, None, None], exact_angles=e_ang)


# ── circles ───────────────────────────────────────────────────────────────

def c_circle(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    centre = obj.get("centre") or ctx.model.new_point_id("o")
    e_r = ctx.exact(_req(obj, "radius", w), where=w)
    r = ctx.num(obj["radius"], "length", where=w)
    m = ctx.model
    if not m.has_point(centre):
        ox, oy = ctx.free_origin()
        ctx.place(centre, ox + r, oy + r)
    cid = obj.get("id") or m.new_point_id("circle_")
    if cid in m.circles:
        raise GeometryRefusal("bad_reference", f"{w}: circle {cid!r} is defined twice", w)
    m.circles[cid] = Circle(cid, centre, r, e_r)
    m.object_ids[cid] = ("circle", cid)


def c_point_on_circle(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    pid, cid = _req(obj, "id", w), _req(obj, "circle", w)
    m = ctx.model
    c = m.circles.get(cid)
    if c is None:
        raise GeometryRefusal("bad_reference", f"{w}: circle {cid!r} is not defined", w)
    ang = ctx.num(obj.get("angle", 0), "angle", where=w)
    cx, cy = m.xy(c.centre)
    ctx.place(pid, cx + c.radius * math.cos(math.radians(ang)), cy + c.radius * math.sin(math.radians(ang)))
    m.segment(c.centre, pid, c.exact)
    for other in list(m.points):
        if other != pid and other != c.centre and m.segments.get(seg_key(c.centre, other)) is not None \
                and abs(m.length_float(seg_key(c.centre, other)) - c.radius) < 1e-9:
            m.set_equal_lengths(seg_key(c.centre, other), seg_key(c.centre, pid))


def c_radius_segment(ctx: BuildContext, obj: dict) -> None:
    c_point_on_circle(ctx, obj)


def c_diameter(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    a, b = _ids(_req(obj, "points", w), 2, w, "points")
    cid = _req(obj, "circle", w)
    ang = obj.get("angle", 0)
    c_point_on_circle(ctx, {"id": a, "circle": cid, "angle": ang})
    c_point_on_circle(ctx, {"id": b, "circle": cid, "angle": ctx.num(ang, "angle", where=w, free=False) + 180.0})
    m = ctx.model
    c = m.circles[cid]
    m.add_line(obj.get("id") or m.new_point_id("line_"), [a, c.centre, b], hidden=True)
    m.segment(a, b, 2 * c.exact if c.exact is not None else None)
    m.angle(c.centre, a, b, exact=sp.Integer(180))


def c_chord(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    a, b = _ids(_req(obj, "points", w), 2, w, "points")
    cid = _req(obj, "circle", w)
    angs = obj.get("angles", [30, 150])
    if not isinstance(angs, list) or len(angs) != 2:
        raise GeometryRefusal("bad_schema", f"{w}: `angles` is [at A, at B] round the centre", w)
    c_point_on_circle(ctx, {"id": a, "circle": cid, "angle": angs[0]})
    c_point_on_circle(ctx, {"id": b, "circle": cid, "angle": angs[1]})
    s = ctx.model.segment(a, b)
    if "id" in obj:
        ctx.model.segment_ids[obj["id"]] = s.key
        ctx.model.object_ids[obj["id"]] = ("segment", obj["id"])


# ── discrete ──────────────────────────────────────────────────────────────

def c_grid_pattern(ctx: BuildContext, obj: dict) -> None:
    w = _where(obj)
    rows, cols = _req(obj, "rows", w), _req(obj, "cols", w)
    cells, palette = _req(obj, "cells", w), _req(obj, "palette", w)
    blank = obj.get("blank", "*")
    if not (isinstance(rows, int) and isinstance(cols, int) and 1 <= rows <= 12 and 1 <= cols <= 12):
        raise GeometryRefusal("bad_schema", f"{w}: rows and cols are whole numbers up to 12", w)
    if not (isinstance(cells, list) and len(cells) == rows and all(isinstance(r, list) and len(r) == cols for r in cells)):
        raise GeometryRefusal("bad_schema", f"{w}: `cells` is {rows} rows of {cols} entries", w)
    if not (isinstance(palette, list) and palette and all(isinstance(p, str) for p in palette)):
        raise GeometryRefusal("bad_schema", f"{w}: `palette` lists the colours", w)
    for r in cells:
        for c in r:
            if c != blank and c not in palette:
                raise GeometryRefusal("bad_schema", f"{w}: cell colour {c!r} is not in the palette", w)
    gid = obj.get("id") or ctx.model.new_point_id("grid_")
    ctx.model.grids[gid] = GridPattern(gid, rows, cols, [list(r) for r in cells], list(palette), blank)
    ctx.model.object_ids[gid] = ("grid", gid)


# ── the registry ──────────────────────────────────────────────────────────

CONSTRUCTIONS: dict[str, Callable[[BuildContext, dict], None]] = {
    "segment": c_segment,
    "line_through": c_line_through,
    "ray": c_ray,
    "extend": c_extend,
    "point_on_segment": c_point_on_segment,
    "midpoint": c_midpoint,
    "intersection": c_intersection,
    "ray_at_angle": c_ray_at_angle,
    "angles_at_point": c_angles_at_point,
    "parallel_through": c_parallel_through,
    "perpendicular_through": c_perpendicular_through,
    "transversal": c_transversal,
    "parallels_transversal": c_parallels_transversal,
    "angle_bisector": c_angle_bisector,
    "perpendicular_bisector": c_perpendicular_bisector,
    "triangle_sss": c_triangle_sss,
    "triangle_sas": c_triangle_sas,
    "triangle_asa": c_triangle_asa,
    "triangle_aas": c_triangle_aas,
    "triangle_rhs": c_triangle_rhs,
    "triangle_isosceles": c_triangle_isosceles,
    "triangle_equilateral": c_triangle_equilateral,
    "square": c_square,
    "rectangle": c_rectangle,
    "parallelogram": c_parallelogram,
    "rhombus": c_rhombus,
    "trapezium": c_trapezium,
    "kite": c_kite,
    "regular_polygon": c_regular_polygon,
    "turtle_polygon": c_turtle_polygon,
    "polyline_open": c_polyline_open,
    "quadrilateral_by_angles": c_quadrilateral_by_angles,
    "circle": c_circle,
    "point_on_circle": c_point_on_circle,
    "radius_segment": c_radius_segment,
    "diameter": c_diameter,
    "chord": c_chord,
    "grid_pattern": c_grid_pattern,
}

# Features the corpus met and v1 deliberately does not express. Naming
# them keeps the refusal explicit: "unsupported_feature: reflection", not a
# plausible figure drawn in its place.
UNSUPPORTED: dict[str, str] = {
    "reflection": "transformations are a later phase",
    "rotation": "transformations are a later phase",
    "translation": "transformations are a later phase",
    "tessellation": "tilings are a later phase",
    "curved_boundary": "regions bounded by arcs are not in v1",
    "arc_capped_rectangle": "regions bounded by arcs are not in v1",
    "net": "3D solids and nets are a later phase",
    "solid": "3D solids and nets are a later phase",
    "compass_construction": "animated compass work is a later phase; use construction_arc marks",
}


def run(ctx: BuildContext, obj: dict) -> None:
    make = obj.get("make")
    fn = CONSTRUCTIONS.get(make)
    if fn is None:
        if make in UNSUPPORTED:
            raise GeometryRefusal("unsupported_feature", f"{make}: {UNSUPPORTED[make]}", _where(obj))
        raise GeometryRefusal("unknown_construction", f"{make!r} is not a construction v1 knows", _where(obj))
    fn(ctx, obj)
