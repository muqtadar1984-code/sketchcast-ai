"""v3: 3D solids drawn flat — the geometry behind the `cube`, `cuboid`,
`prism`, `pyramid`, `cylinder`, `cone` and `sphere` constructions
(geometry-3d-v3-proposal.md; founder decisions 2026-10-09: all eight
solids, ISOMETRIC projection, nets from the closed list with improvisation
as a fallback).

A solid is a 3D object the figure can only SHOW as a 2D picture. The
picture is a view, never a measurement: the exact numbers live in the
facts (edge lengths, heights, radii as measures on named segments), and an
evidence question on a solid is about what can be COUNTED. One fixed
projection draws every solid: the isometric drawing convention —

    screen_x = (x − y) · √3/2        screen_y = z + (x + y) / 2

— the viewer above and in front, left. Under it every edge along an axis
keeps its TRUE length (an x-edge of length l spans l on the page), which
is why a given edge length agrees with the compiled figure and the
compiler's given-measure check passes; only angles change. Hidden edges
are found by back-face culling against the view direction and drawn
dashed. Curved solids draw their rims as ellipses (the projection of a
horizontal circle), the front arc solid and the back arc dashed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import sympy as sp

Vec3 = tuple[float, float, float]
VIEW: Vec3 = (-1.0, -1.0, 1.0)          # towards the viewer: a face is seen when its outward normal leans this way
_C30 = math.sqrt(3) / 2
_E30 = sp.sqrt(3) / 2

KINDS = ("cube", "cuboid", "prism", "pyramid", "cylinder", "cone", "sphere")

# faces / edges / vertices a student counts (a cylinder has 3 faces, 2
# edges and no vertex; a cone 2, 1, 1; a sphere 1, 0, 0 — the textbook
# convention maths/facts.py also uses)
COUNTS = {"cube": (6, 12, 8), "cuboid": (6, 12, 8), "prism": (5, 9, 6), "pyramid": (5, 8, 5),
          "cylinder": (3, 2, 0), "cone": (2, 1, 1), "sphere": (1, 0, 0)}
NAMES = {"cube": "cube", "cuboid": "cuboid", "prism": "triangular prism", "pyramid": "square-based pyramid",
         "cylinder": "cylinder", "cone": "cone", "sphere": "sphere"}


def project(p: Vec3) -> tuple[float, float]:
    x, y, z = p
    return ((x - y) * _C30, z + (x + y) / 2.0)


def project_exact(p: tuple) -> tuple:
    x, y, z = (sp.nsimplify(v) for v in p)
    return (sp.expand((x - y) * _E30), sp.expand(z + (x + y) / 2))


@dataclass
class Solid:
    id: str
    kind: str
    dims: dict[str, sp.Expr]                          # edge / length / width / height / radius / base / base_height
    vertices: dict[str, Vec3] = field(default_factory=dict)   # point id -> 3D position
    faces: list[list[str]] = field(default_factory=list)       # vertex ids, outward-wound once oriented
    edges: list[tuple[str, str, bool]] = field(default_factory=list)   # (a, b, hidden)
    # the measurable segments by role: the theorem reads them by ROLE and
    # turns them into length symbols of the question's segment ids
    roles: dict[str, tuple[str, str]] = field(default_factory=dict)
    # polylines in figure (screen) units a curved solid draws, (points, dashed, tag)
    curves: list[tuple[list[tuple[float, float]], bool, str]] = field(default_factory=list)
    extent: list[tuple[float, float]] = field(default_factory=list)   # screen points the bbox must hold
    hidden_points: set[str] = field(default_factory=set)      # helper points: no dot, no name

    @property
    def counts(self) -> tuple[int, int, int]:
        return COUNTS[self.kind]

    @property
    def name(self) -> str:
        return NAMES[self.kind]


# ── visibility ─────────────────────────────────────────────────────────────


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def face_normal(face: list[str], verts: dict[str, Vec3], centre: Vec3) -> Vec3:
    """Newell's normal, flipped to point away from the solid's centre."""
    nx = ny = nz = 0.0
    for i, a in enumerate(face):
        p, q = verts[a], verts[face[(i + 1) % len(face)]]
        nx += (p[1] - q[1]) * (p[2] + q[2])
        ny += (p[2] - q[2]) * (p[0] + q[0])
        nz += (p[0] - q[0]) * (p[1] + q[1])
    n = (nx, ny, nz)
    cx = sum(verts[v][0] for v in face) / len(face)
    cy = sum(verts[v][1] for v in face) / len(face)
    cz = sum(verts[v][2] for v in face) / len(face)
    if _dot(n, _sub((cx, cy, cz), centre)) < 0:
        n = (-nx, -ny, -nz)
    return n


def visible_faces(faces: list[list[str]], verts: dict[str, Vec3]) -> list[bool]:
    n = len(verts)
    centre = (sum(v[0] for v in verts.values()) / n, sum(v[1] for v in verts.values()) / n,
              sum(v[2] for v in verts.values()) / n)
    return [_dot(face_normal(f, verts, centre), VIEW) > 1e-9 for f in faces]


def cull_edges(faces: list[list[str]], verts: dict[str, Vec3]) -> list[tuple[str, str, bool]]:
    """Every edge of the polyhedron once, hidden when no face it belongs to
    faces the viewer."""
    seen = visible_faces(faces, verts)
    vis: dict[frozenset, bool] = {}
    order: list[frozenset] = []
    for f, ok in zip(faces, seen):
        for i, a in enumerate(f):
            k = frozenset((a, f[(i + 1) % len(f)]))
            if k not in vis:
                vis[k] = False
                order.append(k)
            vis[k] = vis[k] or ok
    out = []
    for k in order:
        a, b = sorted(k)
        out.append((a, b, not vis[k]))
    return out


# ── curved rims ────────────────────────────────────────────────────────────


def rim(cx: float, cy: float, cz: float, r: float, *, front: Optional[bool] = None, n: int = 48) -> list[tuple[float, float]]:
    """A horizontal circle of radius r at (cx, cy, cz), projected: the whole
    rim, or its front half (nearer the viewer: x + y below the centre's,
    θ in 135°..315°) or its back half."""
    if front is None:
        t0, t1 = 0.0, 2 * math.pi
    elif front:
        t0, t1 = 3 * math.pi / 4, 7 * math.pi / 4
    else:
        t0, t1 = -math.pi / 4, 3 * math.pi / 4
    pts = []
    for i in range(n + 1):
        t = t0 + (t1 - t0) * i / n
        pts.append(project((cx + r * math.cos(t), cy + r * math.sin(t), cz)))
    return pts


def silhouette(cx: float, cy: float, r: float) -> tuple[Vec3, Vec3]:
    """The two rim points at the picture's left and right extremes (θ = 135°
    and −45°): where a cylinder's straight sides stand."""
    k = r / math.sqrt(2)
    return ((cx - k, cy + k, 0.0), (cx + k, cy - k, 0.0))
