"""The linear notation both the model and SymPy speak, and its parser.

One notation, three readers: the model writes it, SymPy verifies it, the
board typesets it and the voice speaks it. Keeping it LINEAR (no LaTeX) is
deliberate: ``\\frac`` is exactly what the JSON salvage mangles
(shared/claude_client.py), a model writes ``3x + 5 = 20`` reliably, and every
downstream reader can tokenize it with one small grammar.

Accepted:  numbers, variables, + - * / ^, brackets, sqrt(...), = < > <= >=,
           implicit multiplication (3x, 2(x+1)), and the Unicode the model may
           reach for anyway (− × ÷ ² ³ √ ≤ ≥), folded to ASCII first.
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

_UNICODE = {
    "−": "-", "–": "-", "—": "-", "×": "*", "·": "*", "÷": "/", "⁄": "/",
    "²": "^2", "³": "^3", "√": "sqrt", "≤": "<=", "≥": ">=", "≠": "!=",
    " ": " ", "π": "pi",
}
_REL_RE = re.compile(r"(<=|>=|!=|==|=|<|>)")
# `sqrt x` (no bracket) → sqrt(x); `sqrt(...)` untouched
_SQRT_BARE_RE = re.compile(r"sqrt\s+([A-Za-z0-9.]+)")
# a decimal comma the model might write in some locales: 3,5 → 3.5 (only digit,digit)
_DEC_COMMA_RE = re.compile(r"(?<=\d),(?=\d)")


def normalise(text: str) -> str:
    """Fold notation the model may emit into the ASCII the parser accepts."""
    s = str(text or "")
    for k, v in _UNICODE.items():
        s = s.replace(k, v)
    s = _DEC_COMMA_RE.sub(".", s)
    s = _SQRT_BARE_RE.sub(r"sqrt(\1)", s)
    return " ".join(s.split())


@dataclass(frozen=True)
class Relation:
    """One line of a state: ``lhs op rhs`` or a bare expression (op None)."""
    lhs: sp.Expr
    op: Optional[str]          # "=", "<", "<=", ">", ">=" or None
    rhs: Optional[sp.Expr]
    text: str

    @property
    def is_equation(self) -> bool:
        return self.op == "="

    @property
    def is_inequality(self) -> bool:
        return self.op in ("<", "<=", ">", ">=")

    @property
    def is_expression(self) -> bool:
        return self.op is None

    def as_sympy(self):
        if self.op is None:
            return self.lhs
        if self.op == "=":
            return sp.Eq(self.lhs, self.rhs)
        return {"<": sp.Lt, "<=": sp.Le, ">": sp.Gt, ">=": sp.Ge}[self.op](self.lhs, self.rhs)

    @property
    def free_symbols(self) -> set:
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


def parse_relation(text: str) -> Relation:
    """``3x + 5 = 20`` -> Relation(3x+5, "=", 20); ``2x - 1`` -> a bare expression."""
    raw = normalise(text)
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


__all__ = ["normalise", "Relation", "NotationError", "parse_relation", "parse_state",
           "symbols_named", "is_notation"]
