"""In-class activities generator → TWO editable .docx files.

Activities are run in the physical classroom, monitored and managed by the
teacher — so each one carries explicit facilitation + grouping + timing notes.

Student/teacher document split (founder direction 2026-08-18): the teacher-only
content (the intro overview, per-activity facilitation and success criteria —
the "answers" of an activity) must never ride the student handout. Like
docgen/exam.py, build() returns [student_sheet_path, teacher_notes_path]:
the student sheet keeps everything a learner runs the activity from (name,
objective, grouping, duration, materials, steps) and the teacher document
carries the facilitation notes in workbook-style answer panels, enumerated
Activity 1..N in the SAME order and count as the student sheet."""

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
          template: str | None = None, language: str = "en") -> list[Path]:
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

    title = f"{dx._t('doc_activities', language)} — {chapter_title}"
    subtitle = f"{grade} · {subject}"
    # Filter non-dicts ONCE — the student sheet and the teacher notes must
    # enumerate the SAME list, or notes N stops describing Activity N.
    acts = [a for a in (data.get("activities") or []) if isinstance(a, dict)]
    # Catalogue documents carry their curriculum block (decision 10); a
    # textbook activity passes none and renders as before.
    header_lines = (params or {}).get("curriculum_header")

    # ── Student sheet: everything a learner runs the activity from ──────────
    # The intro ("overview for the teacher") and the per-activity facilitation
    # and success criteria are teacher-only — they live in the second document.
    doc = dx.new_doc(title, subtitle, template=template, kind="activity",
                     language=language, header_lines=header_lines)
    for i, act in enumerate(acts, 1):
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

    # ── Teacher notes: a SEPARATE document (student/teacher split, 2026-08-18)
    # Same workbook style; one answer panel per activity, headed "Activity N:
    # name" in the SAME order and count as the student sheet, so panel N always
    # describes Activity N. Masthead reuses the localized teacher_facilitation
    # string — no new docgen strings.
    key_doc = dx.new_doc(f"{title} — {dx._t('teacher_facilitation', language)}",
                         subtitle, template=template, kind="activity",
                         language=language, header_lines=header_lines)
    dx.para(key_doc, dx._t("teacher_only", language), italic=True)
    if data.get("intro"):
        dx.instructions(key_doc, data["intro"])
    for i, act in enumerate(acts, 1):
        items = []
        if act.get("teacher_facilitation"):
            items.append(f"{dx._t('teacher_facilitation', language)}: "
                         f"{act['teacher_facilitation']}")
        if act.get("success_looks_like"):
            items.append(f"{dx._t('success_looks_like', language)}: "
                         f"{act['success_looks_like']}")
        dx.answer_section(key_doc, f"{dx._t('activity', language)} {i}: "
                                   f"{act.get('name', dx._t('activity', language))}",
                          items)

    sheet_path = dx.save(doc, out_dir / "activity.docx")
    key_path = dx.save(key_doc, out_dir / "activity_teacher_notes.docx")
    return [sheet_path, key_path]
