"""Exam / test paper generator → editable .docx.

Reads `params`:
  {"objective": {"fill_blank": N, "true_false": N, "match_column": N},
   "subjective": N, "include_answer_key": bool}
"""

from __future__ import annotations

import logging
import random
from pathlib import Path

from docgen import docx_builder as dx

logger = logging.getLogger("worker")

PROMPT = """You are an examiner writing a test paper from the chapter provided above.

Write EXACTLY:
- {n_fill} fill-in-the-blank questions
- {n_tf} true/false statements
- {n_match} match-the-column pairs (a single matching exercise)
- {n_subj} subjective (long-answer) questions

Return ONLY valid JSON in exactly this shape:
{{
  "title": "Test title",
  "instructions": "General instructions for students",
  "fill_blank": [{{"q": "Sentence with a ____ blank", "answer": "word"}}],
  "true_false": [{{"statement": "...", "answer": true}}],
  "match_column": [{{"left": "term", "right": "matching description"}}],
  "subjective": [{{"q": "...", "marks": 5, "answer_outline": "key points expected"}}]
}}
All questions must be answerable from the chapter content. Honor the exact counts."""


def _counts(params: dict) -> tuple[int, int, int, int, bool]:
    obj = (params or {}).get("objective", {}) or {}
    def g(d, k):
        try:
            return max(0, int(d.get(k, 0) or 0))
        except (TypeError, ValueError):
            return 0
    n_fill = g(obj, "fill_blank")
    n_tf = g(obj, "true_false")
    n_match = g(obj, "match_column")
    n_subj = g(params or {}, "subjective")
    if n_fill + n_tf + n_match + n_subj == 0:  # sensible default
        n_fill, n_tf, n_match, n_subj = 5, 5, 4, 3
    key = bool((params or {}).get("include_answer_key", True))
    return n_fill, n_tf, n_match, n_subj, key


def build(book: dict, chapter: dict, analysis: dict, client, params: dict, out_dir: Path,
          template: str | None = None, language: str = "en") -> Path:
    n_fill, n_tf, n_match, n_subj, want_key = _counts(params)
    grade = book.get("grade") or "school"
    subject = book.get("subject") or "general"
    chapter_title = chapter.get("title") or dx._t("chapter", language)

    prompt = PROMPT.format(n_fill=n_fill, n_tf=n_tf, n_match=n_match, n_subj=n_subj)
    grounding = dx.chapter_grounding(book, chapter, analysis)
    data = client.analyze(prompt, max_tokens=4096, cache_prefix=grounding).get("data", {}) or {}

    # Filter non-dicts ONCE at extraction — paper and answer key must enumerate
    # the exact same list, or a stray string skews their numbering apart.
    def _dicts(items) -> list[dict]:
        return [q for q in (items or []) if isinstance(q, dict)]

    fill = _dicts(data.get("fill_blank"))[:n_fill]
    tf = _dicts(data.get("true_false"))[:n_tf]
    match = _dicts(data.get("match_column"))[:n_match]
    subj = _dicts(data.get("subjective"))[:n_subj]

    title = data.get("title") or f"{dx._t('doc_test_paper', language)} — {chapter_title}"
    doc = dx.new_doc(
        title,
        f"{grade} · {subject}",
        template=template, kind="exam_paper", language=language,
    )

    # ONE localized letter sequence (A B C… / أ ب ج…) shared by the marks
    # table, section headings, match options, and the answer key.
    letters = dx.letters(language)

    # Examiner marks summary: fill/tf/match default to 1 mark per item,
    # subjective uses each question's real marks value (default 5).
    def _subj_marks(q: dict) -> int:
        try:
            m = int(q.get("marks"))
            return m if m > 0 else 5
        except (TypeError, ValueError):
            return 5

    marks_entries: list[tuple[str, int]] = []
    for present, total in ((fill, len(fill)), (tf, len(tf)), (match, len(match)),
                           (subj, sum(_subj_marks(q) for q in subj))):
        if present:
            marks_entries.append((letters[len(marks_entries)], total))
    if marks_entries:
        dx.marks_table(doc, marks_entries)

    if data.get("instructions"):
        dx.instructions(doc, data["instructions"])

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
        dx.tf_boxes(doc, [dx.strip_leading_number(str(q.get("statement", ""))) for q in tf])
        # Key values = the SAME localized pair the tf_boxes cue names.
        answers.append((name, [dx.tf_word(q.get("answer"), language) for q in tf]))
        si += 1

    if match:
        name = dx.section_heading(language, si, "sec_match")
        dx.heading(doc, name, 1)
        lefts = [dx.txt(p.get("left")) for p in match]
        rights = [dx.txt(p.get("right")) for p in match]
        order = list(range(len(rights)))
        random.shuffle(order)
        rows = []
        for i, left in enumerate(lefts):
            rows.append([f"{i + 1}. {left}", f"{letters[i]}. {rights[order[i]]}"])
        dx.table(doc, [dx.column_label(language, 0), dx.column_label(language, 1)], rows)
        # answer key: bare letters — numbered() supplies the question numbers;
        # drawn from the SAME sequence as the table, or the key is un-gradeable
        key_lines = [letters[order.index(i)] for i in range(len(lefts))]
        answers.append((name, key_lines))
        si += 1

    if subj:
        name = dx.section_heading(language, si, "sec_long")
        dx.heading(doc, name, 1)
        for i, q in enumerate(subj, 1):
            marks = q.get("marks", "")
            dx.question(doc, f"{i}. {dx.strip_leading_number(q.get('q', ''))}"
                        + (f"   {dx.marks_bracket(marks, language)}" if marks else ""),
                        first=(i == 1))
        answers.append((name, [dx.txt(q.get("answer_outline")) for q in subj]))

    dx.end_of_paper(doc)

    if want_key:
        doc.add_page_break()
        dx.heading(doc, dx._t("answer_key", language), 0)
        for sec_name, items in answers:
            dx.heading(doc, sec_name, 2)
            dx.numbered(doc, items)

    # Structured questions for the app's interactive quiz player (best-effort).
    try:
        from docgen.questions import write_exam
        write_exam(out_dir, title,
                   data.get("instructions"), fill, tf, match, subj,
                   language=language)
    except Exception as exc:  # noqa: BLE001
        logger.warning("questions.json (exam) skipped: %s", exc)

    return dx.save(doc, out_dir / "exam_paper.docx")
