"""Step-level verification of a worked example, with SymPy.

Not "is the final answer right": a correct answer reached through an invalid
step teaches the invalid step. So every TRANSFORM is checked as a change of
state that preserves what the state means — the same solution set for
relations, the same value for expressions — and only then is the final
answer checked against the problem. A step whose meaning SymPy cannot
establish is reported as unverifiable, and an unverifiable transform fails
the example exactly as a wrong one does (founder direction 2026-09-24): the
generator gets the reasons back and rewrites that example, and nothing
unproven reaches a board.

Semantics of a state (a list of lines):
  * expressions             — line i must stay equivalent to line i
  * relations, task solve_* — ONE unknown's solution set: a list is the UNION
                              of its lines (a quadratic's two cases), except
                              for solve_system, where the lines hold
                              TOGETHER and the set is that of the system
  * a mix of kinds          — not verifiable

ROUNDING IS NOT EQUIVALENCE. A step of kind "round" (tasks round and
estimate, founder direction 2026-09-25 after a Grade 6 place-value chapter
had every example dropped) keeps the SHAPE of each line and replaces
numbers by their roundings: token for token the same, and every number that
changed is the old one rounded half away from zero to the step's stated
precision (a unit like 1000 or 0.01, "2 dp", "2 sf"), or to some power of
ten or 1-4 significant figures when none is stated. The answer of such a
task is the value the verified rounding steps reach, never the exact value
of the problem — 7583 + 3421 estimated as 8000 + 3000 is 11000.

DATA TASKS (mean, median, mode, range; 2026-09-26, after a Grade 7
statistics chapter failed every worksheet and exam question because a comma
list could only be a refusal): the givens are ONE data list, "4, 8, 6, 10,
12". A transform from a data state to an expression is verified as the
task's statistic of that data — (4 + 8 + 6 + 10 + 12)/5 for the mean, 12 -
4 for the range, the middle value(s) for the median, the commonest value(s)
for the mode — and a transform from a data state to a data state must keep
the same numbers (sorting, for the median) or, for the mode, name exactly
the modes. From there the working is expressions, checked as always, and
the answer is the statistic's exact value.

The common mistake is verified the other way round: SymPy must show the
wrong route really changes the meaning, or a valid method would be taught as
an error. Every SymPy call runs under mathsvc's hard timeout — a hung
verification is worse than an unverified one.

DECIMALS ARE EXACT. A decimal in a line is the rational it writes, never a
binary float: "188.4 + 56.52" IS "244.92", and the 2.8e-14 a float left
over made that step "WRONG" on the first geometry kit (2026-10-09).

π MAY BE TAKEN AS A SCHOOL APPROXIMATION (founder direction 2026-10-09, after
the same kit lost its only cylinder-surface-area example three times). A
line "pi = 3.14" (or "π ≈ 22/7") among the givens or the working is a
DECLARATION of the approximation in force, not an equation to solve (as an
equation it is false, and the state's solution set was EmptySet). And when
two sides are not equivalent exactly, but the side with π in it equals the
side without once π is 22/7, 3.14, 3.142, 3.1416, 3.14159 or 3.141593 —
or that value rounded to a few decimal places — the step is verified and
the report says which value π was taken as. An answer in terms of π still
verifies exactly; a wrong approximation (π as 3.1) still fails.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Optional

import sympy as sp

from maths.notation import NotationError, Relation, notation_of, parse_point, parse_relation, parse_state, symbols_named
from maths.schema import DATA_TASKS, LINE_TASKS, Lesson, Step, TryIt, WorkedExample
from maths.tokens import TokenError, tokenize
from mathsvc.safety import MathError, MathTimeoutError, run_with_timeout

SOLVE_TASKS = ("solve", "solve_system", "solve_inequality")
EXPRESSION_TASKS = ("simplify", "expand", "factorise", "evaluate")
ROUND_TASKS = ("round", "estimate")
_TIMEOUT = 5.0


@dataclass
class Check:
    name: str
    ok: Optional[bool]        # True verified, False wrong, None not verifiable
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class ExampleReport:
    label: str
    status: str = "verified"   # verified | failed
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.ok is False]

    @property
    def unverified(self) -> list[Check]:
        return [c for c in self.checks if c.ok is None and c.name.startswith(("step", "answer", "chain"))]

    @property
    def reasons(self) -> list[str]:
        """What to tell the generator, one line per problem."""
        out = [f"{c.name}: WRONG — {c.detail}" for c in self.failures]
        out += [f"{c.name}: could not be verified — {c.detail}" for c in self.unverified]
        return out

    def to_dict(self) -> dict:
        return {"label": self.label, "status": self.status,
                "checks": [c.to_dict() for c in self.checks], "reasons": self.reasons}


# ── helpers ──────────────────────────────────────────────────────────────


def _timed(fn: Callable, *args):
    return run_with_timeout(fn, *args, timeout=_TIMEOUT)


def _exact(expr):
    """The expression with every decimal read as the rational it writes:
    188.4 is 942/5, not the nearest binary float. Floats are what the
    calculator's parser makes of a decimal, and float arithmetic is why
    "188.4 + 56.52" was not "244.92" (2.84e-14 over) on the first geometry
    kit. A non-SymPy value comes back unchanged."""
    try:
        floats = expr.atoms(sp.Float)
    except AttributeError:
        return expr
    if not floats:
        return expr
    return expr.xreplace({f: sp.nsimplify(f, rational=True) for f in floats})


def _zero(expr) -> bool:
    """Is this expression identically zero? expand first (cheap and exact
    for polynomials), simplify only when needed (radicals, fractions)."""
    e = sp.expand(_exact(expr))
    if e == 0:
        return True
    try:
        return sp.simplify(e) == 0
    except Exception:  # noqa: BLE001 — a simplify that blows up is "no"
        return False


# ── π as a school approximation ──────────────────────────────────────────
#
# The values a syllabus tells a learner to take π as: 22/7 (CBSE), 3.14
# (CBSE and Cambridge), 3.142 / 3.1416 / 3.14159 / 3.141593 (3-7 figures,
# the calculator's value rounded). A line of working that replaces π by one
# of these is a step a textbook prints, not an error — but only these: a
# step that takes π as 3.1 is wrong.
_PI_VALUES = (sp.Rational(22, 7), sp.Rational(314, 100), sp.Rational(3142, 1000), sp.Rational(31416, 10000),
              sp.Rational(314159, 100000), sp.Rational(3141593, 1000000))
_PI_DECLARED_RE = re.compile(r"(?:π|\bpi\b)\s*(?:=|≈|~|is|as|be)\s*(\d+(?:\.\d+)?(?:\s*/\s*\d+)?)", re.I)


def _fmt_pi(q) -> str:
    q = sp.Rational(q)
    for k in range(0, 7):
        scaled = q * 10 ** k
        if scaled.q == 1:
            return f"{int(scaled) / 10 ** k:.{k}f}" if k else str(q.p)
    return f"{q.p}/{q.q}"


def _is_pi_declaration(r: Relation) -> bool:
    """``pi = 3.14``: the approximation declared, not an equation (false as
    one — SymPy gave the whole state no solution)."""
    return (r.is_equation and r.rhs is not None and not r.free_symbols
            and ((r.lhs == sp.pi and r.rhs.is_number and not r.rhs.has(sp.pi))
                 or (r.rhs == sp.pi and r.lhs.is_number and not r.lhs.has(sp.pi))))


def _pi_values(ex: Optional[WorkedExample]) -> tuple:
    """The approximations accepted for this example: the standard ones, and
    any the problem's words or a declaration line name ("take π = 3.14",
    "pi = 22/7"), so a stated value is honoured even off the standard list."""
    out = list(_PI_VALUES)
    if ex is None:
        return tuple(out)
    texts = [ex.problem or ""] + list(ex.givens)
    for st in ex.steps:
        texts += list(st.before) + list(st.after)
    for t in texts:
        for m in _PI_DECLARED_RE.finditer(t.replace("π", "pi")):
            try:
                q = sp.Rational(m.group(1).replace(" ", ""))
            except (TypeError, ValueError):
                continue
            if 3 <= q <= sp.Rational(32, 10) and q not in out:
                out.append(q)
    return tuple(out)


def _as_fraction(v) -> Optional[Fraction]:
    v = _exact(v)
    if getattr(v, "is_Rational", False):
        return Fraction(int(v.p), int(v.q))
    return None


def _is_decimal_rounding(b: Fraction, a: Fraction) -> bool:
    """``a`` is ``b`` rounded to 0-4 decimal places or 3-4 significant
    figures — what a printed answer does with 282.7431... (282.74), never
    "300" for it."""
    return any(_round_to(b, Fraction(10) ** k) == a for k in range(-4, 1)) or \
        any(_round_sf(b, n) == a for n in (3, 4))


def _pi_match(b, a, ex: Optional[WorkedExample] = None) -> Optional[str]:
    """``a`` is ``b`` with π taken as a school approximation (and, for a
    bare number, perhaps rounded after): the reason to report, or None.
    Only from the exact side to the approximate one — once π is 3.14 it
    does not come back."""
    try:
        if not (b.has(sp.pi) and not a.has(sp.pi)):
            return None
    except AttributeError:
        return None
    for q in _pi_values(ex):
        if _zero(b.subs(sp.pi, q) - a):
            return f"π taken as {_fmt_pi(q)}"
    fa = _as_fraction(a)
    if fa is None:
        return None
    for q in _pi_values(ex):
        fb = _as_fraction(sp.nsimplify(b.subs(sp.pi, q), rational=True))
        if fb is not None and _is_decimal_rounding(fb, fa):
            return f"π taken as {_fmt_pi(q)}, then rounded"
    return None


_PI_TAKEN_RE = re.compile(r"π taken as (\d+(?:\.\d+)?(?:/\d+)?)")


def pi_taken_as(detail: str) -> Optional[sp.Rational]:
    """The value π was taken as in a verdict this module wrote ("… (π taken
    as 3.14)"), or None. For a caller that must carry the approximation
    forward — the geometry chain's knowledge, once a step has taken it."""
    m = _PI_TAKEN_RE.search(detail or "")
    return sp.Rational(m.group(1)) if m else None


def _pi_sets_match(sb, sa, ex: Optional[WorkedExample] = None) -> Optional[str]:
    """Two solution sets that agree once π is taken as an approximation in
    ``sb`` — finite sets element by element, solution lists value by value."""
    notes: list[str] = []

    def value(vb, va) -> bool:
        if _equal_values(vb, va):
            return True
        why = _pi_match(vb, va, ex)
        if why:
            notes.append(why)
            return True
        return False

    if isinstance(sb, sp.FiniteSet) and isinstance(sa, sp.FiniteSet) and len(sb) == len(sa):
        left = list(sa.args)
        for vb in sb.args:
            hit = next((i for i, va in enumerate(left) if value(vb, va)), None)
            if hit is None:
                return None
            left.pop(hit)
        return notes[0] if notes else None
    if isinstance(sb, list) and isinstance(sa, list) and len(sb) == len(sa):
        left = list(sa)
        for db in sb:
            hit = next((i for i, da in enumerate(left)
                        if set(db) == set(da) and all(value(db[k], da[k]) for k in db)), None)
            if hit is None:
                return None
            left.pop(hit)
        return notes[0] if notes else None
    return None


def _equal_values(a, b) -> bool:
    try:
        return _zero(sp.sympify(a) - sp.sympify(b))
    except Exception:  # noqa: BLE001
        return False


def _split_answers(entries: list[str]) -> list[str]:
    """["x = 2 or x = 3"] -> ["x = 2", "x = 3"]; ["x = 1, y = 2"] -> both.
    A comma splits only between two relations, never inside one."""
    out: list[str] = []
    for e in entries:
        for part in re.split(r"\bor\b", e, flags=re.IGNORECASE):
            pieces = [p.strip() for p in part.split(",")]
            if len(pieces) > 1 and all(re.search(r"(<=|>=|=|<|>)", p) for p in pieces):
                out.extend(pieces)
            elif part.strip():
                out.append(part.strip())
    return out


def _kinds(rels: list[Relation]) -> str:
    if not rels:
        return "empty"
    if all(r.is_data for r in rels):
        return "data"
    if any(r.is_data for r in rels):
        return "mixed"
    if all(r.is_expression for r in rels):
        return "expressions"
    if all(not r.is_expression for r in rels):
        return "relations"
    return "mixed"


def half_plane(rel: Relation):
    """A linear inequality in two unknowns as the canonical half-plane it
    names: ("halfplane", a, b, c, op) for a·x + b·y + c op 0 with op "<" or
    "<=" (a ">" is turned round) and the first non-zero coefficient scaled
    to ±1 — so "x + y <= 4" and "y <= 4 - x" are one state. None when the
    relation is not a linear inequality in two unknowns."""
    if not rel.is_inequality or rel.rhs is None:
        return None
    syms = sorted(rel.free_symbols, key=str)
    if len(syms) != 2:
        return None
    expr = sp.expand(rel.lhs - rel.rhs)
    op = rel.op
    if op in (">", ">="):
        expr, op = -expr, {">": "<", ">=": "<="}[op]
    try:
        poly = sp.Poly(expr, *syms)
    except sp.PolynomialError:
        return None
    if poly.total_degree() != 1:
        return None
    a, b = poly.coeff_monomial(syms[0]), poly.coeff_monomial(syms[1])
    c = poly.coeff_monomial(1)
    if not all(v.is_Rational for v in (a, b, c)) or (a == 0 and b == 0):
        return None
    k = abs(a) if a != 0 else abs(b)
    return ("halfplane", sp.Rational(a) / k, sp.Rational(b) / k, sp.Rational(c) / k, op, tuple(str(s) for s in syms))


def _solution_set(rels: list[Relation], variables: list[sp.Symbol], mode: str):
    """The solutions of a state. Univariate states become SymPy Sets (so
    inequalities and equations compose); multivariate systems become a list
    of solution dicts. ``mode`` is "all" (the lines hold together) or "any"
    (the lines are alternatives)."""
    if len(variables) == 1 and all(r.free_symbols <= {variables[0]} for r in rels):
        x = variables[0]
        sets = []
        for r in rels:
            if r.is_inequality:
                sets.append(sp.solve_univariate_inequality(r.as_sympy(), x, relational=False))
            else:
                s = sp.solveset(sp.Eq(_exact(r.lhs), _exact(r.rhs)), x, domain=sp.S.Reals)
                sets.append(s)
        if not sets:
            return sp.S.EmptySet
        out = sets[0]
        for s in sets[1:]:
            out = sp.Intersection(out, s) if mode == "all" else sp.Union(out, s)
        return out
    eqs = [r for r in rels if r.is_equation]
    if len(eqs) != len(rels):
        # charts phase 7 (2026-10-09): ONE linear inequality in two unknowns
        # is a half-plane, compared by its canonical form
        if len(rels) == 1:
            hp = half_plane(rels[0])
            if hp is not None:
                return hp
        raise ValueError("a multivariate inequality is not verifiable here")
    if mode == "all":
        sols = sp.solve([sp.Eq(_exact(r.lhs), _exact(r.rhs)) for r in eqs], variables, dict=True)
        return [dict(s) for s in sols]
    out = []
    for r in eqs:
        out.extend(dict(s) for s in sp.solve(sp.Eq(_exact(r.lhs), _exact(r.rhs)), variables, dict=True))
    return out


def _same_solutions(a, b) -> bool:
    if isinstance(a, tuple) or isinstance(b, tuple):
        return a == b                       # two half-planes: the same canonical form
    if isinstance(a, sp.Set) and isinstance(b, sp.Set):
        if a == b:
            return True
        try:
            return bool(a.is_subset(b)) and bool(b.is_subset(a))
        except Exception:  # noqa: BLE001
            return False
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        used = set()
        for sa in a:
            hit = None
            for j, sb in enumerate(b):
                if j in used or set(sa) != set(sb):
                    continue
                if all(_equal_values(sa[k], sb[k]) for k in sa):
                    hit = j
                    break
            if hit is None:
                return False
            used.add(hit)
        return True
    return False


def _free(rels: list[Relation]) -> set:
    out: set = set()
    for r in rels:
        out |= r.free_symbols
    return out


def _project(sols, keep: list[sp.Symbol]):
    """A multivariate solution list restricted to ``keep`` — what the state
    says about those unknowns alone. One unknown becomes a Set, so it
    compares with a univariate state's solutions."""
    if not isinstance(sols, list):
        return sols
    rows: list[dict] = []
    for d in sols:
        row = {k: v for k, v in d.items() if k in keep}
        if not any(set(row) == set(r) and all(_equal_values(row[k], r[k]) for k in row) for r in rows):
            rows.append(row)
    if len(keep) == 1:
        k = keep[0]
        vals = [r[k] for r in rows if k in r]
        if len(vals) == len(rows):
            return sp.FiniteSet(*vals) if vals else sp.S.EmptySet
    return rows


def _proper_subset(a, b) -> bool:
    """``a`` is a non-empty strict subset of ``b`` (Sets or solution lists)."""
    if isinstance(a, sp.Set) and isinstance(b, sp.Set):
        try:
            return bool(a.is_subset(b)) and not bool(b.is_subset(a)) and a != sp.S.EmptySet
        except Exception:  # noqa: BLE001
            return False
    if isinstance(a, list) and isinstance(b, list):
        if not a or len(a) >= len(b):
            return False
        for sa in a:
            if not any(set(sa) == set(sb) and all(_equal_values(sa[k], sb[k]) for k in sa) for sb in b):
                return False
        return True
    return False


# A step that keeps only SOME of the solutions is a different step from a
# transformation, and it has to say so: a root rejected for a stated reason
# ("c is a length", "reject the negative root") — or a problem whose unknown
# is a magnitude, where only the non-positive roots may go.
_DISCARD_WORDS = re.compile(r"\b(positive|negative|length|distance|side|reject(?:ed|s)?|discard(?:ed|s)?|"
                            r"rule[sd]? out|cannot be|can't be|not possible|invalid|extraneous|"
                            r"does not fit|doesn't fit|measure|physical)\b", re.I)
_MAGNITUDE_WORDS = re.compile(r"\b(hypotenuse|side|length|distance|height|width|depth|radius|diameter|"
                              r"perimeter|area|volume|speed|time|age|mass|weight|number of)\b", re.I)


def _discarded(before_sols, after_sols) -> list:
    """The solutions ``before`` has and ``after`` does not."""
    if isinstance(before_sols, sp.Set) and isinstance(after_sols, sp.Set):
        gone = sp.Complement(before_sols, after_sols)
        return list(gone) if isinstance(gone, sp.FiniteSet) else [gone]
    if isinstance(before_sols, list) and isinstance(after_sols, list):
        return [d for d in before_sols
                if not any(set(d) == set(a) and all(_equal_values(d[k], a[k]) for k in d) for a in after_sols)]
    return []


def _discard_allowed(before_sols, after_sols, operation: str, problem: str) -> tuple[bool, str]:
    """May this step keep only some solutions? Yes when the operation names
    a reason (any root may go), or the problem's unknown is a magnitude and
    every root that went is non-positive."""
    gone = _discarded(before_sols, after_sols)
    if not gone:
        return False, ""
    if _DISCARD_WORDS.search(operation or ""):
        return True, f"root(s) discarded for the stated reason: {_fmt_solutions(gone) if isinstance(gone[0], dict) else ', '.join(str(g) for g in gone)}"
    if _MAGNITUDE_WORDS.search(problem or ""):
        values = []
        for g in gone:
            values.extend(g.values() if isinstance(g, dict) else [g])
        try:
            if values and all(v.is_number and bool(v <= 0) for v in values):
                return True, f"non-positive root(s) discarded: the unknown is a magnitude ({', '.join(str(v) for v in values)})"
        except Exception:  # noqa: BLE001
            return False, ""
    return False, ""


def _fully_determined(rels: list[Relation]) -> bool:
    """Every line is ``unknown = number``: the state is a list of known
    values, nothing is left to solve."""
    if not rels:
        return False
    for r in rels:
        if not r.is_equation or r.rhs is None:
            return False
        if isinstance(r.lhs, sp.Symbol) and not r.rhs.free_symbols:
            continue
        if isinstance(r.rhs, sp.Symbol) and not r.lhs.free_symbols:
            continue
        return False
    return True


def _fmt_solutions(s) -> str:
    if isinstance(s, tuple) and s and s[0] == "halfplane":
        _t, a, b, c, op, (x, y) = s
        return f"the half-plane {a}*{x} + {b}*{y} + {c} {op} 0"
    if isinstance(s, list):
        return "; ".join(", ".join(f"{k} = {v}" for k, v in sorted(d.items(), key=lambda kv: str(kv[0])))
                         for d in s) or "no solution"
    return str(s)


def _is_system(rels: list[Relation]) -> bool:
    """Two or more equations in two or more unknowns: the lines hold
    TOGETHER whatever the task says. A word problem with two unknowns
    arrives as task "solve" (its target is one letter), and reading its
    two equations as alternatives failed every step of a correct solution
    (Hindi demo, 2026-09-25: "x = 180 - y; x = y - 40" -> "x = 70; x = y -
    40" judged WRONG, the example dropped)."""
    if len(rels) < 2 or not all(r.is_equation for r in rels):
        return False
    free: set = set()
    for r in rels:
        free |= r.free_symbols
    return len(free) >= 2


def _state_mode(rels: list[Relation], default: str) -> str:
    return "all" if _is_system(rels) else default


# ── data lists: mean, median, mode, range ────────────────────────────────


def _canon(v):
    """A data value as an exact rational where it can be one, so 2.5 and
    5/2 count as the same value and sort together."""
    try:
        return sp.nsimplify(v, rational=True)
    except Exception:  # noqa: BLE001
        return v


def _modes(data: tuple) -> list:
    """The value(s) with the highest count, in first-seen order; empty when
    every value occurs equally often (no mode)."""
    counts: dict = {}
    order: list = []
    for v in data:
        c = _canon(v)
        if c not in counts:
            counts[c] = 0
            order.append(c)
        counts[c] += 1
    if not counts:
        return []
    top = max(counts.values())
    if top == 1 or len(set(counts.values())) == 1:
        return []
    return [c for c in order if counts[c] == top]


def _statistic(task: str, data: tuple):
    """The exact value of ``task`` over ``data`` — a SymPy number, or for
    the mode a LIST of values (several modes, or none)."""
    vals = [_canon(v) for v in data]
    if task == "mean":
        return sp.Add(*vals) / len(vals)
    if task == "median":
        srt = sorted(vals, key=lambda v: float(v))
        n = len(srt)
        return srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2
    if task == "range":
        return max(vals, key=lambda v: float(v)) - min(vals, key=lambda v: float(v))
    if task == "mode":
        return _modes(data)
    raise ValueError(f"{task} is not a data task")


def _multiset(data: tuple) -> list:
    return sorted((_canon(v) for v in data), key=lambda v: float(v))


def _value_line(r: Relation):
    """The value a line states: a bare expression, or the right side of an
    equation whose left side is a single name ("mean = 40/5", "x̄ = 8").
    None for anything else."""
    if r.is_data:
        return None
    if r.is_expression:
        return r.lhs
    if r.is_equation and isinstance(r.lhs, sp.Symbol) and not r.rhs.free_symbols:
        return r.rhs
    return None


def _values_of(rels: list[Relation]) -> Optional[list]:
    """Every value a state names: one data line's items, or one value per
    expression/equation line. None when a line names no value."""
    if len(rels) == 1 and rels[0].is_data:
        return [_canon(v) for v in rels[0].data]
    out = []
    for r in rels:
        v = _value_line(r)
        if v is None:
            return None
        out.append(v)
    return out


def _same_values(a: list, b: list) -> bool:
    """The same values, in any order, each matched once."""
    if len(a) != len(b):
        return False
    left = list(b)
    for x in a:
        hit = next((i for i, y in enumerate(left) if _equal_values(x, y)), None)
        if hit is None:
            return False
        left.pop(hit)
    return True


def _fmt_stat(v) -> str:
    if isinstance(v, list):
        return ", ".join(str(x) for x in v) if v else "no mode"
    return str(v)


def _data_step(before: list[Relation], after: list[Relation], task: Optional[str]) -> tuple[Optional[bool], str]:
    """(verdict, detail) for a step whose ``before`` is a data list."""
    if len(before) != 1:
        return None, "a data state is one list"
    data = before[0].data
    ka = _kinds(after)
    if ka == "data":
        if len(after) != 1:
            return None, "a data state is one list"
        if _multiset(data) == _multiset(after[0].data):
            return True, "the same data, reordered"
        if task == "mode":
            want = _modes(data)
            if want and _same_values([_canon(v) for v in after[0].data], want):
                return True, f"the mode(s): {_fmt_stat(want)}"
            return False, f"the mode of {before[0].text!r} is {_fmt_stat(want)}, not {after[0].text!r}"
        return False, f"{after[0].text!r} is not the same data as {before[0].text!r}"
    if task not in DATA_TASKS:
        return None, f"a data list is only worked in a mean/median/mode/range task, not {task!r}"
    values = _values_of(after)
    if values is None:
        return None, f"could not read a value from {'; '.join(r.text for r in after)!r}"
    want = _statistic(task, data)
    if task == "mode":
        if not want:
            return False, f"{before[0].text!r} has no mode: every value occurs equally often"
        if _same_values(values, want):
            return True, f"the mode(s): {_fmt_stat(want)}"
        return False, f"the mode of {before[0].text!r} is {_fmt_stat(want)}, not {_fmt_stat(values)}"
    if len(values) != 1:
        return False, f"the {task} is one value, not {len(values)}"
    if _timed(_equal_values, values[0], want):
        return True, f"the {task} of the data: {_fmt_stat(want)}"
    return False, f"the {task} of {before[0].text!r} is {_fmt_stat(want)}, not {after[0].text!r}"


# ── the values an expression task supplies ───────────────────────────────────
#
# "Evaluate x + 5 when x = 3" (Substitution kit 604b3b79, 2026-10-06): every
# substitution step read as "'x + 5' is not equivalent to '3 + 5'", the answer
# as "'8' is not equivalent to 'x + 5'", and a lesson that wrote the values as
# lines ("3x + 7", "x = 4") or named the expression ("E = 3x + 7") was "mixed"
# and unreadable. The values are part of the problem: a step or an answer is
# judged with them substituted.

# a letter given a number in the words: "when x = 3", "for a = 2 and b = -1",
# "x = 1/2". Never "E = 3x + 7" (a number followed at once by a letter or a
# bracket is a term, not a value); "a = 3 and b = -2" is two values — the
# word after a space is prose (measured 2026-10-06: a = 3 was missed).
_VALUE_RE = re.compile(r"\b([a-zA-Z])\s*=\s*(-?\s*\d+(?:\.\d+)?(?:\s*/\s*\d+)?)(?![a-zA-Z(\d]|\.\d)")


def _is_assignment(r: Relation) -> bool:
    """``x = 3``: a letter given a number."""
    return (r.is_equation and isinstance(r.lhs, sp.Symbol) and r.rhs is not None
            and not r.rhs.free_symbols and bool(r.rhs.is_number))


def _values(ex: WorkedExample) -> dict:
    """The values the problem assigns its letters, for an EXPRESSION task:
    assignment lines among the givens first, then the words."""
    if ex.task not in EXPRESSION_TASKS:
        return {}
    out: dict = {}
    named: set = set()   # letters that NAME a given expression ("E = 0.5mv^2"): never values
    rels, _err = _parse(ex.givens, "the givens")
    for r in rels or []:
        if _is_assignment(r):
            out[r.lhs] = r.rhs
        elif r.is_equation and isinstance(r.lhs, sp.Symbol) and r.rhs is not None and r.lhs not in r.rhs.free_symbols:
            named.add(r.lhs)
    for name, num in _VALUE_RE.findall(ex.problem or ""):
        sym = sp.Symbol(name)
        if sym in named:
            continue
        try:
            out.setdefault(sym, sp.Rational(num.replace(" ", "")))
        except (TypeError, ValueError):
            continue
    return out


def _problem_symbols(ex: WorkedExample) -> set:
    """The letters of the problem's expression(s) — the ones a value may be
    given for; any other letter on the left of an equation is a NAME for the
    expression ("E = 3x + 7"), not an unknown."""
    rels, _err = _parse(ex.givens, "the givens")
    free: set = set()
    for r in rels or []:
        if _is_assignment(r) or r.is_data:
            continue
        if r.is_equation and isinstance(r.lhs, sp.Symbol) and r.rhs is not None and r.lhs not in r.rhs.free_symbols:
            free |= r.rhs.free_symbols   # "E = 3x + 7": E names the expression, x is its letter
        else:
            free |= r.free_symbols
    if not free:
        for name, _num in _VALUE_RE.findall(ex.problem or ""):
            free.add(sp.Symbol(name))
    return free


def _expression_lines(rels: list[Relation], values: dict, letters: set) -> Optional[list[tuple]]:
    """An expression-task state as ``[(relation, expression)]`` with the
    values substituted: a bare expression is itself; an assignment of one of
    the problem's letters is a value restated, not a line; ``E = 3x + 7`` with
    E not a letter of the problem is the expression under a name. None when
    a line is a genuine relation (an equation to solve, an inequality)."""
    out: list[tuple] = []
    for r in rels:
        if r.is_data:
            return None
        if r.is_expression:
            out.append((r, r.lhs.subs(values)))
        elif _is_assignment(r) and (r.lhs in letters or r.lhs in values):
            continue
        elif r.is_equation and isinstance(r.lhs, sp.Symbol) and r.lhs not in letters and r.rhs is not None:
            out.append((r, r.rhs.subs(values)))
        else:
            return None
    return out


# ── a word is not a quantity ─────────────────────────────────────────────────
#
# Cambridge Primary Mathematics 5, "2D shape and pattern" (generation
# 8861d1e6, 2026-10-07): "Identify the number of sides of a triangle" was
# written as the step "triangle" -> "3", and "Name the regular polygon with
# 5 sides" as "5 sides" -> "pentagon". The parser accepts long names (a word
# problem may call a quantity "speed"), so SymPy compared the SYMBOL
# triangle with 3 and reported "WRONG" — a false proof: nothing about
# triangles was decided, the lines were English. When two sides differ only
# by words one side has and the other lacks, the verdict is "cannot be
# checked", never "wrong". (It still fails the example: unproven is never
# printed. A naming question belongs to maths.facts.)

def _words(e) -> set:
    return {s for s in getattr(e, "free_symbols", set()) if len(str(s)) > 1}


def _word_gap(b, a) -> Optional[str]:
    """The reason two sides cannot be compared when their WORDS differ, or
    None when they carry the same words (or none)."""
    wb, wa = _words(b), _words(a)
    if wb == wa:
        return None
    names = sorted(str(s) for s in wb ^ wa)
    return (f"{', '.join(repr(n) for n in names)} {'is a word' if len(names) == 1 else 'are words'}, not a "
            "quantity: a naming or classifying answer is not something algebra can check")


def _expression_step(before: list[Relation], after: list[Relation], ex: WorkedExample) -> Optional[tuple]:
    """The expression-task reading of a step, or None to fall through: each
    line after says what the matching line before said, once the problem's
    values are in. One line after several (the values written, then used)
    is matched against the last line before."""
    values, letters = _values(ex), _problem_symbols(ex)
    eb, ea = _expression_lines(before, values, letters), _expression_lines(after, values, letters)
    if not eb or not ea:
        return None
    if len(eb) != len(ea):
        if len(ea) == 1:
            eb = eb[-1:]
        else:
            return None, f"{len(eb)} expression(s) became {len(ea)}"
    notes: list[str] = []
    for (rb, b), (ra, a) in zip(eb, ea):
        if not _timed(_zero, b - a):
            why = _timed(_pi_match, b, a, ex)
            if why:
                notes.append(why)
                continue
            gap = _word_gap(b, a)
            if gap:
                return None, gap
            with_values = (" with " + ", ".join(f"{k} = {v}" for k, v in sorted(values.items(), key=lambda kv: str(kv[0])))
                           if values else "")
            return False, f"{rb.text!r} is not equivalent to {ra.text!r}{with_values}"
    return True, "equivalent expressions" + (" under the given values" if values else "") + \
        (f" ({notes[0]})" if notes else "")


def _states_equivalent(before: list[Relation], after: list[Relation], variables: list[sp.Symbol],
                       mode: str, task: Optional[str] = None, *, operation: str = "",
                       problem: str = "", ex: Optional[WorkedExample] = None) -> tuple[Optional[bool], str]:
    """(verdict, detail) for "does `after` mean what `before` meant".
    ``mode`` is the example's default ("all" for a declared system, "any"
    otherwise); a state that IS a system is read as one regardless.
    ``task`` matters only when ``before`` is a data list: what `after`
    must be is then the task's statistic of it (_data_step).

    Two readings beyond plain equivalence, both decidable:
      * SUBSTITUTION — `after` has dropped unknowns `before` determined
        ("c^2 = a^2 + b^2; a = 3; b = 4" -> "c^2 = 3^2 + 4^2"): the states
        agree when `before`, projected onto the unknowns that remain, is
        `after`. Measured 2026-10-02: every Pythagoras step was "solutions
        before: a = 3, b = 4; after: no solution", because the sides were
        solved for the hypotenuse.
      * A DISCARDED ROOT — `after` keeps some of `before`'s solutions
        ("c^2 = 25" -> "c = 5"): allowed when ``operation`` says why, or the
        ``problem``'s unknown is a magnitude and only non-positive roots
        went (_discard_allowed). A step that silently halves the solutions
        of a plain equation still fails.
    """
    kb, ka = _kinds(before), _kinds(after)
    if kb == "data":
        return _data_step(before, after, task)
    if ex is not None and ex.task in EXPRESSION_TASKS:
        # an expression task: the lines as expressions with the problem's
        # values in, assignments and names set aside (_expression_step)
        read = _expression_step(before, after, ex)
        if read is not None:
            return read
    if kb != ka or kb in ("mixed", "empty"):
        return None, f"the lines change kind ({kb} -> {ka})"
    if kb == "expressions":
        if len(before) != len(after):
            return None, f"{len(before)} expression(s) became {len(after)}"
        notes: list[str] = []
        for i, (b, a) in enumerate(zip(before, after)):
            if not _timed(_zero, b.lhs - a.lhs):
                why = _timed(_pi_match, b.lhs, a.lhs, ex)
                if why:
                    notes.append(why)
                    continue
                gap = _word_gap(b.lhs, a.lhs)
                if gap:
                    return None, gap
                return False, f"{b.text!r} is not equivalent to {a.text!r}"
        return True, "equivalent expressions" + (f" ({notes[0]})" if notes else "")
    fb, fa = _free(before), _free(after)
    try:
        if fa and fb and fa < fb:
            # substitution: compare what `before` says about the unknowns
            # that remain with what `after` says about them
            vb, va = sorted(fb, key=str), sorted(fa, key=str)
            sb = _project(_timed(_solution_set, before, vb, _state_mode(before, mode)), va)
            sa = _timed(_solution_set, after, va, _state_mode(after, mode))
            if _same_solutions(sb, sa):
                return True, f"solutions unchanged for {', '.join(map(str, va))}: {_fmt_solutions(sa)}"
            approx = _pi_sets_match(sb, sa, ex)
            if approx:
                return True, f"solutions unchanged for {', '.join(map(str, va))} ({approx}): {_fmt_solutions(sa)}"
            ok, why = _discard_allowed(sb, sa, operation, problem)
            if ok and _proper_subset(sa, sb):
                return True, why
            return False, f"solutions before (for {', '.join(map(str, va))}): {_fmt_solutions(sb)}; after: {_fmt_solutions(sa)}"
        sb = _timed(_solution_set, before, variables, _state_mode(before, mode))
        sa = _timed(_solution_set, after, variables, _state_mode(after, mode))
    except MathTimeoutError:
        return None, "SymPy timed out"
    except Exception as exc:  # noqa: BLE001 — solve refused; try the weaker test
        # equal counts: each equation a nonzero multiple of its predecessor
        if len(before) == len(after) and all(r.is_equation for r in before + after):
            for b, a in zip(before, after):
                try:
                    ratio = sp.simplify((a.lhs - a.rhs) / (b.lhs - b.rhs))
                except Exception:  # noqa: BLE001
                    return None, f"could not compare {b.text!r} with {a.text!r}"
                if not (ratio.is_number and ratio != 0):
                    return None, f"could not establish {b.text!r} ~ {a.text!r} ({type(exc).__name__})"
            return True, "each equation is a nonzero multiple of its predecessor"
        return None, f"could not solve: {exc}"
    if _same_solutions(sb, sa):
        return True, f"solutions unchanged: {_fmt_solutions(sa)}"
    approx = _pi_sets_match(sb, sa, ex)
    if approx:
        return True, f"solutions unchanged ({approx}): {_fmt_solutions(sa)}"
    if _proper_subset(sa, sb):
        ok, why = _discard_allowed(sb, sa, operation, problem)
        if ok:
            return True, why
    return False, f"solutions before: {_fmt_solutions(sb)}; after: {_fmt_solutions(sa)}"


def _parse(lines: list[str], what: str) -> tuple[Optional[list[Relation]], str]:
    """The lines as relations — less any ``pi = 3.14`` line, which declares
    the approximation in force (_pi_values reads it) and is no equation."""
    try:
        return [_exact_relation(r) for r in parse_state(lines) if not _is_pi_declaration(r)], ""
    except (NotationError, MathError) as exc:
        return None, f"{what} could not be read: {exc}"


def _exact_relation(r: Relation) -> Relation:
    """The relation with its decimals exact (_exact). Done here, at the
    parse, because two parsed floats subtract into a float BEFORE any check
    can rationalise them — "188.4 + 56.52" minus "244.92" was already the
    float 2.84e-14 by the time _zero saw it."""
    if r.data is not None:
        return r
    return Relation(_exact(r.lhs), r.op, None if r.rhs is None else _exact(r.rhs), r.text)


# ── rounding and estimation ──────────────────────────────────────────────

_SF_RE = re.compile(r"^\s*(\d+)\s*(?:s\.?\s*f\.?|sig(?:nificant)?\.?\s*fig(?:ure)?s?\.?)\s*$", re.I)
_DP_RE = re.compile(r"^\s*(\d+)\s*(?:d\.?\s*p\.?|decimal\s*places?)\s*$", re.I)
_NEAREST_RE = re.compile(r"^\s*(?:to\s+)?(?:the\s+)?nearest\s+", re.I)
_UNIT_WORDS = {
    "crore": Fraction(10_000_000), "million": Fraction(1_000_000), "lakh": Fraction(100_000),
    "ten thousand": Fraction(10_000), "thousand": Fraction(1000), "hundred": Fraction(100),
    "ten": Fraction(10), "unit": Fraction(1), "one": Fraction(1), "whole number": Fraction(1),
    "whole": Fraction(1), "integer": Fraction(1), "tenth": Fraction(1, 10),
    "hundredth": Fraction(1, 100), "thousandth": Fraction(1, 1000),
}


def _precision(text: str) -> Optional[tuple[str, object]]:
    """``("unit", Fraction)`` for "1000", "0.01", "nearest hundred", "2 dp";
    ``("sf", n)`` for "2 sf"; None when there is nothing readable."""
    t = str(text or "").strip().lower().rstrip(".")
    if not t:
        return None
    m = _SF_RE.match(t)
    if m:
        return ("sf", max(1, int(m.group(1))))
    m = _DP_RE.match(t)
    if m:
        return ("unit", Fraction(1, 10 ** int(m.group(1))))
    t = _NEAREST_RE.sub("", t)
    words = re.sub(r"[^a-z ]", " ", t).split()
    key = " ".join(words)
    for w in (key, key.rstrip("s"), key[:-2] if key.endswith("es") else key):
        if w in _UNIT_WORDS:
            return ("unit", _UNIT_WORDS[w])
    try:
        unit = Fraction(t.replace(",", ""))
    except (ValueError, ZeroDivisionError):
        return None
    return ("unit", unit) if unit > 0 else None


def _round_to(x: Fraction, unit: Fraction) -> Fraction:
    """School rounding: half away from zero, exact."""
    q = abs(x) / unit
    n = math.floor(q + Fraction(1, 2))
    return (n if x >= 0 else -n) * unit


def _magnitude(x: Fraction) -> int:
    """e with 10^e <= |x| < 10^(e+1), exactly."""
    y, e = abs(x), 0
    if y >= 1:
        while y >= 10:
            y, e = y / 10, e + 1
    else:
        while y < 1:
            y, e = y * 10, e - 1
    return e


def _round_sf(x: Fraction, n: int) -> Fraction:
    if x == 0:
        return x
    return _round_to(x, Fraction(10) ** (_magnitude(x) - n + 1))


def _is_some_rounding(b: Fraction, a: Fraction) -> bool:
    """Is ``a`` ``b`` rounded to SOME power of ten or to 1-4 significant
    figures? Truncation (7583 -> 7500) is not rounding and is refused."""
    for k in range(-6, 10):
        if _round_to(b, Fraction(10) ** k) == a:
            return True
    return any(_round_sf(b, n) == a for n in (1, 2, 3, 4))


def _num(text: str) -> Fraction:
    return Fraction(text.rstrip(".") or "0")


def _fmt_num(q: Fraction) -> str:
    if q.denominator == 1:
        return str(q.numerator)
    return f"{float(q):.10g}"


def _check_round_step(name: str, st: Step) -> Check:
    """Every line keeps its shape, token for token; every number that
    changed is the old one rounded to the step's precision."""
    if len(st.before) != len(st.after) or not st.after:
        return Check(name, None, f"{len(st.before)} line(s) became {len(st.after)}")
    spec = _precision(st.precision)
    if st.precision and spec is None:
        return Check(name, None, f"could not read the precision {st.precision!r}")
    pairs: list[tuple[str, str]] = []
    for b_text, a_text in zip(st.before, st.after):
        try:
            tb, ta = tokenize(b_text), tokenize(a_text)
        except TokenError as exc:
            return Check(name, None, f"could not read the line: {exc}")
        if len(tb) != len(ta) or any(x.kind != y.kind or (x.kind != "NUM" and x.text != y.text)
                                     for x, y in zip(tb, ta)):
            return Check(name, False, f"{a_text!r} is not {b_text!r} with its numbers rounded")
        pairs += [(x.text, y.text) for x, y in zip(tb, ta) if x.kind == "NUM"]
    changed = 0
    for bt, at in pairs:
        b, a = _num(bt), _num(at)
        if a == b:
            continue
        changed += 1
        if spec is not None:
            want = _round_to(b, spec[1]) if spec[0] == "unit" else _round_sf(b, spec[1])
            if a != want:
                return Check(name, False, f"{bt} rounded to {st.precision} is {_fmt_num(want)}, not {at}")
        elif not _is_some_rounding(b, a):
            return Check(name, False, f"{at} is not a rounding of {bt}")
    how = f"to {st.precision}" if st.precision else "each to a power of ten or a few significant figures"
    return Check(name, True, f"{changed} number(s) rounded {how}")


# ── the checks ───────────────────────────────────────────────────────────


def _variables(ex: WorkedExample, givens: Optional[list[Relation]]) -> list[sp.Symbol]:
    if ex.task in SOLVE_TASKS:
        if givens and _is_system(givens):
            # every unknown of a system, whatever the target names — AND the
            # target, which the givens may not mention yet ("a = 3", "b = 4";
            # find c: the hypotenuse enters with the theorem, 2026-10-02)
            free: set = set(symbols_named(ex.variables))
            for r in givens:
                free |= r.free_symbols
            return sorted(free, key=str)
        return symbols_named(ex.variables)
    free: set = set()
    for r in givens or []:
        free |= r.free_symbols
    return sorted(free, key=str) or symbols_named(ex.variables)


def _mode(ex: WorkedExample) -> str:
    return "all" if ex.task == "solve_system" else "any"


def _check_step(i: int, st: Step, ex: WorkedExample, variables: list[sp.Symbol]) -> Check:
    name = f"step {i + 1}"
    if st.kind == "setup":
        return Check(name, None, "setup: not a transformation")
    if st.kind == "round":
        c = _check_round_step(name, st)
        return Check(name, c.ok, f"{st.operation}: {c.detail}" if st.operation else c.detail)
    after, err = _parse(st.after, "the line after")
    if after is None:
        return Check(name, None, err) if st.kind != "check" else Check(name, None, err)
    if st.kind == "check":
        # every line must be a TRUE numeric statement: "3(5) + 5 = 20"
        for r in after:
            if not r.is_equation:
                return Check(name, None, f"a check must be an equation: {r.text!r}")
            if r.free_symbols:
                return Check(name, None, f"a check must have no unknowns left: {r.text!r}")
            if not _timed(_zero, r.lhs - r.rhs):
                why = _timed(_pi_match, r.lhs, r.rhs, ex) or _timed(_pi_match, r.rhs, r.lhs, ex)
                if why:
                    return Check(name, True, f"the check holds ({why})")
                return Check(name, False, f"{r.text!r} is false")
        return Check(name, True, "the check holds")
    before, err = _parse(st.before, "the line before")
    if before is None:
        return Check(name, None, err)
    if _fully_determined(before) and (_free(after) - _free(before)):
        # the givens are known values and the step writes a relation among
        # NEW unknowns — Pythagoras from two sides, an area formula from a
        # length: a relation the problem's words supply, which no algebra
        # can prove from "a = 3, b = 4". A setup, whatever the model called
        # it; verified from here on, never against the values alone.
        return Check(name, None, f"setup: {st.operation or 'a relation from the problem'} — "
                                 "brought in from the problem's words, not a transformation")
    ok, detail = _states_equivalent(before, after, variables, _mode(ex), ex.task,
                                    operation=st.operation, problem=ex.problem, ex=ex)
    return Check(name, ok, f"{st.operation}: {detail}" if st.operation else detail)


def _check_chain(ex: WorkedExample, givens: Optional[list[Relation]], variables) -> list[Check]:
    """The working starts from the problem and each step starts where the
    previous one ended. Equivalence, not equality: a model may tidy spacing."""
    out: list[Check] = []
    steps = [s for s in ex.steps]
    if not steps:
        return [Check("chain", False, "no steps")]
    first = next((s for s in steps if s.before), None)
    # a setup before the first worked line wrote the state the working
    # starts from; the problem's givens are what IT started from
    after_a_setup = first is not None and any(s.kind == "setup" for s in steps[:steps.index(first)])
    if givens and first is not None and first.kind != "setup" and not after_a_setup:
        b, err = _parse(first.before, "the first line")
        if b is None:
            out.append(Check("chain start", None, err))
        else:
            ok, detail = _states_equivalent(givens, b, variables, _mode(ex), ex.task, ex=ex)
            out.append(Check("chain start", ok, "the working starts from the problem" if ok
                             else f"the working does not start from the problem: {detail}"))
    prev: Optional[list[str]] = None
    for i, st in enumerate(steps):
        if prev and st.before and st.kind != "check":
            if [x.strip() for x in prev] != [x.strip() for x in st.before]:
                a, e1 = _parse(prev, "previous line")
                b, e2 = _parse(st.before, "this line")
                if a is None or b is None:
                    out.append(Check(f"chain {i + 1}", None, e1 or e2))
                else:
                    ok, detail = _states_equivalent(a, b, variables, _mode(ex), ex.task, ex=ex)
                    if not ok:
                        out.append(Check(f"chain {i + 1}", ok,
                                         f"step {i + 1} does not start where step {i} ended: {detail}"))
        if st.kind != "check" and st.after:
            prev = st.after
    return out


def _check_answer(ex: WorkedExample, givens: Optional[list[Relation]], variables) -> Check:
    answers = _split_answers(ex.final_answer)
    if not answers:
        return Check("answer", False, "no final answer")
    if givens is None:
        return Check("answer", None, "the problem could not be read")
    if ex.task in DATA_TASKS:
        # The answer is the statistic of the data, exactly — a data task's
        # givens are the list, and nothing in the working can change what
        # its mean is.
        if len(givens) != 1 or not givens[0].is_data:
            return Check("answer", None, f"a {ex.task} task needs a data list as its givens: write 4, 8, 6, 10, 12")
        ans, err = _parse(answers, "the final answer")
        if ans is None:
            return Check("answer", None, err)
        ok, detail = _data_step(givens, ans, ex.task)
        if ok is None:
            return Check("answer", None, detail)
        return Check("answer", ok, "answer verified: " + detail if ok else detail)
    if ex.task in ROUND_TASKS:
        # The answer is what the verified rounding steps reach, not the
        # problem's exact value: the steps carry the proof, the chain ties
        # them to the problem, and here the answer must be the last line.
        if not any(s.kind == "round" for s in ex.steps):
            return Check("answer", False, "a rounding task needs a step of kind 'round'")
        ans, err = _parse(answers, "the final answer")
        if ans is None:
            return Check("answer", None, err)
        if any(r.is_expression and r.free_symbols for r in ans):
            return Check("answer", False, "the answer must be a number")
        last = next((s for s in reversed(ex.steps) if s.kind in ("transform", "round") and s.after), None)
        if last is None:
            return Check("answer", False, "no working reaches the answer")
        a, err = _parse(last.after, "the last line")
        if a is None:
            return Check("answer", None, err)
        ok, detail = _states_equivalent(a, ans, variables, _mode(ex), ex=ex)
        if ok:
            return Check("answer", True, "the answer is what the rounding reaches")
        return Check("answer", ok, f"the answer is not the last line's value: {detail}")
    if ex.task in SOLVE_TASKS:
        ans, err = _parse(answers, "the final answer")
        if ans is None:
            return Check("answer", None, err)
        if any(r.is_expression for r in ans):
            return Check("answer", False, "a solution must name the unknown: write x = 5")
        problem = givens
        if not (set(symbols_named(ex.variables)) & _free(givens)):
            # the givens never mention the unknown ("a = 3", "b = 4"; find
            # c): the problem's equation is the one the setup wrote down —
            # its steps are verified from there, so the answer is judged
            # from there too
            start = _setup_state(ex)
            if start is None:
                return Check("answer", None, "the problem's equation for the unknown was never written down")
            problem = start
        try:
            expected = _timed(_solution_set, problem, variables, "all")
            actual = _timed(_solution_set, ans, variables, _state_mode(ans, _mode(ex)))
        except MathTimeoutError:
            return Check("answer", None, "SymPy timed out")
        except Exception as exc:  # noqa: BLE001
            return Check("answer", None, f"could not solve the problem: {exc}")
        targets = [v for v in variables if v in set(symbols_named(ex.variables))]
        if targets and len(targets) < len(variables) and isinstance(expected, list) and isinstance(actual, list) \
                and all(set(d) <= set(targets) for d in actual):
            # the answer names the target alone; the given values travel in
            # the problem's solutions and are not what the answer is judged on
            expected, actual = _project(expected, targets), _project(actual, targets)
        if _same_solutions(expected, actual):
            return Check("answer", True, f"answer verified: {_fmt_solutions(actual)}")
        approx = _pi_sets_match(expected, actual, ex)
        if approx:
            return Check("answer", True, f"answer verified ({approx}): {_fmt_solutions(actual)}")
        if _proper_subset(actual, expected):
            reasons = " ".join(s.operation for s in ex.steps if s.kind == "transform")
            ok, why = _discard_allowed(expected, actual, reasons, ex.problem)
            if ok:
                return Check("answer", True, f"answer verified: {_fmt_solutions(actual)} ({why})")
        return Check("answer", False, f"the problem's solution is {_fmt_solutions(expected)}, "
                                      f"the answer says {_fmt_solutions(actual)}")
    # expression tasks: the problem's expression, with the values the problem
    # gives its letters substituted, against the answer read the same way
    # ("8" for "x + 5 when x = 3"; "E = 19" names the value; two answer lines
    # must agree)
    ans, err = _parse(answers, "the final answer")
    if ans is None:
        return Check("answer", None, err)
    values, letters = _values(ex), _problem_symbols(ex)
    pe = _expression_lines(givens, values, letters)
    ae = _expression_lines(ans, values, letters)
    if not pe or not ae:
        return Check("answer", None, "the problem and the answer must both be expressions")
    if len(pe) != 1:
        return Check("answer", None, f"one expression expected in the givens, got {len(pe)}")
    problem_rel, problem_e = pe[0]
    approx = ""
    for rel, a_e in ae:
        if not _timed(_zero, problem_e - a_e):
            why = _timed(_pi_match, problem_e, a_e, ex)
            if why:
                approx = f" ({why})"
                continue
            gap = _word_gap(problem_e, a_e)
            if gap:
                return Check("answer", None, gap)
            with_values = (" with " + ", ".join(f"{k} = {v}" for k, v in sorted(values.items(), key=lambda kv: str(kv[0])))
                           if values else "")
            return Check("answer", False, f"{rel.text!r} is not equivalent to {problem_rel.text!r}{with_values}")
    if ex.task in ("expand", "factorise", "simplify") and len(ae) != 1:
        return Check("answer", False, f"one expression expected, got {len(ae)}")
    rel, a_e = ae[-1]
    a_form = rel.lhs if rel.is_expression else rel.rhs
    if ex.task == "expand" and sp.expand(a_form) != a_form:
        return Check("answer", False, f"{rel.text!r} is not fully expanded")
    if ex.task == "factorise" and a_form.is_Add and sp.expand(a_form) == a_form and len(a_form.args) > 1:
        return Check("answer", False, f"{rel.text!r} is not factorised")
    if ex.task == "evaluate" and a_form.free_symbols:
        return Check("answer", False, f"{rel.text!r} is not a value")
    return Check("answer", True, "answer verified" + (f": {a_e}" if ex.task == "evaluate" else "") + approx)


def _setup_state(ex: WorkedExample) -> Optional[list[Relation]]:
    """The state the first setup step wrote down — a declared "setup", or a
    transform that brought a relation in from known values (_check_step's
    implicit setup). None when there is no such step or it does not parse."""
    for st in ex.steps:
        if not st.after:
            continue
        after, _e = _parse(st.after, "the setup's result")
        if after is None:
            return None
        if st.kind == "setup":
            return after
        if st.kind == "transform" and st.before:
            before, _e = _parse(st.before, "the setup's start")
            if before is not None and _fully_determined(before) and (_free(after) - _free(before)):
                return after
    return None


def _check_last_step(ex: WorkedExample, variables) -> Optional[Check]:
    last = next((s for s in reversed(ex.steps) if s.kind in ("transform", "round") and s.after), None)
    answers = _split_answers(ex.final_answer)
    if last is None or not answers:
        return None
    a, e1 = _parse(last.after, "the last line")
    b, e2 = _parse(answers, "the final answer")
    if a is None or b is None:
        return Check("chain end", None, e1 or e2)
    if ex.task in SOLVE_TASKS and any(r.is_expression for r in b):
        return None  # the answer check already reports this
    if ex.task in DATA_TASKS:
        # "8", "mean = 8" and "40/5" all state the same value; the modes may
        # be a short data list. Values, not states.
        va, vb = _values_of(a), _values_of(b)
        if va is None or vb is None:
            return Check("chain end", None, "could not read a value from the last line or the answer")
        if _same_values(va, vb):
            return Check("chain end", True, "the last line gives the answer")
        return Check("chain end", False, f"the last line does not give the stated answer: "
                                          f"{_fmt_stat(va)} vs {_fmt_stat(vb)}")
    ok, detail = _states_equivalent(a, b, variables, _mode(ex), ex.task, ex=ex)
    if ok:
        return Check("chain end", True, "the last line gives the answer")
    return Check("chain end", ok, f"the last line does not give the stated answer: {detail}")


def _check_mistake(ex: WorkedExample, variables) -> Optional[Check]:
    m = ex.common_mistake
    if m is None or not (m.from_state and m.wrong_state):
        return None
    a, e1 = _parse(m.from_state, "the mistake's starting line")
    b, e2 = _parse(m.wrong_state, "the mistake's result")
    if a is None or b is None:
        return Check("mistake", None, e1 or e2)
    ok, detail = _states_equivalent(a, b, variables, _mode(ex), ex.task, ex=ex)
    if ok is True:
        return Check("mistake", False, "the 'mistake' is actually a valid step — it must not be taught as wrong")
    if ok is False:
        return Check("mistake", True, f"confirmed wrong: {detail}")
    return Check("mistake", None, detail)


# ── straight lines ────────────────────────────────────────────────────────
#
# "Gradient and Intercept of a Straight Line" (kit 6c7be369, 2026-10-09)
# lost nine of twelve examples to four shapes the solve path cannot read: a
# point written as a line ("(2, 3)"), an answer that is the EQUATION of a
# line (solved for x: "the answer says x = y/2 + 1/2"), m and c read off
# "y = 3x + 5" (no transformation of anything), and "6 = m(2) - 4". A line
# problem is not an equation to transform; it is a set of FACTS — points on
# the line, its equation, its gradient, a line it is parallel to — and
# every step of the working is a CONSEQUENCE of those facts. So the line
# task is verified by entailment: the facts (and the line before) must
# imply every line after, SymPy solving the facts for every symbol in
# play; the answer is the line the facts fix (y = m x + c), or its m, its
# c, its x-intercept, as the target asks.

_X, _Y, _M, _C = sp.Symbol("x"), sp.Symbol("y"), sp.Symbol("m"), sp.Symbol("c")
_REF_LINE_RE = re.compile(r"^\s*(parallel|perpendicular)\s+(?:to\s+)?(?:the\s+line\s+)?(.+?)\s*$", re.I)
_INTERCEPT_GIVEN_RE = re.compile(r"^\s*([xy])[\s_-]*intercept\s*(?:=|:|is)\s*(.+?)\s*$", re.I)
_PARAM_GIVEN_RE = re.compile(r"^\s*(m|c|gradient|slope|intercept)\s*=\s*(.+?)\s*$", re.I)


@dataclass
class LineFacts:
    """The problem's facts as SymPy expressions equal to zero, the given
    points, what is asked, and the m and c the facts fix (None when they do
    not). ``problem`` names a given that could not be read."""
    facts: list = field(default_factory=list)
    points: list = field(default_factory=list)
    target: str = "equation"
    m: Optional[sp.Expr] = None
    c: Optional[sp.Expr] = None
    problem: str = ""


def _line_target(ex: WorkedExample) -> str:
    """What a line task asks for: equation | m | c | x_intercept | mc — read
    from ``target``, then from the problem's words."""
    for text in (ex.target or "", ex.problem or ""):
        t = " ".join(text.lower().replace("_", "-").split())
        if not t:
            continue
        has_m = bool(re.search(r"\b(m|gradient|slope)\b", t))
        has_c = bool(re.search(r"\b(c|y-?intercept|intercept)\b", t)) and not re.search(r"\bx-?intercept\b", t)
        if "equation" in t:
            return "equation"      # "the equation of the line with gradient 3": the equation is what is asked
        if re.search(r"\bx-?intercept\b", t):
            return "x_intercept"
        if has_m and has_c:
            return "mc"
        if has_m:
            return "m"
        if has_c:
            return "c"
        if "equation" in t or t in ("line", "y", "x, y", "x and y"):
            return "equation"
    return "equation"


def _abc(rel: Relation) -> Optional[tuple]:
    """(a, b, d) with a·x + b·y + d = 0 for an equation linear in x and y,
    else None."""
    if not rel.is_equation or rel.rhs is None:
        return None
    expr = sp.expand(_exact(rel.lhs - rel.rhs))
    if not expr.free_symbols or not expr.free_symbols <= {_X, _Y}:
        return None
    try:
        poly = sp.Poly(expr, _X, _Y)
    except sp.PolynomialError:
        return None
    if poly.total_degree() != 1:
        return None
    return poly.coeff_monomial(_X), poly.coeff_monomial(_Y), poly.coeff_monomial(1)


def _line_value(text: str) -> Optional[sp.Expr]:
    try:
        return _exact(parse_relation(f"q = {text}").rhs)
    except NotationError:
        return None


def line_facts(ex: WorkedExample) -> LineFacts:
    """The facts of a line task, from its givens. Public: the chart reads the
    line and the points from here too."""
    F = LineFacts(target=_line_target(ex))
    F.facts.append(_Y - (_M * _X + _C))
    k = 0
    for text in ex.givens:
        text = str(text or "").strip()
        if not text:
            continue
        pt = parse_point(text)
        if pt is not None:
            k += 1
            xs, ys = pt.names or (f"x{k}", f"y{k}")
            F.facts += [sp.Symbol(xs) - pt.x, sp.Symbol(ys) - pt.y, pt.y - (_M * pt.x + _C)]
            F.points.append((pt.x, pt.y))
            continue
        ref = _REF_LINE_RE.match(text)
        if ref:
            try:
                abc = _abc(parse_relation(ref.group(2)))
            except NotationError:
                abc = None
            if abc is None or abc[1] == 0:
                F.problem = f"{text!r}: the reference line must be an equation in x and y, not vertical"
                return F
            m1, c1 = -abc[0] / abc[1], -abc[2] / abc[1]
            F.facts += [sp.Symbol("m1") - m1, sp.Symbol("m_1") - m1, sp.Symbol("c1") - c1, sp.Symbol("c_1") - c1]
            F.facts.append(_M - m1 if ref.group(1).lower() == "parallel" else _M * m1 + 1)
            continue
        ig = _INTERCEPT_GIVEN_RE.match(text)
        if ig:
            v = _line_value(ig.group(2))
            if v is None:
                F.problem = f"{text!r}: could not read the intercept"
                return F
            x0, y0 = (v, sp.Integer(0)) if ig.group(1).lower() == "x" else (sp.Integer(0), v)
            F.facts.append(y0 - (_M * x0 + _C))
            F.points.append((x0, y0))
            continue
        pg = _PARAM_GIVEN_RE.match(text)
        if pg:
            v = _line_value(pg.group(2))
            if v is None:
                F.problem = f"{text!r}: could not read the value"
                return F
            F.facts.append((_M if pg.group(1).lower() in ("m", "gradient", "slope") else _C) - v)
            continue
        try:
            rel = parse_relation(text)
        except NotationError as exc:
            F.problem = f"{text!r} is not a line fact: {exc}"
            return F
        if _is_pi_declaration(rel):
            continue
        abc = _abc(rel)
        if abc is not None:
            if abc[1] == 0:
                F.problem = f"{text!r} is a vertical line; its gradient is undefined and the line task does not cover it"
                return F
            F.facts += [_exact(rel.lhs - rel.rhs), _M + abc[0] / abc[1], _C + abc[2] / abc[1]]
            continue
        if rel.is_equation and rel.rhs is not None:
            F.facts.append(_exact(rel.lhs - rel.rhs))
            continue
        F.problem = f"{text!r} is not a line fact (a point, an equation, m = …, c = …, parallel/perpendicular to …)"
        return F
    # the intercept asked for is a point of the line on an axis: y = 0 on the
    # x-axis, x = 0 on the y-axis — the fact a step like "4x + 3(0) = 12" uses
    if F.target == "x_intercept":
        F.facts.append(_Y)
    elif F.target == "c":
        F.facts.append(_X)
    fixed = [f for f in F.facts if not (f.free_symbols & {_X, _Y})]
    syms = sorted(set().union(*(f.free_symbols for f in fixed)) if fixed else set(), key=str)
    if fixed and syms:
        try:
            sols = sp.solve(fixed, syms, dict=True)
        except Exception:  # noqa: BLE001
            sols = []
        if len(sols) == 1:
            m, c = sols[0].get(_M), sols[0].get(_C)
            if m is not None and c is not None and not m.free_symbols and not c.free_symbols:
                F.m, F.c = sp.nsimplify(m, rational=True), sp.nsimplify(c, rational=True)
    return F


def _entails(premises: list, conclusions: list) -> Optional[bool]:
    """Every solution of the premises satisfies every conclusion (all
    expressions equal to zero); None when SymPy cannot decide or the
    premises contradict each other."""
    premises = [sp.expand(_exact(p)) for p in premises]
    premises = [p for p in premises if p != 0]
    conclusions = [sp.expand(_exact(c)) for c in conclusions]
    syms = sorted(set().union(*(e.free_symbols for e in premises + conclusions)) if premises + conclusions else set(),
                  key=str)
    if not syms:
        return all(_zero(c) for c in conclusions)
    try:
        sols = sp.solve(premises, syms, dict=True) if premises else [{}]
    except Exception:  # noqa: BLE001
        return None
    if premises and not sols:
        return None
    for sol in sols:
        for c in conclusions:
            if not _zero(c.subs(sol)):
                return False
    return True


def _line_state(lines: list[str], F: LineFacts, what: str) -> tuple[Optional[list], str]:
    """A state of a line task as expressions equal to zero: an equation is
    lhs - rhs; a point restating a given is nothing to prove, any other
    point must lie on the line. None (with the reason) for a line that is
    not an equation."""
    out: list = []
    for text in lines:
        pt = parse_point(text)
        if pt is not None:
            if not any(_equal_values(pt.x, x0) and _equal_values(pt.y, y0) for x0, y0 in F.points):
                out.append(pt.y - (_M * pt.x + _C))
            continue
        try:
            rel = parse_relation(text)
        except NotationError as exc:
            return None, f"{what} could not be read: {exc}"
        if _is_pi_declaration(rel):
            continue
        if not rel.is_equation or rel.rhs is None:
            return None, f"{what}: {rel.text!r} is not an equation"
        out.append(_exact(rel.lhs - rel.rhs))
    return out, ""


def _check_line_step(i: int, st: Step, ex: WorkedExample, F: LineFacts) -> Check:
    name = f"step {i + 1}"
    if st.kind in ("check", "round"):
        return _check_step(i, st, ex, [])
    after, err = _line_state(st.after, F, "the line after")
    if after is None:
        return Check(name, None, err)
    if st.kind == "setup":
        ok = _timed(_entails, F.facts, after)
        if ok is False:
            return Check(name, False, f"{st.operation or 'setup'}: contradicts the problem's facts")
        return Check(name, ok, f"setup: {st.operation or 'a fact of the problem'}"
                     + (" — follows from the givens" if ok else " — could not be checked against the givens"))
    before, err = _line_state(st.before, F, "the line before")
    if before is None:
        return Check(name, None, err)
    ok = _timed(_entails, F.facts + before, after)
    label = f"{st.operation}: " if st.operation else ""
    if ok:
        return Check(name, True, label + "follows from the givens and the line before")
    if ok is False:
        return Check(name, False, label + f"{'; '.join(st.after)!r} does not follow from the givens and {'; '.join(st.before)!r}")
    return Check(name, None, label + "could not be decided from the givens")


def _line_same(a: list, b: list, F: LineFacts) -> Optional[bool]:
    x, y = _timed(_entails, F.facts + a, b), _timed(_entails, F.facts + b, a)
    if x is None or y is None:
        return None
    return x and y


def _check_line_chain(ex: WorkedExample, F: LineFacts) -> list[Check]:
    out: list[Check] = []
    prev: Optional[list[str]] = None
    for i, st in enumerate(ex.steps):
        if prev and st.before and st.kind != "check" and [x.strip() for x in prev] != [x.strip() for x in st.before]:
            a, e1 = _line_state(prev, F, "previous line")
            b, e2 = _line_state(st.before, F, "this line")
            if a is None or b is None:
                out.append(Check(f"chain {i + 1}", None, e1 or e2))
            elif not _line_same(a, b, F):
                out.append(Check(f"chain {i + 1}", False, f"step {i + 1} does not start where step {i} ended"))
        if st.kind != "check" and st.after:
            prev = st.after
    return out


def _fmt_line(m, c) -> str:
    ms = "" if m == 1 else "-" if m == -1 else f"{_fmt_q(m)}"
    cs = "" if c == 0 else f" - {_fmt_q(-c)}" if c < 0 else f" + {_fmt_q(c)}"
    return f"y = {ms}x{cs}" if m != 0 else f"y = {_fmt_q(c)}"


def _fmt_q(v) -> str:
    v = sp.nsimplify(v, rational=True)
    if getattr(v, "is_Rational", False):
        return _fmt_num(Fraction(int(v.p), int(v.q)))
    return str(v)


def _answer_value(text: str, names: set[str]) -> Optional[sp.Expr]:
    """``m = 3``, ``gradient = 3`` or a bare ``3`` as the value; None when
    the line names something else."""
    try:
        rel = parse_relation(text)
    except NotationError:
        return None
    if rel.is_expression:
        return _exact(rel.lhs) if not rel.lhs.free_symbols else None
    if rel.is_equation and rel.rhs is not None:
        if rel.lhs.is_Symbol and str(rel.lhs).lower() in names and not rel.rhs.free_symbols:
            return _exact(rel.rhs)
        if rel.rhs.is_Symbol and str(rel.rhs).lower() in names and not rel.lhs.free_symbols:
            return _exact(rel.lhs)
    return None


def _check_line_answer(ex: WorkedExample, F: LineFacts) -> Check:
    answers = _split_answers(ex.final_answer)
    if not answers:
        return Check("answer", False, "no final answer")
    if F.problem:
        return Check("answer", None, F.problem)
    if F.m is None or F.c is None:
        return Check("answer", None, "the givens do not fix the line (two points, a point and a gradient, an equation…)")
    want_line = _fmt_line(F.m, F.c)
    if F.target == "equation":
        for a in answers:
            try:
                abc = _abc(parse_relation(a))
            except NotationError:
                abc = None
            if abc is None or abc[1] == 0:
                return Check("answer", False, f"{a!r} is not the equation of a line in x and y")
            m2, c2 = -abc[0] / abc[1], -abc[2] / abc[1]
            if not (_equal_values(m2, F.m) and _equal_values(c2, F.c)):
                return Check("answer", False, f"the givens fix the line {want_line}; the answer's line is {_fmt_line(m2, c2)}")
        return Check("answer", True, f"answer verified: {want_line}")
    if F.target == "mc":
        got_m = got_c = None
        for a in answers:
            got_m = got_m if got_m is not None else _answer_value(a, {"m", "gradient", "slope"})
            got_c = got_c if got_c is not None else _answer_value(a, {"c", "intercept", "y"})
        if got_m is None or got_c is None:
            return Check("answer", False, "the answer must give both: m = … and c = …")
        if _equal_values(got_m, F.m) and _equal_values(got_c, F.c):
            return Check("answer", True, f"answer verified: m = {_fmt_q(F.m)}, c = {_fmt_q(F.c)}")
        return Check("answer", False, f"the line is {want_line}: m = {_fmt_q(F.m)}, c = {_fmt_q(F.c)}; "
                                      f"the answer says m = {_fmt_q(got_m)}, c = {_fmt_q(got_c)}")
    if F.target == "m":
        want, names, what = F.m, {"m", "gradient", "slope"}, "the gradient"
    elif F.target == "c":
        want, names, what = F.c, {"c", "intercept", "y"}, "the y-intercept"
    else:
        if F.m == 0:
            return Check("answer", None, "a horizontal line has no x-intercept to find")
        want, names, what = -F.c / F.m, {"x"}, "the x-intercept"
    for a in answers:
        pt = parse_point(a)
        if pt is not None:
            got = pt.y if F.target == "c" and pt.x == 0 else pt.x if F.target == "x_intercept" and pt.y == 0 else None
        else:
            got = _answer_value(a, names)
        if got is None:
            return Check("answer", False, f"{a!r} does not state {what} (write {sorted(names)[0]} = …)")
        if not _equal_values(got, want):
            return Check("answer", False, f"the line is {want_line}, so {what} is {_fmt_q(want)}; the answer says {_fmt_q(got)}")
    return Check("answer", True, f"answer verified: {what} is {_fmt_q(want)} ({want_line})")


def _check_line_last_step(ex: WorkedExample, F: LineFacts) -> Optional[Check]:
    last = next((s for s in reversed(ex.steps) if s.kind in ("transform", "round", "deduce") and s.after), None)
    answers = _split_answers(ex.final_answer)
    if last is None or not answers or F.problem:
        return None
    a, e1 = _line_state(last.after, F, "the last line")
    b, e2 = _line_state(answers, F, "the final answer")
    if a is None or b is None:
        return Check("chain end", None, e1 or e2)
    # one way: the last line (with the facts) gives the answer — an answer
    # written as the point (3, 0) is given by the line "x = 3", not the
    # other way round
    same = _timed(_entails, F.facts + a, b)
    if same:
        return Check("chain end", True, "the last line gives the answer")
    return Check("chain end", same, "the last line does not give the stated answer")


def _check_line_mistake(ex: WorkedExample, F: LineFacts) -> Optional[Check]:
    m = ex.common_mistake
    if m is None or not (m.from_state and m.wrong_state):
        return None
    a, e1 = _line_state(m.from_state, F, "the mistake's starting line")
    b, e2 = _line_state(m.wrong_state, F, "the mistake's result")
    if a is None or b is None:
        return Check("mistake", None, e1 or e2)
    ok = _timed(_entails, F.facts + a, b)
    if ok is True:
        return Check("mistake", False, "the 'mistake' is actually a valid step — it must not be taught as wrong")
    if ok is False:
        return Check("mistake", True, "confirmed wrong: it does not follow from the givens")
    return Check("mistake", None, "could not be decided from the givens")


def _verify_line_example(ex: WorkedExample) -> ExampleReport:
    rep = ExampleReport(label=ex.label or "example")
    F = line_facts(ex)
    if F.problem:
        rep.checks.append(Check("problem", None, F.problem))
    for i, st in enumerate(ex.steps):
        rep.checks.append(_check_line_step(i, st, ex, F))
    rep.checks.extend(_check_line_chain(ex, F))
    rep.checks.append(_check_line_answer(ex, F))
    last = _check_line_last_step(ex, F)
    if last is not None:
        rep.checks.append(last)
    mistake = _check_line_mistake(ex, F)
    if mistake is not None:
        rep.checks.append(mistake)
    transforms_unverified = [c for c in rep.checks if c.ok is None and c.name.startswith("step")
                             and ex.steps[int(c.name.split()[1]) - 1].kind in ("transform", "deduce", "round")]
    answer_unverified = [c for c in rep.checks if c.ok is None and c.name == "answer"]
    if rep.failures or transforms_unverified or answer_unverified or not ex.steps:
        rep.status = "failed"
    return rep


def _verify_figure_example(ex: WorkedExample) -> ExampleReport:
    """A figure example: the geometry chain (constructions build, theorems
    deduce, SymPy proves) re-run on the example's question. Deterministic,
    so it agrees with the acceptance that put the example in the lesson;
    the re-run is what makes the report honest about THIS record."""
    from maths.geometry import verify_question

    rep = ExampleReport(label=ex.label or "example")
    q = verify_question(ex.figure or {})
    rep.checks.extend(Check(c.name, c.ok, str(c.detail or "")) for c in q.checks)
    if not q.ok:
        r = q.refusal or {}
        rep.checks.append(Check("figure", False, f"{r.get('code')}: {r.get('message', '')}"))
        rep.status = "failed"
    return rep


def verify_example(ex: WorkedExample) -> ExampleReport:
    if ex.figure:
        return _verify_figure_example(ex)
    if ex.task in LINE_TASKS:
        return _verify_line_example(ex)
    rep = ExampleReport(label=ex.label or "example")
    givens, err = _parse(ex.givens or ([ex.problem] if ex.problem else []), "the problem")
    if givens is None:
        rep.checks.append(Check("problem", None, err))
    variables = _variables(ex, givens)
    for i, st in enumerate(ex.steps):
        rep.checks.append(_check_step(i, st, ex, variables))
    rep.checks.extend(_check_chain(ex, givens, variables))
    rep.checks.append(_check_answer(ex, givens, variables))
    last = _check_last_step(ex, variables)
    if last is not None:
        rep.checks.append(last)
    mistake = _check_mistake(ex, variables)
    if mistake is not None:
        rep.checks.append(mistake)
    transforms_unverified = [c for c in rep.checks if c.ok is None and c.name.startswith("step")
                             and ex.steps[int(c.name.split()[1]) - 1].kind in ("transform", "round")
                             and not c.detail.startswith("setup:")]
    answer_unverified = [c for c in rep.checks if c.ok is None and c.name == "answer"]
    if rep.failures or transforms_unverified or answer_unverified or not ex.steps:
        rep.status = "failed"
    return rep


def try_it_example(t: TryIt) -> WorkedExample | None:
    """The try-it as a worked example — what the board solves after the
    pause, and what the verifier checks. None without a problem."""
    if not t.problem or not t.answer:
        return None
    if t.figure:
        # the learner paused on a diagram: solved and verified exactly as a
        # figure example (the geometry chain), never as notation
        return WorkedExample(label="Try it", difficulty=2, task="solve", problem=t.problem, givens=[],
                             target="x", intro_speech=t.solution_speech, steps=list(t.steps),
                             final_answer=list(t.answer), answer_speech=t.answer_speech, figure=t.figure)
    if (t.task or "").strip().lower() in LINE_TASKS:
        # a straight-line try-it carries its facts in 'givens' (points are
        # not notation the solve path reads) and what is asked in 'target'
        givens = list(t.givens) or (list(t.steps[0].before) if t.steps and t.steps[0].before else [])
        return WorkedExample(label="the try-it question", difficulty=2, task="line", problem=t.problem,
                             givens=givens, target=t.target or "", intro_speech=t.solution_speech,
                             steps=list(t.steps), final_answer=list(t.answer), answer_speech=t.answer_speech)
    # the problem's NOTATION, with any lead-in words in front of it set
    # aside ("the quadratic expression x^2 - x - 12" starts the working at
    # x^2 - x - 12; the words are the teacher's, not the algebra's)
    clean = notation_of(t.problem)
    rels, _err = _parse([clean], "the try-it problem") if clean else (None, "")
    givens = [clean] if clean else [t.problem]
    if rels is None and t.steps:
        # a word problem: the working starts at its equation — the setup
        # step's result, or the state the first step transforms
        first = t.steps[0]
        start = list(first.after) if first.kind == "setup" and first.after else list(first.before)
        if start:
            givens = start
            rels, _err = _parse(givens, "the try-it equation")
    expression = bool(rels) and rels[0].is_expression
    target = ", ".join(sorted(str(s) for s in rels[0].free_symbols)) if rels else "x"
    # the unknown is what the ANSWER names when that is some of the
    # equation's symbols: a triangle whose setup wrote c^2 = a^2 + b^2 with
    # a = 6 and b = 8 is solved for c, and its answer "c = 10" was judged
    # against solutions carrying a and b too (a0fcb332, 2026-10-02: the
    # try-it dropped for "the answer says c = 10")
    if rels:
        ans, _e = _parse(_split_answers(t.answer), "the try-it answer")
        named = {str(sym) for r in (ans or []) if r.is_equation and r.lhs.is_Symbol for sym in [r.lhs]}
        known = {str(sym) for r in rels for sym in r.free_symbols}
        if named and named < known:
            target = ", ".join(sorted(named))
    rounding = any(s.kind == "round" for s in t.steps)
    data_task = None
    if rels and rels[0].is_data:
        # the statistic the words ask for; mean when they name none
        words = (t.problem or "").lower()
        data_task = next((k for k in ("median", "mode", "range", "mean") if k in words), "mean")
    return WorkedExample(label="the try-it question", problem=t.problem, givens=givens, final_answer=t.answer,
                         task=data_task or ("estimate" if rounding else "simplify" if expression else "solve"),
                         target=target or "x",
                         intro_speech=t.solution_speech, steps=list(t.steps) or [Step(kind="setup")],
                         answer_speech=t.answer_speech)


def verify_try_it(t: TryIt) -> Check:
    """The try-it's answer, and — when the model gave them — its steps,
    checked exactly as an example's are."""
    ex = try_it_example(t)
    if ex is None:
        return Check("try it", None, "no try-it question")
    if t.figure:
        rep = verify_example(ex)
        ok = rep.status == "verified"
        return Check("try it", ok, "figure verified by the geometry chain" if ok else "; ".join(rep.reasons)[:400])
    if ex.task in LINE_TASKS:
        if t.steps:
            rep = verify_example(ex)
            ok = rep.status == "verified"
            return Check("try it", ok, f"{len(t.steps)} step(s) verified" if ok else "; ".join(rep.reasons)[:400])
        c = _check_line_answer(ex, line_facts(ex))
        return Check("try it", c.ok, c.detail)
    rels, err = _parse(ex.givens, "the try-it problem")
    if rels is None:
        return Check("try it", None, err)
    if t.steps:
        rep = verify_example(ex)
        if rep.status != "verified":
            return Check("try it", False, "; ".join(rep.reasons)[:400])
        return Check("try it", True, f"{len(t.steps)} step(s) verified")
    variables = _variables(ex, rels)
    c = _check_answer(ex, rels, variables)
    return Check("try it", c.ok, c.detail)


def verify_lesson(lesson: Lesson) -> dict:
    """Every example, and the try-it question. ``status`` is verified only
    when every example is."""
    reports = [verify_example(ex) for ex in lesson.examples]
    try_it = verify_try_it(lesson.try_it)
    status = "verified" if reports and all(r.status == "verified" for r in reports) \
        and try_it.ok is not False else "failed"
    return {"status": status, "examples": [r.to_dict() for r in reports],
            "try_it": try_it.to_dict()}


__all__ = ["Check", "ExampleReport", "verify_example", "verify_try_it", "verify_lesson", "line_facts", "LineFacts",
           "SOLVE_TASKS", "EXPRESSION_TASKS", "ROUND_TASKS", "DATA_TASKS", "LINE_TASKS"]
