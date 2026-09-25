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

from maths.notation import NotationError, Relation, parse_state, symbols_named
from maths.schema import Lesson, Step, TryIt, WorkedExample
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


def _states_equivalent(before: list[Relation], after: list[Relation], variables: list[sp.Symbol],
                       mode: str) -> tuple[Optional[bool], str]:
    """(verdict, detail) for "does `after` mean what `before` meant".
    ``mode`` is the example's default ("all" for a declared system, "any"
    otherwise); a state that IS a system is read as one regardless."""
    kb, ka = _kinds(before), _kinds(after)
    if kb != ka or kb in ("mixed", "empty"):
        return None, f"the lines change kind ({kb} -> {ka})"
    if kb == "expressions":
        if len(before) != len(after):
            return None, f"{len(before)} expression(s) became {len(after)}"
        for i, (b, a) in enumerate(zip(before, after)):
            if not _timed(_zero, b.lhs - a.lhs):
                return False, f"{b.text!r} is not equivalent to {a.text!r}"
        return True, "equivalent expressions"
    try:
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
            # every unknown of a system, whatever the target names
            free: set = set()
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
    ok, detail = _states_equivalent(before, after, variables, _mode(ex))
    return Check(name, ok, f"{st.operation}: {detail}" if st.operation else detail)


def _check_chain(ex: WorkedExample, givens: Optional[list[Relation]], variables) -> list[Check]:
    """The working starts from the problem and each step starts where the
    previous one ended. Equivalence, not equality: a model may tidy spacing."""
    out: list[Check] = []
    steps = [s for s in ex.steps]
    if not steps:
        return [Check("chain", False, "no steps")]
    first = next((s for s in steps if s.before), None)
    if givens and first is not None and first.kind != "setup":
        b, err = _parse(first.before, "the first line")
        if b is None:
            out.append(Check("chain start", None, err))
        else:
            ok, detail = _states_equivalent(givens, b, variables, _mode(ex))
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
                    ok, detail = _states_equivalent(a, b, variables, _mode(ex))
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
        ok, detail = _states_equivalent(a, ans, variables, _mode(ex))
        if ok:
            return Check("answer", True, "the answer is what the rounding reaches")
        return Check("answer", ok, f"the answer is not the last line's value: {detail}")
    if ex.task in SOLVE_TASKS:
        ans, err = _parse(answers, "the final answer")
        if ans is None:
            return Check("answer", None, err)
        if any(r.is_expression for r in ans):
            return Check("answer", False, "a solution must name the unknown: write x = 5")
        try:
            expected = _timed(_solution_set, givens, variables, "all")
            actual = _timed(_solution_set, ans, variables, _state_mode(ans, _mode(ex)))
        except MathTimeoutError:
            return Check("answer", None, "SymPy timed out")
        except Exception as exc:  # noqa: BLE001
            return Check("answer", None, f"could not solve the problem: {exc}")
        if _same_solutions(expected, actual):
            return Check("answer", True, f"answer verified: {_fmt_solutions(actual)}")
        return Check("answer", False, f"the problem's solution is {_fmt_solutions(expected)}, "
                                      f"the answer says {_fmt_solutions(actual)}")
    # expression tasks
    if len(answers) != 1:
        return Check("answer", False, f"one expression expected, got {len(answers)}")
    ans, err = _parse(answers, "the final answer")
    if ans is None:
        return Check("answer", None, err)
    a = ans[0]
    problem = givens[0] if givens else None
    if problem is None or not problem.is_expression or not a.is_expression:
        if ex.task == "evaluate" and a.is_equation and problem is not None:
            a = Relation(a.rhs, None, None, a.text)
        else:
            return Check("answer", None, "the problem and the answer must both be expressions")
    if not _timed(_zero, problem.lhs - a.lhs):
        return Check("answer", False, f"{a.text!r} is not equivalent to {problem.text!r}")
    if ex.task == "expand" and sp.expand(a.lhs) != a.lhs:
        return Check("answer", False, f"{a.text!r} is not fully expanded")
    if ex.task == "factorise" and a.lhs.is_Add and sp.expand(a.lhs) == a.lhs and len(a.lhs.args) > 1:
        return Check("answer", False, f"{a.text!r} is not factorised")
    if ex.task == "evaluate" and a.lhs.free_symbols:
        return Check("answer", False, f"{a.text!r} is not a value")
    return Check("answer", True, "answer verified")


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
    ok, detail = _states_equivalent(a, b, variables, _mode(ex))
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
    ok, detail = _states_equivalent(a, b, variables, _mode(ex))
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
                             and ex.steps[int(c.name.split()[1]) - 1].kind in ("transform", "round")]
    answer_unverified = [c for c in rep.checks if c.ok is None and c.name == "answer"]
    if rep.failures or transforms_unverified or answer_unverified or not ex.steps:
        rep.status = "failed"
    return rep


def try_it_example(t: TryIt) -> WorkedExample | None:
    """The try-it as a worked example — what the board solves after the
    pause, and what the verifier checks. None without a problem."""
    if not t.problem or not t.answer:
        return None
    rels, _err = _parse([t.problem], "the try-it problem")
    givens = [t.problem]
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
    rounding = any(s.kind == "round" for s in t.steps)
    return WorkedExample(label="the try-it question", problem=t.problem, givens=givens, final_answer=t.answer,
                         task="estimate" if rounding else "simplify" if expression else "solve",
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
           "SOLVE_TASKS", "EXPRESSION_TASKS", "ROUND_TASKS"]
