"""In-class activities generator → editable .docx.

Activities are run in the physical classroom, monitored and managed by the
teacher — so each one carries explicit facilitation + grouping + timing notes."""

from __future__ import annotations

import logging
from pathlib import Path

from docgen import docx_builder as dx

logger = logging.getLogger("worker")

PROMPT = """You are an expert teacher designing IN-CLASS ACTIVITIES that a teacher
runs and supervises physically in the classroom, based on the chapter provided above.

Return ONLY valid JSON in exactly this shape:
{{
  "intro": "1-2 sentence overview for the teacher",
  "activities": [
    {{
      "name": "...",
      "objective": "...",
      "grouping": "individual | pairs | small groups | whole class",
      "duration_minutes": 15,
      "materials": ["..."],
      "steps": ["..."],
      "teacher_facilitation": "What the teacher does to run/monitor it",
      "success_looks_like": "How the teacher knows students got it"
    }}
  ]
}}
Produce EXACTLY {n} varied, hands-on activities tied to the chapter content."""


def build(book: dict, chapter: dict, analysis: dict, client, params: dict, out_dir: Path,
          template: str | None = None, language: str = "en") -> Path:
    try:
        n = max(1, min(8, int((params or {}).get("num_activities", 4))))
    except (TypeError, ValueError):
        n = 4
    grade = book.get("grade") or "school"
    subject = book.get("subject") or "general"
    chapter_title = chapter.get("title") or dx._t("chapter", language)
    prompt = PROMPT.format(n=n)
    grounding = dx.chapter_grounding(book, chapter, analysis)
    data = client.analyze(prompt, max_tokens=3500, cache_prefix=grounding).get("data", {}) or {}

    doc = dx.new_doc(f"{dx._t('doc_activities', language)} — {chapter_title}",
                     f"{grade} · {subject}",
                     template=template, kind="activity", language=language)
    if data.get("intro"):
        dx.instructions(doc, data["intro"])

    for i, act in enumerate(data.get("activities", []), 1):
        if not isinstance(act, dict):
            continue
        dx.heading(doc, f"{dx._t('activity', language)} {i}: "
                        f"{act.get('name', dx._t('activity', language))}", 1)
        dx.labelled(doc, dx._t("objective", language), act.get("objective", ""))
        dx.labelled(doc, dx._t("grouping", language), str(act.get("grouping", "")))
        dx.labelled(doc, dx._t("duration", language),
                    dx._t("n_minutes", language).format(n=act.get("duration_minutes", "")))
        if act.get("materials"):
            dx.labelled(doc, dx._t("materials", language),
                        ", ".join(str(m) for m in act["materials"]))
        if act.get("steps"):
            dx.para(doc, f"{dx._t('steps', language)}:", bold=True)
            dx.numbered(doc, act["steps"])
        if act.get("teacher_facilitation"):
            dx.labelled(doc, dx._t("teacher_facilitation", language), act["teacher_facilitation"])
        if act.get("success_looks_like"):
            dx.labelled(doc, dx._t("success_looks_like", language), act["success_looks_like"])

    return dx.save(doc, out_dir / "activity.docx")
