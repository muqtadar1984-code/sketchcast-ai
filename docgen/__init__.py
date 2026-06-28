"""Document generators — turn a chapter into an editable .docx (lesson plan,
class activities, exam paper). Dispatched from the worker by generation `kind`."""

from __future__ import annotations

from pathlib import Path


def generate_document(
    kind: str,
    book: dict,
    chapter: dict,
    analysis: dict,
    client,
    params: dict,
    out_dir: Path,
) -> Path:
    """Build the .docx for `kind` and return its path."""
    if kind == "lesson_plan":
        from docgen.lesson_plan import build
        return build(book, chapter, analysis, client, out_dir)
    if kind == "activity":
        from docgen.activity import build
        return build(book, chapter, analysis, client, out_dir)
    if kind == "exam_paper":
        from docgen.exam_paper import build
        return build(book, chapter, analysis, client, params, out_dir)
    raise ValueError(f"Unknown document kind: {kind}")
