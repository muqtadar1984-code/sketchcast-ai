"""Content structuring: transforms raw extraction into hierarchical JSON."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from agent1_ingestion.config import PROCESSED_DIR
from agent1_ingestion.extractor import ExtractionResult, FontProfile, PageData, TextSpan, TOCItem
from agent1_ingestion.image_extractor import ExtractedImage
from agent1_ingestion.models import (
    ChapterContent,
    ImageInfo,
    ImagePosition,
    KeyBox,
    Section,
    StructuredBook,
    Subsection,
    TOCEntry,
)

# Patterns for special textbook boxes
BOX_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("activity", re.compile(
        r"(LET['']?S\s+EXPLORE|TRY\s+THIS|ACTIVITY|DO\s+THIS|HANDS[- ]ON|LET US DO)",
        re.IGNORECASE,
    )),
    ("definition", re.compile(
        r"(KEY\s+TERM|DEFINITION|GLOSSARY|IMPORTANT\s+TERM|KEYWORD)",
        re.IGNORECASE,
    )),
    ("info", re.compile(
        r"(DID\s+YOU\s+KNOW|DON['']?T\s+MISS\s+OUT|FUN\s+FACT|NOTE|REMEMBER|INFO\s+BOX|INTERESTING\s+FACT)",
        re.IGNORECASE,
    )),
    ("exercise", re.compile(
        r"(EXERCISE|QUESTION|REVIEW\s+QUESTION|PRACTICE|ASSESSMENT|CHECK\s+YOUR\s+UNDERSTANDING|WORKSHEET)",
        re.IGNORECASE,
    )),
    ("quote", re.compile(
        r"(QUOTE|CITATION|SOURCE|SAID|ACCORDING\s+TO)",
        re.IGNORECASE,
    )),
]


def _classify_span(span: TextSpan, profile: FontProfile) -> str:
    """Classify a text span as chapter_title, section, subsection, or body."""
    if abs(span.font_size - profile.chapter_title_size) < 0.5 and profile.chapter_title_size > profile.body_size:
        return "chapter_title"
    if abs(span.font_size - profile.section_heading_size) < 0.5 and profile.section_heading_size > profile.body_size:
        return "section"
    if span.is_bold and span.font_size >= profile.body_size:
        return "subsection"
    return "body"


def _detect_key_box(text: str) -> Optional[tuple[str, str]]:
    """Check if text starts a special content box. Returns (type, title) or None."""
    for box_type, pattern in BOX_PATTERNS:
        match = pattern.search(text[:100])
        if match:
            return box_type, match.group(0).strip()
    return None


def _build_chapters_from_toc(
    toc: list[TOCItem],
    total_pages: int,
) -> list[dict]:
    """Create chapter boundary dicts from TOC entries (level 1 only)."""
    level1 = [item for item in toc if item.level == 1]
    chapters = []
    for i, item in enumerate(level1):
        end_page = (level1[i + 1].page_num - 1) if i + 1 < len(level1) else total_pages - 1
        chapters.append({
            "chapter_num": i,
            "title": item.title,
            "start_page": item.page_num,
            "end_page": end_page,
        })
    return chapters


def _infer_chapters_from_fonts(
    pages: list[PageData],
    profile: FontProfile,
) -> list[dict]:
    """Infer chapter boundaries by finding chapter-title-sized headings."""
    chapters: list[dict] = []
    current_title: Optional[str] = None
    current_start: Optional[int] = None

    for page in pages:
        for span in page.spans:
            if _classify_span(span, profile) == "chapter_title" and len(span.text.strip()) > 2:
                # Close previous chapter
                if current_title is not None:
                    chapters.append({
                        "chapter_num": len(chapters),
                        "title": current_title,
                        "start_page": current_start,
                        "end_page": page.page_num - 1 if page.page_num > 0 else 0,
                    })
                current_title = span.text.strip()
                current_start = page.page_num

    # Close last chapter
    if current_title is not None:
        chapters.append({
            "chapter_num": len(chapters),
            "title": current_title,
            "start_page": current_start,
            "end_page": pages[-1].page_num if pages else 0,
        })

    return chapters


def _build_sections(
    pages: list[PageData],
    start_page: int,
    end_page: int,
    profile: FontProfile,
) -> tuple[list[Section], list[KeyBox]]:
    """Build sections and detect key boxes for a range of pages."""
    sections: list[Section] = []
    key_boxes: list[KeyBox] = []

    current_section: Optional[Section] = None
    body_buffer: list[str] = []

    def flush_body():
        nonlocal current_section, body_buffer
        if current_section and body_buffer:
            text = " ".join(body_buffer).strip()
            if current_section.content:
                current_section.content += " " + text
            else:
                current_section.content = text
            body_buffer = []

    for page in pages:
        if page.page_num < start_page or page.page_num > end_page:
            continue

        for span in page.spans:
            text = span.text.strip()
            if not text:
                continue

            classification = _classify_span(span, profile)

            # Check for key boxes
            box_hit = _detect_key_box(text)
            if box_hit:
                flush_body()
                key_boxes.append(KeyBox(
                    type=box_hit[0],
                    title=box_hit[1],
                    content=text,
                    page_num=page.page_num,
                ))
                continue

            if classification == "chapter_title":
                # Skip chapter titles inside the chapter
                continue

            if classification == "section":
                flush_body()
                if current_section:
                    sections.append(current_section)
                current_section = Section(
                    section_title=text,
                    section_type="heading",
                    content="",
                    page_num=page.page_num,
                )

            elif classification == "subsection":
                flush_body()
                if current_section:
                    current_section.subsections.append(Subsection(
                        section_title=text,
                        content="",
                        page_num=page.page_num,
                    ))
                else:
                    # Create an implicit section
                    current_section = Section(
                        section_title=text,
                        section_type="subheading",
                        content="",
                        page_num=page.page_num,
                    )

            else:
                # Body text
                if current_section and current_section.subsections:
                    # Append to latest subsection
                    sub = current_section.subsections[-1]
                    sub.content = (sub.content + " " + text).strip() if sub.content else text
                else:
                    body_buffer.append(text)

    flush_body()
    if current_section:
        sections.append(current_section)

    # If no sections were detected, create a single section with all text
    if not sections:
        all_text = " ".join(
            span.text.strip()
            for page in pages
            if start_page <= page.page_num <= end_page
            for span in page.spans
            if span.text.strip()
        )
        if all_text:
            sections.append(Section(
                section_title="Content",
                section_type="body",
                content=all_text,
                page_num=start_page,
            ))

    return sections, key_boxes


def _map_images_to_chapter(
    images: list[ExtractedImage],
    start_page: int,
    end_page: int,
) -> list[ImageInfo]:
    """Filter images that belong to a chapter's page range."""
    result: list[ImageInfo] = []
    for img in images:
        if start_page <= img.page_num <= end_page:
            result.append(ImageInfo(
                filename=img.filename,
                page_num=img.page_num,
                context_label=img.context_label,
                position=ImagePosition(
                    x=img.x, y=img.y, width=img.width, height=img.height,
                ),
            ))
    return result


def structure_book(
    book_id: str,
    title: str,
    author: str,
    isbn: Optional[str],
    extraction: ExtractionResult,
    images: list[ExtractedImage],
) -> StructuredBook:
    """
    Transform extraction results into the full StructuredBook hierarchy.

    Chapter detection strategy:
      1. Use PDF TOC bookmarks if available.
      2. Fall back to font-size-based chapter detection.
      3. If nothing found, treat the entire document as one chapter.
    """
    profile = extraction.font_profile

    # Determine chapters
    if extraction.toc:
        chapter_defs = _build_chapters_from_toc(extraction.toc, extraction.total_pages)
    else:
        chapter_defs = _infer_chapters_from_fonts(extraction.pages, profile)

    # Fallback: entire document as one chapter
    if not chapter_defs:
        chapter_defs = [{
            "chapter_num": 0,
            "title": title or "Full Document",
            "start_page": 0,
            "end_page": extraction.total_pages - 1,
        }]

    # Fix end_page for last chapter
    if chapter_defs:
        chapter_defs[-1]["end_page"] = extraction.total_pages - 1

    # Build chapter content
    toc_entries: list[TOCEntry] = []
    chapters: list[ChapterContent] = []

    for cdef in chapter_defs:
        sections, key_boxes = _build_sections(
            extraction.pages,
            cdef["start_page"],
            cdef["end_page"],
            profile,
        )
        chapter_images = _map_images_to_chapter(
            images, cdef["start_page"], cdef["end_page"],
        )

        toc_entries.append(TOCEntry(
            chapter_num=cdef["chapter_num"],
            title=cdef["title"],
            start_page=cdef["start_page"],
        ))

        chapters.append(ChapterContent(
            chapter_num=cdef["chapter_num"],
            title=cdef["title"],
            start_page=cdef["start_page"],
            end_page=cdef["end_page"],
            sections=sections,
            images=chapter_images,
            key_boxes=key_boxes,
        ))

    return StructuredBook(
        book_id=book_id,
        title=title,
        author=author,
        isbn=isbn,
        total_pages=extraction.total_pages,
        total_chapters=len(chapters),
        readability_score=extraction.readability_score,
        table_of_contents=toc_entries,
        chapters=chapters,
    )


def save_structured_book(book: StructuredBook) -> Path:
    """Persist the structured book as a JSON file and return the path."""
    output_path = PROCESSED_DIR / f"{book.book_id}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(book.model_dump(), f, indent=2, ensure_ascii=False)
    return output_path
