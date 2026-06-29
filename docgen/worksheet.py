"""Worksheet generator → editable .docx.

Reads `params`: {"num_questions": int, "difficulty": "easy|medium|hard",
                 "include_answer_key": bool}
A worksheet is practice-oriented (mixed short questions with working space),
distinct from the formal exam paper.
"""

from __future__ import annotations

import logging
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

Create EXACTLY {n} practice questions of mixed kinds (short answer, fill-in,
quick problems) appropriate for the difficulty. Return ONLY valid JSON:
{{
  "title": "Worksheet title",
  "instructions": "Short instructions for students",
  "questions": [{{"q": "...", "answer": "...", "work_space_lines": 2}}]
}}
Every question must be answerable from the chapter content."""

_DEFAULTS = {"num_questions": 10, "difficulty": "medium", "include_answer_key": True}


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
    questions = (data.get("questions") or [])[:n]

    doc = dx.new_doc(
        data.get("title") or f"Worksheet — {chapter_title}",
        f"{grade} · {subject}    |    Difficulty: {p['difficulty']}",
        template=template,
    )
    if data.get("instructions"):
        dx.para(doc, data["instructions"], italic=True)

    for i, q in enumerate(questions, 1):
        if not isinstance(q, dict):
            continue
        dx.para(doc, f"{i}. {q.get('q', '')}")
        lines = q.get("work_space_lines", 2)
        try:
            lines = max(0, min(6, int(lines)))
        except (TypeError, ValueError):
            lines = 2
        for _ in range(lines):
            dx.para(doc, " ")  # blank working line

    if p.get("include_answer_key"):
        doc.add_page_break()
        dx.heading(doc, "Answer Key", 0)
        dx.numbered(doc, [str(q.get("answer", "")) for q in questions if isinstance(q, dict)])

    # Structured questions for the app's interactive quiz player (best-effort).
    try:
        from docgen.questions import write_worksheet
        write_worksheet(out_dir, data.get("title") or f"Worksheet — {chapter_title}",
                        data.get("instructions"), questions)
    except Exception as exc:  # noqa: BLE001
        logger.warning("questions.json (worksheet) skipped: %s", exc)

    return dx.save(doc, out_dir / "worksheet.docx")
