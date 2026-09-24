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

The common mistake is verified the other way round: SymPy must show the
wrong route really changes the meaning, or a valid method would be taught as
an error. Every SymPy call runs under mathsvc's hard timeout — a hung
verification is worse than an unverified one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

import sympy as sp

from maths.notation import NotationError, Relation, parse_state, symbols_named
from maths.schema import Lesson, Step, TryIt, WorkedExample
from mathsvc.safety import MathError, MathTimeoutError, run_with_timeout

SOLVE_TASKS = ("solve", "solve_system", "solve_inequality")
EXPRESSION_TASKS = ("simplify", "expand", "factorise", "evaluate")
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


def _states_equivalent(before: list[Relation], after: list[Relation], variables: list[sp.Symbol],
                       mode: str) -> tuple[Optional[bool], str]:
    """(verdict, detail) for "does `after` mean what `before` meant"."""
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
        sb = _timed(_solution_set, before, variables, mode)
        sa = _timed(_solution_set, after, variables, mode)
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


# ── the checks ───────────────────────────────────────────────────────────


def _variables(ex: WorkedExample, givens: Optional[list[Relation]]) -> list[sp.Symbol]:
    if ex.task in SOLVE_TASKS:
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
    if ex.task in SOLVE_TASKS:
        ans, err = _parse(answers, "the final answer")
        if ans is None:
            return Check("answer", None, err)
        if any(r.is_expression for r in ans):
            return Check("answer", False, "a solution must name the unknown: write x = 5")
        try:
            expected = _timed(_solution_set, givens, variables, "all")
            actual = _timed(_solution_set, ans, variables, _mode(ex))
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
    last = next((s for s in reversed(ex.steps) if s.kind == "transform" and s.after), None)
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
                             and ex.steps[int(c.name.split()[1]) - 1].kind == "transform"]
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
    if rels is None and t.steps and t.steps[0].kind == "setup" and t.steps[0].after:
        givens = list(t.steps[0].after)      # a word problem: the working starts at its equation
        rels, _err = _parse(givens, "the try-it equation")
    expression = bool(rels) and rels[0].is_expression
    target = ", ".join(sorted(str(s) for s in rels[0].free_symbols)) if rels else "x"
    return WorkedExample(label="the try-it question", problem=t.problem, givens=givens, final_answer=t.answer,
                         task="simplify" if expression else "solve", target=target or "x",
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
           "SOLVE_TASKS", "EXPRESSION_TASKS"]
