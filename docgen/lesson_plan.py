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
    chapter_title = chapter.get("title") or dx._t("chapter", language)
    prompt = PROMPT.format(
        duration=duration,
        homework_field=',\n  "homework": ["..."]' if want_homework else "",
        diff_field=',\n  "differentiation": {"support": "...", "challenge": "..."}' if want_diff else "",
    )
    grounding = dx.chapter_grounding(book, chapter, analysis)
    data = client.analyze(prompt, max_tokens=3000, cache_prefix=grounding).get("data", {}) or {}

    minutes_text = dx._t("n_minutes", language).format(n=data.get("duration_minutes", duration))
    doc = dx.new_doc(
        data.get("title") or f"{dx._t('doc_lesson_plan', language)} — {chapter_title}",
        f"{grade} · {subject}    |    {dx._t('duration', language)}: {minutes_text}",
        template=template, kind="lesson_plan", language=language,
    )

    dx.heading(doc, dx._t("learning_objectives", language), 1)
    dx.bullets(doc, data.get("learning_objectives", []))

    if data.get("materials"):
        dx.heading(doc, dx._t("materials", language), 1)
        dx.bullets(doc, data["materials"])

    vocab = data.get("key_vocabulary", [])
    if vocab:
        dx.heading(doc, dx._t("key_vocabulary", language), 1)
        dx.table(
            doc, [dx._t("term", language), dx._t("definition", language)],
            [[v.get("term", ""), v.get("definition", "")] for v in vocab if isinstance(v, dict)],
        )

    flow = data.get("lesson_flow", [])
    if flow:
        dx.heading(doc, dx._t("lesson_flow", language), 1)
        dx.table(
            doc, [dx._t("phase", language), dx._t("min_col", language),
                  dx._t("teacher_does", language), dx._t("students_do", language)],
            [[f.get("phase", ""), f.get("minutes", ""), f.get("teacher_does", ""), f.get("students_do", "")]
             for f in flow if isinstance(f, dict)],
        )

    if data.get("assessment"):
        dx.heading(doc, dx._t("assessment", language), 1)
        dx.bullets(doc, data["assessment"])

    if data.get("homework"):
        dx.heading(doc, dx._t("homework", language), 1)
        dx.bullets(doc, data["homework"])

    diff = data.get("differentiation") or {}
    if diff:
        dx.heading(doc, dx._t("differentiation", language), 1)
        if diff.get("support"):
            dx.labelled(doc, dx._t("support", language), diff["support"])
        if diff.get("challenge"):
            dx.labelled(doc, dx._t("challenge", language), diff["challenge"])

    return dx.save(doc, out_dir / "lesson_plan.docx")
