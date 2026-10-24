"""Deterministic checks on the mathematics a slide shows.

The Director may RE-EXPRESS a worked step — "Substitute the radius into the
area formula. Area = (22 divided by 7 times 7 squared) divided by 2" becomes
"A = ½ × 22/7 × 7²" — because a slide is for notation and the prose is for
the notes. It may not change the mathematics. Two checks hold it to that:

1. NO NEW NUMBERS. Every number on the slide's line appears in the source
   example (its problem, its steps, its answer) or is the product of an
   arithmetic chain on the same line that the second check verifies. A
   rewritten step that quietly turns 6² = 36 into 6² = 12 introduces "12".
2. EVERY CHAIN HOLDS. "C = (22 ÷ 7) × 14 = 44" is split at "=" and each side
   that is pure arithmetic is evaluated, with π taken as the example's own
   declared value (22/7, 3.14) or as π itself; a side with π in it is
   accepted under either. Rounding is allowed to the precision the right
   side is written in ("113 (3 s.f.)" for 113.04 passes; 113.4 fails).

A line that fails either check is not repaired — the renderer falls back to
the source's own line for that step. Wrong-but-pretty is the one outcome a
worked example cannot have.
"""

from __future__ import annotations

import math
import re

_NUM = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?")


def numbers(text: str) -> set[str]:
    """Numbers by VALUE as written: "01" is 1 (a card badge), "44.0" is 44."""
    out = set()
    for m in _NUM.findall(text or ""):
        n = m.rstrip("0").rstrip(".") if "." in m else m
        n = n.lstrip("0") or "0"
        if n.startswith("."):
            n = "0" + n
        out.add(n)
    return out


def _sup(s: str) -> str:
    return (s.replace("²", "^2").replace("³", "^3").replace("½", "(1/2)").replace("¼", "(1/4)")
            .replace("¾", "(3/4)"))


_SAFE = re.compile(r"^[0-9π.+\-*/^() ]+$")


def _to_python(side: str, pi: float) -> str | None:
    s = _sup(side)
    s = s.replace("×", "*").replace("·", "*").replace("÷", "/").replace("−", "-").replace("–", "-")
    s = re.sub(r"\bpi\b", "π", s, flags=re.I)
    s = re.sub(r"(?<=[\dπ)\s])(?:mm|cm|km|m)(?:\^[23])?(?![A-Za-z])", "", s)   # units
    s = s.strip()
    if not s or not _SAFE.match(s):
        return None
    # implicit multiplication: 2π, 3(4), )(, π(
    s = re.sub(r"(\d|\))\s*(π|\()", r"\1*\2", s)
    s = re.sub(r"(π|\))\s*(\d|\()", r"\1*\2", s)
    s = s.replace("π", f"({pi!r})").replace("^", "**")
    return s


def evaluate(side: str, pi: float) -> float | None:
    expr = _to_python(side, pi)
    if expr is None:
        return None
    try:
        v = eval(expr, {"__builtins__": {}}, {})          # noqa: S307 — whitelist-checked above
    except Exception:  # noqa: BLE001
        return None
    return float(v) if isinstance(v, (int, float)) else None


def _decimals(text: str) -> int:
    m = re.findall(r"\d+\.(\d+)", text)
    return max((len(x) for x in m), default=0)


def _close(a: float, b: float, written: str) -> bool:
    if a == b:
        return True
    # tolerance: half a unit in the last written place, or 3 s.f. rounding
    tol = 0.5 * 10 ** (-_decimals(written)) + 1e-9
    if abs(a - b) <= tol:
        return True
    if b != 0 and abs(a - b) / abs(b) <= 0.005 and re.search(r"s\.?f|≈|approx", written, re.I):
        return True
    return abs(a - b) <= max(abs(a), abs(b)) * 1e-9


def chain_ok(line: str, pi_value: str = "") -> tuple[bool, str]:
    """Does every '=' / '≈' link between two evaluable sides hold?"""
    text = re.sub(r"\(.*?s\.?f\.?\)", "", line)                 # "(3 s.f.)"
    sides = [s.strip() for s in re.split(r"=|≈", text) if s.strip()]
    pis = [math.pi]
    if pi_value:
        try:
            num, _, den = pi_value.partition("/")
            pis.append(float(num) / float(den) if den else float(num))
        except ValueError:
            pass
    vals = []
    for s in sides:
        # strip a leading name ("C", "Area", "r²") — it is not arithmetic
        v = [evaluate(s, p) for p in pis]
        vals.append((s, v))
    prev = None
    for s, v in vals:
        if all(x is None for x in v):
            prev = None
            continue
        if prev is not None:
            ps, pv = prev
            if not any(a is not None and b is not None and (_close(a, b, s) or _close(b, a, ps))
                       for a, b in zip(pv, v)):
                return False, f"'{ps}' ≠ '{s}'"
        prev = (s, v)
    return True, ""


def line_ok(line: str, source_text: str, pi_value: str = "") -> tuple[bool, str]:
    ok, why = chain_ok(line, pi_value)
    if not ok:
        return False, f"arithmetic: {why}"
    extra = numbers(line) - numbers(source_text) - {"1", "2"}
    if extra:
        # A number not in the source is acceptable only as the evaluated
        # result of a chain on this same line (chain_ok has already held it).
        # Only the chain's RESULT (its last side, when an earlier side is
        # arithmetic it was checked against) may be new; an operand never
        # may — "A = π × 7² = 49π" for a 6 cm radius is a held chain about
        # the wrong circle.
        sides = [s for s in re.split(r"=|≈", re.sub(r"\(.*?s\.?f\.?\)", "", line)) if s.strip()]
        if len(sides) >= 2 and any(evaluate(s, math.pi) is not None for s in sides[:-1]):
            last = sides[-1]
            pure = re.sub(r"(?<=[\dπ\s])(?:mm|cm|km|m)(?:²|\^[23])?", "", last)
            if not (numbers(pure) & numbers(" ".join(sides[:-1])) - numbers(source_text)):
                extra -= numbers(last)
    if extra:
        return False, f"numbers not in the source: {', '.join(sorted(extra))}"
    return True, ""


# Clauses end at ". " / ": " / ";" — never at a bare "." — because a decimal
# point is a full stop to a naive splitter: "A = 3.14 × 36 = 113.04" became
# the two "steps" "A = 3" and "14 × 36 = 113" on the first live deck.
_CLAUSES = re.compile(r"(?<!\d)\.(?=\s|$)|\.\s|:\s|;")
_EQ_PART = re.compile(r"=")


def concise(line: str) -> str:
    """A prose step reduced to its working, for the slide: the clause(s) that
    carry an '=' (spelled-out operators turned into symbols), else the first
    clause, short. Deterministic; used when the model's line fails a check."""
    t = " ".join(line.split())
    t = re.sub(r"\bdivided by\b", "÷", t, flags=re.I)
    t = re.sub(r"\btimes\b", "×", t, flags=re.I)
    t = re.sub(r"\bplus\b", "+", t, flags=re.I)
    t = re.sub(r"\bminus\b", "−", t, flags=re.I)
    t = re.sub(r"\bpi\b", "π", t, flags=re.I)
    t = re.sub(r"(\w+) squared", r"\1²", t)
    t = re.sub(r"\bequals\b", "=", t, flags=re.I)
    clauses = [c.strip(" ,") for c in _CLAUSES.split(t) if c and c.strip(" ,")]
    eqs = [c for c in clauses if _EQ_PART.search(c) and len(c) <= 90]
    if eqs:
        # "…which gives 22 ÷ 7 × 7, which equals 22 cm" — keep from the first
        # symbol-bearing word, drop the prose lead-in
        out = eqs[-1] if len(eqs) == 1 else "; ".join(eqs[-2:])
        m = re.search(r"\b[A-Za-z]{1,9}\s*=|(?<![\w.])[\d(π]", out)
        out = out[m.start():].strip() if m and m.start() < 70 else out
        return out.rstrip(".")
    first = clauses[0] if clauses else t
    words = first.split()
    return " ".join(words[:12]) + ("…" if len(words) > 12 else "")
