"""Notation -> readable text for documents (worksheets, answer keys).

The docx has no typesetter, so an expression is printed the way a textbook
prints it inline: x², 3x − 5, ½-style fractions kept as (x + 1)/2, √(x + 1),
≤ and ≥. Read through the same tokens as the board and the voice, so the
printed question is the expression that was verified.
"""

from __future__ import annotations

from maths.tokens import FUNCTIONS, Tok, TokenError, tokenize

_SUP = str.maketrans("0123456789-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻")
_REL = {"=": "=", "<": "<", "<=": "≤", ">": ">", ">=": "≥", "!=": "≠", "==": "="}


def pretty(notation: str) -> str:
    """Inline print of one expression or relation; the text itself when it
    is not notation (a word problem stays a word problem)."""
    try:
        toks = tokenize(notation)
    except TokenError:
        return " ".join(str(notation or "").split())
    out: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        prev = toks[i - 1] if i > 0 else None
        if t.kind == "NUM" or t.kind == "NAME":
            if t.kind == "NAME" and t.text in FUNCTIONS and nxt is not None and nxt.kind == "LP":
                out.append("√" if t.text == "sqrt" else t.text)
            elif t.kind == "NAME" and t.text == "pi":
                out.append("π")
            else:
                out.append(t.text)
        elif t.kind == "REL":
            out.append(f" {_REL.get(t.text, t.text)} ")
        elif t.kind == "OP":
            if t.text == "^":
                # a plain numeric exponent goes superscript; anything else keeps the caret
                if nxt is not None and nxt.kind == "NUM" and "." not in nxt.text:
                    out.append(nxt.text.translate(_SUP))
                    i += 2
                    continue
                out.append("^")
            elif t.text == "*":
                out.append(" × " if (prev is not None and prev.kind == "NUM" and nxt is not None
                                      and nxt.kind == "NUM") else "")
            elif t.text == "-":
                unary = prev is None or prev.kind in ("OP", "REL", "LP", "IMPLICIT")
                out.append("−" if unary else " − ")
            elif t.text == "+":
                unary = prev is None or prev.kind in ("OP", "REL", "LP")
                out.append("" if unary else " + ")
            else:
                out.append(t.text)
        elif t.kind == "LP":
            out.append("(")
        elif t.kind == "RP":
            out.append(")")
        # IMPLICIT: nothing — 3x, 2(x + 1)
        i += 1
    return "".join(out).replace("  ", " ").strip()


__all__ = ["pretty"]
