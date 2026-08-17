"""Lesson plan generator → editable .docx."""

from __future__ import annotations

import logging
from pathlib import Path

from docgen import docx_builder as dx

logger = logging.getLogger("worker")

PROMPT = """You are an expert teacher writing a classroom-ready LESSON PLAN based on the chapter provided above.

Total lesson duration: {duration} minutes.
Return ONLY valid JSON in exactly this shape:
{{
  "title": "Lesson plan title",
  "duration_minutes": {duration},
  "learning_objectives": ["..."],
  "materials": ["..."],
  "key_vocabulary": [{{"term": "...", "definition": "..."}}],
  "lesson_flow": [
    {{"phase": "Introduction / Hook", "minutes": 5, "teacher_does": "...", "students_do": "..."}}
  ],
  "assessment": ["..."]{homework_field}{diff_field}
}}
Make it specific to the chapter content. 4-6 lesson_flow phases that sum to roughly {duration} minutes."""


def build(book: dict, chapter: dict, analysis: dict, client, params: dict, out_dir: Path,
          template: str | None = None, language: str = "en") -> Path:
    p = params or {}
    try:
        duration = max(10, min(180, int(p.get("duration_minutes", 45))))
    except (TypeError, ValueError):
        duration = 45
    want_homework = p.get("include_homework", True)
    want_diff = p.get("include_differentiation", True)

    grade = book.get("grade") or "school"
    subject = book.get("subject") or "general"
    chapter_title = chapter.get("title") or "Chapter"
    prompt = PROMPT.format(
        duration=duration,
        homework_field=',\n  "homework": ["..."]' if want_homework else "",
        diff_field=',\n  "differentiation": {"support": "...", "challenge": "..."}' if want_diff else "",
    )
    grounding = dx.chapter_grounding(book, chapter, analysis)
    data = client.analyze(prompt, max_tokens=3000, cache_prefix=grounding).get("data", {}) or {}

    doc = dx.new_doc(
        data.get("title") or f"Lesson Plan — {chapter_title}",
        f"{grade} · {subject}    |    Duration: {data.get('duration_minutes', duration)} minutes",
        template=template, kind="lesson_plan", language=language,
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
