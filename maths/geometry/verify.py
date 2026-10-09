"""The verification chain for a figure-bearing question.

PROOF (the only thing that establishes an answer):
  1. parse the spec; 2. compile every figure (constructions refuse what
  they cannot build); 3. claimed relations are implied by the facts;
  4. each `deduce` step's theorem applies to the objects it cites and its
  line is what the theorem yields; 5. each `transform` step is algebra
  maths.verify accepts; 6. an evidence question's answer is the property
  the engine computes from the figure.

CONSISTENCY (catches a mis-specified figure; never proof):
  7. every given measure is realised by the drawing (compiler);
  8. the unknown's drawn value agrees with the proved value, and the
     figure's bound value of the unknown is the proved one.

The proof's knowledge starts from the GIVEN measures only: a construction's
own exact facts (the 110° the straight line fixes) stay in the facts graph
for check 8 and never enter the chain, or the theorem step would be
trivial. A step's line is accepted when, together with what is already
known, it says exactly what the theorem says — mutual implication, decided
by SymPy over the symbols involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import sympy as sp

from maths.geometry.compiler import compile_figure, measure_exact, realised
from maths.geometry.constructions import exact_value, exact_pair
from maths.geometry.errors import GeometryRefusal
from maths.geometry.model import ANGLE_TOL_DEG, LENGTH_REL_TOL, Model, to_float, coord_symbols
from maths.geometry.properties import compute as compute_property
from maths.geometry.spec import Answer, Asks, QuestionSpec, StepSpec, parse_question
from maths.geometry.theorems import REASONS, angle_symbol, apply as apply_theorem, length_symbol
from maths.notation import NotationError, Relation, parse_relation
from maths.verify import Check, ExampleReport, _pi_match, _states_equivalent, pi_taken_as
from mathsvc.safety import MathTimeoutError

DISCERN_LEN = 0.5        # sides meant to differ differ by this much (figure units; 5 mm at 1 cm/unit)
DISCERN_ANGLE = 10.0     # an acute/obtuse angle sits this far from 90°
DISCERN_EQUAL_ANGLE = 8.0

# marks that would answer the property being asked
_GIVEAWAY = {
    "triangle_class_by_sides": {"equal_ticks"},
    "triangle_class_by_angles": {"right_angle_square"},
    "count_right_angles": {"right_angle_square"},
    "lines_of_symmetry": {"equal_ticks", "right_angle_square"},
    "polygon_name": {"equal_ticks"},
}

_ANG_RE = re.compile(r"\bang\(\s*([A-Za-z0-9_]+)\s*\)")
_LEN_RE = re.compile(r"\blen\(\s*([A-Za-z0-9_]+)\s*\)")


@dataclass
class QuestionReport:
    label: str
    status: str = "verified"                  # verified | failed
    checks: list[Check] = field(default_factory=list)
    refusal: Optional[dict] = None
    models: dict[str, Model] = field(default_factory=dict)
    computed: dict[str, Any] = field(default_factory=dict)   # evidence answers the engine computed
    proved: dict[str, Any] = field(default_factory=dict)     # symbol -> value the chain established
    reasons_given: list[str] = field(default_factory=list)   # answer-key reasons, from theorem ids

    @property
    def ok(self) -> bool:
        return self.status == "verified"

    def to_dict(self) -> dict:
        ex = ExampleReport(self.label, self.status, self.checks)
        d = ex.to_dict()
        d.update({"refusal": self.refusal, "computed": {k: _plain(v) for k, v in self.computed.items()},
                  "proved": {k: str(v) for k, v in self.proved.items()}, "reasons_given": self.reasons_given})
        return d


def _plain(v):
    if isinstance(v, (set, frozenset)):
        return sorted(v)
    return v


def _refuse(rep: QuestionReport, exc: GeometryRefusal, name: str) -> QuestionReport:
    rep.status = "failed"
    rep.refusal = exc.as_dict()
    rep.checks.append(Check(name, False, str(exc)))
    return rep


# ── notation ──────────────────────────────────────────────────────────────

def parse_line(text: str) -> Relation:
    """A step line in the figure's vocabulary: ``ang(abc)`` is the measure
    of angle ``angle_abc``, ``len(ab)`` of segment ``seg_ab``; a degree
    sign is decoration."""
    s = _ANG_RE.sub(r"ang_\1", str(text))
    s = _LEN_RE.sub(r"len_\1", s)
    s = s.replace("°", "").replace("º", "")
    try:
        return parse_relation(s)
    except NotationError as exc:
        raise GeometryRefusal("bad_schema", f"step line {text!r}: {exc}") from exc


def _eq(rel: Relation) -> sp.Expr:
    """lhs - rhs, the zero-form of an equation; a bare expression is itself."""
    if rel.op == "=":
        return rel.lhs - rel.rhs
    if rel.op is None:
        return rel.lhs
    raise GeometryRefusal("bad_schema", f"{rel.text!r}: a step line is an equation")


def _measure_substitutions(m: Model, q: QuestionSpec, fid: str) -> dict[sp.Symbol, sp.Expr]:
    fig = q.figure_by_id(fid).figure
    subs = {}
    for ms in fig.measures:
        if ms.target in m.points:
            # v2: a point's coordinates — the question's unknowns (a, b) or
            # its stated pair — stand for the point's coordinate symbols
            ex, ey = exact_pair(ms.value, where=f"measure.{ms.target}")
            xs, ys = coord_symbols(ms.target)
            subs[xs], subs[ys] = ex, ey
            continue
        e = exact_value(ms.value, where=f"measure.{ms.target}")
        if ms.target in m.solids:
            from maths.geometry.theorems import SURFACE, VOLUME  # noqa: PLC0415
            subs[VOLUME if ms.kind == "volume" else SURFACE] = e     # v3: a given volume / surface area
        elif ms.target in m.angle_ids:
            subs[angle_symbol(m.canonical_angle_id(ms.target))] = e
        else:
            subs[length_symbol(ms.target)] = e
    # v2: a point the figure fixes exactly resolves to its coordinates
    for pid, pt in m.points.items():
        if pt.exact is not None:
            xs, ys = coord_symbols(pid)
            subs.setdefault(xs, sp.sympify(pt.exact[0]))
            subs.setdefault(ys, sp.sympify(pt.exact[1]))
    return subs


# ── implication over small systems ────────────────────────────────────────

def _implies(premises: list[sp.Expr], conclusions: list[sp.Expr]) -> Optional[bool]:
    """Every solution of the premises satisfies every conclusion. None when
    SymPy cannot decide."""
    # an identically-true premise (a theorem whose two sides are the same
    # unknown, ang(b) = ang(c) with both x) says nothing: dropped, or SymPy
    # refuses the system and a wrong line reads as "unverifiable"
    premises = [p for p in premises if sp.simplify(p) != 0]
    syms = sorted(set().union(*(e.free_symbols for e in premises + conclusions)), key=str)
    if not syms:
        return all(sp.simplify(c) == 0 for c in conclusions)
    try:
        sols = sp.solve(premises, syms, dict=True) if premises else [{}]
    except Exception:  # noqa: BLE001 — SymPy refused
        return None
    if premises and not sols:
        return None          # inconsistent premises prove nothing
    for sol in sols:
        for c in conclusions:
            try:
                if sp.simplify(c.subs(sol)) != 0:
                    return False
            except Exception:  # noqa: BLE001
                return None
    return True


def _equivalent(knowledge: list[sp.Expr], a: list[sp.Expr], b: list[sp.Expr]) -> Optional[bool]:
    x = _implies(knowledge + a, b)
    y = _implies(knowledge + b, a)
    if x is None or y is None:
        return None
    return x and y


def _determined(knowledge: list[sp.Expr], sym: sp.Symbol) -> Optional[sp.Expr]:
    syms = sorted(set().union(*(e.free_symbols for e in knowledge)) | {sym}, key=str)
    try:
        sols = sp.solve(knowledge, syms, dict=True)
    except Exception:  # noqa: BLE001
        return None
    if len(sols) != 1 or sym not in sols[0]:
        return None
    v = sp.simplify(sols[0][sym])
    return v if not v.free_symbols else None


# ── the reasoning chain ───────────────────────────────────────────────────

def _check_figure_ops(m: Model, st: StepSpec, name: str) -> None:
    for op in st.figure_ops:
        target = op.get("target")
        if not isinstance(target, str) or not (
            target in m.angle_ids or target in m.segment_ids or target in m.object_ids or target in m.points
        ):
            raise GeometryRefusal("bad_reference", f"{name}: figure_ops target {target!r} does not exist", name)


def _run_chain(rep: QuestionReport, q: QuestionSpec, fid: str, m: Model) -> list[sp.Expr]:
    subs = _measure_substitutions(m, q, fid)
    # one symbol per angle: an alias spelling (angle_cab for angle_bac)
    # maps straight to the canonical symbol's value, or to the canonical
    # symbol when it has none — a flat map, nothing chains
    for a in list(m.angle_ids):
        c = m.canonical_angle_id(a)
        if c != a:
            subs[angle_symbol(a)] = subs.get(angle_symbol(c), angle_symbol(c))
    knowledge: list[sp.Expr] = []
    prev_after: list[Relation] = []
    for i, st in enumerate(q.steps, 1):
        name = f"step {i} ({st.kind})"
        _check_figure_ops(m, st, name)
        after_rels = [parse_line(x) for x in st.after]
        after = [_in_degrees(_eq(r).subs(subs)) for r in after_rels]
        if st.kind == "deduce":
            if not st.theorem:
                raise GeometryRefusal("bad_schema", f"{name}: a deduce step names its theorem", name)
            uses = list(st.uses)
            if not any(x in m.angle_ids for x in uses):
                # nothing cited (the live lesson call, 2026-10-07): the angles
                # the step's own equation names ARE its citation — every
                # premise of the theorem still runs on them
                uses += _angles_named(m, st.after)
            template = [_in_degrees((e.lhs - e.rhs).subs(subs)) for e in apply_theorem(m, st.theorem, uses)]
            if not after:
                raise GeometryRefusal("bad_schema", f"{name}: write the equation the theorem gives", name)
            verdict = _equivalent(knowledge, template, after)
            if verdict is None:
                raise GeometryRefusal("step_unverifiable", f"{name}: could not compare the line with {st.theorem}", name)
            if not verdict:
                want = "; ".join(str(sp.Eq(t, 0)) for t in template)
                raise GeometryRefusal("step_not_equivalent",
                                      f"{name}: {st.theorem} gives {want}, not {'; '.join(st.after)}", name)
            rep.checks.append(Check(name, True, f"{st.theorem}: {REASONS.get(st.theorem, st.theorem)}"))
            rep.reasons_given.append(REASONS.get(st.theorem, st.theorem))
            knowledge += after
        elif st.kind == "transform":
            before_rels = [parse_line(x) for x in st.before] if st.before else prev_after
            if not before_rels or not after_rels:
                raise GeometryRefusal("bad_schema", f"{name}: a transform needs a line before and after", name)
            b_sub = [_with_subs(r, subs) for r in before_rels]
            a_sub = [_with_subs(r, subs) for r in after_rels]
            variables = sorted(set().union(*(r.free_symbols for r in b_sub + a_sub)), key=str)
            try:
                verdict, detail = _states_equivalent(b_sub, a_sub, variables, "any")
            except MathTimeoutError:
                verdict, detail = None, "SymPy timed out"
            if not verdict:
                # v4: an inverse trig step — tan(x) = 3/4 to x = atan(3/4) — is
                # one solution of a periodic set; the chain's own implication
                # (sp.solve, principal values) decides it, both ways
                alt = _equivalent(knowledge, [_eq(r) for r in b_sub], [_eq(r) for r in a_sub])
                if alt:
                    verdict, detail = True, f"{detail}; equivalent by the chain's own solver"
            if verdict is None:
                raise GeometryRefusal("step_unverifiable", f"{name}: {detail}", name)
            if not verdict:
                raise GeometryRefusal("step_not_equivalent", f"{name}: {detail}", name)
            rep.checks.append(Check(name, True, detail))
            taken = pi_taken_as(detail)
            if taken is not None:
                # the step took π as a school value (maths.verify, 2026-10-09):
                # from here the knowledge says what the working now says, or
                # "V = 72π" and "V = 226.08" together prove nothing and the
                # answer is "never established"
                knowledge = [k.subs(sp.pi, taken) for k in knowledge]
            knowledge += after
        elif st.kind == "check":
            if not after:
                raise GeometryRefusal("bad_schema", f"{name}: a check evaluates a line", name)
            ok = _implies(knowledge, after)
            if ok is None:
                raise GeometryRefusal("step_unverifiable", f"{name}: could not evaluate", name)
            if not ok:
                raise GeometryRefusal("step_not_equivalent", f"{name}: {'; '.join(st.after)} does not follow", name)
            rep.checks.append(Check(name, True, "evaluates"))
        else:  # setup
            knowledge += after
            rep.checks.append(Check(name, True, "setup"))
        if after_rels:
            prev_after = after_rels
    return knowledge


from maths.geometry.model import in_degrees as _in_degrees  # noqa: E402


def _rounds_to(got, text) -> bool:
    """The stated value is the proved one rounded to the decimals it
    states (7.78 for 5/sin 40°, 36.87 for tan⁻¹ 0.75) — the one inexact
    answer the engine accepts, and only as the CORRECT rounding."""
    t = str(text).strip()
    if "." not in t:
        return False
    try:
        decimals = len(t.split(".")[1].rstrip())
        want = float(t)
        have = float(sp.N(got, 30))
    except (TypeError, ValueError):
        return False
    return round(have, decimals) == round(want, decimals)


def _with_subs(r: Relation, subs: dict) -> Relation:
    lhs = _in_degrees(r.lhs.subs(subs))
    rhs = _in_degrees(r.rhs.subs(subs)) if r.rhs is not None else None
    return Relation(lhs, r.op, rhs, r.text, r.data)


def _answer_symbols(q: QuestionSpec, fid: str, m: Model) -> list[sp.Symbol]:
    """The unknowns the chain must establish: every symbol a measure
    mentions. A given written as ``2x`` makes x an unknown as much as an
    unknown written as ``x`` does (B2: four givens in x, nothing else)."""
    fig = q.figure_by_id(fid).figure
    syms: set = set()
    for ms in fig.measures:
        if ms.target in m.points:
            ex, ey = exact_pair(ms.value, where=f"measure.{ms.target}")
            syms |= ex.free_symbols | ey.free_symbols
            continue
        syms |= exact_value(ms.value, where=f"measure.{ms.target}").free_symbols
    # v3: a given volume or surface area is a known; the unknown is the edge
    given_solid = {ms.kind for ms in fig.measures if ms.target in m.solids}
    # v2: a coordinate written in an unknown (A(k, 2)) makes k an unknown
    for pt in m.points.values():
        if pt.exact is not None:
            syms |= sp.sympify(pt.exact[0]).free_symbols | sp.sympify(pt.exact[1]).free_symbols
    # a quantity a theorem establishes (an area, a perimeter, a circumference,
    # a gradient, an intercept) is the question's unknown too — nothing else
    # names it
    from maths.geometry.theorems import (AREA, CIRCUMFERENCE, EDGES, FACES, GRADIENT, INTERCEPT, PERIMETER, SURFACE,
                                         VERTICES, VOLUME)

    cited = {st.theorem for st in q.steps if st.kind == "deduce" and st.theorem}
    if any(t.startswith("area_") for t in cited):
        syms.add(AREA)
    if "perimeter" in cited:
        syms.add(PERIMETER)
    if "circumference" in cited:
        syms.add(CIRCUMFERENCE)
    if "gradient" in cited or "line_equation" in cited:
        syms.add(GRADIENT)
    if "line_equation" in cited:
        syms.add(INTERCEPT)
    # v3: a volume, a surface area, the counts of Euler's formula
    if any(t.startswith("volume_") for t in cited) and "volume" not in given_solid:
        syms.add(VOLUME)
    if any(t.startswith("surface_area_") for t in cited) and "surface_area" not in given_solid:
        syms.add(SURFACE)
    if "euler_solids" in cited:
        syms.add(EDGES)          # F and N are counted by the theorem itself
    return sorted(syms, key=str)


def _check_reasoning_answer(rep: QuestionReport, q: QuestionSpec, fid: str, m: Model, knowledge: list[sp.Expr]) -> None:
    ans = q.answer
    if ans is None:
        raise GeometryRefusal("bad_schema", "a reasoning question states its answer")
    syms = _answer_symbols(q, fid, m)
    if ans.kind == "number":
        if len(syms) != 1:
            raise GeometryRefusal("bad_schema", f"answer kind 'number' needs one unknown; the figure has {len(syms)}")
        want = {str(syms[0]): exact_value(ans.value, where="answer")}
    elif ans.kind == "values":
        if not isinstance(ans.value, dict):
            raise GeometryRefusal("bad_schema", "answer kind 'values' is {symbol: value}")
        want = {k: exact_value(v, where=f"answer.{k}") for k, v in ans.value.items()}
        missing = [str(s) for s in syms if str(s) not in want]
        if missing:
            raise GeometryRefusal("bad_schema", f"answer gives no value for {', '.join(missing)}")
    else:
        raise GeometryRefusal("bad_schema", f"a reasoning question's answer is a number or values, not {ans.kind}")
    for name, value in want.items():
        sym = sp.Symbol(name)
        got = _determined(knowledge, sym)
        if got is None:
            raise GeometryRefusal("answer_unproved", f"the steps never establish {name}")
        approx = ""
        if sp.simplify(got - value) != 0 and not _rounds_to(got, ans.value if ans.kind == "number" else ans.value.get(name)):
            # an exact chain and a decimal answer: 72π stated as 226.08 is π
            # taken as 3.14 (maths.verify, 2026-10-09)
            why = _pi_match(got, value)
            if not why:
                raise GeometryRefusal("answer_mismatch", f"the steps give {name} = {got}, the answer says {value}")
            approx = f" ({why})"
        rep.proved[name] = got
        rep.checks.append(Check(f"answer {name}", True, f"{name} = {got}{approx}"))
    # consistency 8a: the bound value is the proved one
    for name, bound in m.bind.items():
        got = rep.proved.get(name)
        if got is not None and sp.simplify(got - bound) != 0 and not _rounds_to(got, bound):
            raise GeometryRefusal("bind_mismatch", f"the figure was drawn with {name} = {bound}, the proof gives {got}")
    # consistency 8b: the drawn unknown agrees with the proof
    fig = q.figure_by_id(fid).figure
    proved_subs = {sp.Symbol(k): v for k, v in rep.proved.items()}
    for ms in fig.measures:
        if ms.role not in ("unknown", "derived") or ms.target in m.solids:
            continue
        if ms.target in m.points:
            ex, ey = (v.subs(proved_subs) for v in exact_pair(ms.value, where=f"measure.{ms.target}"))
            if ex.free_symbols or ey.free_symbols:
                continue
            hx, hy = m.xy(ms.target)
            if abs(float(ex) - hx) > LENGTH_REL_TOL * max(1.0, abs(float(ex))) or \
                    abs(float(ey) - hy) > LENGTH_REL_TOL * max(1.0, abs(float(ey))):
                raise GeometryRefusal("answer_mismatch",
                                      f"the proof puts {ms.target} at ({ex}, {ey}) but the figure draws it at ({hx:.6g}, {hy:.6g})")
            continue
        e = exact_value(ms.value, where=f"measure.{ms.target}").subs(proved_subs)
        if e.free_symbols:
            continue
        true, drawn = float(e), realised(m, ms.target)
        tol = ANGLE_TOL_DEG if ms.target in m.angle_ids else LENGTH_REL_TOL * max(1.0, abs(true))
        if abs(true - drawn) > tol:
            raise GeometryRefusal("answer_mismatch",
                                  f"{ms.target}: the proof gives {e} but the constructed figure has {drawn:.4g}", ms.target)
        rep.checks.append(Check(f"realised {ms.target}", True, f"drawn {drawn:.4g} = proved {e}"))


# ── evidence questions ────────────────────────────────────────────────────

def _norm_label(x) -> str:
    """'A', 'a' and 'p_a' name the same point: an answer keyed by the point
    id is read the way the figure's own naming reads it."""
    t = str(x).strip().lower()
    return t[2:] if t.startswith("p_") and len(t) > 2 else t


# the engine's value may be more specific than a correct answer
_LESS_SPECIFIC = {"regular quadrilateral": {"square", "quadrilateral"},
                  "equilateral triangle": {"regular triangle", "triangle"}}


def _label_eq(got, want) -> bool:
    """The answer names what the engine computed: exactly, or less
    specifically ('hexagon' for a 'regular hexagon'). Never the other way."""
    g, w = _norm_label(got), _norm_label(want)
    if g == w:
        return True
    if g.startswith("regular ") and g[len("regular "):] == w:
        return True
    return w in _LESS_SPECIFIC.get(g, ())


def _label_sets_eq(have, want) -> bool:
    have, want = list(have), list(want)
    return len(have) == len(want) and all(any(_label_eq(h, w) for h in have) for w in want) \
        and all(any(_label_eq(h, w) for w in want) for h in have)


def resolve_over(rep: QuestionReport, q: QuestionSpec, over: list[str]) -> tuple[list[str], set[str]]:
    """(figure ids, point ids) for an `asks.over`. A model writes the figure's
    id — or, as the chapter-17 video did on 2026-10-08, the POINTS it wants
    read ("coordinates of A, B, C, D": over = [p_a, p_b, …]) or the SHAPE it
    wants named (over = [rect]). A name that is an object of exactly one
    figure means that figure; named points also restrict a per-point
    answer to themselves. A name nothing owns is still a bad reference."""
    fids: list[str] = []
    points: set[str] = set()
    for name in over:
        if q.figure_by_id(name) is not None:
            fid = name
        else:
            owners = [ref.id for ref in q.figures if _figure_owns(rep.models.get(ref.id), name)]
            if len(owners) != 1:
                raise GeometryRefusal("bad_reference", f"asks.over names {name!r}, not a figure of this question", name)
            fid = owners[0]
            if rep.models[fid].has_point(name):
                points.add(name)
        if fid not in fids:
            fids.append(fid)
    return fids, points


def _figure_owns(m: Optional[Model], name: str) -> bool:
    if m is None:
        return False
    return bool(m.has_point(name) or name in m.object_ids or name in m.polygons or name in m.segment_ids
                or name in m.angle_ids or name in m.lines)


def _compute_part(rep: QuestionReport, q: QuestionSpec, asks: Asks) -> Any:
    values: dict[str, Any] = {}
    over, named_points = resolve_over(rep, q, asks.over)
    for fid in over:
        ref = q.figure_by_id(fid)
        if ref is None:
            raise GeometryRefusal("bad_reference", f"asks.over names {fid!r}, not a figure of this question", fid)
        m = rep.models[fid]
        label = ref.label or fid
        if asks.fill is not None:
            grid = next(iter(m.grids.values()), None)
            if grid is None:
                raise GeometryRefusal("bad_schema", "`fill` applies to a grid pattern")
            per_colour = {c: compute_property(m, asks.property, fill=c) for c in grid.palette}
            def passes(v):
                if asks.equals is not None:
                    return v == asks.equals
                if asks.greater_than is not None:
                    return v > asks.greater_than
                if asks.less_than is not None:
                    return v < asks.less_than
                raise GeometryRefusal("bad_schema", "`fill` needs equals / greater_than / less_than")
            values[label] = sorted(c for c, v in per_colour.items() if passes(v))
            rep.computed[f"{fid}:{asks.property}:by_colour"] = per_colour
        else:
            got = compute_property(m, asks.property)
            if isinstance(got, dict) and len(over) == 1:
                # v2: a property per POINT of the one figure (read the
                # coordinates, name the quadrant): the points are the labels —
                # the named ones when the question named points (the property
                # keys by LABEL, the question by point id)
                keep = {m.points[pid].label or pid for pid in named_points if m.has_point(pid)} | named_points
                values.update({str(k): v for k, v in got.items() if not named_points or str(k) in keep})
            elif isinstance(got, dict) and len(got) == 1:
                # several figures, one point each ("which quadrant is P in,
                # in each diagram"): the figure's label carries its point's value
                values[label] = next(iter(got.values()))
            else:
                values[label] = got
    rep.computed[asks.property] = dict(values)
    if asks.select is not None:
        return {lab for lab, v in values.items() if _norm_label(v) == _norm_label(asks.select)}
    if asks.fill is not None and len(values) == 1:
        return set(next(iter(values.values())))
    if len(values) == 1:
        return next(iter(values.values()))
    return values


def _relabel(value: Any, id_to_label: dict[str, str]) -> Any:
    """An answer keyed by figure ID (fig_a) means the figure's LABEL (A):
    models write either, the engine keys by label."""
    if isinstance(value, dict):
        return {id_to_label.get(str(k), k): v for k, v in value.items()}
    if isinstance(value, list):
        return [id_to_label.get(str(v), v) for v in value]
    return id_to_label.get(str(value), value) if isinstance(value, str) else value


def _compare_answer(ans: Answer, got: Any, name: str, rep: QuestionReport,
                    id_to_label: Optional[dict[str, str]] = None) -> None:
    if id_to_label:
        ans = Answer(kind=ans.kind, value=_relabel(ans.value, id_to_label), unit=ans.unit)
    if ans.kind == "label_set" or ans.kind == "value_set":
        want = {_norm_label(x) for x in (ans.value if isinstance(ans.value, list) else [ans.value])}
        have = {_norm_label(x) for x in (got if isinstance(got, (set, list)) else [got])}
        if not _label_sets_eq(have, want):
            raise GeometryRefusal("answer_mismatch", f"{name}: the figure gives {sorted(have)}, the answer says {sorted(want)}")
    elif ans.kind == "label_map" and not isinstance(got, dict):
        # one figure: the engine computed a single value; a label_map answer
        # with one entry (or a bare value) names it
        want = ans.value
        if isinstance(want, dict) and len(want) == 1:
            want = next(iter(want.values()))
        elif isinstance(want, list) and len(want) == 1:
            want = want[0]
        if isinstance(want, (dict, list)) or not _label_eq(got, want):
            raise GeometryRefusal("answer_mismatch", f"{name}: the figure gives {_plain(got)}, the answer says {_plain(want)}")
    elif ans.kind == "label_map":
        if not isinstance(ans.value, dict):
            raise GeometryRefusal("bad_schema", f"{name}: label_map is {{label: value}}")
        want = {_norm_label(k): _norm_label(v) for k, v in ans.value.items()}
        have = {_norm_label(k): _norm_label(v) for k, v in got.items()}
        if set(want) != set(have) or not all(_label_eq(have[k], want[k]) for k in want):
            raise GeometryRefusal("answer_mismatch", f"{name}: the figure gives {have}, the answer says {want}")
    elif ans.kind == "number":
        if isinstance(got, dict):
            raise GeometryRefusal("bad_schema", f"{name}: several figures need a label_map answer")
        if isinstance(got, bool) or not isinstance(got, (int, float)):
            raise GeometryRefusal("bad_schema", f"{name}: the property is not a number")
        if sp.simplify(exact_value(ans.value, where="answer") - got) != 0:
            raise GeometryRefusal("answer_mismatch", f"{name}: the figure gives {got}, the answer says {ans.value}")
    else:
        raise GeometryRefusal("bad_schema", f"{name}: unknown answer kind {ans.kind!r}")
    rep.checks.append(Check(name, True, f"computed {_plain(got)}"))


def _check_discernible(m: Model, prop: str, fid: str) -> None:
    """An evidence figure the student reads must be readable: what is
    meant to differ differs by a margin a ruler or a glance resolves."""
    from maths.geometry.properties import _angle_keys, _side_keys, _the_polygon  # noqa: PLC0415
    if m.solids and prop not in ("faces", "edges", "vertices", "solid_name", "folds_to_cube", "opposite_face"):
        # v3: a solid is counted, never measured — its picture is a view
        raise GeometryRefusal("not_discernible", f"{fid}: {prop} cannot be read off a solid's picture; a solid is counted", fid)
    if prop in ("coordinates_of", "quadrant") and m.axes is not None:
        # v2: a point the student reads sits on a grid intersection
        for pid, pt in m.points.items():
            for v in (pt.x, pt.y):
                k = v / m.axes.step
                if abs(k - round(k)) > 1e-6:
                    raise GeometryRefusal("not_discernible",
                                          f"{fid}: {pid} at ({pt.x:g}, {pt.y:g}) is not on a grid intersection at step {m.axes.step:g}", fid)
        return
    if prop not in ("triangle_class_by_sides", "triangle_class_by_angles", "count_right_angles",
                    "count_obtuse_angles", "count_acute_angles", "lines_of_symmetry", "polygon_name"):
        return
    if not any(p.closed for p in m.polygons.values()):
        return
    pg = _the_polygon(m, prop)
    sides, angles = _side_keys(pg), _angle_keys(m, pg)
    if prop in ("triangle_class_by_sides", "lines_of_symmetry", "polygon_name"):
        for i in range(len(sides)):
            for j in range(i + 1, len(sides)):
                if m.lengths_equal(sides[i], sides[j]):
                    continue
                a, b = m.length_float(sides[i]), m.length_float(sides[j])
                if abs(a - b) < DISCERN_LEN:
                    raise GeometryRefusal("not_discernible",
                                          f"{fid}: two sides meant to differ are {a:.3g} and {b:.3g} — a ruler would call them equal", fid)
    if prop in ("triangle_class_by_angles", "count_right_angles", "count_obtuse_angles", "count_acute_angles"):
        for k in angles:
            v = m.angle_float(k)
            an = m.angles.get(k)
            is_right = (an is not None and an.exact is not None and sp.simplify(an.exact - 90) == 0) \
                or abs(v - 90.0) <= ANGLE_TOL_DEG   # a 3-4-5 triangle's right angle arrives by floats
            if not is_right and abs(v - 90.0) < DISCERN_ANGLE:
                raise GeometryRefusal("not_discernible",
                                      f"{fid}: an angle of {v:.3g}° is too close to a right angle to read", fid)
    if prop == "lines_of_symmetry":
        for i in range(len(angles)):
            for j in range(i + 1, len(angles)):
                if m.angles_equal(angles[i], angles[j]):
                    continue
                if abs(m.angle_float(angles[i]) - m.angle_float(angles[j])) < DISCERN_EQUAL_ANGLE:
                    raise GeometryRefusal("not_discernible", f"{fid}: two angles meant to differ look equal", fid)


def _check_giveaway(q: QuestionSpec, prop: str) -> None:
    banned = _GIVEAWAY.get(prop, set())
    for ref in q.figures:
        for mk in ref.figure.marks:
            if mk.kind in banned:
                raise GeometryRefusal("mark_gives_away", f"{ref.id}: a {mk.kind} mark answers {prop}", ref.id)


# ── entry point ───────────────────────────────────────────────────────────

_ANG_RE = re.compile(r"ang\(\s*([A-Za-z0-9_]+)\s*\)")


def _angles_named(m: Model, lines: list[str]) -> list[str]:
    """The angle ids a step's lines mention (``ang(abc)`` -> ``angle_abc``),
    in order, once each, only those the figure knows."""
    out: list[str] = []
    for line in lines:
        for name in _ANG_RE.findall(line):
            aid = f"angle_{name}"
            if aid in m.angle_ids and aid not in out:
                out.append(aid)
    return out


def verify_question(raw: dict | QuestionSpec) -> QuestionReport:
    try:
        q = raw if isinstance(raw, QuestionSpec) else parse_question(raw)
    except GeometryRefusal as exc:
        return _refuse(QuestionReport(str((raw or {}).get("id", "q")) if isinstance(raw, dict) else "q"), exc, "parse")
    rep = QuestionReport(q.id)
    rep.checks.append(Check("parse", True, q.schema_version))
    if not q.figures:
        return _refuse(rep, GeometryRefusal("bad_schema", "no figure: a question without one is not a geometry "
                                            "question (maths.facts proves naming questions)"), "figures")
    for ref in q.figures:
        try:
            rep.models[ref.id] = compile_figure(ref.figure)
        except GeometryRefusal as exc:
            return _refuse(rep, exc, f"compile {ref.id}")
        rep.checks.append(Check(f"compile {ref.id}", True, f"{len(rep.models[ref.id].points)} points"))
    try:
        if q.parts:
            for i, part in enumerate(q.parts, 1):
                name = f"part {i} ({part.asks.property})"
                _check_giveaway(q, part.asks.property)
                if q.figure_role == "evidence":
                    for fid in resolve_over(rep, q, part.asks.over)[0]:
                        _check_discernible(rep.models[fid], part.asks.property, fid)
                got = _compute_part(rep, q, part.asks)
                _compare_answer(part.answer, got, name, rep,
                                {ref.id: (ref.label or ref.id) for ref in q.figures})
        if q.steps:
            if len(q.figures) != 1:
                raise GeometryRefusal("bad_schema", "a reasoning chain works on exactly one figure")
            fid = q.figures[0].id
            knowledge = _run_chain(rep, q, fid, rep.models[fid])
            _check_reasoning_answer(rep, q, fid, rep.models[fid], knowledge)
        elif q.answer is not None and not q.parts:
            raise GeometryRefusal("answer_unproved", "an answer with no steps and no property is unproved")
        if not q.parts and not q.steps:
            if q.figure_role == "illustration":
                # an answer-key construction: the figure compiling IS the
                # content (B14, B15); its given measures were checked
                rep.checks.append(Check("illustration", True, "the construction is the answer"))
            else:
                raise GeometryRefusal("bad_schema", "a question asks something: `asks`/`parts` or `steps`")
    except GeometryRefusal as exc:
        return _refuse(rep, exc, exc.where or "verify")
    return rep
