"""The linear notation both the model and SymPy speak, and its parser.

One notation, three readers: the model writes it, SymPy verifies it, the
board typesets it and the voice speaks it. Keeping it LINEAR (no LaTeX) is
deliberate: ``\\frac`` is exactly what the JSON salvage mangles
(shared/claude_client.py), a model writes ``3x + 5 = 20`` reliably, and every
downstream reader can tokenize it with one small grammar.

Accepted:  numbers, variables, + - * / ^, brackets, sqrt(...), = < > <= >=,
           implicit multiplication (3x, 2(x+1)), and the Unicode the model may
           reach for anyway (− × ÷ ² ³ √ ≤ ≥), folded to ASCII first; and a
           DATA LIST — numbers separated by comma-and-space or semicolons
           ("4, 8, 6, 10, 12"), the givens of a mean/median/mode/range task
           (a Grade 7 statistics chapter failed every worksheet question on
           2026-09-26 because a list could only ever be a refusal).
Rejected:  everything else — the parse runs through mathsvc.safety, the same
           character whitelist and restricted eval the tutor's calculator
           uses, because this text was written by a model on a child's
           behalf and SymPy's parser is ``eval``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import sympy as sp

from mathsvc.safety import MathError, MathInputError, _parse, validate_text

from maths.tokens import normalise  # noqa: E402  (sympy-free, shared with the typesetter)

_REL_RE = re.compile(r"(<=|>=|!=|==|=|<|>)")
# "Solve: 3x + 5 = 20", "Solve for x: ...", "Find x if ...", "Simplify ..." —
# the task verb a model puts in front of a problem. Production, 2026-09-24:
# "Solve 4z = 16" parsed with `Solve` as a SYMBOL (mathsvc allows long
# names for word problems) and the try-it was dropped as wrong.
# The single-letter unknowns after the verb ("solve for x, y") are whole
# words: without the boundary "Find the mean" lost the "t" of "the" and
# handed the parser "he mean of ...". A statistic's name ("the arithmetic
# mean of", "the median of the data:") is part of the verb too, so the
# data list after it is what gets parsed.
_VERB_RE = re.compile(
    r"^\s*(?:solve|simplify|expand|factori[sz]e|evaluate|find|calculate|work\s+out|determine|"
    r"estimate|approximate|round(?:\s+off)?)\b"
    r"(?:\s+(?:for|the\s+value\s+of))?"
    r"(?:\s+the\s+(?:arithmetic\s+)?(?:mean|median|mode|range|average)(?:\s+of)?)?"
    r"(?:\s+(?:the\s+)?(?:data|numbers|values|observations|following|scores|marks)(?:\s+set)?)?"
    r"(?:\s+[a-z]\b(?:\s*,\s*[a-z]\b)*)?(?:\s+(?:if|when|where|given))?\s*:?\s*",
    re.IGNORECASE,
)


def strip_task_verb(text: str) -> str:
    """The notation without a leading task verb, when one is there."""
    s = str(text or "")
    m = _VERB_RE.match(s)
    if m and m.end() < len(s):
        return s[m.end():].strip()
    return s


@dataclass(frozen=True)
class Relation:
    """One line of a state: ``lhs op rhs``, a bare expression (op None), or
    a data list (``data`` set: the numbers, in the order written; ``lhs`` is
    then their SymPy Tuple)."""
    lhs: sp.Expr
    op: Optional[str]          # "=", "<", "<=", ">", ">=" or None
    rhs: Optional[sp.Expr]
    text: str
    data: Optional[tuple] = None

    @property
    def is_equation(self) -> bool:
        return self.op == "="

    @property
    def is_inequality(self) -> bool:
        return self.op in ("<", "<=", ">", ">=")

    @property
    def is_expression(self) -> bool:
        return self.op is None and self.data is None

    @property
    def is_data(self) -> bool:
        return self.data is not None

    def as_sympy(self):
        if self.data is not None:
            return sp.Tuple(*self.data)
        if self.op is None:
            return self.lhs
        if self.op == "=":
            return sp.Eq(self.lhs, self.rhs)
        return {"<": sp.Lt, "<=": sp.Le, ">": sp.Gt, ">=": sp.Ge}[self.op](self.lhs, self.rhs)

    @property
    def free_symbols(self) -> set:
        if self.data is not None:
            return set()
        out = set(self.lhs.free_symbols)
        if self.rhs is not None:
            out |= set(self.rhs.free_symbols)
        return out


class NotationError(ValueError):
    """The text is not notation this pipeline can read. The message names
    the line, so a verification report can say which one."""


def _side(text: str, *, where: str) -> sp.Expr:
    cleaned = validate_text(text, field=where)
    expr = _parse(cleaned)
    if not isinstance(expr, sp.Expr):
        raise MathInputError(f"{where}: not an expression")
    return expr


# A data list's separators: a comma FOLLOWED BY A SPACE, or a semicolon, or
# the word "and" between two numbers ("4, 8, 6, 10 and 12"). A comma with no
# space after it stays what maths.tokens.normalise makes of it — a decimal
# comma ("3,14") or a digit group ("58,672") — so "1, 234" is two numbers
# and "1,234" is one.
_DATA_SEP_RE = re.compile(r"\s*(?:,\s+(?:and\s+)?|;\s*|\s+and\s+)")
_DATA_ITEM_RE = re.compile(r"^-?\s*(?:\d+(?:[.,]\d+)*|\d+\s*/\s*\d+)$")


def _data_items(text: str) -> Optional[list[str]]:
    """The items of a data list, or None when the text is not one: at
    least two pieces, every piece a plain number (a decimal, a digit-grouped
    integer, a simple fraction), no relation sign anywhere."""
    s = str(text or "").strip().rstrip(".")
    if _REL_RE.search(s) or not (", " in s or ";" in s or " and " in s):
        return None
    pieces = [p.strip() for p in _DATA_SEP_RE.split(s)]
    if len(pieces) < 2 or not all(p and _DATA_ITEM_RE.match(p) for p in pieces):
        return None
    return pieces


def parse_data(text: str) -> Relation:
    """``4, 8, 6, 10, 12`` -> a data Relation. Raises NotationError when the
    text is not a list of numbers."""
    pieces = _data_items(strip_task_verb(text))
    if pieces is None:
        raise NotationError(f"{str(text)!r} is not a list of numbers")
    values = []
    try:
        for piece in pieces:
            v = _side(normalise(piece), where="data value")
            if v.free_symbols or not v.is_number:
                raise NotationError(f"{piece!r} is not a number")
            values.append(v)
    except MathError as exc:
        raise NotationError(f"{str(text)!r}: {exc}") from exc
    return Relation(sp.Tuple(*values), None, None, ", ".join(pieces), data=tuple(values))


def parse_relation(text: str) -> Relation:
    """``3x + 5 = 20`` -> Relation(3x+5, "=", 20); ``2x - 1`` -> a bare
    expression; ``4, 8, 6, 10, 12`` -> a data list (parse_data)."""
    if _data_items(strip_task_verb(text)) is not None:
        return parse_data(text)
    raw = normalise(strip_task_verb(text))
    if not raw:
        raise NotationError("empty line")
    if "!=" in raw or "==" in raw:
        raise NotationError(f"unsupported relation in {raw!r}")
    parts = _REL_RE.split(raw)
    try:
        if len(parts) == 1:
            return Relation(_side(raw, where="expression"), None, None, raw)
        if len(parts) != 3:
            raise NotationError(f"more than one relation in {raw!r}")
        lhs, op, rhs = parts[0].strip(), parts[1], parts[2].strip()
        if not lhs or not rhs:
            raise NotationError(f"a side of {raw!r} is empty")
        return Relation(_side(lhs, where="left side"), op, _side(rhs, where="right side"), raw)
    except MathError as exc:
        raise NotationError(f"{raw!r}: {exc}") from exc


def parse_state(lines: list[str]) -> list[Relation]:
    return [parse_relation(x) for x in lines]


def symbols_named(names: list[str]) -> list[sp.Symbol]:
    return [sp.Symbol(n) for n in names]


def is_notation(text: str) -> bool:
    """Does this string parse as notation? Used to tell an equation from a
    word problem — never as a security gate (the parser is that)."""
    try:
        parse_relation(text)
        return True
    except (NotationError, MathError, Exception):  # noqa: BLE001
        return False


__all__ = ["normalise", "Relation", "NotationError", "parse_relation", "parse_data", "parse_state",
           "symbols_named", "is_notation", "strip_task_verb"]
