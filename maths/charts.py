"""Charts for algebra examples — DERIVED, never authored.

The geometry figure path lets the model describe a figure the engine then
verifies. A chart starts from what is already verified — the example's
givens and its proved answer — and is a geometry.figure.v2 ILLUSTRATION the
same renderer, board adapter, worksheet printer, deck and quiz player
already consume. The model never writes one; one that cannot be built is
omitted, never faked (founder decisions, 2026-10-09; the catalogue's
"Simultaneous Linear Equations" solved well and drew nothing).

Phase 1: linear graphs and simultaneous equations. `chart_for(ex)` gives,
for a solve_system of two lines in x and y, or a solve with one
two-variable line, two specs: `beside` (the lines, drawn next to the
working from the start) and `closing` (the lines and the solution point,
for the scene after the solution), with the point as text.
"""

from __future__ import annotations

import math
from typing import Optional

import sympy as sp

from maths.notation import NotationError, parse_relation
from maths.schema import WorkedExample

X, Y = sp.Symbol("x"), sp.Symbol("y")
SCHEMA = "geometry.figure.v2"
MAX_SPAN = 60            # units across an axis beyond which no chart is drawn
LABEL_MAX = 18


def _linear(text: str) -> Optional[tuple[sp.Rational, sp.Rational, sp.Rational]]:
    """(a, b, c) with a·x + b·y = c for a line in x and y, else None."""
    try:
        rel = parse_relation(text)
    except NotationError:
        return None
    if not rel.is_equation or rel.rhs is None:
        return None
    expr = sp.expand(rel.lhs - rel.rhs)
    if not expr.free_symbols or not expr.free_symbols <= {X, Y}:
        return None
    try:
        poly = sp.Poly(expr, X, Y)
    except sp.PolynomialError:
        return None
    if poly.total_degree() != 1:
        return None
    a, b = poly.coeff_monomial(X), poly.coeff_monomial(Y)
    c = -poly.coeff_monomial(1)
    if not all(v.is_Rational for v in (a, b, c)):
        return None
    return sp.Rational(a), sp.Rational(b), sp.Rational(c)


def _solution(ex: WorkedExample) -> Optional[dict]:
    out: dict = {}
    for line in ex.final_answer:
        try:
            rel = parse_relation(line)
        except NotationError:
            return None
        if not rel.is_equation or rel.rhs is None or not rel.lhs.is_Symbol:
            return None
        v = sp.nsimplify(rel.rhs)
        if not v.is_Rational:
            return None
        out[str(rel.lhs)] = sp.Rational(v)
    return out


def _axis(values: list, lo_min: float = -1.0, hi_min: float = 1.0) -> Optional[tuple[int, int, int]]:
    lo = min(min(float(v) for v in values), lo_min)
    hi = max(max(float(v) for v in values), hi_min)
    lo, hi = math.floor(lo) - 1, math.ceil(hi) + 1
    span = hi - lo
    step = 1 if span <= 14 else 2 if span <= 30 else 5 if span <= MAX_SPAN else None
    if step is None:
        return None
    lo, hi = step * math.floor(lo / step), step * math.ceil(hi / step)
    return lo, hi, step


def _label(text: str) -> str:
    t = " ".join(str(text).split()).replace("**", "^").replace("*", "")
    return t if len(t) <= LABEL_MAX else t[:LABEL_MAX - 1] + "…"


def _fmt(v: sp.Rational) -> str:
    return str(int(v)) if v.is_Integer else str(v)


def chart_for(ex: WorkedExample) -> Optional[dict]:
    """{"kind": "lines", "beside": spec, "closing": spec, "point": "(2, 1)" | None,
    "lines": [labels]} for a plottable example, else None. Never raises."""
    try:
        return _lines_chart(ex)
    except Exception:  # noqa: BLE001 — a chart is a bonus; the example stands without it
        return None


def _lines_chart(ex: WorkedExample) -> Optional[dict]:
    if ex.figure or ex.task not in ("solve_system", "solve"):
        return None
    givens = [g for g in (ex.givens or []) if str(g).strip()]
    lines = [(g, _linear(g)) for g in givens]
    lines = [(g, abc) for g, abc in lines if abc is not None]
    if ex.task == "solve_system":
        if len(lines) != 2 or len(givens) != 2:
            return None
    else:
        # one line in BOTH x and y (y = 2x + 1): the graph of the equation
        if len(lines) != 1 or len(givens) != 1 or not (lines[0][1][0] != 0 and lines[0][1][1] != 0):
            return None
    point = None
    if ex.task == "solve_system":
        sol = _solution(ex)
        if not sol or set(sol) != {"x", "y"}:
            return None
        px, py = sol["x"], sol["y"]
        for _g, (a, b, c) in lines:
            if a * px + b * py != c:
                return None
        point = (px, py)
    xs: list = [0]
    ys: list = [0]
    for _g, (a, b, c) in lines:
        if a != 0:
            xs.append(c / a)
        if b != 0:
            ys.append(c / b)
    if point is not None:
        xs.append(point[0])
        ys.append(point[1])
    ax, ay = _axis(xs), _axis(ys)
    if ax is None or ay is None:
        return None
    step = max(ax[2], ay[2])
    axes = {"make": "axes", "id": "ax", "x": [ax[0], ax[1]], "y": [ay[0], ay[1]], "step": step, "grid": True}
    objs = [axes] + [{"make": "line_eq", "id": f"l{i + 1}", "a": _fmt(a), "b": _fmt(b), "c": _fmt(c), "label": _label(g)}
                     for i, (g, (a, b, c)) in enumerate(lines)]

    def spec(with_point: bool) -> dict:
        objects = list(objs)
        points: list[dict] = []
        measures: list[dict] = []
        if with_point and point is not None:
            objects.append({"make": "point_at", "id": "p_s", "x": _fmt(point[0]), "y": _fmt(point[1])})
            points.append({"id": "p_s"})
            measures.append({"target": "p_s", "value": f"({_fmt(point[0])}, {_fmt(point[1])})", "role": "given"})
        return {"schema_version": SCHEMA, "id": "chart", "figure_role": "illustration", "prompt": "",
                "figures": [{"id": "g", "figure": {"units": "units", "points": points, "objects": objects,
                                                   "measures": measures}}]}

    from maths.geometry import verify_question  # noqa: PLC0415 — the engine checks what it drew
    beside, closing = spec(False), spec(True)
    if not verify_question(beside).ok or not verify_question(closing).ok:
        return None
    return {"kind": "lines", "beside": beside, "closing": closing,
            "point": f"({_fmt(point[0])}, {_fmt(point[1])})" if point is not None else None,
            "lines": [_label(g) for g, _abc in lines]}


def chart_image(chart: Optional[dict], *, which: str = "closing") -> Optional[tuple[bytes, float]]:
    """(PNG, width in mm) of a chart's spec, rendered metric by the same
    renderer the worksheet prints figures with; None when it cannot be
    drawn. For the deck slide and the answer key."""
    spec = (chart or {}).get(which)
    if not isinstance(spec, dict):
        return None
    try:
        from maths.geometry import parse_question, verify_question  # noqa: PLC0415
        from maths.geometry.render_static import render_figure  # noqa: PLC0415

        rep = verify_question(spec)
        if not rep.ok:
            return None
        q = parse_question(spec)
        ref = q.figures[0]
        r = render_figure(rep.models[ref.id], ref.figure, role="illustration", policy="instructional_metric")
        return r.png, float(r.width_mm)
    except Exception:  # noqa: BLE001 — a picture is a bonus
        return None
