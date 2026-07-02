"""Claude-powered chapter detection for books the text heuristics can't read.

Two failure modes the heuristics in ``structurer.py`` cannot handle:

1. **Scanned books** — every page is an image; there is no text layer at all
   (e.g. photographed/scanned textbooks). ``detect_chapters_vision`` renders the
   pages and has Claude READ them, so any labelling convention ("Chapter 3",
   "Unit 3", "Lesson Three", a themed heading…) works — Claude reads pages the
   way a person does. ``chapter_text_vision`` then transcribes a chapter's pages
   at generation time so the lesson pipeline has real content to teach from.

2. **Text books with unconventional structure** — there IS text, but no PDF
   outline and no marker pattern the heuristics recognise.
   ``detect_chapters_from_text_llm`` sends the headings digest to Claude and
   asks it to identify the top-level teaching units.

Cost guardrails: detection renders at low resolution and caps page counts;
transcription caps pages per chapter. All calls go through the shared
ClaudeClient (token usage is logged).
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Detection: low-res is enough to read headings. Transcription: higher res.
_DETECT_WIDTH = 720
_DETECT_MAX_PAGES = 120
_DETECT_BATCH = 40
_TRANSCRIBE_WIDTH = 1000
_TRANSCRIBE_MAX_PAGES = 18
_TRANSCRIBE_CHUNK = 6  # pages per call — keeps output well under max_tokens
_JPEG_QUALITY = 60

# A book "has no text" when its extracted items total less than this.
_MIN_TEXT_CHARS = 200


def extraction_has_text(extraction) -> bool:
    """True if the PDF has a usable text layer."""
    total = sum(len(i.text or "") for i in extraction.items)
    return total >= _MIN_TEXT_CHARS


def _render_pages(pdf_path: str | Path, pages: range, width: int, out_dir: Path) -> list[Path]:
    """Render PDF pages (0-indexed) to JPEGs of the given width."""
    import fitz
    from PIL import Image

    paths: list[Path] = []
    doc = fitz.open(str(pdf_path))
    try:
        for p in pages:
            if p < 0 or p >= doc.page_count:
                continue
            page = doc.load_page(p)
            zoom = width / max(1.0, page.rect.width)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            out = out_dir / f"p{p:04d}.jpg"
            img.save(str(out), "JPEG", quality=_JPEG_QUALITY)
            paths.append(out)
    finally:
        doc.close()
    return paths


def _normalize_starts(raw: list[dict], total_pages: int) -> list[dict]:
    """Claude's [{title, start_page(1-based)}] → clean, ascending chapter defs."""
    seen: set[int] = set()
    cleaned: list[tuple[int, str]] = []
    for ch in raw:
        if not isinstance(ch, dict):
            continue
        try:
            start = int(ch.get("start_page", 0)) - 1  # → 0-indexed
        except (TypeError, ValueError):
            continue
        title = str(ch.get("title") or "").strip()
        if not (0 <= start < total_pages) or start in seen or not title:
            continue
        seen.add(start)
        cleaned.append((start, title[:120]))
    cleaned.sort()
    if len(cleaned) < 2:  # one "chapter" = no better than the whole-book fallback
        return []
    defs = []
    for i, (start, title) in enumerate(cleaned):
        end = cleaned[i + 1][0] - 1 if i + 1 < len(cleaned) else total_pages - 1
        defs.append({"chapter_num": i, "title": title, "start_page": start, "end_page": end})
    return defs


def detect_chapters_vision(pdf_path: str | Path, total_pages: int, client) -> list[dict]:
    """Have Claude READ a scanned book's pages and find its top-level units.

    Returns chapter dicts [{chapter_num, title, start_page, end_page}] (0-indexed
    pages) or [] when nothing trustworthy was found.
    """
    pages_to_scan = min(total_pages, _DETECT_MAX_PAGES)
    tmp = Path(tempfile.mkdtemp(prefix="vision_toc_"))
    found: list[dict] = []
    try:
        for batch_start in range(0, pages_to_scan, _DETECT_BATCH):
            batch = range(batch_start, min(batch_start + _DETECT_BATCH, pages_to_scan))
            paths = _render_pages(pdf_path, batch, _DETECT_WIDTH, tmp)
            if not paths:
                continue
            prompt = (
                f"These are consecutive pages of a school textbook. The FIRST image is "
                f"PDF page {batch.start + 1}; each next image is the next page "
                f"(so the last is PDF page {batch.start + len(paths)}).\n\n"
                "Find every page where a NEW top-level teaching unit begins. Textbooks "
                "label these many ways — Chapter 3, Unit 3, Lesson Three, Topic 3, "
                "Module 3, a numbered theme, or just a big chapter-opening title page. "
                "Count all of these as chapters. Do NOT count: the cover, publisher "
                "pages, the contents page itself, section headings inside a unit, or "
                "exercise blocks.\n\n"
                "If one of these pages is a table of contents, use it to get accurate "
                "titles — but report each chapter's ACTUAL opening page number from the "
                "images, not the printed page number in the contents list.\n\n"
                'Return ONLY JSON: {"chapters": [{"title": "<chapter title>", '
                '"start_page": <PDF page number as counted above>}]} — chapters that '
                "START within these pages only. Empty list if none start here."
            )
            try:
                result = client.analyze_images_batch(paths, prompt, max_tokens=1500)
                data = result.get("data", {})
                # Claude may reply with a bare JSON list ("Empty list if none
                # start here") — .get only exists on dicts.
                chunk = data.get("chapters", []) if isinstance(data, dict) else data
                if isinstance(chunk, list):
                    found.extend(chunk)
            except Exception as exc:  # noqa: BLE001 — one bad batch must not
                # discard what the other batches already found
                logger.warning("vision batch at page %d failed: %s", batch_start + 1, exc)
            finally:
                for p in paths:  # keep the temp dir small across batches
                    p.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 — detection must never break indexing
        logger.error("vision chapter detection failed: %s", exc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    defs = _normalize_starts(found, total_pages)
    logger.info("vision chapter detection: %d chapters", len(defs))
    return defs


def detect_chapters_from_text_llm(extraction, client) -> list[dict]:
    """Claude fallback for TEXT books whose structure the heuristics missed.

    Sends a compact digest of headings (with page numbers) and asks Claude to
    identify the top-level teaching units, whatever they're called.
    """
    lines: list[str] = []
    for it in extraction.items:
        t = (it.text or "").strip()
        if not t:
            continue
        if it.level in (1, 2) and len(t) <= 90:
            lines.append(f"p{it.page_num + 1}: {t}")
        if len(lines) >= 350:
            break
    if len(lines) < 5:
        return []
    digest = "\n".join(lines)[:9000]

    prompt = (
        "Below are the headings extracted from a school textbook PDF, each prefixed "
        "with its PDF page number. Identify the TOP-LEVEL teaching units. Textbooks "
        "label these many ways — Chapter 3, Unit 3, Lesson Three, Topic 3, Module 3, "
        "numbered themes, or plain titles. Ignore front matter, the contents list, "
        "section headings inside a unit, and exercises.\n\n"
        f"{digest}\n\n"
        'Return ONLY JSON: {"chapters": [{"title": "<title>", "start_page": '
        "<PDF page number from the p-prefix>}]} in reading order. Return an empty "
        "list if this document genuinely has no chapter structure."
    )
    try:
        result = client.analyze(prompt, max_tokens=1500)
    except Exception as exc:  # noqa: BLE001
        logger.error("LLM text chapter detection failed: %s", exc)
        return []
    data = result.get("data", {})
    # Claude may reply with a bare JSON list — .get only exists on dicts.
    raw = data.get("chapters", []) if isinstance(data, dict) else data
    defs = _normalize_starts(raw if isinstance(raw, list) else [], extraction.total_pages)
    logger.info("LLM text chapter detection: %d chapters", len(defs))
    return defs


def chapter_text_vision(pdf_path: str | Path, start_page: int, end_page: int, client) -> str:
    """Transcribe a scanned chapter's pages (0-indexed, inclusive) to plain text.

    Used at generation time so the lesson pipeline has real chapter content.
    Caps at _TRANSCRIBE_MAX_PAGES pages to bound cost.
    """
    last = min(end_page, start_page + _TRANSCRIBE_MAX_PAGES - 1)
    prompt = (
        "These are consecutive pages of one chapter of a school textbook. "
        "Transcribe the educational content faithfully as plain text, in reading "
        "order: headings, paragraphs, definitions, examples, activity boxes, and "
        "exercise questions. Skip page numbers, running headers/footers, and "
        "decorative text. Keep the original wording. "
        "Return ONLY the transcription — no commentary, no JSON, no preamble."
    )
    tmp = Path(tempfile.mkdtemp(prefix="vision_ocr_"))
    parts: list[str] = []
    try:
        # Small chunks keep each response well under the output-token ceiling —
        # a truncated transcription would silently lose chapter content.
        for chunk_start in range(start_page, last + 1, _TRANSCRIBE_CHUNK):
            chunk = range(chunk_start, min(chunk_start + _TRANSCRIBE_CHUNK, last + 1))
            paths = _render_pages(pdf_path, chunk, _TRANSCRIBE_WIDTH, tmp)
            if not paths:
                continue
            result = client.transcribe_images(paths, prompt, max_tokens=8000)
            if result.get("text"):
                parts.append(result["text"])
            for p in paths:
                p.unlink(missing_ok=True)
        text = "\n\n".join(parts).strip()
        logger.info(
            "vision transcription: pages %d-%d -> %d chars", start_page + 1, last + 1, len(text)
        )
        return text
    except Exception as exc:  # noqa: BLE001
        logger.error("vision chapter transcription failed: %s", exc)
        return "\n\n".join(parts).strip()  # keep whatever was transcribed
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
