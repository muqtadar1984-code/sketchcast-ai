"""In-class activities generator → editable .docx.

Activities are run in the physical classroom, monitored and managed by the
teacher — so each one carries explicit facilitation + grouping + timing notes."""

from __future__ import annotations

import logging
from pathlib import Path

from docgen import docx_builder as dx

logger = logging.getLogger("worker")

PROMPT = """You are an expert teacher designing IN-CLASS ACTIVITIES that a teacher
runs and supervises physically in the classroom.

Audience: {grade} {subject} students.
Chapter: "{chapter_title}"
Key concepts: {concepts}

Chapter content excerpt:
\"\"\"
{content}
\"\"\"

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
Produce 3-4 varied, hands-on activities tied to the chapter content."""


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
    data = client.analyze(prompt, max_tokens=3500).get("data", {}) or {}

    doc = dx.new_doc(f"Class Activities — {chapter_title}", f"{grade} · {subject}")
    if data.get("intro"):
        dx.para(doc, data["intro"], italic=True)

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
