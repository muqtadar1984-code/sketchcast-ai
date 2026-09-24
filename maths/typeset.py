"""School-level equation typesetting for the board.

Not a full maths typesetter and not meant to be: the first algebra release
needs fractions, powers, roots, subscripts-by-name and the ordinary operators
laid out the way a textbook prints them, and nothing else (founder direction
2026-09-24). Unicode ² and ³ get you part of the way; a stacked fraction, a
radical with its bar and an exponent that is actually raised do not exist as
characters, so they are laid out here.

The input is the same linear notation SymPy verified (maths.notation) and the
same tokens the speech layer reads (maths.tokens), so the board shows the
expression that was checked and the voice says the one that is shown.

Output is a ``Layout``: glyph runs (text, position, size) and strokes
(fraction bars, radicals) in world units relative to the expression's own
top-left corner, plus the order they are written in, so the renderer can
reveal the expression progressively like any handwritten line and anchor an
arrow to a term ("5x") by name. Fonts are measured through a ``Measurer``
the caller supplies — the renderer passes its own font cache, so layout and
drawing use one face.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from maths.tokens import FUNCTIONS, Tok, TokenError, normalise, tokenize

Point = tuple[float, float]

_BIN_OPS = {"+": "+", "-": "−", "=": "=", "<": "<", "<=": "≤", ">": ">", ">=": "≥", "!=": "≠",
            "==": "=", "*": "×"}
_GAP_BIN = 0.24       # around + − = < > (fraction of size)
_GAP_TIMES = 0.16
_GAP_IMPLICIT = 0.05
_FRAC_SCALE = 0.92
_EXP_SCALE = 0.62


class Measurer(Protocol):
    def measure(self, text: str, size: float) -> tuple[float, float, float, float]:
        """(advance width, ascent, ink_top, ink_bottom) for ``text`` at ``size``.
        ascent is the font's, so the drawer places the run at
        baseline - ascent; ink_top/ink_bottom are the glyphs' vertical
        extents RELATIVE TO THE BASELINE (top negative, bottom positive)."""


class PilMeasurer:
    """A Measurer over PIL fonts. ``loader(size)`` returns a FreeType font."""

    def __init__(self, loader: Callable[[int], object]):
        self.loader = loader
        self._cache: dict[tuple[str, int], tuple[float, float, float, float]] = {}

    def measure(self, text: str, size: float) -> tuple[float, float, float, float]:
        key = (text, int(round(size)))
        if key not in self._cache:
            f = self.loader(int(round(size)))
            try:
                w = float(f.getlength(text))
                asc, _desc = f.getmetrics()
                x0, y0, x1, y1 = f.getbbox(text)
                self._cache[key] = (w, float(asc), float(y0) - asc, float(y1) - asc)
            except Exception:  # noqa: BLE001 — a font without metrics: estimate
                self._cache[key] = (len(text) * size * 0.55, size * 0.8, -size * 0.7, size * 0.2)
        return self._cache[key]


@dataclass
class Run:
    text: str
    x: float          # left edge, relative to the layout's origin
    baseline: float   # y of the baseline, relative to the layout's origin
    size: float
    src: str = ""     # the notation this run came from (for `sub` anchors)
    role: str = "ink"  # ink | op

    @property
    def weight(self) -> int:
        return max(1, len(self.text.replace(" ", "")))


@dataclass
class Stroke:
    pts: list[Point]
    width: float
    weight: int = 2


@dataclass
class _Box:
    """A laid-out node: items relative to (x=0, baseline=0)."""
    w: float
    ink_top: float      # negative: above the baseline
    ink_bottom: float   # positive: below
    runs: list[Run] = field(default_factory=list)
    strokes: list[Stroke] = field(default_factory=list)

    def shifted(self, dx: float, dy: float) -> "_Box":
        return _Box(self.w, self.ink_top + dy, self.ink_bottom + dy,
                    [Run(r.text, r.x + dx, r.baseline + dy, r.size, r.src, r.role) for r in self.runs],
                    [Stroke([(x + dx, y + dy) for x, y in s.pts], s.width, s.weight) for s in self.strokes])

    @property
    def h(self) -> float:
        return self.ink_bottom - self.ink_top


@dataclass
class Layout:
    """The finished expression. Origin (0, 0) is its top-left; ``w``/``h``
    are its ink extents; ``baseline`` the main line's baseline from the top."""
    runs: list[Run]
    strokes: list[Stroke]
    w: float
    h: float
    baseline: float
    size: float
    notation: str

    def order(self) -> list[tuple[str, int]]:
        """Write order: left to right, top to bottom within a column."""
        items: list[tuple[float, float, str, int]] = []
        for i, r in enumerate(self.runs):
            items.append((round(r.x, 1), r.baseline, "run", i))
        for i, s in enumerate(self.strokes):
            x0 = min(p[0] for p in s.pts)
            y0 = min(p[1] for p in s.pts)
            items.append((round(x0, 1), y0, "stroke", i))
        items.sort(key=lambda t: (t[0], t[1]))
        return [(kind, i) for _x, _y, kind, i in items]

    @property
    def units(self) -> int:
        return sum(r.weight for r in self.runs) + sum(s.weight for s in self.strokes)

    def visible(self, frac: float) -> list[tuple[str, int, float]]:
        """What is on the board when ``frac`` of the writing is done:
        (kind, index, portion) with portion in (0, 1] — a run partly written
        shows that prefix of its characters."""
        budget = frac * self.units
        out: list[tuple[str, int, float]] = []
        for kind, i in self.order():
            w = self.runs[i].weight if kind == "run" else self.strokes[i].weight
            if budget <= 0:
                break
            if budget >= w:
                out.append((kind, i, 1.0))
            else:
                out.append((kind, i, budget / w))
            budget -= w
        return out

    def find(self, sub: str) -> Optional[tuple[float, float, float, float]]:
        """The box of a term named in notation ("5x", "x^2", "(x + 2)"):
        the first match in write order, spaces ignored."""
        hits = self.find_all(sub)
        return hits[0][0] if hits else None

    def find_all(self, sub: str) -> list[tuple[tuple[float, float, float, float], bool]]:
        """Every match of ``sub`` as (box, standalone): standalone means the
        matched runs are not glued to a variable — the 5 of "3x + 5", not
        the 5 of "5x" — so a caller can prefer the constant term."""
        want = _norm(sub)
        if not want:
            return []
        runs = [self.runs[i] for kind, i in self.order() if kind == "run"]
        texts = [_norm(r.src or r.text) for r in runs]
        joined = "".join(texts)
        out = []
        k = joined.find(want)
        while k >= 0:
            pos = 0
            hit: list[int] = []
            for j, t in enumerate(texts):
                if pos + len(t) > k and pos < k + len(want):
                    hit.append(j)
                pos += len(t)
            if hit:
                rs = [runs[j] for j in hit]
                x0 = min(r.x for r in rs)
                x1 = max(r.x + self._run_w(r) for r in rs)
                y0 = min(r.baseline - r.size * 0.75 for r in rs)
                y1 = max(r.baseline + r.size * 0.25 for r in rs)
                nxt = runs[hit[-1] + 1] if hit[-1] + 1 < len(runs) else None
                prev = runs[hit[0] - 1] if hit[0] > 0 else None
                glued = ((nxt is not None and nxt.text[:1].isalpha() and nxt.role == "ink")
                         or (prev is not None and prev.text[-1:].isalnum() and prev.role == "ink"))
                out.append(((x0, y0, x1, y1), not glued))
            k = joined.find(want, k + 1)
        return out

    _widths: dict = field(default_factory=dict, repr=False)

    def _run_w(self, r: Run) -> float:
        return self._widths.get(id(r), len(r.text) * r.size * 0.55)


def _norm(s: str) -> str:
    return normalise(s).replace(" ", "").replace("**", "^")


# ── parse into a small tree ──────────────────────────────────────────────


class _Node:
    pass


@dataclass
class _Atom(_Node):
    text: str
    src: str
    role: str = "ink"


@dataclass
class _Row(_Node):
    items: list          # nodes and ("gap", frac) markers


@dataclass
class _Frac(_Node):
    num: _Node
    den: _Node


@dataclass
class _Pow(_Node):
    base: _Node
    exp: _Node


@dataclass
class _Root(_Node):
    inner: _Node


@dataclass
class _Brack(_Node):
    inner: _Node


def _unwrap(n: _Node) -> _Node:
    return n.inner if isinstance(n, _Brack) else n


class _Parser:
    def __init__(self, toks: list[Tok]):
        self.t = toks
        self.i = 0

    def peek(self) -> Optional[Tok]:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self) -> Tok:
        tok = self.t[self.i]
        self.i += 1
        return tok

    def relation(self) -> _Node:
        items: list = [self.sum()]
        while (p := self.peek()) and p.kind == "REL":
            self.take()
            items += [("gap", _GAP_BIN), _Atom(_BIN_OPS.get(p.text, p.text), p.text, "op"),
                      ("gap", _GAP_BIN), self.sum()]
        return items[0] if len(items) == 1 else _Row(items)

    def sum(self) -> _Node:
        items: list = [self.term()]
        while (p := self.peek()) and p.kind == "OP" and p.text in "+-":
            self.take()
            items += [("gap", _GAP_BIN), _Atom(_BIN_OPS[p.text], p.text, "op"), ("gap", _GAP_BIN),
                      self.term()]
        return items[0] if len(items) == 1 else _Row(items)

    def term(self) -> _Node:
        items: list = [self.unary()]
        last_kind = self._last_kind()
        while (p := self.peek()) and ((p.kind == "OP" and p.text in "*/") or p.kind == "IMPLICIT"):
            self.take()
            nxt = self.peek()
            if p.text == "/":
                left = items[0] if len(items) == 1 else _Row(items)
                # a bracket that IS the whole numerator or denominator is
                # the author's linear-notation grouping, not a bracket a
                # textbook would print over a stacked fraction
                items = [_Frac(_unwrap(left), _unwrap(self.unary()))]
                last_kind = "frac"
                continue
            right = self.unary()
            explicit_times = (p.text == "*" and nxt is not None and nxt.kind == "NUM"
                              and last_kind == "NUM")
            if explicit_times:
                items += [("gap", _GAP_TIMES), _Atom("×", "*", "op"), ("gap", _GAP_TIMES), right]
            else:
                items += [("gap", _GAP_IMPLICIT), right]
            last_kind = self._last_kind()
        return items[0] if len(items) == 1 else _Row(items)

    def _last_kind(self) -> str:
        return self.t[self.i - 1].kind if self.i > 0 else ""

    def unary(self) -> _Node:
        p = self.peek()
        if p is not None and p.kind == "OP" and p.text == "-":
            self.take()
            return _Row([_Atom("−", "-", "op"), ("gap", 0.02), self.unary()])
        if p is not None and p.kind == "OP" and p.text == "+":
            self.take()
            return self.unary()
        return self.power()

    def power(self) -> _Node:
        base = self.atom()
        p = self.peek()
        if p is not None and p.kind == "OP" and p.text == "^":
            self.take()
            return _Pow(base, self.unary())
        return base

    def atom(self) -> _Node:
        p = self.peek()
        if p is None:
            raise TokenError("unexpected end")
        if p.kind == "NUM":
            self.take()
            return _Atom(p.text, p.text)
        if p.kind == "NAME":
            self.take()
            nxt = self.peek()
            if p.text in FUNCTIONS and nxt is not None and nxt.kind == "LP":
                self.take()
                inner = self.relation()
                self._close()
                if p.text == "sqrt":
                    return _Root(inner)
                return _Row([_Atom(p.text, p.text), _Brack(inner)])
            if p.text == "pi":
                return _Atom("π", "pi")
            return _Atom(p.text, p.text)
        if p.kind == "LP":
            self.take()
            inner = self.relation()
            self._close()
            return _Brack(inner)
        raise TokenError(f"unexpected {p.text!r}")

    def _close(self) -> None:
        p = self.peek()
        if p is None or p.kind != "RP":
            raise TokenError("missing )")
        self.take()


# ── layout ───────────────────────────────────────────────────────────────


class _Layouter:
    def __init__(self, m: Measurer, widths: dict):
        self.m = m
        self.widths = widths

    def atom(self, a: _Atom, size: float) -> _Box:
        w, asc, top, bot = self.m.measure(a.text, size)
        r = Run(a.text, 0.0, 0.0, size, a.src, a.role)
        self.widths[id(r)] = w
        return _Box(w, min(top, -size * 0.45), max(bot, size * 0.05), [r], [])

    def node(self, n, size: float) -> _Box:
        if isinstance(n, _Atom):
            return self.atom(n, size)
        if isinstance(n, _Row):
            return self.row(n, size)
        if isinstance(n, _Frac):
            return self.frac(n, size)
        if isinstance(n, _Pow):
            return self.pow(n, size)
        if isinstance(n, _Root):
            return self.root(n, size)
        if isinstance(n, _Brack):
            return self.brack(n, size)
        raise TypeError(type(n))

    def row(self, r: _Row, size: float) -> _Box:
        x = 0.0
        runs: list[Run] = []
        strokes: list[Stroke] = []
        top, bot = -size * 0.45, size * 0.05
        for it in r.items:
            if isinstance(it, tuple):
                x += it[1] * size
                continue
            b = self.node(it, size).shifted(x, 0.0)
            runs += b.runs
            strokes += b.strokes
            top, bot = min(top, b.ink_top), max(bot, b.ink_bottom)
            x += b.w
        return _Box(x, top, bot, runs, strokes)

    def frac(self, f: _Frac, size: float) -> _Box:
        s = size * _FRAC_SCALE
        num, den = self.node(f.num, s), self.node(f.den, s)
        axis = -size * 0.34
        pad = size * 0.12
        w = max(num.w, den.w) + size * 0.24
        num_base = axis - pad - num.ink_bottom
        den_base = axis + pad - den.ink_top
        nb = num.shifted((w - num.w) / 2, num_base)
        db = den.shifted((w - den.w) / 2, den_base)
        bar = Stroke([(size * 0.05, axis), (w - size * 0.05, axis)], max(2.4, size * 0.075))
        return _Box(w, nb.ink_top, db.ink_bottom, nb.runs + db.runs, nb.strokes + [bar] + db.strokes)

    def pow(self, p: _Pow, size: float) -> _Box:
        base = self.node(p.base, size)
        exp = self.node(p.exp, size * _EXP_SCALE)
        eb = exp.shifted(base.w + size * 0.04, -size * 0.42)
        return _Box(base.w + size * 0.04 + exp.w, min(base.ink_top, eb.ink_top),
                    max(base.ink_bottom, eb.ink_bottom), base.runs + eb.runs, base.strokes + eb.strokes)

    def root(self, r: _Root, size: float) -> _Box:
        inner = self.node(r.inner, size)
        gap = size * 0.14
        vinc = inner.ink_top - size * 0.16
        bottom = inner.ink_bottom + size * 0.06
        x_in = size * 0.62
        ib = inner.shifted(x_in, 0.0)
        w = x_in + inner.w + size * 0.12
        sign = Stroke([(0.0, -size * 0.28), (size * 0.16, -size * 0.34), (size * 0.34, bottom),
                       (x_in - size * 0.06, vinc), (w, vinc)], max(2.4, size * 0.07), weight=3)
        return _Box(w, vinc - size * 0.04, bottom, ib.runs, [sign] + ib.strokes)

    def brack(self, b: _Brack, size: float) -> _Box:
        inner = self.node(b.inner, size)
        h_norm = size * 0.95
        ratio = max(1.0, inner.h / h_norm)
        psize = size * min(2.6, ratio)
        lw, lasc, ltop, lbot = self.m.measure("(", psize)
        rw, _, _, _ = self.m.measure(")", psize)
        # centre the paren on the inner ink
        centre = (inner.ink_top + inner.ink_bottom) / 2
        pcentre = (ltop + lbot) / 2
        pbase = centre - pcentre
        left = Run("(", 0.0, pbase, psize, "(", "op")
        self.widths[id(left)] = lw
        ib = inner.shifted(lw + size * 0.02, 0.0)
        right = Run(")", lw + size * 0.02 + inner.w + size * 0.02, pbase, psize, ")", "op")
        self.widths[id(right)] = rw
        w = lw + size * 0.04 + inner.w + rw
        top = min(inner.ink_top, pbase + ltop)
        bot = max(inner.ink_bottom, pbase + lbot)
        return _Box(w, top, bot, [left] + ib.runs + [right], ib.strokes)


def typeset(notation: str, size: float, measurer: Measurer) -> Layout:
    """Lay out ``notation`` at ``size`` world px. Raises TokenError on text
    that is not notation (the caller falls back to a plain text element)."""
    toks = tokenize(notation)
    if not toks:
        raise TokenError("empty")
    p = _Parser(toks)
    tree = p.relation()
    if p.peek() is not None:
        raise TokenError(f"trailing {p.peek().text!r}")
    widths: dict = {}
    box = _Layouter(measurer, widths).node(tree, size)
    shifted = box.shifted(0.0, -box.ink_top)
    lay = Layout(shifted.runs, shifted.strokes, box.w, box.h, -box.ink_top, size, notation)
    lay._widths = widths
    return lay


__all__ = ["Measurer", "PilMeasurer", "Run", "Stroke", "Layout", "typeset", "TokenError"]
