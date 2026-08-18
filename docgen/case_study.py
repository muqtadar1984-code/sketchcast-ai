"""Case study generator → editable .docx.

Reads `params`: {"length": "short|medium|long", "num_questions": int}
A case study presents a real-world scenario applying the chapter's concepts,
followed by discussion/analysis questions.
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
          template: str | None = None, language: str = "en") -> Path:
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

    doc = dx.new_doc(
        data.get("title") or f"{dx._t('doc_case_study', language)} — {chapter_title}",
        f"{grade} · {subject}",
        template=template, kind="case_study", language=language,
    )

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

    # Teacher notes: answer guidance (kept on a separate page). Same list as
    # the questions above, so guidance N always answers question N.
    if any(q.get("guidance") for q in qs):
        doc.add_page_break()
        dx.heading(doc, dx._t("teacher_notes", language), 0)
        dx.numbered(doc, [dx.txt(q.get("guidance")) for q in qs])

    return dx.save(doc, out_dir / "case_study.docx")
