"""Categorical maths facts, verified against a CLOSED table — the half of a
primary maths chapter SymPy cannot see.

Production, 2026-10-07 (Cambridge Primary Mathematics 5, "2D shape and
pattern", generation 8861d1e6): every worksheet question was a naming or
classifying one — "Identify the number of sides of a triangle", "Name the
regular polygon with 5 sides" — whose working is words. The step-equivalence
verifier read ``triangle`` as a symbol and "proved" ``triangle != 3``, every
question was rejected, and the worksheet failed deterministically; the
sibling test paper verified one computational item and printed a near-empty
page. Retrying cannot help a chapter whose answers are names.

A fact item is a fill-in-the-blank, a true/false statement or a matching
exercise that DECLARES the fact it tests: ``{relation, subject, value}`` —
("polygon_sides", "pentagon", "5"). The table decides whether the fact
holds, and the printed text is checked against the declaration: the blank's
answer is one end of the fact, the question names the other end and only
it, a true/false answer is the table's truth, and every row of a matching
exercise has exactly one right-hand partner. A fact the table does not know
— a relation outside the vocabulary, a shape it has no row for, a "not" in
the sentence — is never printed (fail over degrade: a printed answer is a
proved answer, here proved by lookup instead of algebra).

The vocabulary is English. A document in another language could state one
fact in its sentence and declare another, and nothing here can read the
sentence to tell, so a non-English document never takes this path
(``supported_language``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Optional

# ── the table ────────────────────────────────────────────────────────────────

_POLYGON_SIDES = {
    "triangle": 3, "quadrilateral": 4, "pentagon": 5, "hexagon": 6, "heptagon": 7, "octagon": 8,
    "nonagon": 9, "decagon": 10, "dodecagon": 12,
    # the named quadrilaterals: four sides and four vertices, whatever else
    "square": 4, "rectangle": 4, "rhombus": 4, "parallelogram": 4, "trapezium": 4, "trapezoid": 4, "kite": 4,
}
# aliases fold onto a canonical name before any lookup
_ALIASES = {
    "septagon": "heptagon", "enneagon": "nonagon", "oblong": "rectangle",
    "square based pyramid": "square-based pyramid", "square pyramid": "square-based pyramid",
    "tetrahedron": "triangular pyramid", "triangle-based pyramid": "triangular pyramid",
    "triangle based pyramid": "triangular pyramid",
}
# Lines of symmetry: only shapes whose count is not a matter of the drawing.
# A rectangle is the non-square one (the count every primary book gives); a
# polygon named without "regular" has no fixed count and is not here.
_SYMMETRY_LINES = {
    "equilateral triangle": 3, "isosceles triangle": 1, "scalene triangle": 0, "square": 4, "rectangle": 2,
    "rhombus": 2, "parallelogram": 0, "kite": 1, "regular pentagon": 5, "regular hexagon": 6,
    "regular heptagon": 7, "regular octagon": 8, "regular nonagon": 9, "regular decagon": 10,
    "regular dodecagon": 12, "regular triangle": 3, "regular quadrilateral": 4,
}
# (faces, edges, vertices) of the polyhedra; curved solids are left out on
# purpose — whether a cylinder "has 3 faces" depends on the book.
_SOLIDS = {
    "cube": (6, 12, 8), "cuboid": (6, 12, 8), "triangular prism": (5, 9, 6), "pentagonal prism": (7, 15, 10),
    "hexagonal prism": (8, 18, 12), "octagonal prism": (10, 24, 16), "square-based pyramid": (5, 8, 5),
    "triangular pyramid": (4, 6, 4), "pentagonal pyramid": (6, 10, 6), "hexagonal pyramid": (7, 12, 7),
    "octahedron": (8, 12, 6),
}

_NUMBER_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}

_ANGLE_WORDS = {"acute": "acute", "right": "right", "obtuse": "obtuse", "straight": "straight", "reflex": "reflex",
                "full turn": "full turn", "whole turn": "full turn", "complete turn": "full turn"}
_TRIANGLE_SIDE_WORDS = {"equilateral": "equilateral", "isosceles": "isosceles", "scalene": "scalene"}
_TRIANGLE_ANGLE_WORDS = {"acute-angled": "acute", "acute angled": "acute", "acute": "acute",
                         "right-angled": "right", "right angled": "right", "right": "right",
                         "obtuse-angled": "obtuse", "obtuse angled": "obtuse", "obtuse": "obtuse"}
_PROBABILITY_WORDS = {
    "impossible": "impossible", "no chance": "impossible",
    "unlikely": "unlikely", "poor chance": "unlikely",
    "even chance": "even chance", "evens": "even chance", "equally likely": "even chance",
    "fifty-fifty": "even chance", "50-50": "even chance", "50/50": "even chance",
    "likely": "likely", "good chance": "likely",
    "certain": "certain",
}
_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
_COMPASS_WORDS = {"north": "N", "north-east": "NE", "northeast": "NE", "north east": "NE", "east": "E",
                  "south-east": "SE", "southeast": "SE", "south east": "SE", "south": "S",
                  "south-west": "SW", "southwest": "SW", "south west": "SW", "west": "W",
                  "north-west": "NW", "northwest": "NW", "north west": "NW"}


def _word_re(words) -> re.Pattern:
    """Longest first, whole words, so "south-east" is never read as "south"
    and "acute-angled" never as "acute"."""
    alts = sorted({re.escape(w) for w in words}, key=len, reverse=True)
    return re.compile(r"(?<![\w-])(" + "|".join(alts) + r")(?![\w-])", re.IGNORECASE)


# a full stop after a number ends the sentence ("a probability of 1/2.");
# one followed by a digit is a decimal point, and a colon is a clock time
_NUM_RE = re.compile(r"(?<![\w.:/])(\d+(?:\.\d+)?(?:\s*/\s*\d+)?)\s*(%)?(?![\w:/]|\.\d|:\d)")
_NUMWORD_RE = _word_re(_NUMBER_WORDS)


def _norm(text) -> str:
    s = str(text or "").replace("°", " ").replace("º", " ")
    s = s.replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")
    return " ".join(s.split())


def _numbers(text: str, *, words: bool = True) -> list[Fraction]:
    """Every number written in the text, in order: 5, 2.5, 3/4, 50%, "five"."""
    s = _norm(text)
    out: list[tuple[int, Fraction]] = []
    for m in _NUM_RE.finditer(s):
        raw = m.group(1).replace(" ", "")
        try:
            v = Fraction(raw)
        except (ValueError, ZeroDivisionError):
            continue
        if m.group(2):
            v = v / 100
        out.append((m.start(), v))
    if words:
        out += [(m.start(), Fraction(_NUMBER_WORDS[m.group(1).lower()])) for m in _NUMWORD_RE.finditer(s)]
    return [v for _p, v in sorted(out, key=lambda t: t[0])]


def _names(text: str, table: dict, aliases: dict | None = None) -> list[str]:
    """The table's names found in the text, canonical, in order."""
    s = _norm(text).lower()
    keys = set(table) | set(aliases or {})
    return [(aliases or {}).get(m.group(1).lower(), m.group(1).lower()) for m in _word_re(keys).finditer(s)]


# ── relations: how a subject and a value are read, and what the table says ─

@dataclass(frozen=True)
class Relation:
    name: str
    describe: str                                   # for the prompt
    subjects_in: Callable[[str], list]               # canonical subjects named in a text
    values_in: Callable[[str], list]                 # canonical values named in a text
    lookup: Callable[[object], Optional[object]]     # the table: subject -> value (None: unknown)
    compound_subject: bool = False                   # a blank may not ask for the subject


def _one_of(words: dict):
    """A word-valued reader: every synonym folds onto the table's word
    ("right-angled" -> right, "no chance" -> impossible)."""
    def read(text: str) -> list:
        return [words[n] for n in _names(text, words)]
    return read


def _ints(text: str) -> list:
    return [int(v) for v in _numbers(text) if v.denominator == 1]


def _solid(i: int) -> Callable:
    return lambda s: _SOLIDS[s][i] if s in _SOLIDS else None


# A word an English sentence might use for the shape that is NOT the
# subject: "triangle" inside "triangular prism" is the prism.
_POLYGON_TABLE = {**_POLYGON_SIDES}
_SHAPE_ALIASES = {k: v for k, v in _ALIASES.items() if v in _POLYGON_TABLE}


def _polygons(text: str) -> list:
    # a solid's name contains a polygon's adjective, never its noun, but
    # "square-based pyramid" does contain "square": read solids out first
    s = _word_re(set(_SOLIDS) | {k for k, v in _ALIASES.items() if v in _SOLIDS}).sub(" ", _norm(text))
    return _names(s, _POLYGON_TABLE, _SHAPE_ALIASES)


def _symmetry_shapes(text: str) -> list:
    s = _norm(text).lower()
    found = _names(s, _SYMMETRY_LINES)
    # "a regular pentagon" is in the table; a bare "pentagon" is a polygon
    # with no fixed count, and must not be read as a match for one
    rest = _word_re(set(_SYMMETRY_LINES)).sub(" ", s)
    stray = [p for p in _names(rest, _POLYGON_TABLE, _SHAPE_ALIASES)]
    return found + [("?" + p) for p in stray]


def _solids(text: str) -> list:
    return _names(text, _SOLIDS, {k: v for k, v in _ALIASES.items() if v in _SOLIDS})


def _angle_type(deg) -> Optional[str]:
    d = Fraction(deg)
    if d <= 0 or d > 360:
        return None
    if d < 90:
        return "acute"
    if d == 90:
        return "right"
    if d < 180:
        return "obtuse"
    if d == 180:
        return "straight"
    if d < 360:
        return "reflex"
    return "full turn"


def _triple(text: str) -> list:
    ns = _numbers(text, words=False)
    return [tuple(sorted(ns))] if len(ns) == 3 else ([] if not ns else ["?"])


def _triangle_by_sides(t) -> Optional[str]:
    if not isinstance(t, tuple) or len(t) != 3 or min(t) <= 0 or t[0] + t[1] <= t[2]:
        return None   # not a triangle at all
    distinct = len(set(t))
    return {1: "equilateral", 2: "isosceles", 3: "scalene"}[distinct]


def _triangle_by_angles(t) -> Optional[str]:
    if not isinstance(t, tuple) or len(t) != 3 or min(t) <= 0 or sum(t) != 180:
        return None
    big = max(t)
    return "acute" if big < 90 else ("right" if big == 90 else "obtuse")


def _probability(text: str) -> list:
    s = _word_re(_PROBABILITY_WORDS).sub(" ", _norm(text))   # "50-50" is a word here, not two numbers
    return [v for v in _numbers(s, words=False)]


def _probability_word(p) -> Optional[str]:
    p = Fraction(p)
    if p < 0 or p > 1:
        return None
    if p == 0:
        return "impossible"
    if p == 1:
        return "certain"
    if p == Fraction(1, 2):
        return "even chance"
    return "unlikely" if p < Fraction(1, 2) else "likely"


_TURN_WORDS = {"quarter turn": 90, "quarter-turn": 90, "half turn": 180, "half-turn": 180,
               "three-quarter turn": 270, "three quarter turn": 270, "three-quarter-turn": 270,
               "right angle": 90, "full turn": 360, "whole turn": 360}
_DIRECTION_RE = re.compile(r"(?<![\w-])(anti-?clockwise|counter-?clockwise|clockwise)(?![\w-])", re.IGNORECASE)
_COMPASS_RE = _word_re(_COMPASS_WORDS)


def _compass_points(text: str) -> list[tuple[int, str]]:
    return [(m.start(), _COMPASS_WORDS[m.group(1).lower()]) for m in _COMPASS_RE.finditer(_norm(text))]


def _compass_subjects(text: str) -> list:
    """(start, degrees, "cw"|"acw") — the start is the FIRST point named."""
    s = _norm(text)
    points = _compass_points(s)
    dirs = [m.group(1).lower() for m in _DIRECTION_RE.finditer(s)]
    turns = [v for w, v in _TURN_WORDS.items() if re.search(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", s, re.I)]
    s_nums = _word_re(_TURN_WORDS).sub(" ", s)
    turns += [int(v) for v in _numbers(s_nums, words=False) if v.denominator == 1]
    if not points or len(set(dirs)) != 1 or len(set(turns)) != 1:
        return ["?"] if (points or dirs or turns) else []
    d = "cw" if dirs[0] == "clockwise" else "acw"
    return [(points[0][1], turns[0], d)]


def _compass_values(text: str) -> list:
    """The points named AFTER the start: where the turn ends."""
    return [p for _pos, p in _compass_points(text)[1:]]


def _compass_turn(sub) -> Optional[str]:
    if not isinstance(sub, tuple):
        return None
    start, deg, d = sub
    if deg % 45 or deg <= 0 or deg > 360:
        return None
    steps = deg // 45 * (1 if d == "cw" else -1)
    return _COMPASS[(_COMPASS.index(start) + steps) % 8]


_TIME12_RE = re.compile(r"(?<![\d:.])(\d{1,2})(?:[:.](\d{2}))?\s*([ap])\.?\s*m\b\.?", re.IGNORECASE)
_TIME24_RE = re.compile(r"(?<![\d:.])(\d{1,2})[:.](\d{2})(?!\d)(?!\s*[ap]\.?\s*m\b)", re.IGNORECASE)


def _times12(text: str) -> list:
    out = []
    for m in _TIME12_RE.finditer(_norm(text)):
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if 1 <= h <= 12 and mi < 60:
            out.append((h, mi, m.group(3).lower()))
        else:
            out.append("?")
    return out


def _times24(text: str) -> list:
    out = []
    for m in _TIME24_RE.finditer(_norm(text)):
        h, mi = int(m.group(1)), int(m.group(2))
        out.append(f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else "?")
    return out


def _to_24h(t) -> Optional[str]:
    if not isinstance(t, tuple):
        return None
    h, mi, ap = t
    h = h % 12 + (12 if ap == "p" else 0)
    return f"{h:02d}:{mi:02d}"


RELATIONS: dict[str, Relation] = {r.name: r for r in (
    Relation("polygon_sides", "a 2D shape's number of sides (subject: the shape's name; value: a number)",
             _polygons, _ints, lambda s: _POLYGON_SIDES.get(s)),
    Relation("polygon_vertices", "a 2D shape's number of vertices/corners (subject: the shape; value: a number)",
             _polygons, _ints, lambda s: _POLYGON_SIDES.get(s)),
    Relation("symmetry_lines", "a 2D shape's lines of symmetry (subject: 'regular pentagon', 'rectangle', "
             "'isosceles triangle'...; value: a number)",
             _symmetry_shapes, _ints, lambda s: _SYMMETRY_LINES.get(s)),
    Relation("solid_faces", "a 3D shape's number of faces (subject: 'cube', 'triangular prism', "
             "'square-based pyramid'...; value: a number)", _solids, _ints, _solid(0)),
    Relation("solid_edges", "a 3D shape's number of edges", _solids, _ints, _solid(1)),
    Relation("solid_vertices", "a 3D shape's number of vertices", _solids, _ints, _solid(2)),
    Relation("angle_type", "an angle's type from its size in degrees (subject: the size, e.g. '135'; value: "
             "acute / right / obtuse / straight / reflex / full turn)",
             lambda t: [v for v in _numbers(t, words=False)], _one_of(_ANGLE_WORDS), _angle_type),
    Relation("triangle_by_sides", "a triangle's type from its three side lengths (subject: '5, 5, 8'; value: "
             "equilateral / isosceles / scalene)",
             _triple, _one_of(_TRIANGLE_SIDE_WORDS), _triangle_by_sides, compound_subject=True),
    Relation("triangle_by_angles", "a triangle's type from its three angles (subject: '30, 60, 90'; value: "
             "acute-angled / right-angled / obtuse-angled)",
             _triple, _one_of(_TRIANGLE_ANGLE_WORDS), _triangle_by_angles, compound_subject=True),
    Relation("probability_word", "the word for a probability (subject: a probability as a fraction, decimal or "
             "percentage; value: impossible / unlikely / even chance / likely / certain)",
             _probability, _one_of(_PROBABILITY_WORDS), _probability_word),
    Relation("compass_turn", "where you face after a turn (subject: 'north, 90, clockwise' — start point, "
             "degrees or quarter/half turn, clockwise or anticlockwise; value: the compass point you face)",
             _compass_subjects, _compass_values, _compass_turn, compound_subject=True),
    Relation("time_24h", "a 12-hour clock time as a 24-hour time (subject: '3:45 pm'; value: '15:45')",
             _times12, _times24, _to_24h),
)}


def _canonical(values: list) -> Optional[object]:
    """The one thing a short field names, or None when it names none or
    several (a field is ONE subject or ONE value)."""
    distinct = list(dict.fromkeys(values))
    if len(distinct) != 1 or distinct[0] == "?" or (isinstance(distinct[0], str) and distinct[0].startswith("?")):
        return None
    return distinct[0]


# A sentence that negates can state the opposite of its declared fact while
# every word of the fact is present; nothing here parses English well enough
# to follow a negation, so a negated sentence is never printed. The
# probability words that contain "no" are read out first.
_NEGATION_RE = re.compile(r"(?<![\w-])(not|never|no|none|neither|nor|cannot|isn't|aren't|doesn't|don't|"
                          r"hasn't|haven't|won't|can't|without)(?![\w-])", re.IGNORECASE)
_BLANK_RE = re.compile(r"_{2,}|\.{4,}|\[\s*\]")


def _negated(text: str) -> bool:
    s = _word_re(_PROBABILITY_WORDS).sub(" ", _norm(text))
    return bool(_NEGATION_RE.search(s))


# ── items ────────────────────────────────────────────────────────────────────

FORMATS = ("fill_blank", "true_false", "match")


@dataclass
class FactItem:
    format: str
    difficulty: int = 1
    q: str = ""                     # the question / statement (match: may be empty)
    answer: str = ""                # fill_blank: the word or number; true_false: "true"/"false"
    relation: str = ""
    subject: str = ""
    value: str = ""
    pairs: list[dict] = field(default_factory=list)   # match: [{left, right, subject, value}]

    @property
    def truth(self) -> Optional[bool]:
        a = str(self.answer).strip().lower()
        return True if a in ("true", "t", "yes") else False if a in ("false", "f", "no") else None


def parse_item(data) -> FactItem:
    d = data if isinstance(data, dict) else {}
    try:
        diff = max(1, min(4, int(d.get("difficulty") or 1)))
    except (TypeError, ValueError):
        diff = 1
    ans = d.get("answer")
    if isinstance(ans, bool):
        ans = "true" if ans else "false"
    pairs = [p for p in (d.get("pairs") or []) if isinstance(p, dict)]
    return FactItem(format=str(d.get("format") or "").strip(), difficulty=diff, q=_norm(d.get("q")),
                    answer=_norm(ans), relation=str(d.get("relation") or "").strip(),
                    subject=_norm(d.get("subject")), value=_norm(d.get("value")),
                    pairs=[{k: _norm(p.get(k)) for k in ("left", "right", "subject", "value")} for p in pairs])


@dataclass
class FactCheck:
    ok: bool
    detail: str


def _declared(rel: Relation, subject: str, value: str) -> tuple[Optional[object], Optional[object], str]:
    """(canonical subject, canonical value, problem) of a declared fact."""
    s = _canonical(rel.subjects_in(subject))
    if s is None:
        return None, None, f"the subject {subject!r} is not one {rel.name} knows"
    v = _canonical(rel.values_in(value) if rel.name != "compass_turn" else
                   [p for _pos, p in _compass_points(value)])
    if v is None:
        return s, None, f"the value {value!r} is not one {rel.name} reads"
    return s, v, ""


def _fill_blank(item: FactItem, rel: Relation) -> FactCheck:
    s, v, why = _declared(rel, item.subject, item.value)
    if why:
        return FactCheck(False, why)
    truth = rel.lookup(s)
    if truth is None:
        return FactCheck(False, f"{rel.name} has no entry for {item.subject!r}")
    if truth != v:
        return FactCheck(False, f"the declared fact is false: {rel.name}({item.subject}) is {truth}, not {v}")
    if not _BLANK_RE.search(item.q):
        return FactCheck(False, "a fill-in-the-blank needs a blank (____)")
    q_wo_blank = _BLANK_RE.sub(" ", item.q)
    q_subjects, q_values = rel.subjects_in(q_wo_blank), rel.values_in(q_wo_blank)
    answer_values = rel.values_in(item.answer) if rel.name != "compass_turn" else \
        [p for _pos, p in _compass_points(item.answer)]
    if _canonical(answer_values) == v:
        # the blank is the value: the question names the subject, only it,
        # and does not give the value away
        if _canonical(q_subjects) != s:
            return FactCheck(False, f"the question must name {item.subject!r} and nothing else like it")
        if v in q_values:
            return FactCheck(False, "the question gives its own answer away")
        return FactCheck(True, f"{rel.name}({item.subject}) = {item.value}")
    if not rel.compound_subject and _canonical(rel.subjects_in(item.answer)) == s:
        if _canonical(q_values) != v:
            return FactCheck(False, f"the question must state {item.value!r} and nothing else like it")
        if s in q_subjects:
            return FactCheck(False, "the question gives its own answer away")
        # a value may belong to more than one subject (4 sides: square,
        # rectangle, kite...); the blank then has no single answer
        others = [k for k in _subjects_with(rel, v) if k != s]
        if others:
            return FactCheck(False, f"{item.value!r} is also {', '.join(sorted(map(str, others))[:3])}: "
                                    "the blank has more than one answer")
        return FactCheck(True, f"{rel.name}({item.subject}) = {item.value}")
    return FactCheck(False, f"the answer {item.answer!r} is neither end of the declared fact")


def _subjects_with(rel: Relation, value) -> list:
    """Every subject the table maps to ``value`` (named relations only)."""
    tables = {"polygon_sides": _POLYGON_SIDES, "polygon_vertices": _POLYGON_SIDES,
              "symmetry_lines": _SYMMETRY_LINES}
    if rel.name in tables:
        return [k for k, n in tables[rel.name].items() if n == value]
    if rel.name.startswith("solid_"):
        return [k for k in _SOLIDS if rel.lookup(k) == value]
    if rel.name == "time_24h":
        return []   # one 24-hour time is one 12-hour time
    return ["(many)"]   # an angle type, a probability word: many subjects share it


def _true_false(item: FactItem, rel: Relation) -> FactCheck:
    s, v, why = _declared(rel, item.subject, item.value)
    if why:
        return FactCheck(False, why)
    truth = rel.lookup(s)
    if truth is None:
        return FactCheck(False, f"{rel.name} has no entry for {item.subject!r}")
    if item.truth is None:
        return FactCheck(False, f"a true/false answer must be true or false, not {item.answer!r}")
    if _canonical(rel.subjects_in(item.q)) != s or _canonical(rel.values_in(item.q)) != v:
        return FactCheck(False, "the statement must say exactly the declared fact")
    if item.truth != (truth == v):
        return FactCheck(False, f"the statement is {'true' if truth == v else 'false'} "
                                f"({rel.name}({item.subject}) is {truth}), the answer says {item.answer}")
    return FactCheck(True, f"{rel.name}({item.subject}) = {truth}: the statement is {item.answer}")


def _match(item: FactItem, rel: Relation) -> FactCheck:
    if rel.name == "compass_turn":
        # a row would carry a whole turn on its left; not a matching exercise
        return FactCheck(False, f"{rel.name} does not make a matching exercise")
    if not 3 <= len(item.pairs) <= 6:
        return FactCheck(False, f"a matching exercise has 3 to 6 pairs, not {len(item.pairs)}")
    lefts, rights = [], []
    for p in item.pairs:
        s, v, why = _declared(rel, p["subject"], p["value"])
        if why:
            return FactCheck(False, why)
        if rel.lookup(s) != v:
            return FactCheck(False, f"the pair {p['left']!r} - {p['right']!r} is false")
        ls, rv = _canonical(rel.subjects_in(p["left"])), _canonical(rel.values_in(p["right"]))
        if ls != s or rv != v:
            return FactCheck(False, f"the pair {p['left']!r} - {p['right']!r} does not print its declared fact")
        lefts.append(s)
        rights.append(v)
    if len(set(lefts)) != len(lefts) or len(set(rights)) != len(rights):
        # two lefts with the same partner (a square and a kite both have 4
        # sides): the column no longer has one right answer per row
        return FactCheck(False, "every row must have its own partner: two rows share one")
    return FactCheck(True, f"{len(lefts)} pairs, each partner unique")


def verify_item(item: FactItem) -> FactCheck:
    if item.format not in FORMATS:
        return FactCheck(False, f"unknown format {item.format!r}")
    rel = RELATIONS.get(item.relation)
    if rel is None:
        return FactCheck(False, f"{item.relation!r} is not a relation the table knows")
    texts = [item.q] + [p.get("left", "") + " " + p.get("right", "") for p in item.pairs]
    if any(_negated(t) for t in texts):
        return FactCheck(False, "a negated sentence cannot be checked against its fact")
    if item.format == "fill_blank":
        return _fill_blank(item, rel)
    if item.format == "true_false":
        return _true_false(item, rel)
    return _match(item, rel)


def supported_language(language: str | None) -> bool:
    return (language or "en").split("-")[0].lower() == "en"


__all__ = ["RELATIONS", "FORMATS", "FactItem", "FactCheck", "parse_item", "verify_item", "supported_language"]
