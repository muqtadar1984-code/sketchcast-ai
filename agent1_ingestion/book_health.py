"""Book Health Score — a predictive quality read computed at index time from
signals the pipeline ALREADY produces (text-layer coverage, scanned-ness,
chapter-detection plausibility, page count). Surfaced to the teacher the
moment a book finishes indexing so bad scans are caught BEFORE they generate
failed lessons — prevention ahead of the support agent's diagnosis.

Pure function, no I/O: `compute_book_health(extraction, chapter_defs)` →
a JSON-able dict stored on `books.health`. Deliberately honest about scanned
books: they are handled well by the vision path, so they score "Good" with an
informational note, not "Poor" — we don't want to scare teachers off scans the
pipeline processes fine.
"""

from __future__ import annotations


def _band(score: int) -> str:
    if score >= 85:
        return "excellent"
    if score >= 70:
        return "good"
    if score >= 50:
        return "fair"
    return "poor"


def compute_book_health(extraction, chapter_defs: list[dict]) -> dict:
    """Return {score, band, dimensions, problems, recommendation, note}.

    dimensions each 0-100:
      text_layer — is the text machine-readable, and across how many pages?
      structure  — did chapter detection find a real, usable unit list?
    The overall score weights structure a little higher, because a wrong or
    single-chapter split hurts every downstream lesson more than a slightly
    sparse text layer (which the vision path backstops).
    """
    total_pages = int(getattr(extraction, "total_pages", 0) or 0)
    readability = float(getattr(extraction, "readability_score", 0.0) or 0.0)
    text_chars = sum(len(getattr(i, "text", "") or "") for i in getattr(extraction, "items", []))
    has_text = text_chars >= 200
    n_chapters = len(chapter_defs or [])

    problems: list[str] = []
    note: str | None = None

    # ── text-layer / OCR dimension ────────────────────────────────────────────
    if not has_text:
        # Scanned book — no text layer, but the vision path reads it well.
        text_layer = 74
        note = "Scanned book — read by AI vision (works well, adds a little processing time)."
    elif readability >= 0.75:
        text_layer = 96
    elif readability >= 0.5:
        text_layer = 84
    elif readability >= 0.3:
        text_layer = 68
        problems.append("Sparse text layer — many pages are images, so extraction may miss content.")
    else:
        text_layer = 52
        problems.append("Very little machine-readable text — the PDF may be a low-quality or partial scan.")

    # ── structure / chapter-detection dimension ───────────────────────────────
    if n_chapters >= 3:
        structure = 95
    elif n_chapters == 2:
        structure = 78
    elif n_chapters == 1:
        structure = 50
        problems.append("Only one unit was detected — chapter boundaries weren't found, so lessons can't be split by chapter.")
    else:
        structure = 40
        problems.append("No chapters detected.")

    # ── page-count sanity ─────────────────────────────────────────────────────
    if 0 < total_pages < 5:
        structure = min(structure, 55)
        problems.append("Very short document — there may not be enough content to teach from.")

    # ── overall (structure weighted a touch higher) ───────────────────────────
    score = round(text_layer * 0.45 + structure * 0.55)

    # Honest caps so a strong signal can't mask a real weakness:
    #   * a scanned book works but is never "excellent" (a clean text PDF is);
    #   * a single detected unit is a real structure problem;
    #   * no chapters / too short is a hard failure.
    if not has_text:
        score = min(score, 82)
    if n_chapters <= 1 and total_pages >= 20:
        score = min(score, 66)
    if n_chapters == 0:
        score = min(score, 45)
    if 0 < total_pages < 5:
        score = min(score, 55)
    band = _band(score)

    # ── recommendation: address the single worst signal. Driven by PROBLEMS,
    #    not the band, so a flagged issue always surfaces its fix. ─────────────
    if not problems and band in ("excellent", "good"):
        recommendation = None
    elif has_text and text_layer <= 68:
        recommendation = "Upload a higher-quality scan or a text-based PDF so the full content is captured."
    elif n_chapters <= 1:
        recommendation = "If this book has chapters, a version with a table of contents or clearer chapter headings will split it into per-chapter lessons."
    elif not has_text:
        recommendation = "This scan will work, but a clearer scan or a text-based PDF would produce the best lessons."
    else:
        recommendation = "Review the detected chapters before generating."

    return {
        "score": int(score),
        "band": band,
        "dimensions": {"text_layer": int(text_layer), "structure": int(structure)},
        "facts": {
            "pages": total_pages,
            "chapters": n_chapters,
            "has_text_layer": has_text,
            "text_coverage": round(readability, 2),
        },
        "problems": problems,
        "recommendation": recommendation,
        "note": note,
    }
