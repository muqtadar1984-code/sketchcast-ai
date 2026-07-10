"""Worksheet generator → editable .docx.

Reads `params`: {"num_questions": int, "difficulty": "easy|medium|hard",
                 "include_answer_key": bool}
A worksheet is practice-oriented (mixed short questions with working space),
distinct from the formal exam paper — but, like the exam paper, questions are
TYPED and GROUPED BY KIND (all fill-in-the-blanks together, etc.) and the
match-the-columns exercise renders as a two-column TABLE, not running text.
"""

from __future__ import annotations

import logging
import random
import string
from pathlib import Path

from docgen import docx_builder as dx

logger = logging.getLogger("worker")

PROMPT = """You are a teacher creating a practice WORKSHEET from the chapter below.

Audience: {grade} {subject} students.
Chapter: "{chapter_title}"
Difficulty: {difficulty}

Chapter content excerpt:
\"\"\"
{content}
\"\"\"

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

_DEFAULTS = {"num_questions": 10, "difficulty": "medium", "include_answer_key": True}


def _dicts(items) -> list[dict]:
    return [q for q in (items or []) if isinstance(q, dict)]


def build(book: dict, chapter: dict, analysis: dict, client, params: dict, out_dir: Path,
          template: str | None = None) -> Path:
    p = {**_DEFAULTS, **(params or {})}
    try:
        n = max(1, min(40, int(p["num_questions"])))
    except (TypeError, ValueError):
        n = 10
    grade = book.get("grade") or "school"
    subject = book.get("subject") or "general"
    chapter_title = chapter.get("title") or "Chapter"

    prompt = PROMPT.format(
        grade=grade, subject=subject, chapter_title=chapter_title,
        difficulty=p["difficulty"], n=n, content=dx.chapter_excerpt(chapter),
    )
    data = client.analyze(prompt, max_tokens=4096).get("data", {}) or {}

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

    doc = dx.new_doc(
        data.get("title") or f"Worksheet — {chapter_title}",
        f"{grade} · {subject}    |    Difficulty: {p['difficulty']}",
        template=template,
    )
    if data.get("instructions"):
        dx.para(doc, data["instructions"], italic=True)

    section = ord("A")
    answers: list[tuple[str, list[str]]] = []

    if fill:
        dx.heading(doc, f"Section {chr(section)} — Fill in the blanks", 1)
        dx.numbered(doc, [str(q.get("q", "")) for q in fill])
        answers.append((f"Section {chr(section)} — Fill in the blanks", [str(q.get("answer", "")) for q in fill]))
        section += 1

    if tf:
        dx.heading(doc, f"Section {chr(section)} — True or False", 1)
        dx.numbered(doc, [str(q.get("statement", "")) for q in tf])
        answers.append((f"Section {chr(section)} — True or False",
                        ["True" if q.get("answer") else "False" for q in tf]))
        section += 1

    if match:
        dx.heading(doc, f"Section {chr(section)} — Match the columns", 1)
        dx.para(doc, "Match each item in Column A to the correct item in Column B.", italic=True)
        lefts = [str(pr.get("left", "")) for pr in match]
        rights = [str(pr.get("right", "")) for pr in match]
        order = list(range(len(rights)))
        random.shuffle(order)  # shuffle Column B so it's a real matching task
        letters = list(string.ascii_uppercase)
        rows = [[f"{i + 1}. {lefts[i]}", f"{letters[i]}. {rights[order[i]]}"] for i in range(len(lefts))]
        dx.table(doc, ["Column A", "Column B"], rows)
        answers.append((f"Section {chr(section)} — Match the columns",
                        [f"{i + 1} → {letters[order.index(i)]}" for i in range(len(lefts))]))
        section += 1

    if short:
        dx.heading(doc, f"Section {chr(section)} — Short answer", 1)
        for i, q in enumerate(short, 1):
            dx.para(doc, f"{i}. {q.get('q', '')}")
            try:
                lines = max(0, min(6, int(q.get("work_space_lines", 2))))
            except (TypeError, ValueError):
                lines = 2
            for _ in range(lines):
                dx.para(doc, " ")  # blank working line
        answers.append((f"Section {chr(section)} — Short answer", [str(q.get("answer", "")) for q in short]))

    if p.get("include_answer_key"):
        doc.add_page_break()
        dx.heading(doc, "Answer Key", 0)
        for sec_name, items in answers:
            dx.heading(doc, sec_name, 2)
            dx.numbered(doc, items)

    # Structured questions for the app's interactive quiz player (best-effort).
    try:
        from docgen.questions import write_worksheet
        write_worksheet(out_dir, data.get("title") or f"Worksheet — {chapter_title}",
                        data.get("instructions"), fill, tf, match, short)
    except Exception as exc:  # noqa: BLE001
        logger.warning("questions.json (worksheet) skipped: %s", exc)

    return dx.save(doc, out_dir / "worksheet.docx")
