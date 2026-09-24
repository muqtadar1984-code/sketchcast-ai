"""Notation -> spoken words, so a voice never reads "x caret 2".

The model is asked to write every spoken line in words; this layer is the
belt behind that instruction. ``spoken`` turns one expression into speech;
``speakable_maths`` finds notation inside prose and replaces it, leaving the
prose alone. It reads the same tokens the typesetter reads, so what is
written on the board and what is said are the same expression.

Spoken forms follow classroom convention, not a formal reading: "x squared",
"three x plus five equals twenty", "x plus one, all squared", "three
quarters", "x over two", "the square root of x plus one". Digits stay digits
— every TTS provider reads "20" as twenty, in every language it speaks.
"""

from __future__ import annotations

import re

from maths.tokens import FUNCTIONS, Tok, TokenError, tokenize

_REL_WORDS = {"=": "equals", "<": "is less than", "<=": "is less than or equal to",
              ">": "is greater than", ">=": "is greater than or equal to",
              "!=": "is not equal to", "==": "equals"}
_SMALL_FRACTIONS = {("1", "2"): "a half", ("1", "3"): "a third", ("2", "3"): "two thirds",
                    ("1", "4"): "a quarter", ("3", "4"): "three quarters", ("1", "5"): "a fifth",
                    ("1", "10"): "a tenth"}
_FUNC_WORDS = {"sqrt": "the square root of", "abs": "the absolute value of", "sin": "sine of",
               "cos": "cosine of", "tan": "tan of", "log": "log of", "ln": "the natural log of",
               "exp": "e to the power of"}


class _P:
    """Recursive descent over the token list, yielding words."""

    def __init__(self, toks: list[Tok]):
        self.t = toks
        self.i = 0

    def peek(self) -> Tok | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self) -> Tok:
        tok = self.t[self.i]
        self.i += 1
        return tok

    # relation := sum (REL sum)*
    def relation(self) -> str:
        out = self.sum()
        while (p := self.peek()) and p.kind == "REL":
            self.take()
            out += f" {_REL_WORDS.get(p.text, p.text)} " + self.sum()
        return out

    # sum := term ((+|-) term)*
    def sum(self) -> str:
        parts = [self.term()]
        while (p := self.peek()) and p.kind == "OP" and p.text in "+-":
            self.take()
            parts.append("plus" if p.text == "+" else "minus")
            parts.append(self.term())
        return " ".join(parts)

    # term := unary ((*|/|IMPLICIT) unary)*
    def term(self) -> str:
        left_tok = self.peek()
        start = self.i
        out = self.unary()
        num_simple = (self.i - start == 1) and left_tok is not None and left_tok.kind in ("NUM", "NAME")
        prev_simple = num_simple
        while (p := self.peek()) and ((p.kind == "OP" and p.text in "*/") or p.kind == "IMPLICIT"):
            self.take()
            nxt = self.peek()
            if p.text == "/":
                out = self._fraction(out, left_tok, num_simple)
                prev_simple = num_simple = False
                continue
            right = self.unary()
            if p.kind == "IMPLICIT" and nxt is not None and nxt.kind == "LP":
                # 2(x + 1), (x+1)(x+2): a spoken "times" keeps the grouping audible
                out = f"{out} times {right}"
            elif p.kind == "IMPLICIT":
                out = f"{out} {right}"                       # 3x, xy
            elif nxt is not None and nxt.kind == "NUM" and prev_simple:
                out = f"{out} times {right}"                # 2 * 3
            else:
                out = f"{out} times {right}"
            prev_simple = False
        return out

    def _fraction(self, num_words: str, num_tok: Tok | None, num_simple: bool) -> str:
        den_start = self.i
        den_words = self.unary()
        den_toks = self.t[den_start:self.i]
        simple_den = len(den_toks) == 1 and den_toks[0].kind in ("NUM", "NAME")
        if num_simple and num_tok is not None and num_tok.kind == "NUM" and simple_den \
                and den_toks[0].kind == "NUM":
            key = (num_tok.text, den_toks[0].text)
            if key in _SMALL_FRACTIONS:
                return _SMALL_FRACTIONS[key]
        if simple_den and num_simple:
            return f"{num_words} over {den_words}"
        if simple_den:
            return f"{num_words}, over {den_words}"
        return f"{num_words}, over {den_words},"

    # unary := '-' unary | power
    def unary(self) -> str:
        p = self.peek()
        if p is not None and p.kind == "OP" and p.text == "-":
            self.take()
            return "negative " + self.unary()
        if p is not None and p.kind == "OP" and p.text == "+":
            self.take()
            return self.unary()
        return self.power()

    # power := atom ('^' unary)?
    def power(self) -> str:
        start = self.i
        base = self.atom()
        base_toks = self.t[start:self.i]
        p = self.peek()
        if p is not None and p.kind == "OP" and p.text == "^":
            self.take()
            e_start = self.i
            exp = self.unary()
            e_toks = self.t[e_start:self.i]
            bracketed = base_toks and base_toks[0].kind == "LP"
            joiner = ", all " if bracketed else " "
            if len(e_toks) == 1 and e_toks[0].kind == "NUM" and e_toks[0].text == "2":
                return f"{base}{joiner}squared"
            if len(e_toks) == 1 and e_toks[0].kind == "NUM" and e_toks[0].text == "3":
                return f"{base}{joiner}cubed"
            return f"{base}{joiner}to the power of {exp}"
        return base

    # atom := NUM | NAME | NAME '(' relation ')' | '(' relation ')'
    def atom(self) -> str:
        p = self.peek()
        if p is None:
            raise TokenError("unexpected end")
        if p.kind == "NUM":
            self.take()
            return p.text
        if p.kind == "NAME":
            self.take()
            nxt = self.peek()
            if p.text in FUNCTIONS and nxt is not None and nxt.kind == "LP":
                self.take()
                inner = self.relation()
                self._close()
                return f"{_FUNC_WORDS.get(p.text, p.text + ' of')} {inner},"
            if p.text == "pi":
                return "pi"
            return p.text
        if p.kind == "LP":
            self.take()
            inner = self.relation()
            self._close()
            return f"{inner}"
        raise TokenError(f"unexpected {p.text!r}")

    def _close(self) -> None:
        p = self.peek()
        if p is None or p.kind != "RP":
            raise TokenError("missing )")
        self.take()


def _tidy(s: str) -> str:
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r",\s*,", ",", s)
    return s.strip(" ,")


def spoken(notation: str) -> str:
    """One expression or relation as words. Raises TokenError on text that
    is not notation — callers that are unsure use speakable_maths."""
    toks = tokenize(notation)
    if not toks:
        return ""
    p = _P(toks)
    out = p.relation()
    if p.peek() is not None:
        raise TokenError(f"trailing {p.peek().text!r}")
    return _tidy(out)


# ── notation inside prose ────────────────────────────────────────────────

# a word that could be part of notation: a number, a single letter, a known
# function, an operator, a bracket — or glued forms like 3x, (x+2), x^2
_MATHY = re.compile(r"^[\d.]+$|^[A-Za-z]$|^(?:sqrt|pi)$|^[-+*/^=<>≤≥−×÷√²³()]+$|"
                    r"^[-+(]*[\d.]*[A-Za-z]?[\d.]*(?:[\^²³][\d]+)?[)]*(?:[-+*/^=<>−×÷]\S*)?$")
_HAS_OP = re.compile(r"[\^*/=<>²³√−×÷]|\d[A-Za-z]\b|\b[A-Za-z]\d+\b|[)(]")
_WORD_RE = re.compile(r"\S+")


def _is_mathy(word: str) -> bool:
    w = word.strip(",.;:!?")
    if not w or w.lower() in ("a", "i"):
        # single letters that are English words are notation only next to an operator
        return bool(w) and False
    return bool(_MATHY.match(w))


def speakable_maths(text: str) -> str:
    """Prose with every notation span replaced by its spoken form.

    A span is a run of mathy words that contains an operator, a power, a
    bracket or a glued product (3x). Plain numbers and lone letters stay as
    they are; a span the tokenizer cannot read is left untouched."""
    if not text:
        return text
    words = [(m.group(0), m.start(), m.end()) for m in _WORD_RE.finditer(text)]
    out: list[str] = []
    last = 0
    i = 0
    while i < len(words):
        if not _is_mathy(words[i][0]):
            i += 1
            continue
        j = i
        while j < len(words) and _is_mathy(words[j][0]):
            j += 1
        # trailing punctuation belongs to the prose
        span_words = [w for w, _s, _e in words[i:j]]
        raw = " ".join(span_words)
        trail = ""
        m = re.search(r"[,.;:!?]+$", raw)
        if m:
            trail = m.group(0)
            raw = raw[:m.start()]
        core = raw.strip()
        if _HAS_OP.search(core) and any(ch.isalnum() for ch in core):
            try:
                said = spoken(core)
            except (TokenError, Exception):  # noqa: BLE001 — leave prose alone
                said = None
            if said:
                start, end = words[i][1], words[j - 1][2]
                out.append(text[last:start])
                out.append(said + trail)
                last = end
        i = j
    out.append(text[last:])
    return "".join(out)


def has_notation(text: str) -> bool:
    return speakable_maths(text) != text


__all__ = ["spoken", "speakable_maths", "has_notation"]
