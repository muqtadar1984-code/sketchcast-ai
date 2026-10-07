"""The theorem registry: a closed enum of the reasons a `deduce` step may
give, each a PREMISE over the facts graph and an equation TEMPLATE.

The model names a theorem id and the objects it applies to (``uses``).
The premise decides whether the theorem applies to those objects — two
angles cited as alternate must sit at two points of one transversal, each
on one of two lines the facts say are parallel, on opposite sides of the
transversal — and the template is the equation the theorem then yields,
in the symbols ``ang_<name>`` (the measure of angle id ``angle_<name>``)
and ``len_<name>``. The verifier checks the step's own line against it.

Where a premise needs "which side" (alternate vs corresponding), the sign
is read from the realisation. That is topology the construction produced,
not a measurement: nothing here compares a size.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional

import sympy as sp

from maths.geometry.errors import GeometryRefusal
from maths.geometry.model import AngleKey, Line, Model, Polygon, SegKey, angle_key, seg_key

ANGLE_PREFIX = "angle_"
SEG_PREFIX = "seg_"


def angle_symbol(angle_id: str) -> sp.Symbol:
    name = angle_id[len(ANGLE_PREFIX):] if angle_id.startswith(ANGLE_PREFIX) else angle_id
    return sp.Symbol(f"ang_{name}")


def length_symbol(seg_id: str) -> sp.Symbol:
    name = seg_id[len(SEG_PREFIX):] if seg_id.startswith(SEG_PREFIX) else seg_id
    return sp.Symbol(f"len_{name}")


PERIMETER = sp.Symbol("P")
AREA = sp.Symbol("A")
CIRCUMFERENCE = sp.Symbol("C")


@dataclass
class Uses:
    """The objects a step cites, sorted by what they are."""
    angles: list[str]
    segments: list[str]
    polygons: list[str]
    lines: list[str]
    circles: list[str]
    other: list[str]


def sort_uses(m: Model, uses: list[str], theorem: str) -> Uses:
    u = Uses([], [], [], [], [], [])
    for x in uses:
        if x in m.angle_ids:
            u.angles.append(m.canonical_angle_id(x))
        elif x in m.segment_ids:
            u.segments.append(x)
        elif x in m.polygons:
            u.polygons.append(x)
        elif x in m.lines:
            u.lines.append(x)
        elif x in m.circles:
            u.circles.append(x)
        else:
            u.other.append(x)   # a relation id or a configuration id: documentation
    return u


def _premise(theorem: str, ok: bool, why: str) -> None:
    if not ok:
        raise GeometryRefusal("theorem_premise", f"{theorem}: {why}")


def _need_angles(m: Model, u: Uses, theorem: str, n: Optional[int] = None, at_least: Optional[int] = None) -> list[AngleKey]:
    if not u.angles and len(u.polygons) == 1:
        # the shape cited instead of its angles: its interior angles, in order
        pg = m.polygons[u.polygons[0]]
        L = len(pg.vertices)
        for i, v in enumerate(pg.vertices):
            k = angle_key(v, pg.vertices[i - 1], pg.vertices[(i + 1) % L])
            aid = next((a for a, kk in m.angle_ids.items() if kk == k), None)
            if aid:
                u.angles.append(aid)
    if n is not None:
        _premise(theorem, len(u.angles) == n, f"cite exactly {n} angles (got {len(u.angles)})")
    if at_least is not None:
        _premise(theorem, len(u.angles) >= at_least, f"cite at least {at_least} angles")
    return [m.angle_ids[a] for a in u.angles]


def _only_triangle(m: Model, u: Uses):
    """The triangle a step means when it cites NO angle and no shape: the
    cited polygon if one, else the figure's only closed triangle. A figure
    with two triangles is ambiguous and stays a refusal. The live lesson
    call left `uses` empty on isosceles_base_angles / equilateral_angles
    twice (2026-10-07); every premise still runs on what is inferred."""
    if u.polygons:
        return u.polygons[0], m.polygons[u.polygons[0]]
    tris = [(pid, pg) for pid, pg in m.polygons.items() if pg.closed and len(pg.vertices) == 3]
    return tris[0] if len(tris) == 1 else (None, None)


def _angle_id(m: Model, k: AngleKey) -> Optional[str]:
    return next((a for a, kk in m.angle_ids.items() if kk == k), None)


def _same_vertex(keys: list[AngleKey], theorem: str) -> str:
    vs = {k[0] for k in keys}
    _premise(theorem, len(vs) == 1, "the angles must share a vertex")
    return next(iter(vs))


def _arm_counts(keys: list[AngleKey]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for k in keys:
        for a in k[1]:
            counts[a] = counts.get(a, 0) + 1
    return counts


def _connected(keys: list[AngleKey]) -> bool:
    """Angles chained by shared arms form one run."""
    if not keys:
        return False
    seen = {0}
    frontier = [0]
    while frontier:
        i = frontier.pop()
        for j in range(len(keys)):
            if j not in seen and keys[i][1] & keys[j][1]:
                seen.add(j)
                frontier.append(j)
    return len(seen) == len(keys)


def _sum_eq(u: Uses, total) -> list[sp.Eq]:
    return [sp.Eq(sum(angle_symbol(a) for a in u.angles), total)]


# ── angles at a vertex ────────────────────────────────────────────────────

def t_angles_on_line(m: Model, u: Uses) -> list[sp.Eq]:
    T = "angles_on_line"
    keys = _need_angles(m, u, T, at_least=2)
    v = _same_vertex(keys, T)
    _premise(T, all(k[2] == "interior" for k in keys), "reflex angles do not lie on a line")
    counts = _arm_counts(keys)
    outer = [a for a, c in counts.items() if c == 1]
    _premise(T, len(outer) == 2 and all(c <= 2 for c in counts.values()) and _connected(keys),
             "the angles must be adjacent, side by side along one line")
    _premise(T, m.between(outer[0], v, outer[1]),
             f"no construction puts {outer[0]}, {v}, {outer[1]} on one straight line")
    total = sum(m.angle_float(k) for k in keys)
    _premise(T, abs(total - 180.0) < 1e-6, "the cited angles overlap or do not fill the straight angle")
    return _sum_eq(u, 180)


def t_angles_at_point(m: Model, u: Uses) -> list[sp.Eq]:
    T = "angles_at_point"
    keys = _need_angles(m, u, T, at_least=2)
    _same_vertex(keys, T)
    if len(set(keys)) < len(keys):
        dup = [u.angles[i] for i, k in enumerate(keys) if keys.index(k) != i]
        _premise(T, False, f"{', '.join(dup)} names the same angle as another cited one; the other angle "
                           f"round the point on the same arms is its reflex (region: \"reflex\")")
    counts = _arm_counts(keys)
    _premise(T, all(c == 2 for c in counts.values()) and _connected(keys),
             "the angles must go all the way round the point, each arm shared by two of them")
    total = sum(m.angle_float(k) for k in keys)
    _premise(T, abs(total - 360.0) < 1e-6, "the cited angles overlap or do not fill the full turn")
    return _sum_eq(u, 360)


def t_vertically_opposite(m: Model, u: Uses) -> list[sp.Eq]:
    T = "vertically_opposite"
    k1, k2 = _need_angles(m, u, T, n=2)
    v = _same_vertex([k1, k2], T)
    p, q = sorted(k1[1])
    r, s = sorted(k2[1])
    ok = (m.between(p, v, r) and m.between(q, v, s)) or (m.between(p, v, s) and m.between(q, v, r))
    _premise(T, ok, "the angles are not formed by two straight lines crossing at the vertex")
    return [sp.Eq(angle_symbol(u.angles[0]), angle_symbol(u.angles[1]))]


def t_angle_addition(m: Model, u: Uses) -> list[sp.Eq]:
    T = "angle_addition"
    k1, k2, kw = _need_angles(m, u, T, n=3)
    v = _same_vertex([k1, k2, kw], T)
    shared = k1[1] & k2[1]
    _premise(T, len(shared) == 1, "the two parts must share one arm")
    _premise(T, (k1[1] | k2[1]) - shared == kw[1], "the whole angle's arms are the parts' outer arms")
    f = m.angle_float(k1) + m.angle_float(k2)
    _premise(T, abs(f - m.angle_float(kw)) < 1e-6, "the shared arm does not lie inside the whole angle")
    a1, a2, aw = (angle_symbol(a) for a in u.angles)
    return [sp.Eq(aw, a1 + a2)]


# ── polygons ──────────────────────────────────────────────────────────────

def _polygon_of_angles(m: Model, keys: list[AngleKey], T: str, n: Optional[int] = None) -> Polygon:
    verts = [k[0] for k in keys]
    _premise(T, len(set(verts)) == len(verts), "one angle per vertex")
    pg = m.polygon_with_vertices(set(verts))
    _premise(T, pg is not None, "no construction makes these vertices one closed shape")
    if n is not None:
        _premise(T, len(pg.vertices) == n, f"the shape has {len(pg.vertices)} vertices, not {n}")
    L = len(pg.vertices)
    for k in keys:
        i = pg.vertices.index(k[0])
        _premise(T, k[1] == frozenset((pg.vertices[i - 1], pg.vertices[(i + 1) % L])),
                 f"the angle at {k[0]} must be between its two neighbouring vertices")
    return pg


def t_triangle_angle_sum(m: Model, u: Uses) -> list[sp.Eq]:
    T = "triangle_angle_sum"
    keys = _need_angles(m, u, T, n=3)
    _polygon_of_angles(m, keys, T, 3)
    return _sum_eq(u, 180)


def t_quadrilateral_angle_sum(m: Model, u: Uses) -> list[sp.Eq]:
    T = "quadrilateral_angle_sum"
    keys = _need_angles(m, u, T, n=4)
    _polygon_of_angles(m, keys, T, 4)
    return _sum_eq(u, 360)


def t_polygon_interior_sum(m: Model, u: Uses) -> list[sp.Eq]:
    T = "polygon_interior_sum"
    keys = _need_angles(m, u, T, at_least=3)
    pg = _polygon_of_angles(m, keys, T)
    _premise(T, len(keys) == len(pg.vertices), "cite every interior angle of the shape")
    return _sum_eq(u, (len(pg.vertices) - 2) * 180)


def t_polygon_exterior_sum(m: Model, u: Uses) -> list[sp.Eq]:
    T = "polygon_exterior_sum"
    keys = _need_angles(m, u, T, at_least=3)
    verts = [k[0] for k in keys]
    pg = m.polygon_with_vertices(set(verts))
    _premise(T, pg is not None and len(keys) == len(pg.vertices), "cite one exterior angle at every vertex of one shape")
    L = len(pg.vertices)
    for k in keys:
        i = pg.vertices.index(k[0])
        prev_v, next_v = pg.vertices[i - 1], pg.vertices[(i + 1) % L]
        arms = set(k[1])
        _premise(T, next_v in arms or prev_v in arms, f"the exterior angle at {k[0]} is between a side and the next side extended")
        (ext,) = arms - {next_v, prev_v} if len(arms - {next_v, prev_v}) == 1 else (None,)
        other = prev_v if next_v in arms else next_v
        _premise(T, ext is not None and m.between(other, k[0], ext), f"{ext} is not on the side through {k[0]} extended")
    return _sum_eq(u, 360)


def t_isosceles_base_angles(m: Model, u: Uses) -> list[sp.Eq]:
    T = "isosceles_base_angles"
    if not u.angles:
        # nothing cited: the base angles of the figure's isosceles triangle —
        # the pair opposite its equal legs. Checked below like any citation.
        _pid, pg = _only_triangle(m, u)
        if pg is not None:
            a, b, c = pg.vertices
            for apex, p, q in ((a, b, c), (b, c, a), (c, a, b)):
                if m.lengths_equal(seg_key(apex, p), seg_key(apex, q)):
                    ids = [_angle_id(m, angle_key(p, apex, q)), _angle_id(m, angle_key(q, apex, p))]
                    if all(ids):
                        u.angles = [m.canonical_angle_id(i) for i in ids]
                        u.polygons = []
                    break
    k1, k2 = _need_angles(m, u, T, n=2)
    b, c = k1[0], k2[0]
    _premise(T, b != c and b in k2[1] and c in k1[1], "the two base angles are at the ends of the base")
    apex = (k1[1] - {c}) | (k2[1] - {b})
    _premise(T, len(apex) == 1, "the angles must share the apex as their other arm")
    (a,) = apex
    _premise(T, m.polygon_with_vertices({a, b, c}) is not None, "no construction makes a triangle of these points")
    _premise(T, m.lengths_equal(seg_key(a, b), seg_key(a, c)), f"no construction makes {a}{b} equal to {a}{c}")
    return [sp.Eq(angle_symbol(u.angles[0]), angle_symbol(u.angles[1]))]


def t_equilateral_angles(m: Model, u: Uses) -> list[sp.Eq]:
    T = "equilateral_angles"
    if not u.angles and not u.polygons:
        # nothing cited: the figure's only triangle, all three angles
        pid, _pg = _only_triangle(m, u)
        if pid is not None:
            u.polygons = [pid]
    keys = _need_angles(m, u, T, at_least=1)
    _premise(T, len(keys) <= 3, "cite at most the three angles of the triangle")
    verts = [k[0] for k in keys]
    pg = None
    for cand in m.polygons.values():
        if cand.closed and len(cand.vertices) == 3 and set(verts) <= set(cand.vertices):
            pg = cand
    _premise(T, pg is not None, "no construction makes a triangle with these vertices")
    for k in keys:
        i = pg.vertices.index(k[0])
        _premise(T, k[1] == frozenset((pg.vertices[i - 1], pg.vertices[(i + 1) % 3])),
                 f"the angle at {k[0]} must be between the triangle's sides")
    a, b, c = pg.vertices
    _premise(T, m.lengths_equal(seg_key(a, b), seg_key(b, c)) and m.lengths_equal(seg_key(b, c), seg_key(c, a)),
             "no construction makes all three sides equal")
    return [sp.Eq(angle_symbol(x), 60) for x in u.angles]


def t_exterior_angle_triangle(m: Model, u: Uses) -> list[sp.Eq]:
    T = "exterior_angle_triangle"
    k1, k2, ke = _need_angles(m, u, T, n=3)
    a, b, c = k1[0], k2[0], ke[0]
    _premise(T, len({a, b, c}) == 3, "two interior angles at two vertices and the exterior angle at the third")
    _premise(T, m.polygon_with_vertices({a, b, c}) is not None, "no construction makes a triangle of these points")
    _premise(T, k1[1] == frozenset((b, c)) and k2[1] == frozenset((a, c)), "the interior angles are between the triangle's sides")
    arms = set(ke[1])
    inner = arms & {a, b}
    _premise(T, len(inner) == 1 and len(arms) == 2, "the exterior angle is between one side and the other side extended")
    (side_end,) = inner
    (ext,) = arms - inner
    other = b if side_end == a else a
    _premise(T, m.between(other, c, ext), f"{ext} is not on {other}{c} extended beyond {c}")
    return [sp.Eq(angle_symbol(u.angles[2]), angle_symbol(u.angles[0]) + angle_symbol(u.angles[1]))]


# ── parallel lines ────────────────────────────────────────────────────────

def _transversal_roles(m: Model, k1: AngleKey, k2: AngleKey, T: str):
    """(inner_P, side_P, inner_Q, side_Q) for two angles at P and Q on a
    transversal, each between the transversal and one of two parallels."""
    p, q = k1[0], k2[0]
    _premise(T, p != q, "the two angles are at different points of the transversal")
    tr = m.line_through(p, q)
    if tr is None and seg_key(p, q) in m.segments:
        # a side of a shape is a transversal too (the parallel through the
        # apex, B12): two points joined by a segment are on one line
        tr = Line("_transversal", [p, q])
    _premise(T, tr is not None, f"no construction puts {p} and {q} on one line (the transversal)")
    out = []
    for k, here, there in ((k1, p, q), (k2, q, p)):
        t_arm = [a for a in k[1] if a in tr.points]
        s_arm = [a for a in k[1] if a not in tr.points]
        _premise(T, len(t_arm) == 1 and len(s_arm) == 1, f"the angle at {here} must have one arm along the transversal")
        ih, it, io = tr.points.index(here), tr.points.index(t_arm[0]), tr.points.index(there)
        inner = (it - ih) * (io - ih) > 0
        own = [ln for ln in m.lines_through(here) if ln is not tr and s_arm[0] in ln.points]
        _premise(T, bool(own), f"the other arm at {here} is not along a known line")
        out.append((inner, m.side_of_line(tr, s_arm[0]), own))
    (ip, sp_, lp), (iq, sq, lq) = out
    _premise(T, any(m.are_parallel(a.id, b.id) for a in lp for b in lq),
             "no construction makes the two lines parallel")
    _premise(T, sp_ != 0 and sq != 0, "an arm lies on the transversal")
    return ip, sp_, iq, sq


def t_alternate_angles(m: Model, u: Uses) -> list[sp.Eq]:
    T = "alternate_angles"
    k1, k2 = _need_angles(m, u, T, n=2)
    ip, sp_, iq, sq = _transversal_roles(m, k1, k2, T)
    _premise(T, ip == iq and sp_ != sq, "these are not alternate angles (opposite sides, both inside or both outside)")
    return [sp.Eq(angle_symbol(u.angles[0]), angle_symbol(u.angles[1]))]


def t_corresponding_angles(m: Model, u: Uses) -> list[sp.Eq]:
    T = "corresponding_angles"
    k1, k2 = _need_angles(m, u, T, n=2)
    ip, sp_, iq, sq = _transversal_roles(m, k1, k2, T)
    _premise(T, ip != iq and sp_ == sq, "these are not corresponding angles (same side, same direction)")
    return [sp.Eq(angle_symbol(u.angles[0]), angle_symbol(u.angles[1]))]


def t_cointerior_angles(m: Model, u: Uses) -> list[sp.Eq]:
    T = "cointerior_angles"
    k1, k2 = _need_angles(m, u, T, n=2)
    ip, sp_, iq, sq = _transversal_roles(m, k1, k2, T)
    _premise(T, ip and iq and sp_ == sq, "these are not co-interior angles (same side, both between the parallels)")
    return _sum_eq(u, 180)


# ── lengths and areas ─────────────────────────────────────────────────────

def _segment_symbol(m: Model, a: str, b: str) -> sp.Symbol:
    k = seg_key(a, b)
    for sid, key in m.segment_ids.items():
        if key == k:
            return length_symbol(sid)
    x, y = sorted(k)
    return sp.Symbol(f"len_{x}_{y}")


def _the_polygon_cited(m: Model, u: Uses, T: str, n: Optional[int] = None) -> Polygon:
    _premise(T, len(u.polygons) == 1, "cite the shape by its id")
    pg = m.polygons[u.polygons[0]]
    _premise(T, pg.closed, "the shape is not closed")
    if n is not None:
        _premise(T, len(pg.vertices) == n, f"the shape has {len(pg.vertices)} sides, not {n}")
    return pg


def t_pythagoras(m: Model, u: Uses) -> list[sp.Eq]:
    T = "pythagoras"
    pg = _the_polygon_cited(m, u, T, 3)
    a, b, c = pg.vertices
    right = None
    for v, p, q in ((a, c, b), (b, a, c), (c, b, a)):
        an = m.angles.get(angle_key(v, p, q))
        if an is not None and an.exact is not None and sp.simplify(an.exact - 90) == 0:
            right = (v, p, q)
    _premise(T, right is not None, "no construction makes a right angle in this triangle")
    v, p, q = right
    return [sp.Eq(_segment_symbol(m, p, q) ** 2, _segment_symbol(m, v, p) ** 2 + _segment_symbol(m, v, q) ** 2)]


def t_perimeter(m: Model, u: Uses) -> list[sp.Eq]:
    pg = _the_polygon_cited(m, u, "perimeter")
    n = len(pg.vertices)
    return [sp.Eq(PERIMETER, sum(_segment_symbol(m, pg.vertices[i], pg.vertices[(i + 1) % n]) for i in range(n)))]


def t_area_rectangle(m: Model, u: Uses) -> list[sp.Eq]:
    T = "area_rectangle"
    pg = _the_polygon_cited(m, u, T, 4)
    v = pg.vertices
    for i in range(4):
        an = m.angles.get(angle_key(v[i], v[i - 1], v[(i + 1) % 4]))
        _premise(T, an is not None and an.exact is not None and sp.simplify(an.exact - 90) == 0,
                 "no construction makes every angle a right angle")
    return [sp.Eq(AREA, _segment_symbol(m, v[0], v[1]) * _segment_symbol(m, v[1], v[2]))]


def t_area_triangle(m: Model, u: Uses) -> list[sp.Eq]:
    T = "area_triangle"
    pg = _the_polygon_cited(m, u, T, 3)
    a, b, c = pg.vertices
    for v, p, q in ((a, c, b), (b, a, c), (c, b, a)):
        an = m.angles.get(angle_key(v, p, q))
        if an is not None and an.exact is not None and sp.simplify(an.exact - 90) == 0:
            return [sp.Eq(AREA, sp.Rational(1, 2) * _segment_symbol(m, v, p) * _segment_symbol(m, v, q))]
    _premise(T, False, "v1 computes a triangle's area from two legs at a right angle; build the height")
    return []


def t_area_circle(m: Model, u: Uses) -> list[sp.Eq]:
    T = "area_circle"
    _premise(T, len(u.circles) == 1, "cite the circle by its id")
    c = m.circles[u.circles[0]]
    return [sp.Eq(AREA, sp.pi * sp.Symbol(f"r_{c.id}") ** 2)]


def t_circumference(m: Model, u: Uses) -> list[sp.Eq]:
    T = "circumference"
    _premise(T, len(u.circles) == 1, "cite the circle by its id")
    c = m.circles[u.circles[0]]
    return [sp.Eq(CIRCUMFERENCE, 2 * sp.pi * sp.Symbol(f"r_{c.id}"))]


def _unsupported(name: str):
    def fn(m: Model, u: Uses) -> list[sp.Eq]:
        raise GeometryRefusal("theorem_premise", f"{name}: in the enum, not yet computed by the v1 engine")
    return fn


THEOREMS: dict[str, Callable[[Model, Uses], list[sp.Eq]]] = {
    "angles_on_line": t_angles_on_line,
    "angles_at_point": t_angles_at_point,
    "vertically_opposite": t_vertically_opposite,
    "angle_addition": t_angle_addition,
    "triangle_angle_sum": t_triangle_angle_sum,
    "quadrilateral_angle_sum": t_quadrilateral_angle_sum,
    "polygon_interior_sum": t_polygon_interior_sum,
    "polygon_exterior_sum": t_polygon_exterior_sum,
    "isosceles_base_angles": t_isosceles_base_angles,
    "equilateral_angles": t_equilateral_angles,
    "exterior_angle_triangle": t_exterior_angle_triangle,
    "alternate_angles": t_alternate_angles,
    "corresponding_angles": t_corresponding_angles,
    "cointerior_angles": t_cointerior_angles,
    "pythagoras": t_pythagoras,
    "perimeter": t_perimeter,
    "area_rectangle": t_area_rectangle,
    "area_triangle": t_area_triangle,
    "area_parallelogram": _unsupported("area_parallelogram"),
    "area_trapezium": _unsupported("area_trapezium"),
    "area_circle": t_area_circle,
    "circumference": t_circumference,
    "area_composite": _unsupported("area_composite"),
}

# The reason an answer key prints for each theorem, English; other
# languages go through maths.i18n when the document path is wired.
REASONS: dict[str, str] = {
    "angles_on_line": "angles on a straight line add up to 180°",
    "angles_at_point": "angles at a point add up to 360°",
    "vertically_opposite": "vertically opposite angles are equal",
    "angle_addition": "the whole angle is the sum of its parts",
    "triangle_angle_sum": "angles in a triangle add up to 180°",
    "quadrilateral_angle_sum": "angles in a quadrilateral add up to 360°",
    "polygon_interior_sum": "interior angles of an n-sided polygon add up to (n − 2) × 180°",
    "polygon_exterior_sum": "exterior angles of a polygon add up to 360°",
    "isosceles_base_angles": "base angles of an isosceles triangle are equal",
    "equilateral_angles": "every angle of an equilateral triangle is 60°",
    "exterior_angle_triangle": "the exterior angle of a triangle equals the sum of the two opposite interior angles",
    "alternate_angles": "alternate angles are equal",
    "corresponding_angles": "corresponding angles are equal",
    "cointerior_angles": "co-interior angles add up to 180°",
    "pythagoras": "Pythagoras' theorem",
    "perimeter": "the perimeter is the sum of the sides",
    "area_rectangle": "area of a rectangle = length × width",
    "area_triangle": "area of a triangle = ½ × base × height",
    "area_parallelogram": "area of a parallelogram = base × height",
    "area_trapezium": "area of a trapezium = ½ × (a + b) × h",
    "area_circle": "area of a circle = πr²",
    "circumference": "circumference = 2πr",
    "area_composite": "the area of a compound shape is the sum of its parts",
}


def apply(m: Model, theorem: str, uses: list[str]) -> list[sp.Eq]:
    fn = THEOREMS.get(theorem)
    if fn is None:
        raise GeometryRefusal("theorem_unknown", f"{theorem!r} is not a theorem v1 knows")
    return fn(m, sort_uses(m, uses, theorem))
