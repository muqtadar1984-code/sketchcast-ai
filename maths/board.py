"""The algebra board: a verified worked example -> scenes for the engine.

Two columns and a pinned card (founder direction 2026-09-24). The question
stays at the top. Working is written one line at a time down the left, the
current line bright and every earlier line dimmed; a short note sits to the
right of each new line ("subtract 5 from both sides") with a leader to the
term it acted on in the line above. The method card is pinned top-right for
the whole lesson and the step in use is highlighted as the example proceeds.

Deterministic on purpose: the model decides the mathematics and the words,
this module decides where everything goes. Nothing here is a model call, and
nothing here is science-flavoured — no illustrations, no sketch lexicon.

Geometry (1280x720, world = pixels) respects what the engine already owns:
the teacher and student avatars at the bottom corners, and the speech
captions across the middle band (whiteboard._STREAM_PANEL). The working
column ends above the avatars; a student's bubble may cover the lower rows
while the student speaks, as it does on every science board.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from agent5_slides.slide_builder import _font
from maths.schema import Lesson, Line, Mistake, Step, WorkedExample
from maths.speech import speakable_maths
from maths.tokens import TokenError, normalise
from maths.typeset import Layout, typeset
from shared.text_clean import strip_ssml
from spike.scene_engine.render import _CAVEAT_MATHS, _HAND_SIZE_COMP, _hand_face
from spike.scene_engine.schema import WORLD_W
from spike.scene_engine.whiteboard import build_whiteboard_scene

# ── geometry ────────────────────────────────────────────────────────────
Q_AT = (60.0, 40.0)
Q_SIZE = 40.0
LINE_X = 60.0
LINE_SIZE = 36.0
FIRST_ROW_Y = 118.0
ROW_GAP = 20.0
# under the question, room for the first line's note (they sit in the gap
# above their line): a word problem's 14 px put the note on its second
# line (TEXT_OVERLAP q1+n3, the demo's consecutive-numbers example)
PROBLEM_GAP = 34.0
MIN_ROW_H = LINE_SIZE * 1.08   # ink height alone packs "x = 5" under "3x = 15"
WORK_BOTTOM = 452.0          # the avatars start at ~460
NOTE_SIZE = 23.0
NOTE_GAP = 34.0
NOTE_RIGHT = 850.0           # the card starts at 880
CARD_X = 880.0
CARD_Y = 44.0
CARD_W = 372.0
CARD_TITLE_SIZE = 24.0
CARD_LINE_SIZE = 21.0
CARD_PITCH = 38.0
# five lines at the full pitch ran the card's frame into the tallest speech
# bubble (top at ~274); the pitch tightens so the frame stays above it
CARD_PITCH_5 = 33.0
CARD_BOTTOM_MAX = 270.0
DIM = 0.42
MAX_NOTE_CHARS = 44
# the model's notation is capped at 200 chars, a board line at this
MAX_LINE_CHARS = 60
_WORD_RE = re.compile(r"\w+", re.UNICODE)


# ── measuring with the renderer's faces ─────────────────────────────────


class _Measurer:
    """The same faces render._math_font picks, so the board's rows are laid
    out on the sizes the frames will be drawn with."""

    def __init__(self):
        self._cache: dict = {}

    def _font(self, size: float, text: str):
        size = max(6, int(size) // 2 * 2)
        hand = all(ord(c) < 0x7F or c in _CAVEAT_MATHS for c in text)
        key = (hand, size)
        if key not in self._cache:
            f = _hand_face(int(size * _HAND_SIZE_COMP)) if hand else None
            self._cache[key] = f if f is not None else _font(False, size, text)
        return self._cache[key]

    def measure(self, text: str, size: float):
        f = self._font(size, text)
        try:
            w = float(f.getlength(text))
            asc, _desc = f.getmetrics()
            x0, y0, x1, y1 = f.getbbox(text)
            return (w, float(asc), float(y0) - asc, float(y1) - asc)
        except Exception:  # noqa: BLE001
            return (len(text) * size * 0.55, size * 0.8, -size * 0.7, size * 0.2)

    def text_width(self, text: str, size: float) -> float:
        return self.measure(text, size)[0]


_M = _Measurer()


def _layout(expr: str, size: float) -> Optional[Layout]:
    try:
        return typeset(expr, size, _M)
    except (TokenError, Exception):  # noqa: BLE001
        return None


# ── speech helpers ──────────────────────────────────────────────────────


_SIDES_RE = re.compile(r"\b([LR])\.?H\.?S(\.)?(?=\s*=\s*[LR]\.?H\.?S|\W|$)", re.I)
_SIDES_EQ_RE = re.compile(r"\b([LR])\.?H\.?S\.?\s*=\s*([LR])\.?H\.?S(\.)?(?=\W|$)", re.I)


def _side(letter: str, before: str = "") -> str:
    side = "left-hand side" if letter.upper() == "L" else "right-hand side"
    # "the LHS" already has its article
    return side if re.search(r"\bthe\s*$", before, re.I) else f"the {side}"


def _expand_sides(text: str) -> str:
    """"L.H.S." spoken and captioned as words: the caption splitter cut a
    recap sentence at the abbreviation's full stop ("...verify that L.H.S."
    | "equals R.H.S.")."""
    def eq(m):
        return (f"{_side(m.group(1), m.string[:m.start()])} equals {_side(m.group(2))}"
                f"{m.group(3) or ''}")

    def one(m):
        dot = m.group(2) or ""
        # keep a full stop that ends the sentence, drop the abbreviation's
        rest = m.string[m.end():]
        ends = not rest.strip() or rest.lstrip()[:1].isupper()
        return _side(m.group(1), m.string[:m.start()]) + (dot if dot and ends else "")

    text = _SIDES_EQ_RE.sub(eq, text)
    return _SIDES_RE.sub(one, text)


def say(text: str) -> str:
    """A spoken line as the voice should receive it: no SSML, notation in
    words, whitespace tidy."""
    return " ".join(speakable_maths(_expand_sides(strip_ssml(str(text or "")))).split())


def _cue(speech: str, narration: str, words: int = 5) -> Optional[dict]:
    """A cue phrase — the opening words of this spoken line, verbatim in the
    narration — or None to let the action follow the previous one."""
    toks = speech.split()
    for n in (words, 4, 3):
        phrase = " ".join(toks[:n]).strip(" ,.;:!?")
        if len(phrase) >= 8 and phrase.lower() in narration.lower():
            return {"phrase": phrase}
    return None


def _short(text: str, cap: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= cap else t[:cap - 1].rstrip() + "…"


# ── the method card ─────────────────────────────────────────────────────


def card_elements(method, present: bool) -> tuple[list[dict], list[dict]]:
    """The pinned card: a hand-drawn frame, its title and its lines. With
    ``present`` the card is on the board from t=0 (every scene after the
    concept); otherwise the returned actions write it in."""
    lines = list(method.steps)[:5]
    if not lines:
        return [], []
    h = 20 + CARD_TITLE_SIZE + 12 + _card_pitch(len(lines)) * len(lines) + 8
    x0, y0, x1, y1 = CARD_X - 16, CARD_Y - 12, CARD_X - 16 + CARD_W, CARD_Y - 12 + h
    els: list[dict] = [
        {"id": "card_box", "type": "shape", "shape": "path", "closed": True, "width": 2.6,
         "color": "muted", "points": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]},
        {"id": "card_title", "type": "text", "text": _short(method.title or "METHOD", 22),
         "role": "term", "color": "accent", "size": CARD_TITLE_SIZE, "at": [CARD_X, CARD_Y], "anchor": "lt"},
    ]
    for i, _line in enumerate(lines):
        text, y = _card_line(method, i)
        els.append({"id": f"card_{i}", "type": "text", "text": text,
                    "size": CARD_LINE_SIZE, "at": [CARD_X, y], "anchor": "lt"})
    acts: list[dict] = []
    if not present:
        acts.append({"verb": "draw", "target": "card_box", "at": {"frac": 0.5}})
        acts.append({"verb": "write", "target": "card_title"})
        for i in range(len(lines)):
            acts.append({"verb": "write", "target": f"card_{i}", "at": {"frac": min(0.92, 0.58 + 0.08 * i)}})
    return els, acts


def _card_pitch(n_lines: int) -> float:
    return CARD_PITCH if n_lines <= 4 else CARD_PITCH_5


def _card_line(method, i: int) -> tuple[str, float]:
    """The card's i-th line as written, and its top."""
    pitch = _card_pitch(len(list(method.steps)[:5]))
    return (_short(f"{i + 1}. {method.steps[i]}", 34), CARD_Y + CARD_TITLE_SIZE + 16 + pitch * i)


def _stem(w: str) -> str:
    return w.lower()[:5]


# Words that say nothing about WHICH method step is in use ("both sides").
_STOP = {"both", "sides", "side", "from", "each", "with", "then", "into", "this", "that", "term",
         "terms", "equation", "value", "number", "step", "same"}
# What a step's operation implies about the card line it belongs to: an
# operation that "divides" is the card's "divide"/"coefficient" line, one
# that moves a plain number is the "constants" line, and so on.
_RULES: list[tuple[re.Pattern, tuple[str, ...]]] = [
    (re.compile(r"\b(divid|multipl)", re.I), ("divid", "coeff", "multipl")),
    (re.compile(r"\b(subtract|add|move|collect|gather|bring|take)\w*\b[^a-z0-9]*\d*[a-z]\b", re.I),
     ("variab", "unknown", "letter")),
    (re.compile(r"\b(subtract|add|move|take)\w*\b[^a-z0-9]*\d+(?:\.\d+)?(?![a-z0-9])", re.I),
     ("const", "number")),
    (re.compile(r"\b(expand|bracket|simplif|collect|combine|like)", re.I), ("simplif", "expand", "bracket", "collect")),
    (re.compile(r"\b(factor)", re.I), ("factor",)),
    (re.compile(r"\b(check|substitut|verify)", re.I), ("check", "substitut", "verify")),
    (re.compile(r"\b(flip|revers|sign)", re.I), ("flip", "sign", "revers")),
]


def method_step_for(step: Step, method) -> Optional[int]:
    """Which card line this step is using. Rules on the operation's verb
    first (divide -> the 'divide by the coefficient' line), then the line
    sharing the most meaningful word stems with the step. None when
    nothing matches — no highlight beats a wrong one."""
    lines = [l.lower() for l in method.steps[:5]]
    op = f"{step.operation} {step.explanation}"
    for pat, keys in _RULES:
        if pat.search(op):
            for i, line in enumerate(lines):
                if any(k in line for k in keys):
                    return i
    words = {_stem(w) for w in _WORD_RE.findall(op) if len(w) > 3 and w.lower() not in _STOP}
    if not words:
        return None
    best, score = None, 0
    for i, line in enumerate(lines):
        lw = {_stem(w) for w in _WORD_RE.findall(line) if len(w) > 3 and w.lower() not in _STOP}
        n = len(words & lw)
        if n > score:
            best, score = i, n
    return best


# ── one worked example -> one scene ─────────────────────────────────────


@dataclass
class _Row:
    eid: str
    expr: str
    y: float
    lay: Layout
    state_index: int


@dataclass
class _Board:
    elements: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    rows: list[_Row] = field(default_factory=list)      # visible working lines
    annotations: list[str] = field(default_factory=list)  # notes and leaders on those lines
    wiped: bool = False                                    # the last state started a fresh column
    carried: list = field(default_factory=list)            # rows re-written after a wipe (the answer)
    top_y: float = FIRST_ROW_Y                             # under the question, where a wipe restarts
    next_y: float = FIRST_ROW_Y
    note_x: float = 0.0                                    # the notes' column since the last wipe
    n: int = 0
    state_no: int = 0
    highlight: Optional[tuple[int, str]] = None            # (card line, marker element) in use
    question: Optional["_Row"] = None      # the pinned problem, when it is notation

    def uid(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}{self.n}"


def _problem_elements(ex: WorkedExample, board: _Board, cue: Optional[dict]) -> None:
    """The pinned question: notation as a typeset title; a word problem as
    up to two short text lines with its equation(s) beneath."""
    problem = ex.problem or (ex.givens[0] if ex.givens else "")
    lay = _layout(problem, Q_SIZE) if problem else None
    if lay is not None and len(problem) <= MAX_LINE_CHARS:
        board.elements.append({"id": "q", "type": "math", "expr": problem, "at": list(Q_AT),
                               "size": Q_SIZE, "role": "title"})
        board.actions.append({"verb": "write", "target": "q", **({"at": cue} if cue else {})})
        board.next_y = max(FIRST_ROW_Y, Q_AT[1] + max(lay.h, Q_SIZE * 1.1) + PROBLEM_GAP)
        board.top_y = board.next_y
        board.question = _Row("q", problem, Q_AT[1], lay, -1)
        return
    # a word problem
    words = " ".join(problem.split())
    lines, cur = [], ""
    for w in words.split():
        if len(cur) + len(w) + 1 > 62 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    lines = lines[:2]
    if len(lines) == 2 and len(words) > len(lines[0]) + len(lines[1]) + 1:
        lines[1] = _short(lines[1], 60)
    y = Q_AT[1]
    for i, ln in enumerate(lines):
        board.elements.append({"id": f"q{i}", "type": "text", "text": ln, "role": "title", "size": 27,
                               "at": [Q_AT[0], y], "anchor": "lt"})
        board.actions.append({"verb": "write", "target": f"q{i}", **({"at": cue} if cue and i == 0 else {})})
        y += 46   # the handwriting face runs ~44 px tall at this size (TEXT_OVERLAP q0+q1)
    # a wipe restarts HERE, not at the notation-question row: the first line
    # after a wipe once sat on the word problem's second line (maths demo)
    board.next_y = y - 2 + PROBLEM_GAP   # the last line's ink ends ~2 px above y
    board.top_y = board.next_y


def _add_state(board: _Board, state: list[str], cue: Optional[dict], color: str = "ink",
               dim_previous: bool = True, carry: Optional[list[str]] = None) -> list[_Row]:
    """Write the lines of a state on the next rows; dim what came before;
    wipe the column first when the state would not fit. ``carry`` names
    lines (the final answer) to write again at the top after such a wipe,
    the way a teacher keeps the result on the board while checking it —
    the answer underline once landed where the wiped row had been."""
    lays = [(x, _layout(x, LINE_SIZE)) for x in state[:4]]
    needed = sum(max(l.h if l else LINE_SIZE, MIN_ROW_H) + ROW_GAP for _x, l in lays)
    board.wiped = False
    board.carried = []
    if board.next_y + needed > WORK_BOTTOM and board.rows:
        # the notes and leaders go with the lines they annotate — a wipe
        # that left them behind wrote the next example's notes over them
        # (production, 2026-09-24: TEXT_OVERLAP n2+n15)
        grp = board.uid("wipe")
        board.elements.append({"id": grp, "type": "group",
                               "children": [r.eid for r in board.rows] + list(board.annotations)})
        board.actions.append({"verb": "erase", "target": grp, "duration": 0.9, **({"at": cue} if cue else {})})
        board.rows = []
        board.annotations = []
        board.next_y = board.top_y
        board.note_x = 0.0
        board.wiped = True
        cue = None  # the writes follow the wipe
        if carry:
            board.carried = _add_state(board, carry, None, dim_previous=False)
            board.wiped = True
    elif dim_previous and board.rows:
        prev = [r for r in board.rows if r.state_index == board.state_no - 1]
        for r in prev:
            board.actions.append({"verb": "fade", "target": r.eid, "to": DIM, "duration": 0.3,
                                  **({"at": cue} if cue else {})})
    rows: list[_Row] = []
    for i, (expr, lay) in enumerate(lays):
        eid = board.uid("w")
        y = board.next_y
        if lay is None:
            board.elements.append({"id": eid, "type": "text", "text": _short(expr, 60), "size": 28,
                                   "at": [LINE_X, y], "anchor": "lt", "color": color})
            h = 34.0
            lay = _layout("0", LINE_SIZE)  # a stand-in for geometry
        else:
            board.elements.append({"id": eid, "type": "math", "expr": expr, "at": [LINE_X, y],
                                   "size": LINE_SIZE, "color": color})
            h = max(lay.h, MIN_ROW_H)
        board.actions.append({"verb": "write", "target": eid, **({"at": cue} if cue and i == 0 else {})})
        rows.append(_Row(eid, expr, y, lay, board.state_no))
        board.next_y = y + h + ROW_GAP
    board.rows.extend(rows)
    board.state_no += 1
    return rows


_TERM_RE = re.compile(r"-?\d+(?:\.\d+)?[a-zA-Z]?|\b[a-zA-Z]\b|[a-zA-Z]\^\d")


def _term_in(op_text: str, row: _Row) -> Optional[tuple[str, tuple]]:
    """A term named in the operation that exists in this row's line, and its
    box: the leader's target ("subtract 5 from both sides" -> the 5). A
    plain number prefers the CONSTANT term over a coefficient (the 2 of
    "2x - 2 = 8", not the 2 of 2x); a term like 3x is matched as written."""
    for tok in _TERM_RE.findall(op_text):
        if tok.lower() in ("a",):
            continue
        hits = row.lay.find_all(tok)
        if not hits:
            continue
        standalone = [h for h in hits if h[1]]
        box = (standalone or hits)[0][0]
        return tok, box
    return None


def _add_note(board: _Board, row: _Row, note: str, target: Optional[_Row], op_text: str) -> None:
    """The step's note, written where a teacher writes it: to the right, in
    the gap between the line it came from and the line it produced, so its
    leader reaches the term above without crossing the new working."""
    note = _short(note, MAX_NOTE_CHARS)
    if not note:
        return
    right = row.lay.w
    # in the gap above the new line: between it and the line it came from,
    # or just above it when that line is gone (a wipe) or was never one (a
    # check, a mistake). A note ON a line collided with the next line's
    # between-note (TEXT_OVERLAP n14+n16).
    y = row.y - ROW_GAP * 0.5
    if target is not None and target.y < row.y:
        right = max(right, target.lay.w if target.eid != "q" else 0.0)
        y = (target.y + target.lay.h + row.y) / 2.0
    # one column for the notes of a column of working — they zigzagged with
    # each line's width
    x = max(LINE_X + right + NOTE_GAP, board.note_x)
    board.note_x = x
    size = NOTE_SIZE
    w = _M.text_width(note, size)
    while x + w > NOTE_RIGHT and size > 18:
        size -= 1.5
        w = _M.text_width(note, size)
    if x + w > NOTE_RIGHT:
        note = _short(note, max(12, int(len(note) * (NOTE_RIGHT - x) / w)))
        w = _M.text_width(note, size)
    nid = board.uid("n")
    board.elements.append({"id": nid, "type": "text", "text": note, "size": size, "color": "muted",
                           "role": "caption", "at": [x, y], "anchor": "lm"})
    board.actions.append({"verb": "write", "target": nid})
    board.annotations.append(nid)
    if target is None:
        return
    found = _term_in(op_text, target)
    if found is None:
        return
    term, box = found
    ox = LINE_X if target.eid != "q" else Q_AT[0]
    oy = target.y
    aid = board.uid("ar")
    # the head is placed from the layout directly (the renderer's `sub`
    # would take the FIRST match, which may be the coefficient)
    board.elements.append({"id": aid, "type": "arrow", "color": "muted", "width": 2.2, "curve": 0,
                           "tail": {"el": nid, "edge": "left", "dx": -4},
                           "head": [ox + (box[0] + box[2]) / 2, oy + box[3] + 3]})
    board.actions.append({"verb": "draw", "target": aid, "duration": 0.6})
    board.annotations.append(aid)


HL_WIDTH = 26.0


def _highlight_method(board: _Board, step: Step, method, cue: Optional[dict], has_card: bool) -> None:
    """Move the card's highlight to the line this step uses. A marker
    ELEMENT, not the highlight verb: the verb's decoration never leaves, so
    by example 4 every line was yellow at once (maths demo, 2026-09-24)."""
    if not has_card:
        return
    k = method_step_for(step, method)
    if k is None or (board.highlight is not None and board.highlight[0] == k):
        return
    text, y = _card_line(method, k)
    w = _M.text_width(text, CARD_LINE_SIZE)
    ym = y + CARD_LINE_SIZE * 0.62
    hid = board.uid("hl")
    board.elements.append({"id": hid, "type": "shape", "shape": "line", "width": HL_WIDTH, "color": "marker",
                           "points": [[CARD_X - 8, ym], [CARD_X + w + 10, ym]]})
    at = {"at": cue} if cue else {}
    if board.highlight is not None:
        board.actions.append({"verb": "fade", "target": board.highlight[1], "to": 0.0, "duration": 0.3, **at})
    board.actions.append({"verb": "draw", "target": hid, "duration": 0.7, **at})
    board.highlight = (k, hid)


def example_scene(ex: WorkedExample, method, seg_id: str, *, has_card: bool = True) -> tuple[dict, list[Line]]:
    """One worked example as a scene plus its dialogue. The scene carries
    the card (present from t=0) and writes the question, the working, the
    notes, the answer underline and, when there is one, the mistake."""
    lines: list[Line] = []

    def teacher(t: str):
        s = say(t)
        if s:
            lines.append(Line(who="teacher", line=s))

    def student(t: str):
        s = say(t)
        if s:
            lines.append(Line(who="student", line=s))

    teacher(ex.intro_speech or f"Here is {ex.label or 'the next example'}: {ex.problem}.")
    student(ex.student_question)
    step_speech: list[str] = []
    for st in ex.steps:
        s = say(st.speech) or say(st.operation)
        step_speech.append(s)
        if s:
            lines.append(Line(who="teacher", line=s))
        student(st.student)
    teacher(ex.answer_speech)
    mistake_speech = say(ex.common_mistake.speech) if ex.common_mistake else ""
    if mistake_speech:
        lines.append(Line(who="teacher", line=mistake_speech))
    narration = " ".join(l.line for l in lines)

    board = _Board()
    els, _acts = card_elements(method, present=True)
    board.elements.extend(els)
    _problem_elements(ex, board, _cue(lines[0].line if lines else "", narration))

    prev_state_rows: list[_Row] = [board.question] if board.question else []
    last_rows: list[_Row] = []
    for k, st in enumerate(ex.steps):
        cue = _cue(step_speech[k], narration)
        if not st.after:
            continue
        if st.kind == "check":
            rows = _add_state(board, st.after[:1], cue, color="muted", dim_previous=False,
                              carry=[r.expr for r in last_rows])
            if board.carried:
                last_rows = board.carried
            if rows:
                _add_note(board, rows[0], "check", None, "")
            continue
        _highlight_method(board, st, method, cue, has_card)
        rows = _add_state(board, st.after, cue)
        if rows:
            note = st.note if st.kind == "transform" else (st.note or "set up")
            # after a wipe the line this step came from is gone: the note
            # sits beside the new line and no leader points at nothing
            target = prev_state_rows[0] if prev_state_rows and not board.wiped else None
            _add_note(board, rows[0], note, target, f"{st.operation} {st.explanation}")
        prev_state_rows = rows
        last_rows = rows or last_rows
    if last_rows:
        acue = _cue(say(ex.answer_speech), narration)
        for r in last_rows:
            board.actions.append({"verb": "underline", "target": r.eid, **({"at": acue} if acue else {})})
            acue = None
    m = ex.common_mistake
    if m is not None and m.wrong_state and mistake_speech:
        mcue = _cue(mistake_speech, narration)
        rows = _add_state(board, m.wrong_state[:1], mcue, color="muted", dim_previous=False,
                          carry=[r.expr for r in last_rows])
        for r in board.carried:
            # the answer written again after the wipe keeps its underline
            board.actions.append({"verb": "underline", "target": r.eid})
        if rows:
            r = rows[0]
            sid = board.uid("strike")
            board.elements.append({"id": sid, "type": "shape", "shape": "line", "width": 3.6, "color": "accent",
                                   "points": [[LINE_X - 6, r.y + r.lay.h * 0.55], [LINE_X + r.lay.w + 6, r.y + r.lay.h * 0.45]]})
            board.actions.append({"verb": "draw", "target": sid, "duration": 0.5})
            _add_note(board, r, f"not allowed: {_short(m.why_wrong or m.operation, 30)}", None, "")
    scene = {"id": f"mx_{seg_id}", "compiled": True, "scene_type": "worked_example",
             "narration": narration, "elements": board.elements, "actions": board.actions,
             "min_hold": 1.0}
    return scene, lines


# ── the other cards ─────────────────────────────────────────────────────


def _dialogue_lines(items: list[Line]) -> list[Line]:
    return [Line(who=l.who, line=say(l.line)) for l in items if say(l.line)]


def hook_segment(lesson: Lesson, seg_id: str, avatars: dict | None) -> dict:
    lines = _dialogue_lines(lesson.hook) or [Line(line=f"Today we learn {lesson.topic}.")]
    seg = _segment(seg_id, "hook", lines, heading=_short(lesson.topic, 60), points=[])
    seg["scene"] = build_whiteboard_scene(seg, avatars=avatars, sketches=False)
    return seg


def concept_segment(lesson: Lesson, seg_id: str) -> dict:
    lines = _dialogue_lines(lesson.concept) or [Line(line="Here is the method we will use.")]
    points = [_short(p, 64) for p in lesson.concept_points][:3]
    seg = _segment(seg_id, "explore", lines, heading=_short(lesson.topic, 60), points=points)
    narration = seg["text"]
    els: list[dict] = [{"id": "wb_h", "type": "text", "text": _short(lesson.topic, 60), "role": "title",
                        "size": 40, "at": [60, 44], "anchor": "lt"}]
    acts: list[dict] = [{"verb": "write", "target": "wb_h"}, {"verb": "underline", "target": "wb_h"}]
    for i, p in enumerate(points):
        els.append({"id": f"wb_d{i}", "type": "shape", "shape": "line", "width": 4.0, "color": "accent",
                    "points": [[64, 150 + 58 * i], [92, 146 + 58 * i]]})
        els.append({"id": f"wb_p{i}", "type": "text", "text": p, "size": 27, "role": "caption",
                    "at": [110, 124 + 58 * i], "anchor": "lt"})
        acts.append({"verb": "draw", "target": f"wb_d{i}", "at": {"frac": min(0.45, 0.08 + 0.14 * i)}})
        acts.append({"verb": "write", "target": f"wb_p{i}"})
    cels, cacts = card_elements(lesson.method, present=False)
    els += cels
    acts += cacts
    seg["scene"] = {"id": f"mc_{seg_id}", "compiled": True, "scene_type": "generic", "narration": narration,
                    "elements": els, "actions": acts}
    return seg


def recap_segment(lesson: Lesson, seg_id: str) -> dict:
    lines = _dialogue_lines(lesson.recap) or [Line(line="Let us recap the method.")]
    points = [_short(p, 64) for p in lesson.misconceptions][:3]
    # the points name mistakes ("Forgetting to change the sign"), so the
    # heading says so — "Remember" over them read as an instruction to forget
    seg = _segment(seg_id, "synthesis", lines, heading="Common mistakes", points=points)
    els: list[dict] = [{"id": "wb_h", "type": "text", "text": "Common mistakes", "role": "title", "size": 40,
                        "at": [60, 44], "anchor": "lt"}]
    acts: list[dict] = [{"verb": "write", "target": "wb_h"}]
    for i, p in enumerate(points):
        els.append({"id": f"wb_d{i}", "type": "shape", "shape": "line", "width": 4.0, "color": "accent",
                    "points": [[64, 150 + 58 * i], [92, 146 + 58 * i]]})
        els.append({"id": f"wb_p{i}", "type": "text", "text": p, "size": 27, "role": "caption",
                    "at": [110, 124 + 58 * i], "anchor": "lt"})
        acts.append({"verb": "draw", "target": f"wb_d{i}", "at": {"frac": min(0.75, 0.15 + 0.22 * i)}})
        acts.append({"verb": "write", "target": f"wb_p{i}"})
    cels, _ = card_elements(lesson.method, present=True)
    els += cels
    acts.append({"verb": "circle", "target": "card_box", "at": {"frac": 0.9}})
    seg["scene"] = {"id": f"mr_{seg_id}", "compiled": True, "scene_type": "generic", "narration": seg["text"],
                    "elements": els, "actions": acts}
    return seg


def try_it_segment(lesson: Lesson, seg_id: str) -> Optional[dict]:
    t = lesson.try_it
    if not t.problem:
        return None
    lines = [Line(line=say(t.speech) or f"Try this one yourself: {t.problem}. Pause the video and work it out.")]
    seg = _segment(seg_id, "question_hook", lines, heading="Try it", points=[t.problem], pause=True)
    els: list[dict] = [{"id": "wb_h", "type": "text", "text": "Try it", "role": "title", "size": 44,
                        "at": [WORLD_W / 2, 120], "anchor": "mt"}]
    acts: list[dict] = [{"verb": "write", "target": "wb_h"}]
    if _layout(t.problem, 44) is not None and len(t.problem) <= MAX_LINE_CHARS:
        els.append({"id": "q", "type": "math", "expr": t.problem, "at": [WORLD_W / 2, 220], "size": 44,
                    "anchor": "mt", "role": "title"})
    else:
        els.append({"id": "q", "type": "text", "text": _short(t.problem, 70), "size": 28,
                    "at": [WORLD_W / 2, 220], "anchor": "mt"})
    acts.append({"verb": "write", "target": "q", "at": {"frac": 0.25}})
    els.append({"id": "pause", "type": "text", "text": "Pause the video and try it", "size": 24, "color": "muted",
                "at": [WORLD_W / 2, 330], "anchor": "mt"})
    acts.append({"verb": "write", "target": "pause", "at": {"frac": 0.7}})
    acts.append({"verb": "underline", "target": "wb_h"})
    seg["scene"] = {"id": f"mt_{seg_id}", "compiled": True, "scene_type": "generic", "narration": seg["text"],
                    "elements": els, "actions": acts}
    return seg


def example_segment(ex: WorkedExample, lesson: Lesson, seg_id: str) -> dict:
    scene, lines = example_scene(ex, lesson.method, seg_id, has_card=bool(lesson.method.steps))
    heading = _short(f"{ex.label or 'Example'}: {ex.problem}", 60)
    points = [_short(x, 64) for st in ex.steps for x in st.after[:1]][:4]
    seg = _segment(seg_id, "explore", lines, heading=heading, points=points)
    seg["scene"] = scene
    return seg


def _segment(seg_id: str, seg_type: str, lines: list[Line], *, heading: str, points: list[str],
             pause: bool = False) -> dict:
    text = " ".join(l.line for l in lines if l.line)
    # ALWAYS dialogue, student line or not: per-line audio gives every
    # caption a measured start and keeps the avatars on the board. A
    # teacher-only example fell to the single-voice stream, where phrase
    # cues in maths narration (numbers repeat) let two captions overlap and
    # the avatars vanished (maths demo, 2026-09-24).
    dialogue = [{"who": l.who, "line": l.line} for l in lines if l.line]
    return {"segment_id": seg_id, "type": seg_type, "text": text, "elevenlabs_text": text,
            "dialogue": dialogue or None, "slide_heading": heading,
            "slide_points": points, "pause_for_question": pause, "no_sketches": True,
            "estimated_duration_seconds": max(5, int(round(len(text) / 14.0)))}


def compile_lesson(lesson: Lesson, avatars: dict | None = None) -> list[dict]:
    """The whole lesson as script segments, in the blueprint's order."""
    segs: list[dict] = [hook_segment(lesson, "s001", avatars), concept_segment(lesson, "s002")]
    n = 3
    for ex in lesson.examples:
        segs.append(example_segment(ex, lesson, f"s{n:03d}"))
        n += 1
    segs.append(recap_segment(lesson, f"s{n:03d}"))
    n += 1
    t = try_it_segment(lesson, f"s{n:03d}")
    if t is not None:
        segs.append(t)
    return segs


__all__ = ["say", "example_scene", "example_segment", "hook_segment", "concept_segment", "recap_segment",
           "try_it_segment", "compile_lesson", "card_elements", "method_step_for"]
