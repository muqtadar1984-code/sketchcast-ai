"""v3: nets — a cube or cuboid unfolded flat (geometry-3d-v3-proposal.md;
founder 2026-10-09: nets from the closed list of the 11 cube nets, with
improvisation as a fallback — a layout not on the list is built from
explicit cells and VALIDATED BY FOLDING).

A cube net is six squares joined edge to edge that fold into a cube.
There are exactly eleven (up to turning and flipping), named below; any
other arrangement a question draws is given as ``cells`` and folded here:
walking the cells from one square, each step across a shared edge turns
the paper's frame about that edge, so every cell lands on one face of
the cube — a valid net lands its six cells on six DIFFERENT faces. The
same walk says which cell is opposite which (the face with the opposite
normal), the fact "which face is opposite the shaded one" asks for.
Nothing is improvised in the picture: a net that does not fold is still
drawn (it may be the wrong answer of a which-folds question) and
``folds`` says so.

A cuboid net is the cross layout with the faces at their own sizes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

Cell2 = tuple[int, int]

# the eleven cube nets as (row, col) cells; row 0 at the top of the page
LAYOUTS: dict[str, list[Cell2]] = {
    # 1-4-1: a strip of four with one square above and one below
    "cross": [(0, 1), (1, 0), (1, 1), (1, 2), (1, 3), (2, 1)],
    "skew_cross": [(0, 1), (1, 0), (1, 1), (1, 2), (1, 3), (2, 2)],
    "end_cross": [(0, 0), (1, 0), (1, 1), (1, 2), (1, 3), (2, 0)],
    "end_skew": [(0, 0), (1, 0), (1, 1), (1, 2), (1, 3), (2, 1)],
    "end_far": [(0, 0), (1, 0), (1, 1), (1, 2), (1, 3), (2, 2)],
    "end_ends": [(0, 0), (1, 0), (1, 1), (1, 2), (1, 3), (2, 3)],
    # 2-3-1: a pair above the strip's first square, the strip of three, one
    # square below it (the enumeration of all 216 hexominoes gives exactly
    # these three, with the eight above, 2026-10-09)
    "pair_mid": [(0, 0), (0, 1), (1, 1), (1, 2), (1, 3), (2, 1)],
    "pair_far": [(0, 0), (0, 1), (1, 1), (1, 2), (1, 3), (2, 2)],
    "pair_end": [(0, 0), (0, 1), (1, 1), (1, 2), (1, 3), (2, 3)],
    # 2-2-2 and 3-3
    "staircase": [(0, 0), (0, 1), (1, 1), (1, 2), (2, 2), (2, 3)],
    "two_rows": [(0, 0), (0, 1), (0, 2), (1, 2), (1, 3), (1, 4)],
}

Vec3 = tuple[int, int, int]


def _neg(v: Vec3) -> Vec3:
    return (-v[0], -v[1], -v[2])


def fold(cells: list[Cell2]) -> Optional[list[Vec3]]:
    """The cube face (as an outward normal) each cell lands on when the
    paper folds, in the cells' order — or None when the cells are not six
    edge-connected squares. Six DISTINCT normals mean a cube net."""
    if len(cells) != 6 or len(set(cells)) != 6:
        return None
    index = {c: i for i, c in enumerate(cells)}
    normals: list[Optional[Vec3]] = [None] * 6
    # frame of the first cell: it lies on the bottom face, right = +x, up = +y
    frames: dict[int, tuple[Vec3, Vec3, Vec3]] = {0: ((0, 0, -1), (1, 0, 0), (0, 1, 0))}
    normals[0] = (0, 0, -1)
    todo = [0]
    while todo:
        i = todo.pop()
        n, r, u = frames[i]
        row, col = cells[i]
        for (dr, dc) in ((0, 1), (0, -1), (-1, 0), (1, 0)):
            j = index.get((row + dr, col + dc))
            if j is None or normals[j] is not None:
                continue
            if (dr, dc) == (0, 1):        # step right: the paper turns about the right edge
                nn, nr, nu = r, _neg(n), u
            elif (dr, dc) == (0, -1):
                nn, nr, nu = _neg(r), n, u
            elif (dr, dc) == (-1, 0):     # step up the page
                nn, nr, nu = u, r, _neg(n)
            else:
                nn, nr, nu = _neg(u), r, n
            frames[j] = (nn, nr, nu)
            normals[j] = nn
            todo.append(j)
    if any(v is None for v in normals):
        return None                        # not connected
    return [v for v in normals if v is not None]


def folds_to_cube(cells: list[Cell2]) -> bool:
    ns = fold(cells)
    return ns is not None and len(set(ns)) == 6


@dataclass
class Cell:
    label: str
    rc: Cell2
    box: tuple[float, float, float, float]      # x0, y0, x1, y1 in figure units (y up)
    shaded: bool = False


@dataclass
class Net:
    id: str
    of: str                                      # cube | cuboid
    layout: str                                  # a LAYOUTS name, or "custom"
    cells: list[Cell]
    folds: bool
    opposite: dict[str, str] = field(default_factory=dict)   # label -> label of the face opposite
    extent: list[tuple[float, float]] = field(default_factory=list)


def _opposites(cells: list[Cell2], labels: list[str]) -> dict[str, str]:
    ns = fold(cells)
    if ns is None or len(set(ns)) != 6:
        return {}
    by_normal = {n: labels[i] for i, n in enumerate(ns)}
    return {labels[i]: by_normal[_neg(n)] for i, n in enumerate(ns)}


def cube_net(nid: str, *, layout: Optional[str], cells: Optional[list[Cell2]], edge: float,
             labels: Optional[list[str]], shaded: Optional[str]) -> Net:
    if cells is None:
        name = layout or "cross"
        if name not in LAYOUTS:
            raise ValueError(f"layout {name!r} is not one of the eleven cube nets: {', '.join(LAYOUTS)}")
        cells = list(LAYOUTS[name])
    else:
        name = "custom"
        if len(cells) != 6 or len(set(cells)) != 6:
            raise ValueError("a cube net has six different cells")
    labs = [str(x) for x in (labels or [str(i + 1) for i in range(6)])]
    if len(labs) != 6 or len(set(labs)) != 6:
        raise ValueError("a net's labels are six different strings")
    if shaded is not None and shaded not in labs:
        raise ValueError(f"shaded cell {shaded!r} is not one of the labels")
    ok = folds_to_cube(cells)
    rows = max(r for r, _c in cells) + 1
    out: list[Cell] = []
    for (r, c), lab in zip(cells, labs):
        x0, y0 = c * edge, (rows - 1 - r) * edge
        out.append(Cell(lab, (r, c), (x0, y0, x0 + edge, y0 + edge), shaded=(lab == shaded)))
    net = Net(nid, "cube", name, out, ok, _opposites(cells, labs) if ok else {})
    net.extent = [(b.box[0], b.box[1]) for b in out] + [(b.box[2], b.box[3]) for b in out]
    return net


def cuboid_net(nid: str, *, length: float, width: float, height: float, labels: Optional[list[str]],
               shaded: Optional[str]) -> Net:
    """The cross: top, front, bottom, back down the middle (each `length`
    wide; top and bottom `width` tall, front and back `height` tall), the
    two sides beside the front."""
    labs = [str(x) for x in (labels or ["top", "front", "bottom", "back", "left", "right"])]
    if len(labs) != 6 or len(set(labs)) != 6:
        raise ValueError("a net's labels are six different strings")
    if shaded is not None and shaded not in labs:
        raise ValueError(f"shaded cell {shaded!r} is not one of the labels")
    l, w, h = float(length), float(width), float(height)
    x0 = w                                   # the column starts right of the left side cell
    # from the top of the page down: top (w tall), front (h), bottom (w), back (h)
    y_top = w + h + w + h
    boxes = [
        (x0, y_top - w, x0 + l, y_top),                      # top
        (x0, y_top - w - h, x0 + l, y_top - w),              # front
        (x0, y_top - 2 * w - h, x0 + l, y_top - w - h),      # bottom
        (x0, 0.0, x0 + l, h),                                # back
        (0.0, y_top - w - h, w, y_top - w),                  # left side, beside the front
        (x0 + l, y_top - w - h, x0 + l + w, y_top - w),      # right side
    ]
    cells = [Cell(lab, rc, box, shaded=(lab == shaded)) for lab, rc, box in
             zip(labs, [(0, 1), (1, 1), (2, 1), (3, 1), (1, 0), (1, 2)], boxes)]
    opposite = {labs[0]: labs[2], labs[2]: labs[0], labs[1]: labs[3], labs[3]: labs[1], labs[4]: labs[5], labs[5]: labs[4]}
    net = Net(nid, "cuboid", "cross", cells, True, opposite)
    net.extent = [(b.box[0], b.box[1]) for b in cells] + [(b.box[2], b.box[3]) for b in cells]
    return net
