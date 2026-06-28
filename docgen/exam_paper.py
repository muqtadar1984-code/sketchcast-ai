"""Exam / test paper generator → editable .docx.

Reads `params`:
  {"objective": {"fill_blank": N, "true_false": N, "match_column": N},
   "subjective": N, "include_answer_key": bool}
"""

from __future__ import annotations

import logging
import random
import string
from pathlib import Path

from docgen import docx_builder as dx

logger = logging.getLogger("worker")

PROMPT = """You are an examiner writing a test paper from the chapter below.

Audience: {grade} {subject} students.
Chapter: "{chapter_title}"

Chapter content excerpt:
\"\"\"
{content}
\"\"\"

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


def build(book: dict, chapter: dict, analysis: dict, client, params: dict, out_dir: Path) -> Path:
    n_fill, n_tf, n_match, n_subj, want_key = _counts(params)
    grade = book.get("grade") or "school"
    subject = book.get("subject") or "general"
    chapter_title = chapter.get("title") or "Chapter"

    prompt = PROMPT.format(
        grade=grade, subject=subject, chapter_title=chapter_title,
        n_fill=n_fill, n_tf=n_tf, n_match=n_match, n_subj=n_subj,
        content=dx.chapter_excerpt(chapter),
    )
    data = client.analyze(prompt, max_tokens=4096).get("data", {}) or {}

    fill = (data.get("fill_blank") or [])[:n_fill]
    tf = (data.get("true_false") or [])[:n_tf]
    match = (data.get("match_column") or [])[:n_match]
    subj = (data.get("subjective") or [])[:n_subj]

    doc = dx.new_doc(
        data.get("title") or f"Test Paper — {chapter_title}",
        f"{grade} · {subject}",
    )
    if data.get("instructions"):
        dx.para(doc, data["instructions"], italic=True)

    section = ord("A")
    answers: list[tuple[str, list[str]]] = []

    if fill:
        dx.heading(doc, f"Section {chr(section)} — Fill in the blanks", 1)
        dx.numbered(doc, [str(q.get("q", "")) for q in fill if isinstance(q, dict)])
        answers.append((f"Section {chr(section)} — Fill in the blanks",
                        [str(q.get("answer", "")) for q in fill if isinstance(q, dict)]))
        section += 1

    if tf:
        dx.heading(doc, f"Section {chr(section)} — True or False", 1)
        dx.numbered(doc, [str(q.get("statement", "")) for q in tf if isinstance(q, dict)])
        answers.append((f"Section {chr(section)} — True or False",
                        ["True" if q.get("answer") else "False" for q in tf if isinstance(q, dict)]))
        section += 1

    if match:
        dx.heading(doc, f"Section {chr(section)} — Match the columns", 1)
        lefts = [str(p.get("left", "")) for p in match if isinstance(p, dict)]
        rights = [str(p.get("right", "")) for p in match if isinstance(p, dict)]
        order = list(range(len(rights)))
        random.shuffle(order)
        letters = list(string.ascii_uppercase)
        rows = []
        for i, left in enumerate(lefts):
            shuffled_pos = order.index(i)  # where the correct right ended up
            rows.append([f"{i + 1}. {left}", f"{letters[i]}. {rights[order[i]]}"])
        dx.table(doc, ["Column A", "Column B"], rows)
        # answer key: question number -> letter of its correct right
        key_lines = [f"{i + 1} → {letters[order.index(i)]}" for i in range(len(lefts))]
        answers.append((f"Section {chr(section)} — Match the columns", key_lines))
        section += 1

    if subj:
        dx.heading(doc, f"Section {chr(section)} — Long answer", 1)
        for i, q in enumerate(subj, 1):
            if isinstance(q, dict):
                marks = q.get("marks", "")
                dx.para(doc, f"{i}. {q.get('q', '')}" + (f"   [{marks} marks]" if marks else ""))
        answers.append((f"Section {chr(section)} — Long answer",
                        [str(q.get("answer_outline", "")) for q in subj if isinstance(q, dict)]))

    if want_key:
        doc.add_page_break()
        dx.heading(doc, "Answer Key", 0)
        for sec_name, items in answers:
            dx.heading(doc, sec_name, 2)
            dx.numbered(doc, items)

    return dx.save(doc, out_dir / "exam_paper.docx")
