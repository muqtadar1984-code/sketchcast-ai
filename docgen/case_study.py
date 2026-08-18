"""Case study generator → TWO editable .docx files.

Reads `params`: {"length": "short|medium|long", "num_questions": int}
A case study presents a real-world scenario applying the chapter's concepts,
followed by discussion/analysis questions.

Student/teacher document split (founder direction 2026-08-18): the teacher
notes (per-question answer guidance) must never ride the student handout. Like
docgen/exam.py, build() returns [case_study_path, teacher_notes_path] — the
student document ends at the discussion questions, and the guidance lives in a
SEPARATE classroom-styled teacher document whose items enumerate the SAME
filtered question list, so guidance N always answers question N.
"""

from __future__ import annotations

import logging
from pathlib import Path

from docgen import docx_builder as dx

logger = logging.getLogger("worker")

_WORDS = {"short": "150-250", "medium": "300-450", "long": "500-700"}

PROMPT = """You are an educator writing a CASE STUDY that applies a chapter's concepts
to a realistic, age-appropriate real-world scenario, based on the chapter provided above.

Scenario length: about {words} words.

Return ONLY valid JSON:
{{
  "title": "Case study title",
  "scenario": "The narrative scenario (a few paragraphs, separated by \\n\\n)",
  "background": ["key background fact", "..."],
  "discussion_questions": [{{"q": "...", "guidance": "what a good answer covers"}}],
  "concepts_applied": ["chapter concept used", "..."]
}}
Produce {n} discussion questions that require applying the chapter's concepts."""

_DEFAULTS = {"length": "medium", "num_questions": 4}


def build(book: dict, chapter: dict, analysis: dict, client, params: dict, out_dir: Path,
          template: str | None = None, language: str = "en") -> list[Path]:
    p = {**_DEFAULTS, **(params or {})}
    length = p["length"] if p["length"] in _WORDS else "medium"
    try:
        n = max(1, min(15, int(p["num_questions"])))
    except (TypeError, ValueError):
        n = 4
    grade = book.get("grade") or "school"
    subject = book.get("subject") or "general"
    chapter_title = chapter.get("title") or dx._t("chapter", language)

    prompt = PROMPT.format(words=_WORDS[length], n=n)
    grounding = dx.chapter_grounding(book, chapter, analysis)
    data = client.analyze(prompt, max_tokens=4096, cache_prefix=grounding).get("data", {}) or {}

    title = data.get("title") or f"{dx._t('doc_case_study', language)} — {chapter_title}"
    subtitle = f"{grade} · {subject}"
    doc = dx.new_doc(title, subtitle, template=template, kind="case_study",
                     language=language)

    if data.get("background"):
        dx.heading(doc, dx._t("background", language), 1)
        dx.bullets(doc, data["background"])

    dx.heading(doc, dx._t("scenario", language), 1)
    for para in str(data.get("scenario", "")).split("\n\n"):
        if para.strip():
            dx.para(doc, para.strip())

    # Filter non-dicts ONCE — questions and teacher notes must enumerate the
    # same list, or a stray string skews guidance numbers off the questions.
    qs = [q for q in (data.get("discussion_questions") or []) if isinstance(q, dict)][:n]
    if qs:
        dx.heading(doc, dx._t("discussion_questions", language), 1)
        for i, q in enumerate(qs, 1):
            dx.question(doc, f"{i}. {dx.strip_leading_number(q.get('q', ''))}", first=(i == 1))

    if data.get("concepts_applied"):
        dx.heading(doc, dx._t("concepts_applied", language), 1)
        dx.bullets(doc, data["concepts_applied"])

    # ── Teacher notes: a SEPARATE document (student/teacher split, 2026-08-18)
    # Same classroom style; the guidance enumerates the SAME filtered `qs`
    # list as the student document's discussion questions, so guidance N
    # always answers question N. numbered() restarts at 1 in this file — it
    # is its own document. Masthead reuses the localized teacher_notes string.
    key_doc = dx.new_doc(f"{title} — {dx._t('teacher_notes', language)}",
                         subtitle, template=template, kind="case_study",
                         language=language)
    dx.para(key_doc, dx._t("teacher_only", language), italic=True)
    if qs:
        dx.heading(key_doc, dx._t("teacher_notes", language), 0)
        dx.numbered(key_doc, [dx.txt(q.get("guidance")) for q in qs])

    study_path = dx.save(doc, out_dir / "case_study.docx")
    key_path = dx.save(key_doc, out_dir / "case_study_teacher_notes.docx")
    return [study_path, key_path]
