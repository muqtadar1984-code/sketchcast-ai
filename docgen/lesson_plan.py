"""Lesson plan generator → editable .docx."""

from __future__ import annotations

import logging
from pathlib import Path

from docgen import docx_builder as dx

logger = logging.getLogger("worker")

PROMPT = """You are an expert teacher writing a classroom-ready LESSON PLAN.

Audience: {grade} {subject} students.
Chapter: "{chapter_title}"
Key concepts: {concepts}

Chapter content excerpt:
\"\"\"
{content}
\"\"\"

Return ONLY valid JSON in exactly this shape:
{{
  "title": "Lesson plan title",
  "duration_minutes": 45,
  "learning_objectives": ["..."],
  "materials": ["..."],
  "key_vocabulary": [{{"term": "...", "definition": "..."}}],
  "lesson_flow": [
    {{"phase": "Introduction / Hook", "minutes": 5, "teacher_does": "...", "students_do": "..."}}
  ],
  "assessment": ["..."],
  "homework": ["..."],
  "differentiation": {{"support": "...", "challenge": "..."}}
}}
Make it specific to the chapter content. 4-6 lesson_flow phases that sum to roughly duration_minutes."""


def build(book: dict, chapter: dict, analysis: dict, client, out_dir: Path) -> Path:
    grade = book.get("grade") or "school"
    subject = book.get("subject") or "general"
    chapter_title = chapter.get("title") or "Chapter"
    prompt = PROMPT.format(
        grade=grade,
        subject=subject,
        chapter_title=chapter_title,
        concepts=", ".join(dx.concept_names(analysis)) or "(see content)",
        content=dx.chapter_excerpt(chapter),
    )
    data = client.analyze(prompt, max_tokens=3000).get("data", {}) or {}

    doc = dx.new_doc(
        data.get("title") or f"Lesson Plan — {chapter_title}",
        f"{grade} · {subject}    |    Duration: {data.get('duration_minutes', 45)} minutes",
    )

    dx.heading(doc, "Learning objectives", 1)
    dx.bullets(doc, data.get("learning_objectives", []))

    if data.get("materials"):
        dx.heading(doc, "Materials", 1)
        dx.bullets(doc, data["materials"])

    vocab = data.get("key_vocabulary", [])
    if vocab:
        dx.heading(doc, "Key vocabulary", 1)
        dx.table(
            doc, ["Term", "Definition"],
            [[v.get("term", ""), v.get("definition", "")] for v in vocab if isinstance(v, dict)],
        )

    flow = data.get("lesson_flow", [])
    if flow:
        dx.heading(doc, "Lesson flow", 1)
        dx.table(
            doc, ["Phase", "Min", "Teacher does", "Students do"],
            [[f.get("phase", ""), f.get("minutes", ""), f.get("teacher_does", ""), f.get("students_do", "")]
             for f in flow if isinstance(f, dict)],
        )

    if data.get("assessment"):
        dx.heading(doc, "Assessment", 1)
        dx.bullets(doc, data["assessment"])

    if data.get("homework"):
        dx.heading(doc, "Homework", 1)
        dx.bullets(doc, data["homework"])

    diff = data.get("differentiation") or {}
    if diff:
        dx.heading(doc, "Differentiation", 1)
        if diff.get("support"):
            dx.labelled(doc, "Support", diff["support"])
        if diff.get("challenge"):
            dx.labelled(doc, "Challenge", diff["challenge"])

    return dx.save(doc, out_dir / "lesson_plan.docx")
