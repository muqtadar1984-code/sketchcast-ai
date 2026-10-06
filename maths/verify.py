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
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Optional

import sympy as sp

from maths.notation import NotationError, Relation, notation_of, parse_state, symbols_named
from maths.schema import DATA_TASKS, Lesson, Step, TryIt, WorkedExample
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


def _zero(expr) -> bool:
    """Is this expression identically zero? expand first (cheap and exact
    for polynomials), simplify only when needed (radicals, fractions)."""
    e = sp.expand(expr)
    if e == 0:
        return True
    try:
        return sp.simplify(e) == 0
    except Exception:  # noqa: BLE001 — a simplify that blows up is "no"
        return False


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
                s = sp.solveset(sp.Eq(r.lhs, r.rhs), x, domain=sp.S.Reals)
                sets.append(s)
        if not sets:
            return sp.S.EmptySet
        out = sets[0]
        for s in sets[1:]:
            out = sp.Intersection(out, s) if mode == "all" else sp.Union(out, s)
        return out
    eqs = [r for r in rels if r.is_equation]
    if len(eqs) != len(rels):
        raise ValueError("a multivariate inequality is not verifiable here")
    if mode == "all":
        sols = sp.solve([sp.Eq(r.lhs, r.rhs) for r in eqs], variables, dict=True)
        return [dict(s) for s in sols]
    out = []
    for r in eqs:
        out.extend(dict(s) for s in sp.solve(sp.Eq(r.lhs, r.rhs), variables, dict=True))
    return out


def _same_solutions(a, b) -> bool:
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
    for (rb, b), (ra, a) in zip(eb, ea):
        if not _timed(_zero, b - a):
            with_values = (" with " + ", ".join(f"{k} = {v}" for k, v in sorted(values.items(), key=lambda kv: str(kv[0])))
                           if values else "")
            return False, f"{rb.text!r} is not equivalent to {ra.text!r}{with_values}"
    return True, "equivalent expressions" + (" under the given values" if values else "")


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
        for i, (b, a) in enumerate(zip(before, after)):
            if not _timed(_zero, b.lhs - a.lhs):
                return False, f"{b.text!r} is not equivalent to {a.text!r}"
        return True, "equivalent expressions"
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
    if _proper_subset(sa, sb):
        ok, why = _discard_allowed(sb, sa, operation, problem)
        if ok:
            return True, why
    return False, f"solutions before: {_fmt_solutions(sb)}; after: {_fmt_solutions(sa)}"


def _parse(lines: list[str], what: str) -> tuple[Optional[list[Relation]], str]:
    try:
        return parse_state(lines), ""
    except (NotationError, MathError) as exc:
        return None, f"{what} could not be read: {exc}"


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
    for rel, a_e in ae:
        if not _timed(_zero, problem_e - a_e):
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
    return Check("answer", True, "answer verified" + (f": {a_e}" if ex.task == "evaluate" else ""))


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


def verify_example(ex: WorkedExample) -> ExampleReport:
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


__all__ = ["Check", "ExampleReport", "verify_example", "verify_try_it", "verify_lesson",
           "SOLVE_TASKS", "EXPRESSION_TASKS", "ROUND_TASKS", "DATA_TASKS"]
