"""A worksheet and its answer key from a ``questions.json`` payload — the
quiz player's unified schema (docgen.questions) rendered back onto paper.

The maths worksheet (docgen.maths_worksheet) is built from VERIFIED
questions and writes that schema beside its .docx; the Kit Translator
(catalogue.translate, T1.5) translates the WORDS of that artifact and needs
the same two documents again in the language — without the model, without
the verifier (which reads English back), from the questions alone. This
module is that renderer: pure docx from a payload, the same primitives,
the same student/teacher split, RTL through the builder's language context
(``<w:rtl/>`` on every run).

Sections follow the sheet the questions came from — fill-in, true/false,
one matching table, then the diagram questions (each with its picture, a
data URL in the payload) and the problems with writing lines. The key
prints a problem's ``working`` lines when the payload carries them and
its answer otherwise.
"""

from __future__ import annotations

import base64
import random
import re
from pathlib import Path

from docgen import docx_builder as dx

_BLANK_RE = re.compile(r"_{2,}")
_LINES_PROBLEM = 4
_LINES_FIGURE = 5
_FIGURE_WIDTH_MM = 70.0
# the player's schema prefixes a data URL; anything else is not a picture
_DATA_URL_RE = re.compile(r"^data:image/(?:png|jpeg);base64,(.+)$", re.DOTALL)


def _s(value) -> str:
    return str(value if value is not None else "").strip()


def section_of(q: dict) -> str:
    """Which section of the sheet a question belongs to: ``figure`` (it has
    a picture), ``fill`` (a blank to fill, no working), ``tf``, ``match``,
    ``mcq``, else ``problem`` (a worked question with writing lines)."""
    t = _s(q.get("type"))
    if _s(q.get("image")):
        return "figure"
    if t == "true_false":
        return "tf"
    if t == "match":
        return "match"
    if t == "mcq":
        return "mcq"
    if t == "fill_blank" and not q.get("working") and _BLANK_RE.search(_s(q.get("prompt"))):
        return "fill"
    return "problem"


def _png(image: str) -> bytes | None:
    m = _DATA_URL_RE.match(_s(image))
    if not m:
        return None
    try:
        return base64.b64decode(m.group(1))
    except (ValueError, TypeError):
        return None


def _answer(q: dict, language: str) -> str:
    t = _s(q.get("type"))
    if t == "true_false":
        return dx.tf_word(bool(q.get("answer")), language)
    accept = [_s(x) for x in (q.get("accept") or []) if _s(x)] if isinstance(q.get("accept"), list) else []
    text = _s(q.get("answer"))
    return " / ".join([text] + accept) if accept and text else (text or " / ".join(accept))


def _key_text(q: dict, language: str) -> str:
    working = [_s(w) for w in (q.get("working") or []) if _s(w)] if isinstance(q.get("working"), list) else []
    return "\n".join(working) if working else _answer(q, language)


def write_sheet(out_dir: Path, payload: dict, *, language: str, template: str | None = None,
                header_lines=None, subtitle: str = "", rng: random.Random | None = None) -> list[Path]:
    """``[student_path, key_path]`` for ``payload`` (``{title, instructions,
    questions}``) in ``language``. ``rng`` decides each matching table's
    Column B order ONCE for both documents (a fixed seed by default, so a
    render is reproducible)."""
    rng = rng or random.Random(0)
    language = (language or "en").strip().lower()
    questions = [q for q in (payload.get("questions") or []) if isinstance(q, dict) and _s(q.get("prompt"))]
    title = _s(payload.get("title")) or dx._t("doc_worksheet", language)
    instructions = _s(payload.get("instructions")) or dx._t("ws_instructions", language)
    letters = dx.letters(language)

    doc = dx.new_doc(title, subtitle, template=template, kind="worksheet", language=language, header_lines=header_lines)
    dx.instructions(doc, instructions)
    key_doc = dx.new_doc(f"{title} — {dx._t('answer_key', language)}", subtitle, template=template,
                         kind="worksheet", language=language, header_lines=header_lines)
    dx.para(key_doc, dx._t("teacher_only", language), italic=True)

    by: dict[str, list[dict]] = {}
    for q in questions:
        by.setdefault(section_of(q), []).append(q)

    si = 0
    if by.get("fill"):
        name = dx.section_heading(language, si, "sec_fill_blank")
        dx.heading(doc, name, 1)
        dx.numbered(doc, [dx.strip_leading_number(_s(q.get("prompt"))) for q in by["fill"]])
        dx.answer_section(key_doc, name, [_answer(q, language) for q in by["fill"]])
        si += 1
    if by.get("tf"):
        name = dx.section_heading(language, si, "sec_true_false")
        dx.heading(doc, name, 1)
        dx.numbered(doc, [dx.strip_leading_number(_s(q.get("prompt"))) for q in by["tf"]])
        dx.answer_section(key_doc, name, [_answer(q, language) for q in by["tf"]])
        si += 1
    if by.get("mcq"):
        name = dx.section_heading(language, si, "sec_short")
        dx.heading(doc, name, 1)
        keys: list[str] = []
        for i, q in enumerate(by["mcq"], 1):
            dx.question(doc, f"{i}. {dx.strip_leading_number(_s(q.get('prompt')))}", first=(i == 1))
            options = [_s(o) for o in (q.get("options") or []) if _s(o)]
            for k, opt in enumerate(options[: len(letters)]):
                dx.para(doc, f"      {letters[k]}) {opt}")
            answer = _s(q.get("answer"))
            keys.append(letters[options.index(answer)] if answer in options else answer)
        dx.answer_section(key_doc, name, keys)
        si += 1
    for q in by.get("match") or []:
        name = dx.section_heading(language, si, "sec_match")
        dx.heading(doc, name, 1)
        dx.para(doc, dx.strip_leading_number(_s(q.get("prompt"))) or dx.match_instruction(language), italic=True)
        pairs = [p for p in (q.get("pairs") or []) if isinstance(p, dict) and _s(p.get("left"))]
        order = list(range(len(pairs)))
        rng.shuffle(order)   # Column B shuffled, so it is a real matching task
        rows = [[f"{i + 1}. {_s(pairs[i].get('left'))}", f"{letters[i]}. {_s(pairs[order[i]].get('right'))}"]
                for i in range(len(pairs))]
        dx.table(doc, [dx.column_label(language, 0), dx.column_label(language, 1)], rows)
        dx.answer_section(key_doc, name, [letters[order.index(i)] for i in range(len(pairs))])
        si += 1

    number = 0
    if by.get("figure"):
        name = dx._t("sec_figures", language)
        dx.heading(doc, name, 1)
        keys = []
        for q in by["figure"]:
            number += 1
            dx.question(doc, f"{number}. {dx.strip_leading_number(_s(q.get('prompt')))}", first=(number == 1))
            png = _png(q.get("image"))
            if png:
                dx.picture(doc, png, _FIGURE_WIDTH_MM)
            dx.writing_lines(doc, _LINES_FIGURE)
            keys.append(_key_text(q, language))
        dx.answer_section(key_doc, name, keys)
    if by.get("problem"):
        name = dx._t("ws_practice", language)
        dx.heading(doc, name, 1)
        keys = []
        for q in by["problem"]:
            number += 1
            dx.question(doc, f"{number}. {dx.strip_leading_number(_s(q.get('prompt')))}", first=(number == 1))
            dx.writing_lines(doc, _LINES_PROBLEM)
            keys.append(_key_text(q, language))
        dx.answer_section(key_doc, name, keys)

    out_dir = Path(out_dir)
    sheet_path = dx.save(doc, out_dir / "worksheet.docx")
    key_path = dx.save(key_doc, out_dir / "worksheet_answer_key.docx")
    return [sheet_path, key_path]


__all__ = ["write_sheet", "section_of"]
