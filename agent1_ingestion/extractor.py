"""PDF text extraction using Docling (primary) with PyMuPDF fallback.

Docling produces clean, semantically-labelled items ideal for academic PDFs
(multi-column, tables, sidebars).  PyMuPDF is used when Docling is not
installed, and always for TOC bookmark extraction.

Extraction results are cached by content-hash so cloud deployments can read
pre-processed JSON without running Docling themselves.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF — TOC extraction + fallback


# ── Public data models ────────────────────────────────────────────────

@dataclass
class DocItem:
    """A single semantic item extracted from a PDF."""

    item_type: str  # "title" | "section_header" | "paragraph" | "list_item" | "table" | "caption" | "picture" | "other"
    text: str
    page_num: int   # 0-indexed
    level: int      # 1=chapter heading, 2=section, 3=subsection, 0=body


@dataclass
class TOCItem:
    """One entry from the PDF bookmark table of contents."""

    level: int
    title: str
    page_num: int  # 0-indexed


@dataclass
class ExtractionResult:
    """Output of extract_pdf()."""

    items: list[DocItem]
    toc: list[TOCItem]
    total_pages: int
    readability_score: float
    extraction_backend: str  # "docling" | "pymupdf"
    markdown: str            # full Docling markdown export; empty string for PyMuPDF


# ── Cache helpers ─────────────────────────────────────────────────────

def _cache_key(pdf_path: Path) -> str:
    """MD5 of first 4 MB + file size → stable, content-based cache key."""
    size = pdf_path.stat().st_size
    h = hashlib.md5()
    with open(pdf_path, "rb") as fh:
        h.update(fh.read(4 * 1024 * 1024))
    h.update(str(size).encode())
    return h.hexdigest()


def _load_cache(cache_path: Path) -> Optional[ExtractionResult]:
    """Return a cached ExtractionResult, or None on miss / deserialisation error."""
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return ExtractionResult(
            items=[DocItem(**i) for i in data.get("items", [])],
            toc=[TOCItem(**t) for t in data.get("toc", [])],
            total_pages=data["total_pages"],
            readability_score=data["readability_score"],
            extraction_backend=data.get("extraction_backend", "docling"),
            markdown=data.get("markdown", ""),
        )
    except Exception:
        return None


def _save_cache(result: ExtractionResult, cache_path: Path) -> None:
    """Persist an ExtractionResult as a JSON cache file."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "items": [
            {"item_type": i.item_type, "text": i.text, "page_num": i.page_num, "level": i.level}
            for i in result.items
        ],
        "toc": [
            {"level": t.level, "title": t.title, "page_num": t.page_num}
            for t in result.toc
        ],
        "total_pages": result.total_pages,
        "readability_score": result.readability_score,
        "extraction_backend": result.extraction_backend,
        "markdown": result.markdown,
    }
    cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── PyMuPDF helpers ───────────────────────────────────────────────────

_BOLD_BIT = 1 << 4


def _is_bold(flags: int) -> bool:
    return bool(flags & _BOLD_BIT)


def _extract_toc_fitz(pdf_path: Path) -> list[TOCItem]:
    """Extract PDF bookmark TOC via PyMuPDF (more reliable than Docling for bookmarks)."""
    items: list[TOCItem] = []
    try:
        doc = fitz.open(str(pdf_path))
        for level, title, page in doc.get_toc(simple=True):
            items.append(TOCItem(level=level, title=title.strip(), page_num=page - 1))
        doc.close()
    except Exception:
        pass
    return items


def _extract_pymupdf(pdf_path: Path) -> ExtractionResult:
    """
    PyMuPDF fallback: extract text spans and classify them into DocItems
    using font-size frequency analysis.
    """
    toc = _extract_toc_fitz(pdf_path)
    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)

    # Collect raw spans: (text, font_size, is_bold, page_num)
    raw_spans: list[tuple[str, float, bool, int]] = []
    for page_idx in range(total_pages):
        page = doc[page_idx]
        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
        for block in blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if text:
                        raw_spans.append((
                            text,
                            round(span.get("size", 0), 1),
                            _is_bold(span.get("flags", 0)),
                            page_idx,
                        ))
    doc.close()

    if not raw_spans:
        return ExtractionResult(
            items=[], toc=toc, total_pages=total_pages,
            readability_score=0.0, extraction_backend="pymupdf", markdown="",
        )

    # Font-size frequency analysis: most common → body; top recurring → headings
    size_counter: Counter[float] = Counter(s[1] for s in raw_spans)
    body_size = size_counter.most_common(1)[0][0]
    candidates = sorted(
        [sz for sz, cnt in size_counter.items() if sz > body_size and cnt >= 2],
        reverse=True,
    )
    chapter_size = candidates[0] if candidates else body_size
    section_size = candidates[1] if len(candidates) > 1 else chapter_size

    # Map each span to a DocItem
    items: list[DocItem] = []
    for text, size, bold, page_num in raw_spans:
        if abs(size - chapter_size) < 0.5 and chapter_size > body_size:
            item_type, level = "title", 1
        elif abs(size - section_size) < 0.5 and section_size > body_size:
            item_type, level = "section_header", 2
        elif bold and size >= body_size:
            item_type, level = "section_header", 3
        else:
            item_type, level = "paragraph", 0
        items.append(DocItem(item_type=item_type, text=text, page_num=page_num, level=level))

    pages_with_content = len({i.page_num for i in items if i.item_type == "paragraph"})
    readability_score = round(pages_with_content / total_pages, 2) if total_pages > 0 else 0.0

    return ExtractionResult(
        items=items,
        toc=toc,
        total_pages=total_pages,
        readability_score=readability_score,
        extraction_backend="pymupdf",
        markdown="",
    )


# ── Docling extraction ────────────────────────────────────────────────

def _item_text(item) -> str:
    """Safely extract text from a Docling item."""
    if hasattr(item, "text") and isinstance(item.text, str):
        return item.text
    try:
        return item.export_to_markdown()
    except Exception:
        return ""


def _item_page(item) -> int:
    """Return the 0-indexed page number from a Docling item's provenance."""
    try:
        if item.prov:
            return item.prov[0].page_no - 1
    except (AttributeError, IndexError, TypeError):
        pass
    return 0


def _extract_docling(pdf_path: Path) -> ExtractionResult:
    """Extract PDF using Docling with OCR enabled."""
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    try:
        from docling_core.types.doc import DocItemLabel
    except ImportError:
        from docling.datamodel.base_models import DocItemLabel  # type: ignore[no-redef]

    # Map Docling labels → (item_type, base_level).
    # base_level == -1 means "derive from tree depth" (used for SECTION_HEADER).
    _LABEL_MAP: dict = {
        DocItemLabel.TITLE:          ("title",          1),
        DocItemLabel.SECTION_HEADER: ("section_header", -1),
        DocItemLabel.PARAGRAPH:      ("paragraph",       0),
        DocItemLabel.LIST_ITEM:      ("list_item",        0),
        DocItemLabel.TABLE:          ("table",            0),
        DocItemLabel.CAPTION:        ("caption",          0),
        DocItemLabel.PICTURE:        ("picture",          0),
    }

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = True

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
        }
    )

    conv_result = converter.convert(str(pdf_path))
    doc = conv_result.document
    markdown = doc.export_to_markdown()
    toc = _extract_toc_fitz(pdf_path)
    total_pages = len(doc.pages) if hasattr(doc, "pages") and doc.pages else 1

    items: list[DocItem] = []
    for item, tree_level in doc.iterate_items():
        if not hasattr(item, "label"):
            continue
        entry = _LABEL_MAP.get(item.label)
        if entry is None:
            continue  # skip groups, unknown labels, etc.

        text = _item_text(item).strip()
        if not text:
            continue

        item_type, base_level = entry
        if base_level == -1:
            # SECTION_HEADER: map tree depth → heading level
            # depth 1 = top-level section → level 1
            # depth 2 = nested section    → level 2
            # depth 3+ = sub-section      → level 3
            level = min(max(tree_level, 1), 3)
        else:
            level = base_level

        items.append(DocItem(
            item_type=item_type,
            text=text,
            page_num=_item_page(item),
            level=level,
        ))

    pages_with_content = len({i.page_num for i in items if i.item_type == "paragraph"})
    readability_score = round(pages_with_content / total_pages, 2) if total_pages > 0 else 0.0

    return ExtractionResult(
        items=items,
        toc=toc,
        total_pages=total_pages,
        readability_score=readability_score,
        extraction_backend="docling",
        markdown=markdown,
    )


# ── Public entry point ────────────────────────────────────────────────

def extract_pdf(
    pdf_path: str | Path,
    cache_dir: Optional[Path] = None,
) -> ExtractionResult:
    """
    Extract text and structure from a PDF file.

    Uses Docling if installed; falls back to PyMuPDF otherwise.
    Results are cached by content-hash to skip re-processing on subsequent calls.

    Parameters
    ----------
    pdf_path:  Path to the PDF file.
    cache_dir: Cache directory for JSON files.  Defaults to DOCLING_CACHE_DIR.
    """
    from agent1_ingestion.config import DOCLING_CACHE_DIR

    pdf_path = Path(pdf_path)
    effective_cache_dir = cache_dir if cache_dir is not None else DOCLING_CACHE_DIR
    cache_path = effective_cache_dir / f"{_cache_key(pdf_path)}.json"

    # Return cached result if available
    cached = _load_cache(cache_path)
    if cached is not None:
        return cached

    # Honour DOCLING_BACKEND=pymupdf override (used in tests / CI to skip heavy models)
    import os
    force_pymupdf = os.getenv("DOCLING_BACKEND", "").lower() == "pymupdf"

    # Try Docling first; fall back to PyMuPDF if not installed, overridden, or on error
    if force_pymupdf:
        result = _extract_pymupdf(pdf_path)
    else:
        try:
            result = _extract_docling(pdf_path)
        except Exception:
            result = _extract_pymupdf(pdf_path)

    # Persist to cache (non-critical — ignore write errors)
    try:
        _save_cache(result, cache_path)
    except Exception:
        pass

    return result
