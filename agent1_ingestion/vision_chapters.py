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

import bisect
import copy
import logging
import shutil
import tempfile
from pathlib import Path

from agent1_ingestion.book_health import text_layer_is_usable
from agent1_ingestion.chapter_check import (audit_chapter_list, topic_of,
                                            verify_chapter_content)

logger = logging.getLogger(__name__)

# Detection: low-res is enough to read headings. Transcription: higher res.
_DETECT_WIDTH = 720
_DETECT_MAX_PAGES = 120
# Openers are reported by their POSITION among the images in a batch (1..N), and we
# derive the physical page ourselves. A smaller batch keeps that ordinal range tight
# so a printed page number the model might read off the page (e.g. "34" from a
# contents list) falls OUTSIDE 1..N and is rejected instead of silently becoming a
# wrong physical page — the exact scanned-book mislabel this module guards against.
_DETECT_BATCH = 20
_TRANSCRIBE_WIDTH = 1000
_TRANSCRIBE_MAX_PAGES = 18
_TRANSCRIBE_CHUNK = 6  # pages per call — keeps output well under max_tokens
_JPEG_QUALITY = 60

# A book "has no text" when its extracted items total less than this.
_MIN_TEXT_CHARS = 200


def extraction_has_text(extraction) -> bool:
    """True if the PDF has a usable text layer.

    Usable means readable, not merely present. A 1,008-page book whose text
    layer extracted as ``Thegreatestconstantofmoderntimesischange`` (0.1%
    spaces, CJK mojibake in the front matter) passed the volume test with a
    million characters to spare — so the text route was taken, health scored it
    "excellent", and 17 kits were generated from unreadable source. The quality
    gate lives in ``book_health.text_quality``; failing it sends the book down
    the SAME vision/OCR path a book with no text layer at all already takes,
    which is the path that reads scans well."""
    total = sum(len(i.text or "") for i in extraction.items)
    if total < _MIN_TEXT_CHARS:
        return False
    return text_layer_is_usable(extraction)


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


def _starts_to_defs(pairs: list[tuple[int, str]], total_pages: int) -> list[dict]:
    """0-indexed (start_page, title) pairs → clean, ascending chapter defs.

    Dedupes by start page, drops out-of-range starts, sorts, and fills each
    chapter's end_page from the next start. Fewer than 2 real starts is no better
    than the whole-book fallback, so it returns []."""
    seen: set[int] = set()
    cleaned: list[tuple[int, str]] = []
    for start, title in pairs:
        try:
            start = int(start)
        except (TypeError, ValueError):
            continue
        title = str(title or "").strip()
        if not (0 <= start < total_pages) or start in seen or not title:
            continue
        seen.add(start)
        cleaned.append((start, title[:120]))
    cleaned.sort()
    if len(cleaned) < 2:
        return []
    defs = []
    for i, (start, title) in enumerate(cleaned):
        end = cleaned[i + 1][0] - 1 if i + 1 < len(cleaned) else total_pages - 1
        defs.append({"chapter_num": i, "title": title, "start_page": start, "end_page": end})
    return defs


def _normalize_starts(raw: list[dict], total_pages: int) -> list[dict]:
    """Claude's [{title, start_page(1-based)}] → clean, ascending chapter defs.

    For the TEXT-LLM detector, whose page prefixes are the extractor's real
    (physical) page numbers, so start_page-1 is the true 0-indexed page."""
    pairs = [
        (
            (int(ch.get("start_page", 0)) - 1) if _is_int(ch.get("start_page")) else -1,
            str(ch.get("title") or "").strip(),
        )
        for ch in raw
        if isinstance(ch, dict)
    ]
    return _starts_to_defs(pairs, total_pages)


def _is_int(v) -> bool:
    try:
        int(v)
        return True
    except (TypeError, ValueError):
        return False


def _page_of(path) -> int:
    """The 0-indexed physical page a rendered image is of (files are p{page}.jpg)."""
    return int(Path(path).stem[1:])


def _parse_openers(data, page_numbers: list[int]) -> list[tuple[int, str]]:
    """Turn one batch's Claude reply into 0-indexed (physical_start, title) pairs.

    ``page_numbers`` is the physical page of each image actually shown, in order.
    The model reports each opener by its IMAGE NUMBER (1..N), and we look the
    physical page up from ``page_numbers`` — so (a) a printed page number the model
    might copy off a contents list (e.g. "34") lands OUTSIDE 1..N and is rejected
    instead of silently becoming a wrong physical page, and (b) the mapping holds
    even if a page failed to render (no reliance on the batch being contiguous)."""
    items = data.get("openers", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    n = len(page_numbers)
    pairs: list[tuple[int, str]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        idx = it.get("image_number")
        if not _is_int(idx):
            continue
        idx = int(idx)
        if not (1 <= idx <= n):
            # Out of range → almost certainly a printed page number, not a position.
            logger.warning(
                "vision opener image_number %r outside 1..%d — skipped (looks like a "
                "printed page number, not an image position)", it.get("image_number"), n,
            )
            continue
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        pairs.append((page_numbers[idx - 1], title))
    return pairs


def detect_chapters_vision(pdf_path: str | Path, total_pages: int, client) -> list[dict]:
    """Have Claude READ a scanned book's pages and find its top-level units.

    Returns chapter dicts [{chapter_num, title, start_page, end_page}] (0-indexed
    pages) or [] when nothing trustworthy was found.

    Positions come from the IMAGE ORDER we control, never from any page number the
    model reads off the page: for each rendered image the model says whether it is
    a unit opener, and we map its image number back to a physical page. This is the
    fix for the failure where the detector copied printed contents-page numbers as
    start pages — landing a "Computer storage" unit on the networking pages.
    """
    pages_to_scan = min(total_pages, _DETECT_MAX_PAGES)
    tmp = Path(tempfile.mkdtemp(prefix="vision_toc_"))
    found: list[tuple[int, str]] = []
    try:
        for batch_start in range(0, pages_to_scan, _DETECT_BATCH):
            batch = range(batch_start, min(batch_start + _DETECT_BATCH, pages_to_scan))
            paths = _render_pages(pdf_path, batch, _DETECT_WIDTH, tmp)
            if not paths:
                continue
            n = len(paths)
            prompt = (
                f"You are shown {n} consecutive scanned pages of a school textbook, in order. "
                f"Call them image 1 (the FIRST) through image {n} (the LAST).\n\n"
                "Find every image that is the OPENING page of a NEW top-level teaching unit — "
                "the page with the big unit banner/title where a unit visibly begins. Textbooks "
                "label these many ways: Chapter 3, Unit 3, Lesson Three, Topic 3, Module 3, a "
                "numbered theme, or a full-page unit title. Do NOT count the cover, publisher "
                "pages, section headings inside a unit, or exercise blocks.\n\n"
                "CRITICAL — how to report position:\n"
                f"- Identify each opener by its IMAGE NUMBER in this set (1..{n}) — the position of "
                "the image among the ones shown to you right now.\n"
                "- NEVER report a printed page number. If an image is a table of contents / index "
                "that LISTS units with page numbers, do NOT treat it as an opener and IGNORE those "
                "printed numbers — they are the book's page numbers, not the positions of these images.\n"
                "- Report only units whose opening page is actually among these images.\n\n"
                'Return ONLY JSON: {"openers": [{"image_number": <int 1..' + str(n) + '>, '
                '"unit_number": <int or null>, "title": "<unit title>"}]}. '
                "Empty list if no unit opens in these images."
            )
            try:
                result = client.analyze_images_batch(paths, prompt, max_tokens=1500)
                found.extend(_parse_openers(result.get("data", {}), [_page_of(p) for p in paths]))
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

    defs = _starts_to_defs(found, total_pages)
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


# ── self-heal: relocate a mislabeled chapter to the pages that match its title ──
#
# Detection can still misplace a unit on a messy scan. These helpers turn a
# detected title↔content MISMATCH into a repair instead of a wrong lesson:
# read what each chapter's stored pages are actually about, and when the topic
# disagrees with the title, find the pages in the book that DO match and move
# the chapter there — verified before it is trusted.


def _range_text(extraction, start: int, end: int, cap: int = 4000) -> str:
    """Text-layer content of physical pages [start, end] (for TEXT books)."""
    txt = " ".join(
        it.text for it in extraction.items
        if start <= it.page_num <= end and (it.text or "").strip()
    )
    return " ".join(txt.split())[:cap]


def chapter_opening_snippets_vision(pdf_path: str | Path, chapters: list[dict], client) -> dict[int, str]:
    """Read each chapter's opening page and describe its topic, for the audit.

    Returns ``{chapter_num: "<=short topic description>"}``. This is what lets the
    index-time audit SEE a scanned book: with no text layer, the text-based
    snippet is empty and every chapter looks fine, so a mislabeled unit slips
    through. One low-res image per chapter, batched. Best-effort — a failure just
    yields fewer snippets (those chapters fall back to the empty text snippet).
    """
    if client is None or not chapters:
        return {}
    out: dict[int, str] = {}
    tmp = Path(tempfile.mkdtemp(prefix="vision_snip_"))
    try:
        for i in range(0, len(chapters), _DETECT_BATCH):
            group = chapters[i:i + _DETECT_BATCH]
            paths: list[Path] = []
            nums: list[int] = []
            for c in group:
                sp = int(c.get("start_page", 0))
                pp = _render_pages(pdf_path, range(sp, sp + 1), _DETECT_WIDTH, tmp)
                if pp:
                    paths.append(pp[0])
                    nums.append(int(c.get("num", c.get("chapter_num", 0))))
            if not paths:
                continue
            n = len(paths)
            prompt = (
                f"You are shown {n} scanned textbook pages, in order (image 1..{n}). Each is the "
                "page where a chapter supposedly begins. For EACH image, give a short (<=12 words) "
                "description of the MAIN topic or heading visible on that page — what a reader would "
                "say that page is about.\n\n"
                'Return ONLY JSON: {"topics": ["<image 1 topic>", ...]} with exactly '
                f"{n} entries, in image order."
            )
            try:
                result = client.analyze_images_batch(paths, prompt, max_tokens=1200)
                data = result.get("data", {})
                topics = data.get("topics") if isinstance(data, dict) else data
                if isinstance(topics, list):
                    for k, num in enumerate(nums):
                        if k < len(topics) and topics[k]:
                            out[num] = str(topics[k])[:200]
            except Exception as exc:  # noqa: BLE001 — one bad batch loses only its snippets
                logger.warning("opening-snippet batch at chapter %d failed: %s", i, exc)
            finally:
                for p in paths:
                    p.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 — snippets are best-effort
        logger.error("vision opening snippets failed: %s", exc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def match_title_to_units(
    title: str,
    units: list[dict],
    client,
    avoid_starts: set[int] | None = None,
    near_page: int | None = None,
) -> dict | None:
    """Pick the detected unit that is the SAME teaching unit as ``title``.

    ``units`` come from a fresh detection pass. Returns the chosen unit dict or
    None (no clear match). ``avoid_starts`` excludes already-used pages so a
    duplicated topic isn't matched twice; ``near_page`` breaks ties toward the
    stored location."""
    if client is None:
        return None
    avoid = avoid_starts or set()
    cand = [u for u in units if int(u.get("start_page", -1)) not in avoid and (u.get("title") or "").strip()]
    if not cand:
        return None
    topic = topic_of(title) or title
    lines = [
        f"#{i} title={u.get('title')!r} start_page={u.get('start_page')}"
        for i, u in enumerate(cand)
    ]
    hint = (
        f"\nIf two candidates are equally plausible, prefer the one whose start_page is closest to {near_page}."
        if near_page is not None else ""
    )
    prompt = (
        "You are matching a requested chapter to a list of detected units from the SAME textbook.\n"
        f"Requested chapter title: {title!r}\nRequested topic: {topic!r}\n\n"
        "Candidate units:\n" + "\n".join(lines) + "\n\n"
        "Which ONE candidate is the SAME teaching unit as the requested chapter (same subject "
        "matter)? If none clearly matches, answer -1." + hint + "\n"
        'Respond ONLY as JSON: {"index": <int>}.'
    )
    try:
        data = client.analyze(prompt, max_tokens=100).get("data") or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("match_title_to_units failed: %s", exc)
        return None
    idx = data.get("index") if isinstance(data, dict) else None
    if not _is_int(idx):
        return None
    idx = int(idx)
    return cand[idx] if 0 <= idx < len(cand) else None


def _validate_chapter_list(chapters: list[dict], total_pages: int) -> bool:
    """Corruption gate: in-range pages, end>=start, unique start_pages, unique
    chapter_nums. A heal that can't produce a structurally valid list is reverted."""
    if not chapters:
        return False
    starts: set[int] = set()
    nums: set[int] = set()
    for c in chapters:
        try:
            s = int(c["start_page"])
            e = int(c["end_page"])
            num = int(c.get("chapter_num", c.get("num")))
        except (KeyError, TypeError, ValueError):
            return False
        if not (0 <= s < total_pages) or not (0 <= e < total_pages) or e < s:
            return False
        if s in starts or num in nums:
            return False
        starts.add(s)
        nums.add(num)
    return True


def _clamp_overlaps(chapters: list[dict], total_pages: int, anchor_starts: set[int]) -> None:
    """In-place: cap each chapter's end_page at the next TRUSTED boundary.

    ``anchor_starts`` are the start pages of trusted chapters (confirmed
    relocations + chapters the audit did not flag). Only those act as caps, so a
    chapter with a STALE/untrusted start (a mismatched chapter we couldn't
    relocate) can never truncate a confirmed relocation down to a sliver. Never
    renumbers or reorders."""
    anchors = sorted({int(a) for a in anchor_starts})
    for c in chapters:
        s = int(c["start_page"])
        i = bisect.bisect_right(anchors, s)  # first anchor strictly greater than s
        nxt = anchors[i] if i < len(anchors) else total_pages
        c["end_page"] = max(s, min(int(c["end_page"]), int(nxt) - 1))


def _confirm_relocation(
    pdf_path: str | Path, extraction, title: str, start: int, end: int, client, scanned: bool
) -> bool:
    """Independent STRICT check that the destination pages really teach ``title``,
    before an index-time relocation is committed to storage. A title-match alone
    can pick a wrong-but-similar unit, and the permissive generation guard would
    then wave it through — so require positive content confirmation here. If the
    destination can't be read, refuse (keep the chapter where it is; the
    generation-time heal can still recover it later)."""
    if not topic_of(title):
        return True  # generic label — nothing descriptive to confirm
    if scanned:
        snip = chapter_opening_snippets_vision(
            pdf_path, [{"chapter_num": 0, "start_page": int(start)}], client
        ).get(0, "")
    else:
        snip = _range_text(extraction, int(start), int(end))
    if not snip:
        return False
    ok, _ = verify_chapter_content(title, snip, client, strict=True)
    return ok


def heal_chapter_boundaries(
    pdf_path: str | Path, extraction, chapters: list[dict], client
) -> tuple[list[dict], list[int]]:
    """Index-time repair: audit the detected list (with VISION eyes on scanned
    books) and relocate any chapter whose pages don't match its title to the
    pages that do — preserving chapter_num and title, moving only the pages, and
    only after an independent STRICT confirm of the destination.

    Returns ``(healed_chapters, relocated_nums)``. On any trouble it returns the
    input list unchanged, and it reverts wholesale only if the result can't be
    made structurally valid — a heal must never corrupt storage.
    """
    if client is None or len(chapters) < 2:
        return chapters, []

    scanned = not extraction_has_text(extraction)
    snippets = chapter_opening_snippets_vision(pdf_path, chapters, client) if scanned else None
    audit = audit_chapter_list(extraction, chapters, client, snippets=snippets)

    # Title corrections apply regardless of relocation.
    for c in chapters:
        num = int(c.get("chapter_num", c.get("num", 0)))
        fix = audit.get("titles", {}).get(num)
        if fix:
            c["title"] = fix

    mismatched = list(audit.get("mismatched") or [])
    if not mismatched:
        return chapters, []

    # One detection pass gives the canonical topic→page map to relocate against.
    if scanned:
        units = detect_chapters_vision(pdf_path, extraction.total_pages, client)
    else:
        units = detect_chapters_from_text_llm(extraction, client)
    if not units:
        for c in chapters:  # mark, but keep pages — better a suspect label than a wrong move
            if int(c.get("chapter_num", c.get("num", 0))) in mismatched:
                c["relocation"] = "suspect"
        return chapters, []

    candidate = copy.deepcopy(chapters)
    # Reserve EVERY chapter's ORIGINAL start: a relocation must never target a page
    # another chapter still occupies (an unresolved 'suspect' keeps its original
    # start), so confirmed relocations can never collide and force a wholesale
    # revert. `used_new` also blocks two relocations landing on the same page.
    original_starts = {int(c["start_page"]) for c in candidate}
    used_new: set[int] = set()
    relocated: list[int] = []
    for c in candidate:
        num = int(c.get("chapter_num", c.get("num", 0)))
        if num not in mismatched:
            continue
        avoid = (original_starts - {int(c["start_page"])}) | used_new
        entry = match_title_to_units(
            c.get("title", ""), units, client,
            avoid_starts=avoid, near_page=int(c.get("start_page", 0)),
        )
        if (entry and _is_int(entry.get("start_page")) and _is_int(entry.get("end_page"))
                and _confirm_relocation(pdf_path, extraction, c.get("title", ""),
                                        int(entry["start_page"]), int(entry["end_page"]), client, scanned)):
            c["start_page"] = int(entry["start_page"])
            c["end_page"] = int(entry["end_page"])
            c["relocation"] = "relocated"
            used_new.add(int(entry["start_page"]))
            relocated.append(num)
        else:
            c["relocation"] = "suspect"

    if not relocated:
        return chapters, []

    # Clamp only against TRUSTED boundaries (confirmed relocations + un-flagged
    # chapters), so a suspect's stale start can neither truncate nor collide with a
    # confirmed relocation.
    trusted = {
        int(c["start_page"]) for c in candidate
        if c.get("relocation") == "relocated"
        or int(c.get("chapter_num", c.get("num", 0))) not in mismatched
    }
    _clamp_overlaps(candidate, extraction.total_pages, trusted)
    if _validate_chapter_list(candidate, extraction.total_pages):
        logger.info("heal_chapter_boundaries: relocated chapters %s", relocated)
        return candidate, relocated
    logger.warning("heal_chapter_boundaries: relocation did not validate — kept original list")
    return chapters, []


def relocate_chapter_for_generation(
    pdf_path: str | Path, extraction, requested: dict, client, exclude: set[int] | None = None
) -> dict:
    """Generation-time repair for a book already stored with wrong pages: find the
    pages that actually teach ``requested``'s title, transcribe them, and confirm
    (strict) before returning.

    Returns a dict with ``status``:
    * ``"ok"``    — plus start_page/end_page/source_text/actual_topic (relocate + generate).
    * ``"absent"`` — we searched properly (detected units, transcribed a candidate,
      strict-rejected it) and the topic isn't there — safe to remember + fail fast.
    * ``"incomplete"`` — we could NOT prove absence (no units detected, empty
      transcription, or nothing matched). The caller must NOT persist this as a
      permanent verdict, or a transient vision/rate-limit blip would brick a chapter
      that is actually present. Fail loud this run; retry cleanly next time.

    The transcription is not wasted work — it becomes the lesson's source text.
    Bounded: at most 2 candidates are tried."""
    if client is None:
        return {"status": "incomplete"}
    title = requested.get("title", "")
    if not topic_of(title):  # nothing descriptive to relocate by
        return {"status": "incomplete"}
    scanned = not extraction_has_text(extraction)
    if scanned:
        units = detect_chapters_vision(pdf_path, extraction.total_pages, client)
    else:
        units = detect_chapters_from_text_llm(extraction, client)
    if not units:
        return {"status": "incomplete"}  # detection outage / no structure — do NOT brick

    saw_real_rejection = False  # matched a candidate with real text, strict said no
    avoid = set(exclude or set())
    for _ in range(2):  # shortlist cap
        entry = match_title_to_units(
            title, units, client, avoid_starts=avoid, near_page=int(requested.get("start_page", 0)),
        )
        if not entry:
            break
        s = int(entry["start_page"])
        e = int(entry["end_page"])
        avoid.add(s)
        text = chapter_text_vision(pdf_path, s, e, client) if scanned else _range_text(extraction, s, e)
        if not text:
            continue  # OCR/read failure — not evidence of absence
        ok, actual = verify_chapter_content(title, text, client, strict=True)
        if ok:
            logger.info("relocate_chapter_for_generation: %r → pages %d-%d", title, s, e)
            return {"status": "ok", "start_page": s, "end_page": e,
                    "source_text": text, "actual_topic": actual}
        saw_real_rejection = True
    # Proven absent only if we actually read a candidate and strict-rejected it.
    return {"status": "absent" if saw_real_rejection else "incomplete"}
