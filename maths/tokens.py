"""One tokenizer for the linear notation, shared by the speech layer and the
typesetter so the two never disagree about where an expression begins.

Tokens: NUM (12, 3.5), NAME (x, y, pi, sqrt), OP (+ - * / ^), REL (= < <= > >=
!=), LP, RP, and IMPLICIT — the multiplication the author left out (3x,
2(x+1), (x+1)(x+2), x y). Unicode is folded first (maths.notation.normalise).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Unicode the model may reach for, folded to the ASCII the parsers accept.
# Lives HERE, sympy-free, because the renderer's child processes import the
# typesetter and must not pay for SymPy on every spawn.
_UNICODE = {
    "−": "-", "–": "-", "—": "-", "×": "*", "·": "*", "÷": "/", "⁄": "/",
    "²": "^2", "³": "^3", "√": "sqrt", "≤": "<=", "≥": ">=", "≠": "!=",
    "\u00a0": " ", "π": "pi",
}
_SQRT_BARE_RE = re.compile(r"sqrt\s+([A-Za-z0-9.]+)")
_DEC_COMMA_RE = re.compile(r"(?<=\d),(?=\d)")


def normalise(text) -> str:
    """Fold notation the model may emit into the ASCII the parsers accept."""
    s = str(text or "")
    for k, v in _UNICODE.items():
        s = s.replace(k, v)
    s = _DEC_COMMA_RE.sub(".", s)
    s = _SQRT_BARE_RE.sub(r"sqrt(\1)", s)
    return " ".join(s.split())

_TOKEN_RE = re.compile(r"\s*(?:(\d+\.\d+|\d+\.|\.\d+|\d+)|([A-Za-z][A-Za-z0-9_]*)|(<=|>=|!=|==|[=<>])|([-+*/^])|(\()|(\)))")

FUNCTIONS = {"sqrt", "abs", "sin", "cos", "tan", "log", "ln", "exp"}
CONSTANTS = {"pi", "e"}
# A variable is a letter, optionally with a digit subscript (x, y, x1, x_1).
# Anything longer is prose ("subtract", "from"), and prose is not notation:
# the typesetter and the speech layer must refuse it rather than lay out
# "subtract" as s·u·b·t·r·a·c·t. SymPy's own parse (maths.notation) keeps
# mathsvc's wider rule, because a word problem may name a quantity "speed".
_VAR_RE = re.compile(r"^[A-Za-z](?:_?\d{1,2})?$")
# two or three letters with no subscript are a PRODUCT of variables (xy, 4ac)
_PRODUCT_RE = re.compile(r"^[A-Za-z]{2,3}$")


@dataclass(frozen=True)
class Tok:
    kind: str    # NUM NAME REL OP LP RP IMPLICIT
    text: str
    pos: int = 0

    @property
    def is_operand_end(self) -> bool:
        return self.kind in ("NUM", "NAME", "RP")

    @property
    def is_operand_start(self) -> bool:
        return self.kind in ("NUM", "NAME", "LP")


class TokenError(ValueError):
    pass


def tokenize(text: str) -> list[Tok]:
    s = normalise(text)
    out: list[Tok] = []
    i = 0
    while i < len(s):
        if s[i].isspace():
            i += 1
            continue
        m = _TOKEN_RE.match(s, i)
        if not m or m.end() == i:
            raise TokenError(f"cannot read {s[i:i + 12]!r}")
        num, name, rel, op, lp, rp = m.groups()
        if num:
            t = Tok("NUM", num, i)
        elif name:
            if name not in FUNCTIONS and name not in CONSTANTS and not _VAR_RE.match(name):
                if not _PRODUCT_RE.match(name):
                    raise TokenError(f"{name!r} is a word, not notation")
                # xy -> x·y: one NAME per letter, implicit products between
                for k, ch in enumerate(name):
                    if out and out[-1].is_operand_end:
                        out.append(Tok("IMPLICIT", "", i + k))
                    out.append(Tok("NAME", ch, i + k))
                i = m.end()
                continue
            t = Tok("NAME", name, i)
        elif rel:
            t = Tok("REL", rel, i)
        elif op:
            t = Tok("OP", op, i)
        elif lp:
            t = Tok("LP", "(", i)
        else:
            t = Tok("RP", ")", i)
        # implicit multiplication: an operand starts right after one ends,
        # except a function name before its bracket (sqrt(...))
        if out and out[-1].is_operand_end and t.is_operand_start:
            prev = out[-1]
            if not (prev.kind == "NAME" and prev.text in FUNCTIONS and t.kind == "LP"):
                # "2x" and "xy": a NAME after a NUM or NAME is a product; "x2"
                # cannot happen (the regex reads x2 as one name — a subscript
                # in the author's mind, kept as a name)
                out.append(Tok("IMPLICIT", "", i))
        out.append(t)
        i = m.end()
    return out


def split_relation(tokens: list[Tok]) -> tuple[list[Tok], Tok | None, list[Tok]]:
    rels = [k for k, t in enumerate(tokens) if t.kind == "REL"]
    if not rels:
        return tokens, None, []
    k = rels[0]
    return tokens[:k], tokens[k], tokens[k + 1:]


__all__ = ["Tok", "TokenError", "tokenize", "split_relation", "FUNCTIONS", "CONSTANTS", "normalise"]
