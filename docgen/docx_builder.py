"""Small python-docx helpers shared by the document generators."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

GREEN = RGBColor(0x2E, 0x6B, 0x4E)
GREY = RGBColor(0x6F, 0x6A, 0x5F)


def _clear_body(doc: Document) -> None:
    """Drop a template's existing paragraphs/tables but keep section properties
    (header/footer references), so generated content sits on the school styling."""
    body = doc.element.body
    for child in list(body):
        if child.tag.endswith("}p") or child.tag.endswith("}tbl"):
            body.remove(child)


def new_doc(title: str, subtitle: str = "", template: str | None = None) -> Document:
    if template:
        doc = Document(template)
        _clear_body(doc)
    else:
        doc = Document()
    h = doc.add_heading(title, level=0)
    for run in h.runs:
        run.font.color.rgb = GREEN
    if subtitle:
        p = doc.add_paragraph()
        r = p.add_run(subtitle)
        r.italic = True
        r.font.color.rgb = GREY
        r.font.size = Pt(10)
    return doc


def heading(doc: Document, text: str, level: int = 1) -> None:
    h = doc.add_heading(text, level=level)
    for run in h.runs:
        run.font.color.rgb = GREEN


def para(doc: Document, text: str, bold: bool = False, italic: bool = False) -> None:
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.bold = bold
    r.italic = italic


def labelled(doc: Document, label: str, value: str) -> None:
    p = doc.add_paragraph()
    r = p.add_run(f"{label}: ")
    r.bold = True
    p.add_run(value)


def bullets(doc: Document, items: Iterable[str]) -> None:
    for it in items:
        if str(it).strip():
            doc.add_paragraph(str(it), style="List Bullet")


def numbered(doc: Document, items: Iterable[str]) -> None:
    for it in items:
        if str(it).strip():
            doc.add_paragraph(str(it), style="List Number")


def table(doc: Document, headers: list[str], rows: list[list[str]]) -> None:
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Light Grid Accent 1"
    for i, htext in enumerate(headers):
        cell = t.rows[0].cells[i]
        cell.text = ""
        r = cell.paragraphs[0].add_run(htext)
        r.bold = True
    for row in rows:
        cells = t.add_row().cells
        for i, val in enumerate(row[: len(headers)]):
            cells[i].text = str(val)


def save(doc: Document, path: str | Path) -> Path:
    path = Path(path)
    doc.save(str(path))
    return path


def chapter_excerpt(chapter: dict, limit: int = 6000) -> str:
    """Flatten a chapter's section text into a single excerpt for prompting."""
    parts: list[str] = []
    for sec in chapter.get("sections", []) or []:
        title = (sec.get("section_title") or "").strip()
        content = (sec.get("content") or "").strip()
        if title:
            parts.append(title)
        if content:
            parts.append(content)
    text = "\n".join(parts).strip()
    return text[:limit] if text else (chapter.get("title") or "")


def concept_names(analysis: dict, limit: int = 12) -> list[str]:
    """Best-effort concept names from the Agent 2 analysis (shape-tolerant)."""
    names: list[str] = []
    for key in ("concepts", "key_concepts"):
        for c in analysis.get(key, []) or []:
            name = c.get("name") or c.get("concept") or c.get("term") if isinstance(c, dict) else str(c)
            if name:
                names.append(str(name))
    return names[:limit]


def chapter_grounding(book: dict, chapter: dict, analysis: dict) -> str:
    """The shared grounding block prepended to EVERY artifact prompt for a chapter.

    Must be byte-identical across all of a book's artifacts (worksheet, exam, …)
    so Claude prompt-caches it — written once, re-read at ~0.1x on the rest. Keep
    it deterministic: only book/chapter/analysis-derived text, no per-artifact
    params, no timestamps. Artifact-specific instructions go AFTER this, in each
    builder's own prompt, and reference "the chapter above"."""
    grade = book.get("grade") or "school"
    subject = book.get("subject") or "general"
    title = chapter.get("title") or "Chapter"
    concepts = ", ".join(concept_names(analysis)) or "(see content)"
    return (
        f"Audience: {grade} {subject} students.\n"
        f'Chapter: "{title}"\n'
        f"Key concepts: {concepts}\n\n"
        f'Chapter content:\n"""\n{chapter_excerpt(chapter)}\n"""'
    )
