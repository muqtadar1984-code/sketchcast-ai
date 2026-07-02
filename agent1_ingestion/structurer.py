"""Content structuring: transforms raw extraction into hierarchical JSON.

Uses DocItem semantic labels (level, item_type) from extractor.py instead
of font-size heuristics to detect chapters, sections, and subsections.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from agent1_ingestion.config import PROCESSED_DIR
from agent1_ingestion.extractor import DocItem, ExtractionResult, TOCItem
from agent1_ingestion.image_extractor import ExtractedImage
from agent1_ingestion.models import (ChapterContent, ImageInfo, ImagePosition,
                                     KeyBox, Section, StructuredBook,
                                     Subsection, TOCEntry)

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


def _infer_chapters_from_items(
    items: list[DocItem],
    total_pages: int,
) -> list[dict]:
    """Infer chapter boundaries from level-1 DocItems (title / top-level headings)."""
    titles = [i for i in items if i.level == 1 and len(i.text.strip()) > 2]
    # If no level-1 titles found, treat level-2 headings as chapters
    if not titles:
        titles = [i for i in items if i.level == 2 and len(i.text.strip()) > 2]

    chapters = []
    for idx, item in enumerate(titles):
        end_page = (titles[idx + 1].page_num - 1) if idx + 1 < len(titles) else total_pages - 1
        chapters.append({
            "chapter_num": idx,
            "title": item.text.strip(),
            "start_page": item.page_num,
            "end_page": end_page,
        })
    return chapters


_FILENAME_RE = re.compile(r"\.(pdf|docx?|epub|indd|ai)$", re.I)
_SUBSEC_RE = re.compile(r"^(\d{1,2})\.\d")

# Labeled chapter markers: "Chapter 3", "UNIT 3", "Lesson Three", "Topic 3:",
# "Module 3 — Fractions", etc. Group 2 = the number (digits or a word),
# group 3 = any title text on the same line.
_LABEL_RE = re.compile(
    r"^(chapter|unit|lesson|topic|module|theme|week|part)\s+"
    r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)"
    r"\s*[:.\-–—]?\s*(.{0,80})$",
    re.IGNORECASE,
)
_WORD_NUMS = {
    w: n for n, w in enumerate(
        ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
         "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
         "seventeen", "eighteen", "nineteen", "twenty"], start=1,
    )
}


def _marker_number(token: str) -> Optional[int]:
    token = token.strip().lower()
    if token.isdigit():
        n = int(token)
        return n if 1 <= n <= 99 else None
    return _WORD_NUMS.get(token)


def _toc_is_usable(toc: list[TOCItem], total_pages: int) -> bool:
    """Reject outlines that clearly aren't real chapters — e.g. a PDF stitched
    from a few source files whose only bookmarks are the file names."""
    level1 = [t for t in toc if t.level == 1]
    if not level1:
        return False
    if any(_FILENAME_RE.search((t.title or "")) for t in level1):
        return False
    # Too few top-level entries for a sizeable book → probably not chapters.
    if total_pages > 40 and len(level1) < 3:
        return False
    return True


def _titles_from_contents(items: list[DocItem]) -> dict[int, str]:
    """Parse a printed 'Contents' page into {chapter_number: title}.

    A bare integer N is a chapter (not a page number) when an 'N.x' subsection
    follows it shortly; the chapter title is the text in between.
    """
    contents_page = next(
        (it.page_num for it in items if it.text.strip().lower() == "contents"), None
    )
    if contents_page is None:
        return {}
    page_items = [i for i in items if contents_page <= i.page_num <= contents_page + 2]
    titles: dict[int, str] = {}
    for i, it in enumerate(page_items):
        t = it.text.strip()
        if not t.isdigit():
            continue
        n = int(t)
        if not (1 <= n <= 99):
            continue
        window = page_items[i + 1 : i + 4]
        sub_ok = any(
            (m := _SUBSEC_RE.match(w.text.strip())) and int(m.group(1)) == n
            for w in window
        )
        if not sub_ok:
            continue
        for w in window:
            wt = w.text.strip()
            if wt and not wt.isdigit() and not _SUBSEC_RE.match(wt) and any(c.isalpha() for c in wt):
                titles.setdefault(n, wt)
                break
    return titles


def _title_near(items: list[DocItem], idx: int, page: int) -> Optional[str]:
    """Best-effort chapter title from the heading text right after a number marker."""
    parts: list[str] = []
    for it in items[idx + 1 : idx + 7]:
        if it.page_num != page:
            break
        t = it.text.strip()
        if not t or t.isdigit() or len(t) <= 2:
            continue
        if t.lower() == "getting started" or _SUBSEC_RE.match(t):
            break
        parts.append(t)
        if sum(len(p) for p in parts) > 28:
            break
    title = " ".join(parts).strip()
    return title if len(title) >= 3 else None


def _detect_labeled_chapters(items: list[DocItem], total_pages: int) -> list[dict]:
    """Detect chapters from labelled heading markers — "Chapter 3", "Unit 3",
    "Lesson Three", "Topic 3: Fractions", … — forming an ascending run from 1.
    Higher-signal than bare numbers, so it runs first.

    Each label family (chapter/unit/lesson/…) is tracked separately so a book
    with "Unit 1" containing "Lesson 1..12" doesn't interleave the two
    sequences; the family with the longest ascending run wins."""
    contents_titles = _titles_from_contents(items)
    # family -> {n -> (page, idx, inline_title)}
    families: dict[str, dict[int, tuple[int, int, str]]] = {}
    for idx, it in enumerate(items):
        if it.level not in (1, 2):
            continue
        m = _LABEL_RE.match(it.text.strip())
        if not m:
            continue
        n = _marker_number(m.group(2))
        if n is None:
            continue
        fam = families.setdefault(m.group(1).lower(), {})
        if n not in fam:
            fam[n] = (it.page_num, idx, m.group(3).strip(" .:-–—"))

    best: list[dict] = []
    for first in families.values():
        chapters: list[dict] = []
        n = 1
        while n in first:
            pg, idx, inline = first[n]
            if chapters and pg <= chapters[-1]["start_page"]:
                break  # pages must strictly increase
            title = (
                (inline if any(c.isalpha() for c in inline) else "")
                or contents_titles.get(n)
                or _title_near(items, idx, pg)
                or f"Chapter {n}"
            )
            chapters.append({"chapter_num": len(chapters), "title": title, "start_page": pg, "end_page": 0})
            n += 1
        if len(chapters) >= 3 and len(chapters) > len(best):
            best = chapters

    if not best:
        return []
    for i in range(len(best)):
        best[i]["end_page"] = (
            best[i + 1]["start_page"] - 1 if i + 1 < len(best) else total_pages - 1
        )
    return best


def _detect_numbered_chapters(items: list[DocItem], total_pages: int) -> list[dict]:
    """Detect chapters from bare-number heading markers ('1', '2', …) that form
    an ascending run from 1 — robust for textbooks whose PDF outline is missing
    or junk. Titles come from the printed contents page when available."""
    contents_titles = _titles_from_contents(items)
    first_page: dict[int, int] = {}
    first_idx: dict[int, int] = {}
    for idx, it in enumerate(items):
        t = it.text.strip()
        if it.level in (1, 2) and t.isdigit():
            n = int(t)
            if 1 <= n <= 99 and n not in first_page:
                first_page[n] = it.page_num
                first_idx[n] = idx

    chapters: list[dict] = []
    n = 1
    while n in first_page:
        pg = first_page[n]
        if chapters and pg <= chapters[-1]["start_page"]:
            break  # pages must strictly increase
        title = (
            contents_titles.get(n)
            or _title_near(items, first_idx[n], pg)
            or f"Chapter {n}"
        )
        chapters.append({"chapter_num": len(chapters), "title": title, "start_page": pg, "end_page": 0})
        n += 1

    if len(chapters) < 3:  # not enough signal to trust
        return []
    for i in range(len(chapters)):
        chapters[i]["end_page"] = (
            chapters[i + 1]["start_page"] - 1 if i + 1 < len(chapters) else total_pages - 1
        )
    return chapters


def _build_sections_from_items(
    items: list[DocItem],
    start_page: int,
    end_page: int,
) -> tuple[list[Section], list[KeyBox]]:
    """Build sections and detect key boxes for a page range using DocItem semantics."""
    sections: list[Section] = []
    key_boxes: list[KeyBox] = []
    current_section: Optional[Section] = None
    body_buffer: list[str] = []

    def flush_body() -> None:
        nonlocal current_section, body_buffer
        if current_section and body_buffer:
            text = " ".join(body_buffer).strip()
            current_section.content = (
                (current_section.content + " " + text).strip()
                if current_section.content
                else text
            )
            body_buffer.clear()

    chapter_items = [i for i in items if start_page <= i.page_num <= end_page]

    for item in chapter_items:
        text = item.text.strip()
        if not text:
            continue

        # Skip the chapter-level heading — already captured in chapter metadata
        if item.level == 1:
            continue

        # Key-box detection (check any item type before structural classification)
        box_hit = _detect_key_box(text)
        if box_hit:
            flush_body()
            key_boxes.append(KeyBox(
                type=box_hit[0],
                title=box_hit[1],
                content=text,
                page_num=item.page_num,
            ))
            continue

        if item.level == 2:  # section heading
            flush_body()
            if current_section:
                sections.append(current_section)
            current_section = Section(
                section_title=text,
                section_type="heading",
                content="",
                page_num=item.page_num,
            )

        elif item.level == 3:  # subsection heading
            flush_body()
            if current_section:
                current_section.subsections.append(Subsection(
                    section_title=text,
                    content="",
                    page_num=item.page_num,
                ))
            else:
                # No parent section yet — create an implicit one
                current_section = Section(
                    section_title=text,
                    section_type="subheading",
                    content="",
                    page_num=item.page_num,
                )

        else:  # body text (level == 0)
            if current_section and current_section.subsections:
                sub = current_section.subsections[-1]
                sub.content = (sub.content + " " + text).strip() if sub.content else text
            else:
                body_buffer.append(text)

    flush_body()
    if current_section:
        sections.append(current_section)

    # Fallback: no sections detected → single content section with all body text
    if not sections:
        all_text = " ".join(
            i.text.strip() for i in chapter_items if i.text.strip() and i.level == 0
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
    pdf_path: Optional[str] = None,
    client=None,
    known_chapters: Optional[list[dict]] = None,
) -> StructuredBook:
    """
    Transform extraction results into the full StructuredBook hierarchy.

    Chapter detection cascade (first hit wins):
      0. ``known_chapters`` — boundaries already detected at indexing time
         (num/title/start_page/end_page), so re-processing is cheap + identical.
      1. The PDF's outline (TOC bookmarks), if it looks like real chapters.
      2. Labelled markers — "Chapter 3" / "Unit 3" / "Lesson Three" / "Topic 3"…
      3. Bare-number heading markers ("1", "2", … ascending).
      4. Level-1 heading inference.
      5. When a ``client`` (Claude) is provided and the above found nothing:
         a text book gets an LLM pass over its headings digest; a SCANNED book
         (no text layer) gets a vision pass that READS the rendered pages —
         handling any labelling convention, since Claude reads pages like a
         person does.
      6. Whole document as a single chapter.
    """
    used_known = bool(known_chapters) and all(
        "start_page" in c and "end_page" in c for c in known_chapters
    )
    if used_known:
        chapter_defs = [
            {
                "chapter_num": c.get("chapter_num", c.get("num", i)),
                "title": c.get("title") or f"Chapter {i + 1}",
                "start_page": int(c["start_page"]),
                "end_page": int(c["end_page"]),
            }
            for i, c in enumerate(known_chapters)
        ]
    elif extraction.toc and _toc_is_usable(extraction.toc, extraction.total_pages):
        chapter_defs = _build_chapters_from_toc(extraction.toc, extraction.total_pages)
    else:
        # No usable outline → labelled markers, bare numbers, heading inference.
        chapter_defs = _detect_labeled_chapters(extraction.items, extraction.total_pages)
        if not chapter_defs:
            chapter_defs = _detect_numbered_chapters(extraction.items, extraction.total_pages)
        if not chapter_defs:
            chapter_defs = _infer_chapters_from_items(extraction.items, extraction.total_pages)

    # Claude fallback: heuristics found nothing usable (0 or 1 pseudo-chapter).
    # Never second-guess stored known_chapters — the split must stay identical
    # to what indexing stored (and vision must not be re-billed per generation).
    if client is not None and not used_known and len(chapter_defs) <= 1:
        from agent1_ingestion.vision_chapters import (
            detect_chapters_from_text_llm,
            detect_chapters_vision,
            extraction_has_text,
        )

        if extraction_has_text(extraction):
            smart = detect_chapters_from_text_llm(extraction, client)
        elif pdf_path:
            smart = detect_chapters_vision(pdf_path, extraction.total_pages, client)
        else:
            smart = []
        if smart:
            chapter_defs = smart

    # Final fallback: whole document as a single chapter
    if not chapter_defs:
        chapter_defs = [{
            "chapter_num": 0,
            "title": title or "Full Document",
            "start_page": 0,
            "end_page": extraction.total_pages - 1,
        }]

    # Ensure the last chapter covers all remaining pages
    if chapter_defs:
        chapter_defs[-1]["end_page"] = extraction.total_pages - 1

    toc_entries: list[TOCEntry] = []
    chapters: list[ChapterContent] = []

    for cdef in chapter_defs:
        sections, key_boxes = _build_sections_from_items(
            extraction.items,
            cdef["start_page"],
            cdef["end_page"],
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
