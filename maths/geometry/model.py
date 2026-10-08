"""The canonical geometry model: a facts graph, plus one float realisation.

Truth lives in the FACTS — which points share a line, which lines are
parallel, which lengths and angles are equal, and the exact measure of an
angle or a length where a construction fixes it (a SymPy Rational, or an
expression in the question's unknown). Coordinates are a REALISATION of
those facts: one set of floats the constructions computed, used to check
that the facts agree with a drawable figure and to feed a renderer. Nothing
reads a coordinate to decide what is true; the verifier reads the facts and
proves with theorems (maths.geometry.verify).

Angles are keyed by (vertex, {arm, arm}, region), so angle ABC and angle
CBA are the same fact; segments by {end, end}. A spec's ids map onto those
keys (``angle_ids``, ``segment_ids``), which is what lets a step name an
angle the model never listed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import sympy as sp

from maths.geometry.errors import GeometryRefusal

Region = str  # "interior" | "reflex"
AngleKey = tuple[str, frozenset, Region]
SegKey = frozenset

ANGLE_TOL_DEG = 1e-6      # a realised angle against its exact value
LENGTH_REL_TOL = 1e-9     # a realised length against its exact value (relative)


@dataclass
class Point:
    id: str
    x: float
    y: float
    label: Optional[str] = None
    # v2: the exact coordinates when the point was placed BY coordinate
    # (point_at) or derived from such points (midpoint, intersection,
    # reflection); None for a v1 construction's point
    exact: Optional[tuple] = None

    @property
    def xy(self) -> tuple[float, float]:
        return (self.x, self.y)


@dataclass
class Line:
    """Points known to be collinear, in order along the line."""
    id: str
    points: list[str]
    hidden: bool = False


@dataclass
class Ray:
    id: str
    vertex: str
    through: str


@dataclass
class Segment:
    key: SegKey
    exact: Optional[sp.Expr] = None     # length, when a construction fixed it


@dataclass
class Angle:
    key: AngleKey
    exact: Optional[sp.Expr] = None     # degrees, when a construction fixed it

    @property
    def vertex(self) -> str:
        return self.key[0]

    @property
    def arms(self) -> tuple[str, str]:
        a, b = sorted(self.key[1])
        return a, b

    @property
    def region(self) -> Region:
        return self.key[2]


@dataclass
class Polygon:
    id: str
    vertices: list[str]           # in order round the boundary
    closed: bool = True
    kind: str = "polygon"         # triangle | quadrilateral | regular | polygon | open


@dataclass
class Circle:
    id: str
    centre: str
    radius: float
    exact: Optional[sp.Expr] = None


@dataclass
class GridPattern:
    id: str
    rows: int
    cols: int
    cells: list[list[str]]
    palette: list[str]
    blank: str = "*"


def angle_key(vertex: str, a: str, b: str, region: Region = "interior") -> AngleKey:
    return (vertex, frozenset((a, b)), region)


def seg_key(a: str, b: str) -> SegKey:
    return frozenset((a, b))


@dataclass
class Axes:
    """v2: the coordinate grid a figure is drawn on. Figure units ARE
    coordinate units; the axes fix what is drawn and what a student may
    read off (a point sits on a grid intersection or is not discernible)."""
    id: str
    x0: float
    x1: float
    y0: float
    y1: float
    step: float = 1.0
    grid: bool = True


def coord_symbols(pid: str) -> tuple[sp.Symbol, sp.Symbol]:
    """The chain's symbols for a point's coordinates, resolved like an
    angle's symbol: to the exact coordinates when the figure fixes them,
    to the question's unknowns (a, b) when it asks for them."""
    return sp.Symbol(f"x_{pid}"), sp.Symbol(f"y_{pid}")


@dataclass
class Model:
    points: dict[str, Point] = field(default_factory=dict)
    lines: dict[str, Line] = field(default_factory=dict)
    rays: dict[str, Ray] = field(default_factory=dict)
    segments: dict[SegKey, Segment] = field(default_factory=dict)
    angles: dict[AngleKey, Angle] = field(default_factory=dict)
    polygons: dict[str, Polygon] = field(default_factory=dict)
    circles: dict[str, Circle] = field(default_factory=dict)
    grids: dict[str, GridPattern] = field(default_factory=dict)
    axes: Optional[Axes] = None                                   # v2
    reflections: dict[str, tuple[str, str]] = field(default_factory=dict)   # image -> (source, mirror)
    parallel: set[frozenset] = field(default_factory=set)        # {line id, line id}
    perpendicular: set[frozenset] = field(default_factory=set)
    equal_lengths: set[frozenset] = field(default_factory=set)   # {SegKey, SegKey}
    equal_angles: set[frozenset] = field(default_factory=set)    # {AngleKey, AngleKey}
    angle_ids: dict[str, AngleKey] = field(default_factory=dict)
    segment_ids: dict[str, SegKey] = field(default_factory=dict)
    object_ids: dict[str, tuple[str, str]] = field(default_factory=dict)  # id -> (kind, key)
    bind: dict[str, sp.Expr] = field(default_factory=dict)
    units: Optional[str] = None
    _auto: int = 0

    # ── points ────────────────────────────────────────────────────────────

    def has_point(self, pid: str) -> bool:
        return pid in self.points

    def place(self, pid: str, x: float, y: float, label: Optional[str] = None) -> Point:
        if pid in self.points:
            raise GeometryRefusal("bad_reference", f"point {pid!r} is placed twice", pid)
        p = Point(pid, float(x), float(y), label)
        self.points[pid] = p
        return p

    def new_point_id(self, hint: str = "v") -> str:
        self._auto += 1
        pid = f"{hint}{self._auto}"
        while pid in self.points:
            self._auto += 1
            pid = f"{hint}{self._auto}"
        return pid

    def xy(self, pid: str) -> tuple[float, float]:
        try:
            return self.points[pid].xy
        except KeyError:
            raise GeometryRefusal("bad_reference", f"point {pid!r} does not exist", pid) from None

    # ── facts ─────────────────────────────────────────────────────────────

    def add_line(self, lid: str, points: list[str], hidden: bool = False) -> Line:
        if lid in self.lines:
            raise GeometryRefusal("bad_reference", f"line {lid!r} is defined twice", lid)
        ln = Line(lid, list(points), hidden)
        self.lines[lid] = ln
        self.object_ids[lid] = ("line", lid)
        return ln

    def add_ray(self, rid: str, vertex: str, through: str) -> Ray:
        r = Ray(rid, vertex, through)
        self.rays[rid] = r
        self.object_ids[rid] = ("ray", rid)
        return r

    def segment(self, a: str, b: str, exact: Optional[sp.Expr] = None) -> Segment:
        k = seg_key(a, b)
        s = self.segments.get(k)
        if s is None:
            s = Segment(k, exact)
            self.segments[k] = s
        elif exact is not None and s.exact is None:
            s.exact = exact
        return s

    def angle(self, vertex: str, a: str, b: str, region: Region = "interior",
              exact: Optional[sp.Expr] = None) -> Angle:
        k = angle_key(vertex, a, b, region)
        an = self.angles.get(k)
        if an is None:
            an = Angle(k, exact)
            self.angles[k] = an
        elif exact is not None and an.exact is None:
            an.exact = exact
        return an

    def add_polygon(self, pid: str, vertices: list[str], kind: str, closed: bool = True) -> Polygon:
        if pid in self.polygons:
            raise GeometryRefusal("bad_reference", f"shape {pid!r} is defined twice", pid)
        pg = Polygon(pid, list(vertices), closed, kind)
        self.polygons[pid] = pg
        self.object_ids[pid] = ("polygon", pid)
        if closed:
            n = len(vertices)
            for i in range(n):
                self.segment(vertices[i], vertices[(i + 1) % n])
        else:
            for i in range(len(vertices) - 1):
                self.segment(vertices[i], vertices[i + 1])
        return pg

    def set_parallel(self, l1: str, l2: str) -> None:
        if l1 != l2:
            self.parallel.add(frozenset((l1, l2)))

    def set_perpendicular(self, l1: str, l2: str) -> None:
        if l1 != l2:
            self.perpendicular.add(frozenset((l1, l2)))

    def set_equal_lengths(self, *keys: SegKey) -> None:
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                if keys[i] != keys[j]:
                    self.equal_lengths.add(frozenset((keys[i], keys[j])))

    def set_equal_angles(self, *keys: AngleKey) -> None:
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                if keys[i] != keys[j]:
                    self.equal_angles.add(frozenset((keys[i], keys[j])))

    def close(self) -> None:
        """Closure rules — the closed set from the plan: parallel is
        transitive; a line perpendicular to one of two parallels is
        perpendicular to the other; equality of lengths and of angles is
        transitive. Relations derived here are facts."""
        changed = True
        while changed:
            changed = False
            for a in list(self.parallel):
                for b in list(self.parallel):
                    common = a & b
                    if a != b and len(common) == 1:
                        new = (a | b) - common
                        if len(new) == 2 and new not in self.parallel:
                            self.parallel.add(new)
                            changed = True
            for p in list(self.perpendicular):
                for q in list(self.parallel):
                    common = p & q
                    if len(common) == 1:
                        new = (p | q) - common
                        if len(new) == 2 and new not in self.perpendicular:
                            self.perpendicular.add(new)
                            changed = True
            for store in (self.equal_lengths, self.equal_angles):
                for a in list(store):
                    for b in list(store):
                        common = a & b
                        if a != b and len(common) == 1:
                            new = (a | b) - common
                            if len(new) == 2 and new not in store:
                                store.add(new)
                                changed = True

    # ── queries over the facts ────────────────────────────────────────────

    def line_through(self, *pids: str) -> Optional[Line]:
        """A line fact containing every one of these points."""
        want = set(pids)
        for ln in self.lines.values():
            if want <= set(ln.points):
                return ln
        return None

    def lines_through(self, pid: str) -> list[Line]:
        return [ln for ln in self.lines.values() if pid in ln.points]

    def collinear(self, pids: list[str]) -> bool:
        return self.line_through(*pids) is not None

    def between(self, a: str, v: str, b: str) -> bool:
        """V lies between A and B on one line fact (A-V-B)."""
        ln = self.line_through(a, v, b)
        if ln is None:
            return False
        ia, iv, ib = (ln.points.index(x) for x in (a, v, b))
        return (ia < iv < ib) or (ib < iv < ia)

    def are_parallel(self, l1: str, l2: str) -> bool:
        return l1 == l2 or frozenset((l1, l2)) in self.parallel

    def are_perpendicular(self, l1: str, l2: str) -> bool:
        return frozenset((l1, l2)) in self.perpendicular

    def lengths_equal(self, s1: SegKey, s2: SegKey) -> bool:
        if s1 == s2 or frozenset((s1, s2)) in self.equal_lengths:
            return True
        a, b = self.segments.get(s1), self.segments.get(s2)
        if a and b and a.exact is not None and b.exact is not None:
            return _exact_equal(a.exact, b.exact)
        return False

    def angles_equal(self, k1: AngleKey, k2: AngleKey) -> bool:
        if k1 == k2 or frozenset((k1, k2)) in self.equal_angles:
            return True
        a, b = self.angles.get(k1), self.angles.get(k2)
        if a and b and a.exact is not None and b.exact is not None:
            return _exact_equal(a.exact, b.exact)
        return False

    def canonical_angle_id(self, aid: str) -> str:
        """The first id bound to this angle's key: angle_bac, angle_cab and
        angle_a are one angle and must be one symbol in a proof."""
        k = self.angle_ids.get(aid)
        if k is None:
            return aid
        return next(i for i, kk in self.angle_ids.items() if kk == k)

    def angle_by_id(self, aid: str) -> Angle:
        k = self.angle_ids.get(aid)
        if k is None or k not in self.angles:
            raise GeometryRefusal("bad_reference", f"angle {aid!r} is not defined", aid)
        return self.angles[k]

    def segment_by_id(self, sid: str) -> Segment:
        k = self.segment_ids.get(sid)
        if k is None or k not in self.segments:
            raise GeometryRefusal("bad_reference", f"segment {sid!r} is not defined", sid)
        return self.segments[k]

    def polygon_with_vertices(self, vertices: set[str]) -> Optional[Polygon]:
        for pg in self.polygons.values():
            if pg.closed and set(pg.vertices) == vertices:
                return pg
        return None

    # ── the realisation ───────────────────────────────────────────────────

    def angle_float(self, key: AngleKey) -> float:
        v, arms, region = key
        a, b = sorted(arms)
        vx, vy = self.xy(v)
        ax, ay = self.xy(a)
        bx, by = self.xy(b)
        d1 = math.atan2(ay - vy, ax - vx)
        d2 = math.atan2(by - vy, bx - vx)
        interior = abs(math.degrees(d2 - d1)) % 360.0
        if interior > 180.0:
            interior = 360.0 - interior
        return 360.0 - interior if region == "reflex" else interior

    def length_float(self, key: SegKey) -> float:
        a, b = sorted(key)
        return math.dist(self.xy(a), self.xy(b))

    def side_of_line(self, line: Line, pid: str) -> int:
        """Which side of a line fact a point lies on, from the realisation:
        +1 / -1, or 0 when it lies on the line. A sign, not a measurement —
        it is the topology the construction produced."""
        p0, p1 = self.xy(line.points[0]), self.xy(line.points[-1])
        px, py = self.xy(pid)
        cross = (p1[0] - p0[0]) * (py - p0[1]) - (p1[1] - p0[1]) * (px - p0[0])
        scale = max(1e-12, math.dist(p0, p1))
        if abs(cross) / scale < 1e-9:
            return 0
        return 1 if cross > 0 else -1

    def exact_xy(self, pid: str) -> tuple[sp.Expr, sp.Expr]:
        """A point's exact coordinates, or its coordinate symbols."""
        p = self.points.get(pid)
        if p is not None and p.exact is not None:
            return sp.sympify(p.exact[0]), sp.sympify(p.exact[1])
        return coord_symbols(pid)

    def bbox(self) -> tuple[float, float, float, float]:
        xs = [p.x for p in self.points.values()]
        ys = [p.y for p in self.points.values()]
        if self.axes is not None:
            xs += [self.axes.x0, self.axes.x1]
            ys += [self.axes.y0, self.axes.y1]
        for c in self.circles.values():
            cx, cy = self.xy(c.centre)
            xs += [cx - c.radius, cx + c.radius]
            ys += [cy - c.radius, cy + c.radius]
        if not xs:
            return (0.0, 0.0, 1.0, 1.0)
        return (min(xs), min(ys), max(xs), max(ys))

    def transform(self, fn) -> None:
        for p in self.points.values():
            p.x, p.y = fn(p.x, p.y)


# ── exact arithmetic helpers ──────────────────────────────────────────────

def _exact_equal(a: sp.Expr, b: sp.Expr) -> bool:
    try:
        return sp.simplify(a - b) == 0
    except Exception:  # noqa: BLE001 — SymPy refused; the facts cannot say
        return False


def to_float(expr: sp.Expr, bind: dict[str, sp.Expr]) -> float:
    """A measure as a number, with the figure's bound unknowns substituted.
    Refuses (closure_failed) when a symbol is left unbound: the figure
    cannot be drawn without it."""
    e = sp.sympify(expr)
    if bind:
        e = e.subs({sp.Symbol(k): v for k, v in bind.items()})
    if e.free_symbols:
        names = ", ".join(sorted(str(s) for s in e.free_symbols))
        raise GeometryRefusal("closure_failed", f"{expr} needs a value for {names} (add it to `bind`)")
    try:
        return float(e)
    except (TypeError, ValueError) as exc:
        raise GeometryRefusal("construction_impossible", f"{expr} is not a number: {exc}") from exc


def is_exact_number(expr: Optional[sp.Expr]) -> bool:
    return expr is not None and not sp.sympify(expr).free_symbols
