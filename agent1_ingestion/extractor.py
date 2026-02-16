"""PDF text extraction & structure detection using PyMuPDF."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF


@dataclass
class FontInfo:
    size: float
    flags: int  # fitz font flags (bold, italic, etc.)
    name: str


@dataclass
class TextSpan:
    text: str
    font_size: float
    is_bold: bool
    page_num: int  # 0-indexed internally


@dataclass
class PageData:
    page_num: int  # 0-indexed
    text: str
    spans: list[TextSpan] = field(default_factory=list)
    char_count: int = 0
    is_low_text: bool = False


@dataclass
class TOCItem:
    level: int
    title: str
    page_num: int  # 0-indexed


@dataclass
class FontProfile:
    chapter_title_size: float = 0.0
    section_heading_size: float = 0.0
    body_size: float = 0.0


@dataclass
class ExtractionResult:
    pages: list[PageData]
    toc: list[TOCItem]
    font_profile: FontProfile
    total_pages: int
    readability_score: float


def _is_bold(flags: int) -> bool:
    """Check if font flags indicate bold. Bit 4 (16) is bold in fitz."""
    return bool(flags & (1 << 4))


def extract_toc(doc: fitz.Document) -> list[TOCItem]:
    """Extract table of contents from PDF bookmarks."""
    toc_items: list[TOCItem] = []
    raw_toc = doc.get_toc(simple=True)
    for level, title, page in raw_toc:
        toc_items.append(TOCItem(level=level, title=title.strip(), page_num=page - 1))
    return toc_items


def analyse_fonts(pages: list[PageData]) -> FontProfile:
    """
    Determine the font size roles by frequency analysis.

    - Most common font size → body text
    - Largest recurring size (>= 3 occurrences, above body) → chapter titles
    - Second largest → section headings
    """
    size_counter: Counter[float] = Counter()
    for page in pages:
        for span in page.spans:
            if span.text.strip():
                rounded = round(span.font_size, 1)
                size_counter[rounded] += 1

    if not size_counter:
        return FontProfile()

    body_size = size_counter.most_common(1)[0][0]

    # Sizes that appear at least 3 times and are larger than body
    candidate_sizes = sorted(
        [s for s, c in size_counter.items() if s > body_size and c >= 2],
        reverse=True,
    )

    chapter_size = candidate_sizes[0] if candidate_sizes else body_size
    section_size = (
        candidate_sizes[1]
        if len(candidate_sizes) > 1
        else (candidate_sizes[0] if candidate_sizes else body_size)
    )

    return FontProfile(
        chapter_title_size=chapter_size,
        section_heading_size=section_size,
        body_size=body_size,
    )


def extract_page(doc: fitz.Document, page_idx: int, low_text_threshold: int) -> PageData:
    """Extract text and font information from a single page."""
    page = doc[page_idx]
    blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]

    spans: list[TextSpan] = []
    full_text_parts: list[str] = []

    for block in blocks:
        if block.get("type") != 0:  # text blocks only
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text.strip():
                    continue
                font_size = span.get("size", 0)
                flags = span.get("flags", 0)
                spans.append(
                    TextSpan(
                        text=text,
                        font_size=round(font_size, 1),
                        is_bold=_is_bold(flags),
                        page_num=page_idx,
                    )
                )
                full_text_parts.append(text)

    full_text = " ".join(full_text_parts)
    char_count = len(full_text.strip())

    return PageData(
        page_num=page_idx,
        text=full_text,
        spans=spans,
        char_count=char_count,
        is_low_text=char_count < low_text_threshold,
    )


def extract_pdf(
    pdf_path: str | Path,
    low_text_threshold: int = 20,
) -> ExtractionResult:
    """
    Full extraction pipeline for a PDF file.

    Returns an ExtractionResult with all pages, TOC, font profile,
    and readability score.
    """
    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)

    # Extract TOC from bookmarks
    toc = extract_toc(doc)

    # Extract all pages
    pages: list[PageData] = []
    for i in range(total_pages):
        pages.append(extract_page(doc, i, low_text_threshold))

    doc.close()

    # Font analysis
    font_profile = analyse_fonts(pages)

    # Readability score: fraction of pages with meaningful text
    pages_with_text = sum(1 for p in pages if not p.is_low_text)
    readability_score = pages_with_text / total_pages if total_pages > 0 else 0.0

    return ExtractionResult(
        pages=pages,
        toc=toc,
        font_profile=font_profile,
        total_pages=total_pages,
        readability_score=round(readability_score, 2),
    )
