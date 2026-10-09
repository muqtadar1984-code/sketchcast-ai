"""Charts for algebra examples — DERIVED, never authored.

The geometry figure path lets the model describe a figure the engine then
verifies. A chart starts from what is already verified — the example's
givens and its proved answer — and is a geometry.figure.v2 ILLUSTRATION the
same renderer, board adapter, worksheet printer, deck and quiz player
already consume. The model never writes one; one that cannot be built is
omitted, never faked (founder decisions, 2026-10-09; the catalogue's
"Simultaneous Linear Equations" solved well and drew nothing).

Phase 5 (data charts): a data task's list as a bar chart beside the
working; closing, the statistic marked — the mean as a line across the
bars, the median as the middle bar(s) of the sorted data, the mode as
the repeated bars, the range as a bracket from smallest to largest.

Phase 4 (inequalities): a solved one-variable inequality gets its number
line — bare beside the working; closing, the solution set marked with
open/closed circles and an arrow. (A two-variable half-plane waits on
the verifier: multivariate inequalities are never verified.)

Phase 3 (quadratics): a solved or factorised quadratic in x gets its
parabola (`curve_eq`), and — closing — its roots on the x-axis and its
vertex, when every root is rational and the answer names them all.

Phase 1: linear graphs and simultaneous equations. `chart_for(ex)` gives,
for a solve_system of two lines in x and y, or a solve with one
two-variable line, two specs: `beside` (the lines, drawn next to the
working from the start) and `closing` (the lines and the solution point,
for the scene after the solution), with the point as text.
"""

from __future__ import annotations

import math
import re
from typing import Optional

import sympy as sp

from maths.notation import NotationError, parse_relation
from maths.schema import DATA_TASKS, WorkedExample

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
    """The equation as the figure writes it: tidy spacing, x² for x^2."""
    t = " ".join(str(text).split()).replace("**", "^").replace("*", "")
    t = t.replace("^2", "²").replace("^3", "³")
    return t if len(t) <= LABEL_MAX else t[:LABEL_MAX - 1] + "…"


def _fmt(v: sp.Rational) -> str:
    return str(int(v)) if v.is_Integer else str(v)


def chart_for(ex: WorkedExample) -> Optional[dict]:
    """A plottable example's chart, else None. Never raises.
    kind "lines":    {beside, closing, point: "(2, 1)" | None, lines: [labels]}
    kind "parabola": {beside, closing, point: None, roots: ["-2", "3"], vertex: "(h, k)", lines: [label]}"""
    try:
        return (_line_task_chart(ex) or _lines_chart(ex) or _parabola_chart(ex) or _number_line_chart(ex)
                or _data_chart(ex) or _half_plane_chart(ex))
    except Exception:  # noqa: BLE001 — a chart is a bonus; the example stands without it
        return None


def _align(ax: tuple[int, int, int], step: int) -> tuple[int, int]:
    return step * math.floor(ax[0] / step), step * math.ceil(ax[1] / step)


def _quadratic(text: str) -> Optional[tuple[sp.Rational, sp.Rational, sp.Rational]]:
    """(a, b, c) of a·x² + b·x + c for a quadratic in x alone — an equation
    (either side), or a bare expression — else None."""
    try:
        rel = parse_relation(text)
    except NotationError:
        return None
    if rel.is_data or rel.is_inequality:
        return None
    expr = sp.expand(rel.lhs - rel.rhs) if rel.is_equation and rel.rhs is not None else sp.expand(rel.lhs)
    if expr.free_symbols != {X}:
        return None
    try:
        poly = sp.Poly(expr, X)
    except sp.PolynomialError:
        return None
    if poly.degree() != 2:
        return None
    a, b, c = (sp.nsimplify(v) for v in poly.all_coeffs())
    if not all(v.is_Rational for v in (a, b, c)):
        return None
    return sp.Rational(a), sp.Rational(b), sp.Rational(c)


_ROOT_RE = re.compile(r"\bx\s*=\s*(-?\d+(?:/\d+)?(?:\.\d+)?)")


def _answer_roots(ex: WorkedExample) -> Optional[set]:
    """The x-values the final answer names ("x = 3 or x = -2", or one per
    line), as rationals; None when it names none."""
    text = " ; ".join(str(t) for t in ex.final_answer)
    found = _ROOT_RE.findall(text)
    if not found:
        return None
    return {sp.Rational(sp.nsimplify(v)) for v in found}


def _parabola_chart(ex: WorkedExample) -> Optional[dict]:
    """After a quadratic is solved (task solve) or factorised (task
    factorise): the parabola, its roots on the x-axis, its vertex. A solve
    whose answer does not name exactly the roots gets no chart."""
    if ex.figure or ex.task not in ("solve", "factorise"):
        return None
    givens = [g for g in (ex.givens or []) if str(g).strip()]
    if len(givens) != 1:
        return None
    q = _quadratic(givens[0])
    if q is None:
        return None
    a, b, c = q
    roots = sorted({sp.nsimplify(r) for r in sp.solve(a * X ** 2 + b * X + c, X) if r.is_real}, key=float)
    if not roots or not all(r.is_Rational for r in roots):
        return None                       # irrational or no real roots: a later phase
    roots = [sp.Rational(r) for r in roots]
    if ex.task == "solve" and _answer_roots(ex) != set(roots):
        return None
    h = -b / (2 * a)
    k = a * h * h + b * h + c
    ax = _axis([0, h] + roots)
    if ax is None:
        return None
    # the y-axis holds the vertex, the y-intercept and the curve at the
    # x-range's ends (so both arms are seen) — each arm clipped to one
    # x-span above or below the vertex, so a steep parabola stays a chart
    # and not a sliver (the curve is cut at the box, never the axes; frame
    # review 2026-10-09: arms to y = 8 squeezed the chart to a column)
    band = ax[1] - ax[0]
    ys = [0, k, c] + [max(float(k) - band, min(float(k) + band, float(a * xe * xe + b * xe + c))) for xe in ax[:2]]
    ay = _axis(ys)
    if ay is None:
        return None
    step = max(ax[2], ay[2])
    (x0, x1), (y0, y1) = _align(ax, step), _align(ay, step)
    label = _label(givens[0])
    axes = {"make": "axes", "id": "ax", "x": [x0, x1], "y": [y0, y1], "step": step, "grid": True}
    curve = {"make": "curve_eq", "id": "c1", "a": _fmt(a), "b": _fmt(b), "c": _fmt(c), "label": label}
    vertex = f"({_fmt(h)}, {_fmt(k)})"

    def spec(with_marks: bool) -> dict:
        objects: list[dict] = [axes, curve]
        points: list[dict] = []
        measures: list[dict] = []
        if with_marks:
            for i, r in enumerate(roots):
                pid = f"p_r{i + 1}"
                objects.append({"make": "point_at", "id": pid, "x": _fmt(r), "y": "0"})
                points.append({"id": pid})
                measures.append({"target": pid, "value": f"({_fmt(r)}, 0)", "role": "given"})
            # the vertex, when its tag reads cleanly (quarters at most) and it
            # is not the double root already marked
            if h.q <= 4 and k.q <= 4 and not (len(roots) == 1 and k == 0):
                objects.append({"make": "point_at", "id": "p_v", "x": _fmt(h), "y": _fmt(k)})
                points.append({"id": "p_v"})
                measures.append({"target": "p_v", "value": vertex, "role": "given"})
        return {"schema_version": SCHEMA, "id": "chart", "figure_role": "illustration", "prompt": "",
                "figures": [{"id": "g", "figure": {"units": "units", "points": points, "objects": objects,
                                                   "measures": measures}}]}

    from maths.geometry import verify_question  # noqa: PLC0415 — the engine checks what it drew
    beside, closing = spec(False), spec(True)
    if not verify_question(beside).ok or not verify_question(closing).ok:
        return None
    return {"kind": "parabola", "beside": beside, "closing": closing, "point": None,
            "roots": [_fmt(r) for r in roots], "vertex": vertex, "lines": [label]}


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


def _line_task_chart(ex: WorkedExample) -> Optional[dict]:
    """A straight-line task (2026-10-09): the line the facts fix, beside the
    working; the given points (and the intercept asked for) on it at the
    close. Read from the verifier's own facts, so the chart is the line the
    example was verified against."""
    if ex.figure or ex.task != "line":
        return None
    from maths.verify import line_facts  # noqa: PLC0415 — the verifier is the source of the line
    F = line_facts(ex)
    if F.problem or F.m is None or F.c is None or not (F.m.is_Rational and F.c.is_Rational):
        return None
    m, c = sp.Rational(F.m), sp.Rational(F.c)
    a, b, k = -m, sp.Integer(1), c          # a·x + b·y = k  ⇔  y = m x + c
    pts = [(sp.Rational(x), sp.Rational(y)) for x, y in F.points if sp.nsimplify(x).is_Rational and sp.nsimplify(y).is_Rational]
    if F.target == "x_intercept" and m != 0:
        pts.append((-c / m, sp.Integer(0)))
    if F.target in ("c", "mc") and not any(x == 0 for x, _y in pts):
        pts.append((sp.Integer(0), c))
    xs: list = [0] + [x for x, _y in pts]
    ys: list = [0, c] + [y for _x, y in pts]
    if m != 0:
        xs.append(-c / m)
    ax, ay = _axis(xs), _axis(ys)
    if ax is None or ay is None:
        return None
    step = max(ax[2], ay[2])
    label = ("y = " + ("" if m == 1 else "-" if m == -1 else _fmt(m)) + "x" + ("" if c == 0 else f" - {_fmt(-c)}" if c < 0 else f" + {_fmt(c)}")
             if m != 0 else f"y = {_fmt(c)}")
    axes = {"make": "axes", "id": "ax", "x": [ax[0], ax[1]], "y": [ay[0], ay[1]], "step": step, "grid": True}
    line = {"make": "line_eq", "id": "l1", "a": _fmt(a), "b": _fmt(b), "c": _fmt(k), "label": label}

    def spec(with_points: bool) -> dict:
        objects = [axes, line]
        points: list[dict] = []
        measures: list[dict] = []
        if with_points:
            for i, (x, y) in enumerate(pts):
                objects.append({"make": "point_at", "id": f"p{i + 1}", "x": _fmt(x), "y": _fmt(y)})
                points.append({"id": f"p{i + 1}"})
                measures.append({"target": f"p{i + 1}", "value": f"({_fmt(x)}, {_fmt(y)})", "role": "given"})
        return {"schema_version": SCHEMA, "id": "chart", "figure_role": "illustration", "prompt": "",
                "figures": [{"id": "g", "figure": {"units": "units", "points": points, "objects": objects,
                                                   "measures": measures}}]}

    from maths.geometry import verify_question  # noqa: PLC0415 — the engine checks what it drew
    beside, closing = spec(False), spec(True)
    if not verify_question(beside).ok or not verify_question(closing).ok:
        return None
    return {"kind": "lines", "beside": beside, "closing": closing,
            "point": f"({_fmt(pts[0][0])}, {_fmt(pts[0][1])})" if pts else None, "lines": [label]}


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


def _interval_of(texts: list[str]) -> Optional[sp.Interval]:
    """The solution set of inequalities in x alone, when it is ONE interval."""
    from maths.verify import _solution_set  # noqa: PLC0415

    rels = []
    for t in texts:
        try:
            rel = parse_relation(t)
        except NotationError:
            return None
        if not rel.is_inequality or rel.free_symbols != {X}:
            return None
        rels.append(rel)
    if not rels:
        return None
    try:
        out = _solution_set(rels, [X], "all")
    except Exception:  # noqa: BLE001
        return None
    return out if isinstance(out, sp.Interval) else None


def _bound(v) -> Optional[sp.Rational]:
    if v in (sp.S.Infinity, sp.S.NegativeInfinity):
        return None
    v = sp.nsimplify(v)
    return sp.Rational(v) if v.is_Rational else None


def _number_line_chart(ex: WorkedExample) -> Optional[dict]:
    """After a one-variable inequality is solved: the number line, and —
    closing — the solution set. The answer's set must be the givens' set."""
    if ex.figure or ex.task != "solve_inequality":
        return None
    givens = [g for g in (ex.givens or []) if str(g).strip()]
    answer = [a for a in (ex.final_answer or []) if str(a).strip()]
    if not givens or not answer:
        return None
    want, got = _interval_of(givens), _interval_of(answer)
    if want is None or got is None or want != got or got.is_empty:
        return None
    lo, hi = got.start, got.end
    if lo == sp.S.NegativeInfinity and hi == sp.S.Infinity:
        return None
    blo, bhi = _bound(lo), _bound(hi)
    if (lo != sp.S.NegativeInfinity and blo is None) or (hi != sp.S.Infinity and bhi is None):
        return None
    lo_closed, hi_closed = not got.left_open, not got.right_open
    vals = [0] + [v for v in (blo, bhi) if v is not None]
    ax = _axis(vals, lo_min=-1.0, hi_min=1.0)
    if ax is None:
        return None
    x0, x1, step = ax
    if blo is None:
        x0 -= 2 * step           # room for the arrow to be seen going on
    if bhi is None:
        x1 += 2 * step
    nl = {"make": "number_line", "id": "nl", "range": [x0, x1], "step": step}
    iv = {"make": "interval", "id": "s", "from_closed": lo_closed, "to_closed": hi_closed}
    if blo is not None:
        iv["from"] = _fmt(blo)
    if bhi is not None:
        iv["to"] = _fmt(bhi)

    def spec(with_set: bool) -> dict:
        objects = [nl] + ([iv] if with_set else [])
        return {"schema_version": SCHEMA, "id": "chart", "figure_role": "illustration", "prompt": "",
                "figures": [{"id": "g", "figure": {"units": "units", "points": [], "objects": objects, "measures": []}}]}

    from maths.geometry import verify_question  # noqa: PLC0415 — the engine checks what it drew
    beside, closing = spec(False), spec(True)
    if not verify_question(beside).ok or not verify_question(closing).ok:
        return None
    shape = "between" if blo is not None and bhi is not None else ("right" if blo is not None else "left")
    return {"kind": "number_line", "beside": beside, "closing": closing, "point": None, "lines": [],
            "answer": " and ".join(_label(a).replace(">=", "≥").replace("<=", "≤") for a in answer), "shape": shape,
            "a": _fmt(blo) if blo is not None else None, "b": _fmt(bhi) if bhi is not None else None,
            "closed": [lo_closed, hi_closed]}


def _answer_values(ex: WorkedExample) -> Optional[list]:
    """The numbers the final answer states ("8", "mean = 8", "2, 3"), exact."""
    out: list = []
    for line in ex.final_answer:
        try:
            rel = parse_relation(line)
        except NotationError:
            return None
        if rel.is_data:
            vals = [sp.nsimplify(v, rational=True) for v in rel.data]
        elif rel.is_equation and rel.rhs is not None:
            vals = [sp.nsimplify(rel.rhs, rational=True)]
        elif rel.is_expression:
            vals = [sp.nsimplify(rel.lhs, rational=True)]
        else:
            return None
        if not all(v.is_Rational for v in vals):
            return None
        out += vals
    return out or None


def _data_chart(ex: WorkedExample) -> Optional[dict]:
    """After mean / median / mode / range of a data list: the bars, and —
    closing — the statistic marked. The answer must be the statistic."""
    if ex.figure or ex.task not in DATA_TASKS:
        return None
    givens = [g for g in (ex.givens or []) if str(g).strip()]
    if len(givens) != 1:
        return None
    try:
        rel = parse_relation(givens[0])
    except NotationError:
        return None
    if not rel.is_data:
        return None
    data = [sp.nsimplify(v, rational=True) for v in rel.data]
    if not 2 <= len(data) <= 12 or not all(v.is_Rational and v >= 0 for v in data):
        return None
    n = len(data)
    srt = sorted(data)
    if ex.task == "mean":
        stat = [sum(data) / n]
    elif ex.task == "median":
        stat = [srt[n // 2]] if n % 2 else [(srt[n // 2 - 1] + srt[n // 2]) / 2]
    elif ex.task == "mode":
        counts = {v: data.count(v) for v in set(data)}
        best = max(counts.values())
        if best < 2:
            return None
        stat = sorted(v for v, c in counts.items() if c == best)
    else:
        stat = [srt[-1] - srt[0]]
    got = _answer_values(ex)
    if got is None or sorted(got) != sorted(stat):
        return None
    labels = [_fmt(v) for v in data]
    value = ", ".join(_fmt(v) for v in stat)

    def bars(values: list, **marks) -> dict:
        obj = {"make": "bar_chart", "id": "bc", "values": [_fmt(v) for v in values]}
        obj.update({k: v for k, v in marks.items() if v not in (None, [], False, "")})
        return {"schema_version": SCHEMA, "id": "chart", "figure_role": "illustration", "prompt": "",
                "figures": [{"id": "g", "figure": {"units": "units", "points": [], "objects": [obj], "measures": []}}]}

    beside = bars(data)
    extra: dict = {"sorted": False}
    if ex.task == "mean":
        closing = bars(data, mean=value)
    elif ex.task == "median":
        mid = [n // 2] if n % 2 else [n // 2 - 1, n // 2]
        closing = bars(srt, highlight=mid)
        extra = {"sorted": True, "a": _fmt(srt[n // 2 - 1]) if n % 2 == 0 else None, "b": _fmt(srt[n // 2]) if n % 2 == 0 else None}
    elif ex.task == "mode":
        closing = bars(data, highlight=[i for i, v in enumerate(data) if v in stat])
    else:
        lo_i, hi_i = data.index(srt[0]), data.index(srt[-1])
        closing = bars(data, highlight=sorted({lo_i, hi_i}), range=True, range_label=value)
        extra = {"sorted": False, "lo": _fmt(srt[0]), "hi": _fmt(srt[-1])}

    from maths.geometry import verify_question  # noqa: PLC0415 — the engine checks what it drew
    if not verify_question(beside).ok or not verify_question(closing).ok:
        return None
    out = {"kind": "data", "stat": ex.task, "beside": beside, "closing": closing, "point": None, "lines": [],
           "value": value, "n": n, "labels": labels}
    out.update(extra)
    return out


def _half_plane_chart(ex: WorkedExample) -> Optional[dict]:
    """After a linear inequality in x and y is solved (rearranged): the
    boundary line beside the working, dashed when strict; closing, the
    region shaded. The answer's half-plane must be the givens'."""
    from maths.verify import half_plane  # noqa: PLC0415

    if ex.figure or ex.task != "solve_inequality":
        return None
    givens = [g for g in (ex.givens or []) if str(g).strip()]
    answer = [a for a in (ex.final_answer or []) if str(a).strip()]
    if len(givens) != 1 or len(answer) != 1:
        return None
    try:
        want, got = half_plane(parse_relation(givens[0])), half_plane(parse_relation(answer[0]))
    except NotationError:
        return None
    if want is None or got is None or want != got or got[5] != ("x", "y"):
        return None
    _t, a, b, c, op, _syms = got                   # a·x + b·y + c op 0
    strict = op == "<"
    # the boundary a·x + b·y = -c; its intercepts range the axes
    xs: list = [0] + ([-c / a] if a != 0 else [])
    ys: list = [0] + ([-c / b] if b != 0 else [])
    ax, ay = _axis(xs), _axis(ys)
    if ax is None or ay is None:
        return None
    step = max(ax[2], ay[2])
    (x0, x1), (y0, y1) = _align(ax, step), _align(ay, step)
    label = _label(answer[0]).replace(">=", "≥").replace("<=", "≤")
    axes = {"make": "axes", "id": "ax", "x": [x0, x1], "y": [y0, y1], "step": step, "grid": True}
    line = {"make": "line_eq", "id": "l1", "a": _fmt(a), "b": _fmt(b), "c": _fmt(-c), "label": label, "dashed": strict}
    region = {"make": "half_plane", "id": "r1", "a": _fmt(a), "b": _fmt(b), "c": _fmt(-c), "op": op}

    def spec(with_region: bool) -> dict:
        objects = [axes, line] + ([region] if with_region else [])
        return {"schema_version": SCHEMA, "id": "chart", "figure_role": "illustration", "prompt": "",
                "figures": [{"id": "g", "figure": {"units": "units", "points": [], "objects": objects, "measures": []}}]}

    from maths.geometry import verify_question  # noqa: PLC0415 — the engine checks what it drew
    beside, closing = spec(False), spec(True)
    if not verify_question(beside).ok or not verify_question(closing).ok:
        return None
    return {"kind": "half_plane", "beside": beside, "closing": closing, "point": None, "lines": [label],
            "answer": label, "strict": strict}
