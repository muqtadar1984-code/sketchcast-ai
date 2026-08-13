"""Document-type classification at index time — the junk-upload gate's SIGNAL.

Live incident (2026-08-13): a teacher uploaded a 1-page scanned class roster;
book health scored it 55 with two problems ("Only one unit was detected…",
"Very short document…") and nothing MARKED the book, so the app offered
Generate as usual and 6 credits were burned on a name list. The missing fact
was never volume alone — it was WHAT the document is.

This module answers only that question: one cheap, bounded Claude call over the
document's opening. The gate DECISION lives in
``book_health.compute_book_health`` (single source of truth), which combines
this classification with volume rules and stamps ``health.gate``; the app reads
the stamp and asks "Generate anyway?". Nothing here blocks anything.

Runs for BOTH ingestion paths: a text-layer book is classified from its opening
text, a scanned book (or one whose text layer fails the quality gate) from its
first rendered pages — the same routing ``extraction_has_text`` already gives
chapter detection. Index time stays on Claude deliberately: the book's language
is not known yet, so there is no script to route on (see ``index_book``).

FAIL-OPEN by construction: any trouble — no client, render failure, API hiccup,
a junk reply — returns "unknown", and "unknown" contributes NOTHING to the gate
(only the volume rules apply). Indexing must never fail or stall because of
this feature.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from agent1_ingestion.vision_chapters import (_DETECT_WIDTH, _render_pages,
                                              extraction_has_text)

logger = logging.getLogger(__name__)

# The categories the gate reasons about. book_health._KNOWN_DOC_TYPES mirrors
# this set rather than importing it (vision_chapters imports book_health, and
# this module leans on vision_chapters' page renderer — importing back would be
# circular); tests/test_doc_type_gate.py asserts the two stay in step.
DOC_TYPES = frozenset({
    "textbook", "workbook", "notes", "exam_material",
    "administrative", "form_or_roster", "other", "unknown",
})

# The fail-open result. Returned as a COPY so no caller can mutate the default.
UNKNOWN: dict = {"doc_type": "unknown", "confidence": 0.0}

_SAMPLE_CHARS = 4000  # ~2 pages of prose — enough to say what a document IS
_SAMPLE_PAGES = 2     # scanned path: the cover/letterhead pages carry the answer
_MAX_TOKENS = 100     # the reply is one category name and one number

_CATEGORY_LINES = (
    "- textbook: a book that teaches subject content (chapters, lessons, explanations)\n"
    "- workbook: a practice or activity book of exercises for students\n"
    "- notes: lecture notes, revision notes, study summaries or class handouts\n"
    "- exam_material: an exam paper, test, quiz, past paper or answer/mark scheme\n"
    "- administrative: school administration — circulars, letters, policies, schedules, minutes\n"
    "- form_or_roster: a form, register, class roster, attendance or name list, sign-up sheet\n"
    "- other: none of the above and not teaching material (poster, certificate, receipt, …)\n"
)
_ANSWER_LINE = (
    'Return ONLY JSON: {"doc_type": "<category>", "confidence": <0.0-1.0>}. '
    'If you genuinely cannot tell, use "unknown".'
)


def _text_sample(extraction) -> str:
    """The document's opening text — what a person would glance at to say what
    it is. Taken in document order (cover, title, letterhead, contents live at
    the front) and hard-bounded so the call stays cheap."""
    parts: list[str] = []
    total = 0
    for it in getattr(extraction, "items", []) or []:
        t = (getattr(it, "text", "") or "").strip()
        if not t:
            continue
        parts.append(t)
        total += len(t)
        if total >= _SAMPLE_CHARS:
            break
    return "\n".join(parts)[:_SAMPLE_CHARS]


def _text_prompt(text: str) -> str:
    return (
        "A teacher uploaded this document to a tool that builds lessons from "
        "teaching material. Classify what KIND of document it is from its "
        "opening text below.\n\nCategories (pick exactly one):\n"
        + _CATEGORY_LINES
        + "\nOpening text:\n---\n" + text + "\n---\n\n" + _ANSWER_LINE
    )


def _vision_prompt(n: int) -> str:
    return (
        f"You are shown the first {n} page(s) of a document a teacher uploaded "
        "to a tool that builds lessons from teaching material. Classify what "
        "KIND of document it is.\n\nCategories (pick exactly one):\n"
        + _CATEGORY_LINES + "\n" + _ANSWER_LINE
    )


def _parse(data) -> dict:
    """Claude's reply → a safe classification. Anything malformed collapses to
    "unknown": a wrong category can soft-gate a good book, so a junk parse must
    never survive as a category. The confidence is clamped to [0, 1]."""
    if not isinstance(data, dict):
        return dict(UNKNOWN)
    doc_type = str(data.get("doc_type") or "").strip().lower()
    if doc_type not in DOC_TYPES:
        return dict(UNKNOWN)
    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {"doc_type": doc_type, "confidence": round(max(0.0, min(1.0, confidence)), 2)}


def classify_doc_type(pdf_path, extraction, client) -> dict:
    """What KIND of document is this? → {"doc_type": <category>, "confidence": <0-1>}.

    Never raises and never returns a category outside ``DOC_TYPES`` — every
    failure path is the fail-open "unknown". ``pdf_path`` may be None for a
    text-layer book (the vision fallback is then simply unavailable).
    """
    if client is None:
        return dict(UNKNOWN)
    try:
        if extraction_has_text(extraction):
            text = _text_sample(extraction)
            if text:
                result = client.analyze(_text_prompt(text), max_tokens=_MAX_TOKENS)
                return _parse(result.get("data"))
        # Scanned, or a text layer the quality gate refused: judge the rendered
        # opening pages instead — the same route chapter detection takes.
        if pdf_path:
            tmp = Path(tempfile.mkdtemp(prefix="doctype_"))
            try:
                paths = _render_pages(pdf_path, range(_SAMPLE_PAGES), _DETECT_WIDTH, tmp)
                if paths:
                    result = client.analyze_images_batch(
                        paths, _vision_prompt(len(paths)), max_tokens=_MAX_TOKENS
                    )
                    return _parse(result.get("data"))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        return dict(UNKNOWN)
    except Exception as exc:  # noqa: BLE001 — fail-open: classification must
        # never fail or stall indexing; "unknown" leaves the volume rules to gate
        logger.warning("doc_type classification failed: %s", exc)
        return dict(UNKNOWN)
