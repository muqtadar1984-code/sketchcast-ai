"""Derived properties: what a figure IS, computed from the facts.

An EVIDENCE question ("which of these triangles are scalene?", "how many
lines of symmetry?") is answered by the engine from the thing drawn, not
looked up from its name: a triangle built with sides 5, 5, 3 is isosceles
because those numbers say so. The ids here share vocabulary with
maths.facts (``lines_of_symmetry``, ``triangle_class_by_sides``), so the
same fact never has two names.

Comparisons prefer the exact value and the construction's equality facts;
a float comparison is the last resort and uses the model's tolerances.
"""

from __future__ import annotations

import math
from typing import Callable, Optional

import sympy as sp

from maths.geometry.errors import GeometryRefusal
from maths.geometry.model import ANGLE_TOL_DEG, LENGTH_REL_TOL, GridPattern, Model, Polygon, angle_key, seg_key

_NAMES = {3: "triangle", 4: "quadrilateral", 5: "pentagon", 6: "hexagon", 7: "heptagon", 8: "octagon",
          9: "nonagon", 10: "decagon", 12: "dodecagon"}


def _the_polygon(m: Model, what: str, *, closed: bool = True) -> Polygon:
    pgs = [p for p in m.polygons.values() if p.closed == closed]
    if len(pgs) != 1:
        raise GeometryRefusal("bad_reference", f"{what}: the figure must contain exactly one shape (it has {len(pgs)})")
    return pgs[0]


def _the_triangle(m: Model, what: str) -> Polygon:
    pg = _the_polygon(m, what)
    if len(pg.vertices) != 3:
        raise GeometryRefusal("bad_reference", f"{what}: the shape is not a triangle")
    return pg


def _side_keys(pg: Polygon) -> list:
    n = len(pg.vertices)
    return [seg_key(pg.vertices[i], pg.vertices[(i + 1) % n]) for i in range(n)]


def _angle_keys(m: Model, pg: Polygon) -> list:
    n = len(pg.vertices)
    out = []
    for i in range(n):
        v, p, q = pg.vertices[i], pg.vertices[i - 1], pg.vertices[(i + 1) % n]
        k = angle_key(v, p, q, "reflex")
        out.append(k if k in m.angles else angle_key(v, p, q))
    return out


def _lengths_equal(m: Model, k1, k2) -> bool:
    if m.lengths_equal(k1, k2):
        return True
    a, b = m.length_float(k1), m.length_float(k2)
    return abs(a - b) <= LENGTH_REL_TOL * max(1.0, a, b)


def _angles_equal(m: Model, k1, k2) -> bool:
    if m.angles_equal(k1, k2):
        return True
    return abs(m.angle_float(k1) - m.angle_float(k2)) <= ANGLE_TOL_DEG


def _angle_value(m: Model, k) -> float:
    an = m.angles.get(k)
    if an is not None and an.exact is not None and not an.exact.free_symbols:
        return float(an.exact)
    return m.angle_float(k)


def triangle_class_by_sides(m: Model) -> str:
    pg = _the_triangle(m, "triangle_class_by_sides")
    s = _side_keys(pg)
    pairs = sum(1 for i, j in ((0, 1), (1, 2), (0, 2)) if _lengths_equal(m, s[i], s[j]))
    return {3: "equilateral", 1: "isosceles", 0: "scalene"}.get(pairs, "isosceles")


def triangle_class_by_angles(m: Model) -> str:
    pg = _the_triangle(m, "triangle_class_by_angles")
    vals = [_angle_value(m, k) for k in _angle_keys(m, pg)]
    if any(abs(v - 90.0) <= ANGLE_TOL_DEG for v in vals):
        return "right"
    if any(v > 90.0 for v in vals):
        return "obtuse"
    return "acute"


def count_right_angles(m: Model) -> int:
    pg = _the_polygon(m, "count_right_angles")
    return sum(1 for k in _angle_keys(m, pg) if k[2] == "interior" and abs(_angle_value(m, k) - 90.0) <= ANGLE_TOL_DEG)


def count_reflex_angles(m: Model) -> int:
    pg = _the_polygon(m, "count_reflex_angles")
    return sum(1 for k in _angle_keys(m, pg) if _angle_value(m, k) > 180.0 + ANGLE_TOL_DEG)


def count_acute_angles(m: Model) -> int:
    pg = _the_polygon(m, "count_acute_angles")
    return sum(1 for k in _angle_keys(m, pg) if _angle_value(m, k) < 90.0 - ANGLE_TOL_DEG)


def count_obtuse_angles(m: Model) -> int:
    pg = _the_polygon(m, "count_obtuse_angles")
    return sum(1 for k in _angle_keys(m, pg) if 90.0 + ANGLE_TOL_DEG < _angle_value(m, k) < 180.0 - ANGLE_TOL_DEG)


def _polygon_symmetry_axes(m: Model, pg: Polygon) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Reflection axes of a polygon from its cyclic side/angle sequence:
    an axis is a position the sequence reads the same both ways from.
    Each axis appears at two positions (its two ends) — position 2i is
    side i's midpoint, position 2i+1 is vertex v_{i+1} — so an axis is
    the pair {k, k+n}, returned as those two anchor points."""
    n = len(pg.vertices)
    sides, angles = _side_keys(pg), _angle_keys(m, pg)
    # seq[2i] = side i (v_i -> v_{i+1}); seq[2i+1] = angle at v_{i+1}
    seq = []
    for i in range(n):
        seq.append(("s", sides[i]))
        seq.append(("a", angles[(i + 1) % n]))
    L = 2 * n

    def same(x, y) -> bool:
        if x[0] != y[0]:
            return False
        return _lengths_equal(m, x[1], y[1]) if x[0] == "s" else _angles_equal(m, x[1], y[1])

    def anchor(k: int) -> tuple[float, float]:
        i, odd = divmod(k, 2)
        if odd:
            return m.xy(pg.vertices[(i + 1) % n])
        a, b = m.xy(pg.vertices[i]), m.xy(pg.vertices[(i + 1) % n])
        return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)

    axes: list[tuple[tuple[float, float], tuple[float, float]]] = []
    seen: set[frozenset] = set()
    for k in range(L):
        if all(same(seq[(k + j) % L], seq[(k - j) % L]) for j in range(1, n + 1)):
            key = frozenset((k, (k + n) % L))
            if key not in seen:
                seen.add(key)
                axes.append((anchor(k), anchor((k + n) % L)))
    return axes


def _polygon_symmetry_lines(m: Model, pg: Polygon) -> int:
    return len(_polygon_symmetry_axes(m, pg))


def symmetry_axes(m: Model) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """The polygon's reflection axes as anchor-point pairs, figure units —
    what a board draws when it says "six lines of symmetry". A grid or a
    circle has none to draw here (the count is still answered)."""
    if m.grids or m.circles:
        return []
    try:
        pg = _the_polygon(m, "symmetry_axes")
    except GeometryRefusal:
        return []
    return _polygon_symmetry_axes(m, pg)


def grid_symmetry_flags(g: GridPattern, fill: Optional[str] = None) -> tuple[bool, bool, bool, bool]:
    """(vertical, horizontal, main diagonal, anti-diagonal): which mirror
    lines the coloured cells have, a blank cell read as ``fill`` when one
    is given. Rows are counted from the top; the main diagonal runs from
    the top-left corner to the bottom-right. Diagonals need a square grid."""
    cells = [[fill if (fill is not None and c == g.blank) else c for c in row] for row in g.cells]
    r, c = g.rows, g.cols
    vertical = all(cells[i][j] == cells[i][c - 1 - j] for i in range(r) for j in range(c))
    horizontal = all(cells[i][j] == cells[r - 1 - i][j] for i in range(r) for j in range(c))
    main = r == c and all(cells[i][j] == cells[j][i] for i in range(r) for j in range(c))
    anti = r == c and all(cells[i][j] == cells[c - 1 - j][r - 1 - i] for i in range(r) for j in range(c))
    return vertical, horizontal, main, anti


def _grid_symmetry_lines(g: GridPattern, fill: Optional[str] = None) -> int:
    return sum(grid_symmetry_flags(g, fill))


def lines_of_symmetry(m: Model, fill: Optional[str] = None) -> int:
    if m.grids:
        if len(m.grids) != 1:
            raise GeometryRefusal("bad_reference", "lines_of_symmetry: one grid per figure")
        return _grid_symmetry_lines(next(iter(m.grids.values())), fill)
    if m.circles and not m.polygons:
        raise GeometryRefusal("bad_reference", "lines_of_symmetry: a circle has infinitely many; not a question v1 asks")
    return _polygon_symmetry_lines(m, _the_polygon(m, "lines_of_symmetry"))


def is_polygon(m: Model) -> bool:
    closed = [p for p in m.polygons.values() if p.closed]
    open_ = [p for p in m.polygons.values() if not p.closed]
    if closed and not open_ and not m.circles:
        return True
    if (open_ or m.circles) and not closed:
        return False
    raise GeometryRefusal("bad_reference", "is_polygon: the figure mixes a polygon with something else")


def polygon_name(m: Model) -> str:
    pg = _the_polygon(m, "polygon_name")
    n = len(pg.vertices)
    name = _NAMES.get(n, f"{n}-gon")
    sides, angles = _side_keys(pg), _angle_keys(m, pg)
    regular = all(_lengths_equal(m, sides[0], s) for s in sides) and all(_angles_equal(m, angles[0], a) for a in angles)
    return f"regular {name}" if regular and n > 3 else ("equilateral triangle" if regular else name)


def _fmt_coord(v) -> str:
    e = v if isinstance(v, sp.Basic) else sp.nsimplify(v)
    return str(int(e)) if getattr(e, "is_Integer", False) else str(e)


def point_name(pid: str, label: Optional[str]) -> str:
    """What a point is called on the paper: its label, else its id's own
    letter (p_a -> A, p_m -> M), else the id. The chapter-17 probe
    (2026-10-08) placed every point without a label and every evidence
    question was refused with 'the figure gives {}'."""
    if label:
        return label
    tail = pid[2:] if pid.lower().startswith("p_") else pid
    return tail.upper() if tail.isalpha() and len(tail) <= 2 else pid


def _labelled_points(m: Model) -> dict[str, str]:
    """name -> point id for every point a student can name: by its label,
    or by its id's letter when the model gave it none. Hidden bookkeeping
    points (an id the engine made, e.g. 'v3', 'grid_1') are not points a
    student reads and stay out."""
    out: dict[str, str] = {}
    for pid, pt in m.points.items():
        if getattr(pt, "hidden", False):
            continue
        name = point_name(pid, pt.label)
        if name != pid or pt.label:
            out[name] = pid
    return out


def coordinates_of(m: Model) -> dict[str, str]:
    """v2: each labelled point's coordinates, read from the figure, as
    "(x, y)" — exact where the point was placed exactly."""
    if m.axes is None:
        raise GeometryRefusal("bad_reference", "coordinates_of: the figure has no axes")
    out = {}
    for lab, pid in _labelled_points(m).items():
        pt = m.points[pid]
        if pt.exact is not None:
            out[lab] = f"({_fmt_coord(pt.exact[0])}, {_fmt_coord(pt.exact[1])})"
        else:
            out[lab] = f"({_fmt_coord(sp.nsimplify(round(pt.x, 6)))}, {_fmt_coord(sp.nsimplify(round(pt.y, 6)))})"
    return out


def quadrant(m: Model) -> dict[str, str]:
    """v2: which quadrant each labelled point lies in (1-4), or the axis it
    sits on."""
    if m.axes is None:
        raise GeometryRefusal("bad_reference", "quadrant: the figure has no axes")
    out = {}
    for lab, pid in _labelled_points(m).items():
        x, y = m.xy(pid)
        if abs(x) < 1e-9 and abs(y) < 1e-9:
            out[lab] = "origin"
        elif abs(y) < 1e-9:
            out[lab] = "x-axis"
        elif abs(x) < 1e-9:
            out[lab] = "y-axis"
        else:
            out[lab] = {(True, True): "1", (False, True): "2", (False, False): "3", (True, False): "4"}[(x > 0, y > 0)]
    return out


PROPERTIES: dict[str, Callable] = {
    "coordinates_of": coordinates_of,
    "quadrant": quadrant,
    "triangle_class_by_sides": triangle_class_by_sides,
    "triangle_class_by_angles": triangle_class_by_angles,
    "count_right_angles": count_right_angles,
    "count_reflex_angles": count_reflex_angles,
    "count_acute_angles": count_acute_angles,
    "count_obtuse_angles": count_obtuse_angles,
    "lines_of_symmetry": lines_of_symmetry,
    "is_polygon": is_polygon,
    "polygon_name": polygon_name,
}


def compute(m: Model, prop: str, **kw):
    fn = PROPERTIES.get(prop)
    if fn is None:
        raise GeometryRefusal("theorem_unknown", f"{prop!r} is not a property v1 computes")
    return fn(m, **kw) if kw else fn(m)
