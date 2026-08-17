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
    chapter_title = chapter.get("title") or "Chapter"
    prompt = PROMPT.format(n=n)
    grounding = dx.chapter_grounding(book, chapter, analysis)
    data = client.analyze(prompt, max_tokens=3500, cache_prefix=grounding).get("data", {}) or {}

    doc = dx.new_doc(f"Class Activities — {chapter_title}", f"{grade} · {subject}",
                     template=template, kind="activity", language=language)
    if data.get("intro"):
        dx.instructions(doc, data["intro"])

    for i, act in enumerate(data.get("activities", []), 1):
        if not isinstance(act, dict):
            continue
        dx.heading(doc, f"Activity {i}: {act.get('name', 'Activity')}", 1)
        dx.labelled(doc, "Objective", act.get("objective", ""))
        dx.labelled(doc, "Grouping", str(act.get("grouping", "")))
        dx.labelled(doc, "Duration", f"{act.get('duration_minutes', '')} minutes")
        if act.get("materials"):
            dx.labelled(doc, "Materials", ", ".join(str(m) for m in act["materials"]))
        if act.get("steps"):
            dx.para(doc, "Steps:", bold=True)
            dx.numbered(doc, act["steps"])
        if act.get("teacher_facilitation"):
            dx.labelled(doc, "Teacher facilitation", act["teacher_facilitation"])
        if act.get("success_looks_like"):
            dx.labelled(doc, "Success looks like", act["success_looks_like"])

    return dx.save(doc, out_dir / "activity.docx")
