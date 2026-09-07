"""Question-bank worksheet → TWO editable .docx files (student sheet + answer
key) from ALREADY-AUTHORED ``topic_questions`` rows. No model call.

Catalogue Phase 3 (decision 9): a composed ``question_sets`` row names the
approved bank items in order; the worker renders them here through the same
docx_builder primitives the textbook worksheet uses, so a composed set looks
like every other SketchCast document — curriculum block under the subtitle
(decision 10), workbook style, one localized letter sequence for MCQ options,
match tables and the key.

Layout rules:
  * one section per item type present, in ``TYPE_ORDER`` (mcq, true_false,
    fill_blank, match, assertion_reason, short_answer, long_answer, numerical,
    diagram_label), each on its OWN page — an explicit page break before every
    section after the first, so a teacher can hand out or photocopy a type;
  * MCQ / assertion-reason options A–D (``letters(lang)``: abjad for Arabic
    script); match pairs as a two-column table whose Column B is shuffled by
    the SET's ``random.Random(seed)`` — the same seed the composer used, so a
    re-render of the set is the same paper;
  * subjective items get ruled writing lines sized from ``est_seconds`` and
    ``marks`` (``answer_lines``): a 5-mark, 8-minute answer gets room, a
    2-mark check gets a few lines;
  * a total-marks line under the header;
  * the answer key (separate file, student/teacher split of 2026-08-18) lists
    the ANSWER, the marking scheme and the explanation per item — never the
    options or pairs again, so a leaked key exposes only what a key must.

``write_set`` returns ``[student_path, key_path]`` like every split kind.
"""

from __future__ import annotations

import logging
import random
from pathlib import Path

from docgen import docx_builder as dx

logger = logging.getLogger("worker")

TYPE_ORDER = ("mcq", "true_false", "fill_blank", "match", "assertion_reason",
              "short_answer", "long_answer", "numerical", "diagram_label")
SUBJECTIVE_TYPES = frozenset({"short_answer", "long_answer", "numerical", "diagram_label"})
# The section heading string per type (docgen.strings keys).
SECTION_KEY = {
    "mcq": "sec_mcq", "true_false": "sec_true_false", "fill_blank": "sec_fill_blank",
    "match": "sec_match", "assertion_reason": "sec_assertion_reason",
    "short_answer": "sec_short", "long_answer": "sec_long",
    "numerical": "sec_numerical", "diagram_label": "sec_diagram_label",
}
KEYS = ("A", "B", "C", "D")
MIN_LINES, MAX_LINES = 2, 16
# One ruled line holds roughly forty seconds of a learner's handwriting.
SECONDS_PER_LINE = 40.0
_BLANK = "________________"


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _s(value) -> str:
    return str(value).strip() if value is not None else ""


def answer_lines(item: dict) -> int:
    """Ruled lines for a subjective item: the larger of the time budget
    (``est_seconds`` / 40 s a line) and two lines per mark, clamped to
    [2, 16] — a 5-mark answer with a 30-second estimate still gets ten
    lines, and a 20-minute estimate does not print a page of rules."""
    marks = max(1, _int(item.get("marks"), 2))
    secs = max(0, _int(item.get("est_seconds"), 120))
    n = max(secs / SECONDS_PER_LINE, 2.0 * marks)
    return max(MIN_LINES, min(MAX_LINES, int(round(n))))


def group_items(items: list[dict]) -> list[tuple[str, list[dict]]]:
    """``[(item_type, [items…])]`` in TYPE_ORDER, empty types skipped, each
    type's items in the order given (the set's order)."""
    by_type: dict[str, list[dict]] = {}
    for it in items:
        t = _s(it.get("item_type"))
        if t in SECTION_KEY:
            by_type.setdefault(t, []).append(it)
    return [(t, by_type[t]) for t in TYPE_ORDER if t in by_type]


def _answer(item: dict) -> dict:
    a = item.get("answer")
    return a if isinstance(a, dict) else {}


def _options(item: dict) -> list[dict]:
    opts = item.get("options")
    if isinstance(opts, list):
        return [o for o in opts if isinstance(o, dict) and _s(o.get("text"))]
    return []


def _pairs(item: dict) -> list[dict]:
    opts = item.get("options") if isinstance(item.get("options"), dict) else {}
    pairs = opts.get("pairs") if isinstance(opts.get("pairs"), list) else _answer(item).get("pairs")
    return [p for p in (pairs or []) if isinstance(p, dict) and _s(p.get("left")) and _s(p.get("right"))]


def _labels(item: dict) -> list[dict]:
    labels = _answer(item).get("labels")
    return [lb for lb in (labels or []) if isinstance(lb, dict) and _s(lb.get("label"))]


def _letter(key: str, letters: list[str]) -> str:
    """The document's letter for an option key A–D (abjad in Arabic script)."""
    k = _s(key).upper()[:1]
    i = KEYS.index(k) if k in KEYS else -1
    return letters[i] if 0 <= i < len(letters) else (k or "?")


def _scheme_text(item: dict) -> str:
    out = []
    for pt in (item.get("marking_scheme") or []):
        if isinstance(pt, dict) and _s(pt.get("point")):
            out.append(f"{_s(pt.get('point'))} ({_int(pt.get('marks'), 1)})")
    return "; ".join(out)


def answer_text(item: dict, language: str, letters: list[str], order: list[int] | None = None) -> str:
    """The key's one-line answer per type. MCQ / assertion-reason give the
    LETTER only (the option text is student-facing); match gives the letter
    mapping under the sheet's shuffle ``order``."""
    t = _s(item.get("item_type"))
    a = _answer(item)
    if t in ("mcq", "assertion_reason"):
        return _letter(a.get("key"), letters)
    if t == "true_false":
        return dx.tf_word(bool(a.get("value")), language)
    if t == "fill_blank":
        accept = [_s(x) for x in (a.get("accept") or []) if _s(x)]
        return " / ".join([_s(a.get("text"))] + accept) if accept else _s(a.get("text"))
    if t == "match":
        pairs = _pairs(item)
        order = order or list(range(len(pairs)))
        return ", ".join(f"{i + 1} → {letters[order.index(i)]}" for i in range(len(pairs)))
    if t == "numerical":
        value = a.get("value")
        text = f"{value:g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else _s(value)
        unit = _s(a.get("unit"))
        tol = a.get("tolerance")
        text = f"{text} {unit}".strip()
        if isinstance(tol, (int, float)) and tol:
            text += f" (±{tol:g})"
        return text
    if t == "diagram_label":
        return "; ".join(f"{i + 1} {_s(lb.get('label'))}" for i, lb in enumerate(_labels(item)))
    return _s(a.get("text"))


def _stem(item: dict) -> str:
    return dx.strip_leading_number(_s(item.get("stem")))


def _q(doc, i: int, item: dict) -> None:
    dx.question(doc, f"{i}. {_stem(item)}   [{max(1, _int(item.get('marks'), 1))}]", first=(i == 1))


def _render_section(doc, t: str, items: list[dict], language: str, letters: list[str],
                    orders: dict[int, list[int]]) -> None:
    if t in ("mcq", "assertion_reason"):
        for i, it in enumerate(items, 1):
            _q(doc, i, it)
            for opt in _options(it)[:4]:
                dx.para(doc, f"      {_letter(opt.get('key'), letters)}) {_s(opt.get('text'))}")
    elif t == "true_false":
        cue = dx._t("tf_box_cue", language).format(t=dx.tf_word(True, language), f=dx.tf_word(False, language))
        dx.para(doc, cue, italic=True)
        for i, it in enumerate(items, 1):
            _q(doc, i, it)
    elif t == "fill_blank":
        for i, it in enumerate(items, 1):
            _q(doc, i, it)
    elif t == "match":
        dx.para(doc, dx.match_instruction(language), italic=True)
        for i, it in enumerate(items, 1):
            _q(doc, i, it)
            pairs = _pairs(it)
            order = orders[id(it)]
            rows = [[f"{k + 1}. {_s(p.get('left'))}", f"{letters[k]}. {_s(pairs[order[k]].get('right'))}"]
                    for k, p in enumerate(pairs)]
            dx.table(doc, [dx.column_label(language, 0), dx.column_label(language, 1)], rows)
    elif t == "diagram_label":
        for i, it in enumerate(items, 1):
            _q(doc, i, it)
            for k, _lb in enumerate(_labels(it), 1):
                dx.para(doc, f"      {k}. {_BLANK}")
    else:  # short_answer, long_answer, numerical — ruled working space
        for i, it in enumerate(items, 1):
            _q(doc, i, it)
            dx.writing_lines(doc, answer_lines(it))


def total_marks(items: list[dict]) -> int:
    return sum(max(1, _int(it.get("marks"), 1)) for it in items)


def write_set(out_dir: Path, title: str, subtitle: str, items: list[dict], *,
              header_lines, language: str, template, blueprint_name: str,
              rng: random.Random | None = None) -> list[Path]:
    """Render a composed set: ``[student_path, key_path]``.

    ``rng`` is the set's ``random.Random(seed)`` (the composer's); it decides
    the match tables' Column B order ONCE, before either document is built,
    so the sheet's table and the key's letters come from the same order. A
    caller without a seed gets a fixed one — a bank worksheet is never
    re-shuffled between renders."""
    rng = rng or random.Random(0)
    language = (language or "en").strip().lower()
    letters = dx.letters(language)
    sections = group_items(items)
    # One shuffle per match item, keyed by the item object, drawn in document
    # order so the sequence is a pure function of (items, seed).
    orders: dict[int, list[int]] = {}
    for t, group in sections:
        if t == "match":
            for it in group:
                order = list(range(len(_pairs(it))))
                rng.shuffle(order)
                orders[id(it)] = order

    meta = (f"{blueprint_name} · " if _s(blueprint_name) else "") + \
        f"{dx._t('n_questions', language).format(n=len(items))} · " \
        f"{dx._t('total_marks', language)}: {total_marks(items)}"

    # ── Student sheet ────────────────────────────────────────────────────────
    doc = dx.new_doc(title, subtitle, template=template, kind="worksheet",
                     language=language, header_lines=header_lines)
    dx.para(doc, meta, bold=True)
    for si, (t, group) in enumerate(sections):
        if si > 0:
            dx.page_break(doc)  # every type starts its own page
        dx.heading(doc, dx.section_heading(language, si, SECTION_KEY[t]), 1)
        _render_section(doc, t, group, language, letters, orders)

    # ── Answer key: a SEPARATE document ──────────────────────────────────────
    # Answers, marking schemes and explanations only — item N of section X
    # answers sheet item N of section X, so neither the stems nor the options
    # need repeating, and a leaked key exposes only what a key must.
    key_doc = dx.new_doc(f"{title} — {dx._t('answer_key', language)}", subtitle,
                         template=template, kind="worksheet", language=language,
                         header_lines=header_lines)
    dx.para(key_doc, dx._t("teacher_only", language), italic=True)
    dx.para(key_doc, meta, bold=True)
    for si, (t, group) in enumerate(sections):
        dx.heading(key_doc, dx.section_heading(language, si, SECTION_KEY[t]), 2)
        for i, it in enumerate(group, 1):
            dx.question(key_doc, f"{i}. {answer_text(it, language, letters, orders.get(id(it)))}", first=(i == 1))
            scheme = _scheme_text(it)
            if scheme and (t in SUBJECTIVE_TYPES or len(it.get("marking_scheme") or []) > 1):
                dx.labelled(key_doc, dx._t("marking_scheme", language), scheme)
            if _s(it.get("explanation")):
                dx.labelled(key_doc, dx._t("explanation", language), _s(it.get("explanation")))

    out_dir = Path(out_dir)
    sheet_path = dx.save(doc, out_dir / "worksheet.docx")
    key_path = dx.save(key_doc, out_dir / "worksheet_answer_key.docx")
    return [sheet_path, key_path]


__all__ = ["TYPE_ORDER", "SUBJECTIVE_TYPES", "SECTION_KEY", "answer_lines", "group_items", "answer_text",
           "total_marks", "write_set"]
