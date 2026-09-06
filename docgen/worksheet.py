"""Worksheet generator → TWO editable .docx files.

Student/teacher document split (founder direction 2026-08-18): answer keys stay
with the teacher/parent; students must never receive a document containing its
own answers. Like docgen/exam.py, build() returns
[worksheet_path, answer_key_path] — the student sheet carries questions only,
and the key is a SEPARATE workbook-styled document (tinted answer_section
panels) the worker uploads under its own artifact kind (answer_key_docx).

Reads `params`: {"num_questions": int, "difficulty": "easy|medium|hard"}
(`include_answer_key` is accepted but ignored: the key is always emitted as its
own file — its presence as a sibling artifact is the app's proof the split ran.)
A worksheet is practice-oriented (mixed short questions with working space),
distinct from the formal exam paper — but, like the exam paper, questions are
TYPED and GROUPED BY KIND (all fill-in-the-blanks together, etc.) and the
match-the-columns exercise renders as a two-column TABLE, not running text.
"""

from __future__ import annotations

import logging
import random
from pathlib import Path

from docgen import docx_builder as dx

logger = logging.getLogger("worker")

PROMPT = """You are a teacher creating a practice WORKSHEET from the chapter provided above.

Difficulty: {difficulty}

Create about {n} practice questions TOTAL, distributed across a MIX of kinds and
GROUPED BY KIND. A good spread: several fill-in-the-blanks, several true/false,
ONE match-the-columns exercise when the content has term↔definition pairs, and
several short-answer questions. Return ONLY valid JSON in exactly this shape:
{{
  "title": "Worksheet title",
  "instructions": "Short instructions for students",
  "fill_blank": [{{"q": "Sentence with a ____ blank", "answer": "word"}}],
  "true_false": [{{"statement": "...", "answer": true}}],
  "match_column": [{{"left": "term", "right": "matching description"}}],
  "short_answer": [{{"q": "...", "answer": "...", "work_space_lines": 2}}]
}}
Put each question under the kind it belongs to (never mix kinds in one list).
Leave a kind's list empty if it doesn't suit the content. Aim for {n} questions
total across the kinds. Every question must be answerable from the chapter."""

_DEFAULTS = {"num_questions": 10, "difficulty": "medium"}


def _dicts(items) -> list[dict]:
    return [q for q in (items or []) if isinstance(q, dict)]


def build(book: dict, chapter: dict, analysis: dict, client, params: dict, out_dir: Path,
          template: str | None = None, language: str = "en") -> list[Path]:
    p = {**_DEFAULTS, **(params or {})}
    try:
        n = max(1, min(40, int(p["num_questions"])))
    except (TypeError, ValueError):
        n = 10
    grade = book.get("grade") or "school"
    subject = book.get("subject") or "general"
    chapter_title = chapter.get("title") or dx._t("chapter", language)

    prompt = PROMPT.format(difficulty=p["difficulty"], n=n)
    grounding = dx.chapter_grounding(book, chapter, analysis)
    data = client.analyze(prompt, max_tokens=4096, cache_prefix=grounding).get("data", {}) or {}

    # Backward-compat: an older/looser model reply may still use the flat
    # {"questions": [...]} shape — treat those as short-answer so nothing is lost.
    fill = _dicts(data.get("fill_blank"))
    tf = _dicts(data.get("true_false"))
    match = _dicts(data.get("match_column"))
    short = _dicts(data.get("short_answer")) or _dicts(data.get("questions"))

    # Keep the total near the requested count. The match exercise stays WHOLE
    # (3–6 pairs, counts as one "question"); short-answer is trimmed last since
    # it's the most space-hungry.
    fill = fill[:n]
    remaining = n - len(fill)
    tf = tf[: max(0, remaining)]
    remaining -= len(tf)
    match = match[:6]  # a real matching exercise keeps all its pairs
    if match:
        remaining -= 1
    short = short[: max(0, remaining)]

    title = data.get("title") or f"{dx._t('doc_worksheet', language)} — {chapter_title}"
    subtitle = (f"{grade} · {subject}    |    {dx._t('difficulty', language)}: "
                f"{dx.difficulty_label(p['difficulty'], language)}")
    # Catalogue documents carry their curriculum block (decision 10); a
    # textbook worksheet passes none and renders as before.
    header_lines = p.get("curriculum_header")
    doc = dx.new_doc(title, subtitle, template=template, kind="worksheet",
                     language=language, header_lines=header_lines)
    if data.get("instructions"):
        dx.instructions(doc, data["instructions"])

    # Section + match letters come from ONE localized sequence (A B C… /
    # أ ب ج…) shared by headings, tables, and the answer key.
    letters = dx.letters(language)
    si = 0
    answers: list[tuple[str, list[str]]] = []

    if fill:
        name = dx.section_heading(language, si, "sec_fill_blank")
        dx.heading(doc, name, 1)
        dx.numbered(doc, [dx.strip_leading_number(str(q.get("q", ""))) for q in fill])
        answers.append((name, [dx.txt(q.get("answer")) for q in fill]))
        si += 1

    if tf:
        name = dx.section_heading(language, si, "sec_true_false")
        dx.heading(doc, name, 1)
        dx.numbered(doc, [dx.strip_leading_number(str(q.get("statement", ""))) for q in tf])
        answers.append((name, [dx.tf_word(q.get("answer"), language) for q in tf]))
        si += 1

    if match:
        name = dx.section_heading(language, si, "sec_match")
        dx.heading(doc, name, 1)
        dx.para(doc, dx.match_instruction(language), italic=True)
        lefts = [str(pr.get("left", "")) for pr in match]
        rights = [str(pr.get("right", "")) for pr in match]
        order = list(range(len(rights)))
        random.shuffle(order)  # shuffle Column B so it's a real matching task
        rows = [[f"{i + 1}. {lefts[i]}", f"{letters[i]}. {rights[order[i]]}"] for i in range(len(lefts))]
        dx.table(doc, [dx.column_label(language, 0), dx.column_label(language, 1)], rows)
        answers.append((name, [letters[order.index(i)] for i in range(len(lefts))]))
        si += 1

    if short:
        name = dx.section_heading(language, si, "sec_short")
        dx.heading(doc, name, 1)
        for i, q in enumerate(short, 1):
            dx.question(doc, f"{i}. {dx.strip_leading_number(q.get('q', ''))}", first=(i == 1))
            try:
                lines = max(0, min(6, int(q.get("work_space_lines", 2))))
            except (TypeError, ValueError):
                lines = 2
            dx.writing_lines(doc, lines)  # ruled working space
        answers.append((name, [dx.txt(q.get("answer")) for q in short]))

    # ── Answer key: a SEPARATE document (student/teacher split, 2026-08-18) ──
    # Same workbook style (tinted answer_section panels) and the SAME `answers`
    # list the sheet's sections were built from, so key item N always answers
    # sheet item N — including the match-key letters, drawn from the same
    # letters()/abjad sequence as the sheet's table. Panel numbering restarts
    # at 1 inside this file — it is its own document.
    key_doc = dx.new_doc(f"{title} — {dx._t('answer_key', language)}", subtitle,
                         template=template, kind="worksheet", language=language,
                         header_lines=header_lines)
    dx.para(key_doc, dx._t("teacher_only", language), italic=True)
    for sec_name, items in answers:
        dx.answer_section(key_doc, sec_name, items)

    # Structured questions for the app's interactive quiz player (best-effort).
    try:
        from docgen.questions import write_worksheet
        write_worksheet(out_dir, title,
                        data.get("instructions"), fill, tf, match, short,
                        language=language)
    except Exception as exc:  # noqa: BLE001
        logger.warning("questions.json (worksheet) skipped: %s", exc)

    sheet_path = dx.save(doc, out_dir / "worksheet.docx")
    key_path = dx.save(key_doc, out_dir / "worksheet_answer_key.docx")
    return [sheet_path, key_path]
