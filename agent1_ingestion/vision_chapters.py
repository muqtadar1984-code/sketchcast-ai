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
import re
import shutil
import tempfile
from pathlib import Path

from agent1_ingestion.book_health import text_layer_is_usable
from agent1_ingestion.chapter_check import (audit_chapter_list, topic_of,
                                            verify_chapter_content)
from agent1_ingestion.chapter_quality import covered_page_count

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

# The most pages a heal may UNCOVER before it is refused (see the acceptance
# check in heal_chapter_boundaries). A correct relocation legitimately loses a
# few pages — the Mona relocation loses 5 of 88 — while the Sara incident's
# hole was 36 of 341; 8 pages / 5% sits between them with room on both sides.
_HEAL_MAX_LOST_PAGES = 8
_HEAL_MAX_LOST_SHARE = 0.05


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


def _starts_to_defs(pairs: list[tuple], total_pages: int) -> list[dict]:
    """0-indexed (start_page, title[, kind]) tuples → clean, ascending chapter
    defs.

    Dedupes by start page, drops out-of-range starts, sorts, and fills each
    chapter's end_page from the next start. Fewer than 2 real starts is no better
    than the whole-book fallback, so it returns [].

    ``kind`` ("unit" | "apparatus") is the detector's semantic unit-vs-apparatus
    verdict (founder decision 2026-08-24: apparatus is not a chapter). An
    apparatus entry STAYS IN the def list — its start is what bounds the
    previous unit's end (a Glossary opener is exactly where unit 9 stops) — and
    carries ``kind: "apparatus"`` so the structurer's split_apparatus moves it
    out and records it after the boundaries are settled."""
    seen: set[int] = set()
    cleaned: list[tuple[int, str, str]] = []
    for tup in pairs:
        start, title = tup[0], tup[1]
        kind = str((tup[2] if len(tup) > 2 else None) or "unit").strip().lower()
        try:
            start = int(start)
        except (TypeError, ValueError):
            continue
        title = str(title or "").strip()
        if not (0 <= start < total_pages) or start in seen or not title:
            continue
        seen.add(start)
        cleaned.append((start, title[:120], "apparatus" if kind == "apparatus" else "unit"))
    cleaned.sort()
    if len(cleaned) < 2:
        return []
    defs = []
    for i, (start, title, kind) in enumerate(cleaned):
        end = cleaned[i + 1][0] - 1 if i + 1 < len(cleaned) else total_pages - 1
        d = {"chapter_num": i, "title": title, "start_page": start, "end_page": end}
        if kind == "apparatus":
            d["kind"] = "apparatus"
        defs.append(d)
    return defs


def _normalize_starts(raw: list[dict], total_pages: int) -> list[dict]:
    """Claude's [{title, start_page(1-based)}] → clean, ascending chapter defs.

    For the TEXT-LLM detector, whose page prefixes are the extractor's real
    (physical) page numbers, so start_page-1 is the true 0-indexed page."""
    pairs = [
        (
            (int(ch.get("start_page", 0)) - 1) if _is_int(ch.get("start_page")) else -1,
            str(ch.get("title") or "").strip(),
            str(ch.get("kind") or "unit"),
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


def _parse_openers(data, page_numbers: list[int]) -> list[tuple[int, str, str]]:
    """Turn one batch's Claude reply into 0-indexed (physical_start, title,
    kind) triples — kind "unit" or "apparatus" (missing/unknown reads "unit",
    so a model that ignores the field degrades to the pre-apparatus behaviour).

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
    pairs: list[tuple[int, str, str]] = []
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
        kind = str(it.get("kind") or "unit").strip().lower()
        pairs.append((page_numbers[idx - 1], title,
                      "apparatus" if kind == "apparatus" else "unit"))
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
                "Find every image that is the OPENING page of a NEW top-level section. Two kinds:\n"
                '- kind "unit": a TEACHING unit — the page with the big unit banner/title where a '
                "unit visibly begins. Textbooks label these many ways: Chapter 3, Unit 3, Lesson "
                "Three, Topic 3, Module 3, a numbered theme, or a full-page unit title.\n"
                '- kind "apparatus": book APPARATUS, in any language — the cover, a table of '
                "contents, a copyright/imprint page, acknowledgements, a glossary, an index, an "
                "answer key, or a reference/skills section (e.g. \"Science Skills\"). These are NOT "
                "teaching units, but report where they BEGIN so unit boundaries land correctly.\n"
                "Do NOT report section headings inside a unit or exercise blocks.\n\n"
                "CRITICAL — how to report position:\n"
                f"- Identify each opener by its IMAGE NUMBER in this set (1..{n}) — the position of "
                "the image among the ones shown to you right now.\n"
                "- NEVER report a printed page number. If an image is a table of contents / index "
                "that LISTS units with page numbers, report it as an apparatus opener and IGNORE "
                "those printed numbers — they are the book's page numbers, not the positions of "
                "these images.\n"
                "- Report only sections whose opening page is actually among these images.\n\n"
                'Return ONLY JSON: {"openers": [{"image_number": <int 1..' + str(n) + '>, '
                '"unit_number": <int or null>, "title": "<title>", '
                '"kind": "unit" | "apparatus"}]}. '
                "Empty list if nothing opens in these images."
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

    # CONTENTS-PAGE route (2026-08-24, with the bookmark rung removed): the
    # page-scan window above reads only the first _DETECT_MAX_PAGES pages, so
    # on a 339-page scan it finds units 1-3 and the rest of the book falls off
    # the map — the dokumen.pub LB8 book would honestly gate at 3 of its 9
    # units. Books PRINT their own structure source: read the contents page,
    # calibrate printed→physical page numbering against the openers the window
    # already found, extrapolate the remaining entries, and VERIFY each
    # extrapolated opener by looking at its page. Best-effort: any failure
    # leaves the window scan's honest partial result (which health then gates).
    if total_pages > pages_to_scan and found:
        try:
            extra, contents_units = _extend_from_contents(
                pdf_path, total_pages, pages_to_scan, found, client
            )
            if extra:
                logger.info(
                    "vision contents-page route: %d verified opener(s) beyond the %d-page window",
                    len(extra), pages_to_scan,
                )
                found.extend(extra)
            # THE CONTENTS PAGE SETS THE ALTITUDE, not just the missing pages.
            #
            # Measured on the founder's own re-upload (2026-08-24, book
            # 3c46ac8d): 17 chapters where the book has 9. Units 4-9 were
            # perfect — they came from the contents route. Units 1 and 3 were
            # SHREDDED into their own subsections — "Respiration" followed by
            # "1.3 Breathing", "1.4 Respiration", "1.5 Blood"; "Forces and
            # energy" followed by 3.3 through 3.7 — because those units sit
            # inside the 120-page window and the window scan reports every
            # heading that looks like an opener, at whatever depth the page
            # happens to show. The guardrails caught it (gate confirm, 58/fair,
            # "section numbers like 1.3 appear as whole chapters"), which is the
            # honest floor — but a gated wrong list is not the goal when the
            # book has already TOLD US its nine units on its contents page.
            #
            # So when the contents page yielded a usable unit list, a window-scan
            # opener that is NOT one of those units and sits inside a span the
            # page asserts is CLOSED — between two units it numbers
            # consecutively — is a subsection, and is dropped. Where the
            # numbering jumps the span stays open and the window's finding
            # stands, which is what stops a partial contents read from merging
            # two real units.
            _drop_subsections_below_contents(found, contents_units)
        except Exception as exc:  # noqa: BLE001 — the window result must survive
            logger.warning("vision contents-page route failed: %s", exc)

    # Backstop for the books the route above cannot help: no contents page, or
    # one that never calibrated. A decimal-numbered opener sitting among plainly
    # numbered units is a subsection on the strength of its own title, and this
    # is the only altitude signal available when the book prints no usable
    # contents page at all.
    _drop_subsection_openers(found)

    defs = _starts_to_defs(found, total_pages)
    logger.info("vision chapter detection: %d chapters", len(defs))
    return defs


# ── contents-page route: printed page numbers, calibrated and verified ───────
# The scanned-book mislabel incident taught this module never to TRUST a
# printed page number as a physical index — the fix was reporting openers by
# image position. This route does not walk that back: printed numbers are used
# only AFTER calibration against openers whose physical pages the window scan
# established by position, and every extrapolated page is then verified by
# actually looking at it. Untrusted numbers in, verified positions out.

_CONTENTS_SCAN_PAGES = 12   # contents pages live in the first few leaves
_CONTENTS_MIN_ANCHORS = 2   # printed→physical offset needs 2 agreeing anchors


def _read_contents_entries(pdf_path: str | Path, client, tmp: Path) -> list[dict]:
    """Transcribe the printed contents page(s) in the book's opening leaves.

    Returns [{"number": int|None, "title": str, "printed_page": int,
    "kind": "unit"|"apparatus"}] — [] when no contents page is found or the
    reply doesn't parse. The printed numbers are UNTRUSTED until calibrated.
    """
    paths = _render_pages(pdf_path, range(0, _CONTENTS_SCAN_PAGES), _DETECT_WIDTH, tmp)
    if not paths:
        return []
    n = len(paths)
    prompt = (
        f"You are shown the first {n} pages of a scanned school textbook, in order "
        f"(image 1..{n}). One or more of them may be the printed TABLE OF CONTENTS.\n\n"
        "If a contents page is present, transcribe its TOP-LEVEL entries only — the "
        "book's units/chapters and its end matter (glossary, index, answer key, "
        "reference/skills sections) — with the printed page number each entry shows. "
        "Skip sub-sections listed inside a unit.\n\n"
        'For each entry give: "number" (the unit number, or null for unnumbered end '
        'matter), "title", "printed_page" (the page number AS PRINTED on the contents '
        'page), and "kind" — "unit" for a teaching unit, "apparatus" for cover/'
        "contents/glossary/index/answers/reference-or-skills sections.\n\n"
        'Return ONLY JSON: {"entries": [{"number": <int or null>, "title": "<t>", '
        '"printed_page": <int>, "kind": "unit" | "apparatus"}]}. '
        "Empty list if none of these pages is a table of contents."
    )
    try:
        result = client.analyze_images_batch(paths, prompt, max_tokens=2000)
    finally:
        for p in paths:
            p.unlink(missing_ok=True)
    data = result.get("data", {})
    entries = data.get("entries") if isinstance(data, dict) else data
    out: list[dict] = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or not _is_int(e.get("printed_page")):
            continue
        title = str(e.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "number": int(e["number"]) if _is_int(e.get("number")) else None,
            "title": title[:120],
            "printed_page": int(e["printed_page"]),
            "kind": "apparatus" if str(e.get("kind") or "").lower() == "apparatus" else "unit",
        })
    return _drop_subsection_entries(out)


# A decimal section number leading a contents entry: "1.3 Breathing", "3.4
# Turning forces". Requires a digit on BOTH sides of the separator, so
# "Chapter 1. Introduction" and a bare "4 Ecosystems" are untouched.
_SUBSECTION_TITLE_RE = re.compile(r"^\s*\d{1,3}\s*[.·‧٫]\s*\d")


def _drop_subsection_entries(entries: list[dict]) -> list[dict]:
    """Remove sub-section rows the contents reply should never have included.

    The prompt already says "TOP-LEVEL entries only ... Skip sub-sections listed
    inside a unit", and on the founder's book the model obeyed. This is the
    guard for when it does not, because the cost of that is the exact bug this
    whole route exists to fix: a Cambridge contents page is a Page|Unit TABLE
    that prints the sub-sections INTERLEAVED with the units —

        8   1 Respiration
        8   1.1 The human respiratory system
        12  1.2 Gas exchange
        19  1.3 Breathing        <- reported as a chapter in the live failure
        40  2 Properties of materials

    — so a single non-compliant reply would both (a) feed sub-section anchors
    into the altitude reconciler, silently making it inert, and (b) let the
    render-verified ADD path promote "1.3 Breathing" to a chapter on the
    contents route's own authority. Prompt compliance was the only thing
    standing between the fix and its own failure mode; this repo has already
    paid for that assumption once, when a model put SSML into a field the
    prompt reserved for clean text and no sanitizer existed to catch it.

    SELF-LIMITING: the rows only go when at least two NON-sub-section units
    survive them. A curriculum that genuinely numbers its top level "1.1, 1.2,
    1.3" would otherwise have its entire contents list deleted; there, the
    decimal rows ARE the top level and are kept.
    """
    flags = [bool(_SUBSECTION_TITLE_RE.match(e["title"])) for e in entries]
    if not any(flags):
        return entries
    subs = [e for e, f in zip(entries, flags) if f]
    kept = [e for e, f in zip(entries, flags) if not f]
    if len([e for e in kept if e["kind"] != "apparatus"]) < 2:
        return entries
    logger.info(
        "contents-page route: ignored %d sub-section row(s) the reply included despite "
        "being asked for top-level entries only — %s",
        len(subs), "; ".join(repr(e["title"]) for e in subs[:6]),
    )
    return kept


def _calibrate_printed_offset(entries: list[dict], found: list[tuple]) -> int | None:
    """The constant ``physical_0based - (printed - 1)`` offset, or None.

    Anchored on entries the window scan ALREADY located by image position:
    an entry matches an opener when the opener's title contains the entry's
    unit number as a leading marker, or the two titles overlap. Requires
    _CONTENTS_MIN_ANCHORS anchors agreeing on ONE offset with no dissenter —
    printed numbering that doesn't calibrate cleanly is printed numbering
    this route refuses to use.
    """
    votes: dict[int, int] = {}
    matched = 0
    for e in entries:
        printed0 = e["printed_page"] - 1
        for tup in found:
            phys, title = int(tup[0]), str(tup[1])
            t_norm = " ".join(title.casefold().split())
            e_norm = " ".join(e["title"].casefold().split())
            title_match = bool(e_norm) and (e_norm in t_norm or t_norm in e_norm)
            num_match = (
                e["number"] is not None
                and t_norm.split()[:1] == [str(e["number"])]
            )
            if not (title_match or num_match):
                continue
            votes[phys - printed0] = votes.get(phys - printed0, 0) + 1
            matched += 1
            break
    if not votes:
        return None
    offset, count = max(votes.items(), key=lambda kv: kv[1])
    if count < _CONTENTS_MIN_ANCHORS or count != matched:
        return None  # too few anchors, or the anchors disagree
    return offset


# How far a window-scan opener may sit from a contents entry and still be
# considered THE SAME unit. The calibrated page comes from printed numbering and
# the window's page from a rendered banner; a unit whose banner spans a spread,
# or a book with one inserted plate, puts them a page apart without either being
# wrong. Two is enough slack for that and far short of the shortest real section
# (the LB8 sections that caused this run are 5-8 pages apart).
_CONTENTS_SAME_UNIT_SLACK = 2

# Below this many calibrated entries the contents page is not a credible map of
# the whole book (a half-read page, a single anchor) and must not be allowed to
# delete anything the window actually SAW.
_CONTENTS_MIN_FOR_AUTHORITY = 3


def _drop_subsections_below_contents(
    found: list[tuple], contents_units: list[tuple]
) -> None:
    """Remove window-scan openers that are subsections of a contents-named unit.

    Mutates ``found`` in place (the caller holds the only reference).

    THE INCIDENT, 2026-08-24, the founder's own re-upload (book 3c46ac8d): 17
    chapters for a 9-unit book. Units 4-9 came from the contents route and were
    exactly right. Units 1 and 3 sit inside the 120-page window, and the window
    scan reports whatever looks like an opener at whatever depth the page shows
    — so "Respiration" was followed by "1.3 Breathing", "1.4 Respiration",
    "1.5 Blood", and "Forces and energy" by 3.3 through 3.7. The chapter-quality
    validator caught it honestly (gate confirm, 58/fair, "section numbers like
    1.3 appear as whole chapters") — but the book had already TOLD us its nine
    units on its own contents page, so gating was the floor, not the answer.

    The rule, and the reason it is narrow. Deleting is the whole point here — a
    reconciler that only ever ADDED would leave all 17 chapters standing — so
    the safety cannot come from refusing to delete. It comes from deleting only
    where the contents page has explicitly said there is no room:

    An opener is dropped when it falls strictly inside a CLOSED span — a pair of
    consecutive contents entries the page itself asserts are adjacent. Two
    entries are adjacent when their printed unit numbers are consecutive
    ("1 Respiration" then "2 Properties of materials" leaves nothing between
    them, so p20 can only be a subsection of unit 1). Where the numbering JUMPS,
    the span stays open and the window's finding stands: a contents read that
    returned units 1, 2, 4, 5 has left a unit-3 shaped hole, and a window opener
    sitting in it is the best evidence anyone has for unit 3. That is the
    difference between this and blanket replacement, and it is what keeps a
    PARTIAL contents read from quietly merging two real units — the one way this
    function could destroy teaching material.

    Books whose contents page carries no numbers at all (common outside Latin
    scripts) fall back to LIST ORDER: the vision reply preserves the printed
    order, so adjacent rows are adjacent units. The residual risk there — a read
    that silently skipped a middle row — is one the contents route already runs
    for its own extrapolation, not a new one.

    APPARATUS IS UNTOUCHED throughout. Its openers bound the units around them,
    and split_apparatus needs them present when the boundaries settle.
    """
    if len(contents_units) < _CONTENTS_MIN_FOR_AUTHORITY:
        return
    ordered = sorted(contents_units, key=lambda t: int(t[0]))
    anchors = [int(t[0]) for t in ordered]
    numbers = [_unit_number_of(t) for t in ordered]

    # Spans where the contents page asserts there is nothing in between.
    closed: list[tuple[int, int]] = []
    for i in range(len(ordered) - 1):
        lo, hi = anchors[i], anchors[i + 1]
        a, b = numbers[i], numbers[i + 1]
        # Both numbered → trust arithmetic. Either unnumbered (including every
        # apparatus entry, which never carries a unit number) → trust the order
        # the page printed them in.
        if a is None or b is None or b == a + 1:
            closed.append((lo, hi))

    def is_anchor(page: int) -> bool:
        return any(abs(page - a) <= _CONTENTS_SAME_UNIT_SLACK for a in anchors)

    kept, dropped = [], []
    for tup in found:
        page, title = int(tup[0]), str(tup[1])
        kind = str((tup[2] if len(tup) > 2 else None) or "unit").lower()
        boxed_in = any(lo < page < hi for lo, hi in closed)
        if kind != "apparatus" and boxed_in and not is_anchor(page):
            dropped.append((page, title))
            continue
        kept.append(tup)
    if not dropped:
        return
    logger.info(
        "contents-page route: dropped %d subsection opener(s) below the %d unit(s) the "
        "contents page names — %s",
        len(dropped), len(anchors),
        "; ".join(f"{t!r}@p{p + 1}" for p, t in dropped[:8]),
    )
    found[:] = kept


# Leading unit number on a contents-page entry: "1 Respiration", "Unit 3:",
# "Bab 4 -", "(2)". The lookahead rejects a DECIMAL section number — "1.3
# Breathing" must not read as unit 1, or a subsection that reached the contents
# list would close a span it has no business closing.
_CONTENTS_UNIT_NUM_RE = re.compile(
    r"^\W*(?:unit|chapter|chap|ch|lesson|topic|module|bab|unidad|chapitre|kapitel)?"
    r"\W*(\d{1,3})(?![.,·‧]\s*\d)",
    re.IGNORECASE,
)


def _drop_subsection_openers(found: list[tuple]) -> None:
    """Drop openers whose own titles mark them as sub-sections. In place.

    The contents-page reconciler is the better instrument and runs first, but it
    needs a contents page that reads and calibrates. Plenty of scans have
    neither, and there the window scan's mixed-altitude output would otherwise
    ship unreconciled — "Respiration" and "1.3 Breathing" side by side as equal
    chapters, each with its own credit-billed kit.

    Deliberately narrower than the reconciler: it can only see titles, so it
    only catches sub-sections a book NUMBERS as such, and is blind to one that
    is merely a smaller heading. Its self-limiting rule is the same — nothing
    goes unless at least two plainly-titled units survive, so a curriculum whose
    top level really is "1.1, 1.2, 1.3" keeps every chapter it has.
    """
    flags = [
        bool(_SUBSECTION_TITLE_RE.match(str(t[1] or "")))
        and str((t[2] if len(t) > 2 else None) or "unit").lower() != "apparatus"
        for t in found
    ]
    if not any(flags):
        return
    kept = [t for t, f in zip(found, flags) if not f]
    if len([t for t in kept
            if str((t[2] if len(t) > 2 else None) or "unit").lower() != "apparatus"]) < 2:
        return
    dropped = [t for t, f in zip(found, flags) if f]
    logger.info(
        "vision scan: dropped %d sub-section opener(s) by title — %s",
        len(dropped), "; ".join(f"{str(t[1])!r}@p{int(t[0]) + 1}" for t in dropped[:8]),
    )
    found[:] = kept


def _contents_unit_number(title: str) -> int | None:
    """The printed unit number, or None when the entry carries none."""
    m = _CONTENTS_UNIT_NUM_RE.match(title.strip())
    return int(m.group(1)) if m else None


def _unit_number_of(entry: tuple) -> int | None:
    """The unit number for one calibrated contents entry.

    Prefers the `number` the contents-page reply carried in its own field —
    _read_contents_entries already parses and validates it, and on the very
    common layout that prints the number in a separate column from the title
    ("4" ⟂ "Ecosystems") it is the ONLY place the number survives. Falls back
    to reading it off the front of the title for callers holding the older
    3-tuple shape. Apparatus never has one by definition: whatever digits sit in
    "Answers to Unit 3" are not a unit number, and letting them read as one
    would close a span the book left open.
    """
    kind = str((entry[2] if len(entry) > 2 else None) or "unit").lower()
    if kind == "apparatus":
        return None
    if len(entry) > 3 and entry[3] is not None:
        try:
            return int(entry[3])
        except (TypeError, ValueError):
            pass
    return _contents_unit_number(str(entry[1]))


def _extend_from_contents(
    pdf_path: str | Path, total_pages: int, _scanned_to: int, found: list[tuple], client
) -> tuple[list[tuple[int, str, str]], list[tuple[int, str, str, int | None]]]:
    """`(verified_openers_the_window_missed, every_calibrated_contents_entry)`,
    both derived from the printed contents page. See the route comment above.

    The SECOND list is what makes the contents page the book's altitude
    authority rather than just a source of missing pages: it is every entry the
    contents page named, calibrated to physical pages, whether or not the window
    scan had already found it. The caller uses it to drop window-scan openers
    that are subsections of a named unit.

    `_scanned_to` is retained for call-site symmetry and logging only: the
    window's last page is deliberately NOT a boundary for candidates any more
    (see the filter below), because the misses this route rescues happen inside
    the window as well as past it."""
    tmp = Path(tempfile.mkdtemp(prefix="vision_contents_"))
    try:
        entries = _read_contents_entries(pdf_path, client, tmp)
        if not entries:
            return [], []
        offset = _calibrate_printed_offset(entries, found)
        if offset is None:
            logger.info("contents-page route: printed numbering did not calibrate — unused")
            return [], []
        # Every contents entry, calibrated — the altitude authority the caller
        # reconciles against. Built before the "did the window already find it?"
        # filter below, because an entry the window found is still a UNIT and
        # still has to protect its own interior from subsection openers.
        calibrated: list[tuple[int, str, str]] = []
        for e in entries:
            phys = e["printed_page"] - 1 + offset
            if 0 <= phys < total_pages:
                calibrated.append((phys, e["title"], e["kind"], e.get("number")))
        calibrated.sort()
        # Candidates are every contents entry the window did not ALREADY FIND —
        # not merely every entry beyond the window's last page.
        #
        # `phys < scanned_to` used to skip them, on the assumption that the scan
        # had settled everything it looked at. It has not: the window reads pages
        # but can MISS a unit banner inside them (a full-bleed photographic
        # opener, a banner mid-page, a page the render dropped). On the founder's
        # 339-page Cambridge scan the window found units 1 and 2 and missed unit
        # 3 at physical page 68 — inside the window — so the old filter discarded
        # the contents page's own correct answer for it and the book shipped a
        # unit short. A unit the window looked at but did not see is exactly the
        # case the contents page exists to rescue.
        #
        # `phys in used` still skips: an opener the window DID find needs no
        # rescue, and re-verifying it would spend a vision call to agree with
        # itself. Out-of-range still skips. Everything else gets verified by
        # looking, which is what makes admitting in-window candidates safe —
        # a wrong extrapolation is refused by the render check below, not
        # trusted because it came from the contents page.
        candidates = []
        used = {int(t[0]) for t in found}
        for e in entries:
            phys = e["printed_page"] - 1 + offset
            if not (0 <= phys < total_pages) or phys in used:
                continue
            candidates.append((phys, e))
        if not candidates:
            return [], calibrated
        # VERIFY by looking: render each candidate page and ask whether a
        # section really opens there. An extrapolated page that fails the look
        # is dropped — a missing unit gates honestly; a wrong boundary bills
        # kits against the wrong pages forever.
        paths, kept_entries = [], []
        for phys, e in candidates:
            pp = _render_pages(pdf_path, range(phys, phys + 1), _DETECT_WIDTH, tmp)
            if pp:
                paths.append(pp[0])
                kept_entries.append((phys, e))
        if not paths:
            return [], calibrated
        n = len(paths)
        expect = "; ".join(
            f"image {i + 1}: {e['title']!r}" for i, (_, e) in enumerate(kept_entries)
        )
        prompt = (
            f"You are shown {n} scanned textbook pages (image 1..{n}). According to the "
            f"book's contents page, each should be the OPENING page of: {expect}.\n\n"
            "For EACH image say whether a top-level section really does begin on that "
            "page (a unit banner/title, or the start of a glossary/index/answers/skills "
            "section), and the title visible on it.\n\n"
            'Return ONLY JSON: {"pages": [{"image_number": <int>, "opens_section": '
            '<true|false>, "title": "<visible title or empty>"}]} with exactly '
            f"{n} entries."
        )
        result = client.analyze_images_batch(paths, prompt, max_tokens=1200)
        data = result.get("data", {})
        rows = data.get("pages") if isinstance(data, dict) else data
        verdicts: dict[int, bool] = {}
        for r in rows if isinstance(rows, list) else []:
            if isinstance(r, dict) and _is_int(r.get("image_number")):
                verdicts[int(r["image_number"])] = bool(r.get("opens_section"))
        out: list[tuple[int, str, str]] = []
        for i, (phys, e) in enumerate(kept_entries):
            if verdicts.get(i + 1):
                out.append((phys, e["title"], e["kind"]))
            else:
                logger.warning(
                    "contents-page route: %r extrapolated to page %d but no section "
                    "opens there — dropped", e["title"], phys + 1,
                )
        return out, calibrated
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
        "numbered themes, or plain titles. Ignore section headings inside a unit and "
        "exercises.\n\n"
        "ALSO list book APPARATUS wherever it starts, in any language — the cover, "
        "table of contents, copyright page, acknowledgements, glossary, index, answer "
        'key, or a reference/skills section — with "kind": "apparatus". Apparatus is '
        "not a teaching unit, but its start page is where the neighbouring unit "
        "ends, so report it rather than skipping it.\n\n"
        f"{digest}\n\n"
        'Return ONLY JSON: {"chapters": [{"title": "<title>", "start_page": '
        '<PDF page number from the p-prefix>, "kind": "unit" | "apparatus"}]} in '
        "reading order. Return an empty list if this document genuinely has no "
        "chapter structure."
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
    # Apparatus entries are dropped from the pool: a unit title must never be
    # relocated onto a glossary or contents page (apparatus is not a chapter).
    if scanned:
        units = detect_chapters_vision(pdf_path, extraction.total_pages, client)
    else:
        units = detect_chapters_from_text_llm(extraction, client)
    units = [u for u in units if u.get("kind") != "apparatus"]
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
    if not _validate_chapter_list(candidate, extraction.total_pages):
        logger.warning("heal_chapter_boundaries: relocation did not validate — kept original list")
        return chapters, []

    # A heal is judged by what it LEAVES BEHIND, exactly like the last-chapter
    # bound in the structurer. _validate_chapter_list checks in-range / unique /
    # end>=start only — NON-CONTIGUITY IS STRUCTURALLY VALID — and nothing
    # downstream re-measured coverage, which is how Sara Junaidi's book
    # (e0459f87, 2026-08-23) stored a 36-page mid-book hole: a chapter owning
    # pages 22-61 was relocated onto a 4-page vision unit, _clamp_overlaps
    # shrank it to 22-25 while its neighbour still started at 62, and pages
    # 26-61 fell out of the book — invisible to health's then-tail-only
    # unmapped_pages, un-generatable forever. A CORRECT relocation loses a few
    # pages (moving a chapter off pages it never owned — the Mona case loses
    # 5 of 88), so the bound is a material-loss one, not zero: a heal that
    # uncovers more than max(8, 5%) of the book is refused wholesale and the
    # chapters it flagged stay marked suspect instead — a suspect label is
    # recoverable at generation time; a hole is not.
    lost = (
        covered_page_count(chapters, extraction.total_pages)
        - covered_page_count(candidate, extraction.total_pages)
    )
    if lost > max(_HEAL_MAX_LOST_PAGES, _HEAL_MAX_LOST_SHARE * extraction.total_pages):
        logger.warning(
            "heal_chapter_boundaries: relocation would uncover %d pages — refused, "
            "chapters kept with suspect markers", lost,
        )
        for c in chapters:
            if int(c.get("chapter_num", c.get("num", 0))) in mismatched:
                c["relocation"] = "suspect"
        return chapters, []

    logger.info("heal_chapter_boundaries: relocated chapters %s", relocated)
    return candidate, relocated


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
    # Never offer apparatus as a relocation target — see heal_chapter_boundaries.
    units = [u for u in units if u.get("kind") != "apparatus"]
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
