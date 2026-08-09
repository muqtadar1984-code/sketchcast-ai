"""Content structuring: transforms raw extraction into hierarchical JSON.

Uses DocItem semantic labels (level, item_type) from extractor.py instead
of font-size heuristics to detect chapters, sections, and subsections.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from agent1_ingestion.config import PROCESSED_DIR
from agent1_ingestion.extractor import DocItem, ExtractionResult, TOCItem
from agent1_ingestion.image_extractor import ExtractedImage
from agent1_ingestion.models import (ChapterContent, ImageInfo, ImagePosition,
                                     KeyBox, Section, StructuredBook,
                                     Subsection, TOCEntry)

logger = logging.getLogger(__name__)

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


_SECTION_NUMBER_RE = re.compile(r"^\s*\d{1,2}\.\d")


def _toc_chapters_at_depth(toc: list[TOCItem], depth: int, total_pages: int) -> list[dict]:
    """Chapter boundary dicts from the outline entries at exactly ``depth``.

    A unit ends where the next entry at THIS depth **or shallower** begins, so a
    level-2 chapter stops at the next level-1 Part rather than running on into
    it. At depth 1 that is identical to the old "next level-1 entry" rule.
    """
    positions = [i for i, item in enumerate(toc) if item.level == depth]
    chapters: list[dict] = []
    for k, i in enumerate(positions):
        item = toc[i]
        nxt = next((t.page_num for t in toc[i + 1:] if t.level <= depth), None)
        chapters.append({
            "chapter_num": k,
            "title": item.title,
            "start_page": item.page_num,
            "end_page": (nxt - 1) if nxt is not None else total_pages - 1,
        })
    return chapters


def _depth_is_sections(entries: list[TOCItem]) -> bool:
    """True when a depth's titles are numbered SECTIONS ("3.2 Feedback loops")
    rather than chapters. Descending into these would shatter a book into
    dozens of pseudo-chapters — the opposite failure to the one being fixed."""
    if not entries:
        return False
    numbered = sum(1 for t in entries if _SECTION_NUMBER_RE.match(t.title or ""))
    return numbered * 2 > len(entries)


def _toc_map_at_depth(toc: list[TOCItem], depth: int, total_pages: int) -> list[dict]:
    """The chapter map for one outline depth, REPAIRED.

    Repair is not optional here, and leaving it out was a regression in its own
    right. Two bookmarks on the same page — Preface + Acknowledgements, Contents
    + "How to use this book", a Part title and the chapter that opens on the
    same page — make ``_toc_chapters_at_depth`` emit ``end_page = start_page - 1``
    and a repeated start, and ``_ranges_valid`` rejects both. That is an entirely
    ordinary outline: judged unrepaired, a 200-page book with 9 real chapters
    plus a Preface and an Acknowledgements on page 4 failed at depth 1 and
    descended to depth 2, turning 9 chapters into 27 pseudo-chapters — and with
    them the teacher's credits, assignments and library rows.

    So ``_pick_toc_depth`` and ``_build_chapters_from_toc`` both come through
    here: the map that is JUDGED must be the map that is RETURNED.
    """
    return _repair_chapter_ranges(
        _toc_chapters_at_depth(toc, depth, total_pages), total_pages
    )


def _pick_toc_depth(toc: list[TOCItem], total_pages: int, page_stats: dict | None = None) -> int:
    """Which outline depth holds the book's CHAPTERS.

    This used to be hardcoded to level 1. For a 1,008-page textbook whose
    outline nests ~21 chapters under 7 Parts, that returned the 7 Parts: units
    of 114-254 pages that each offered 19-28 lesson-parts. Now each depth is
    built and measured, and the SHALLOWEST one that satisfies the teaching-unit
    envelope wins — so a book whose chapters really are at level 1 (every
    correct book in the corpus) is unchanged, and only a failing level 1 causes
    a descent.

    Falls back to depth 1 when no depth passes: keeping today's coarse map is
    strictly better than adopting a deeper one we could not validate.
    """
    for depth in (1, 2, 3):
        entries = [t for t in toc if t.level == depth]
        if not entries:
            continue
        if depth > 1 and _depth_is_sections(entries):
            break  # this depth is sections; anything deeper is more so
        chapters = _toc_map_at_depth(toc, depth, total_pages)
        if not _chapters_plausible(chapters, total_pages, page_stats):
            continue
        return depth
    return 1


def _build_chapters_from_toc(
    toc: list[TOCItem],
    total_pages: int,
    page_stats: dict | None = None,
) -> list[dict]:
    """Create chapter boundary dicts from the TOC, at the depth that holds the
    book's chapters (see ``_pick_toc_depth``)."""
    return _toc_map_at_depth(toc, _pick_toc_depth(toc, total_pages, page_stats), total_pages)


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
            "title": _clean_title(item.text.strip()),
            "start_page": item.page_num,
            "end_page": end_page,
        })
    return chapters


_FILENAME_RE = re.compile(r"\.(pdf|docx?|epub|indd|ai)$", re.I)
_SUBSEC_RE = re.compile(r"^(\d{1,2})\.\d")

# Roman numerals, ASCII and the Unicode Number Forms block. Machine-built PDF
# bookmarks write "PartⅠ" with U+2160 glued straight onto the word — and since
# U+2160 is a LETTER-NUMBER to `re`, neither `\s+` nor `\b` can find that
# boundary (verified: the old `_LABEL_RE` matched neither "PartⅠ …" nor even
# "Part I …"). Hence `\s*` after the label word plus an explicit
# not-a-word-character guard after the numeral, which is what stops "Parties"
# from parsing as "Part" + "I".
_ROMAN_ALT = (
    "xviii|xvii|xiii|viii|xvi|xiv|xix|xii|vii|iii|xv|xi|xx|vi|iv|ix|ii|x|v|i"
)
_UNICODE_ROMAN = "Ⅰ-Ⅿⅰ-ⅿ"

# Labeled chapter markers: "Chapter 3", "UNIT 3", "Lesson Three", "Topic 3:",
# "Module 3 — Fractions", "Part IV", "PartⅠ", etc. Group 2 = the number
# (digits, a word, or a roman numeral), group 3 = any title text on the line.
_LABEL_RE = re.compile(
    r"^(chapter|unit|lesson|topic|module|theme|week|part|section|volume)\s*"
    r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    f"{_ROMAN_ALT}|[{_UNICODE_ROMAN}])"
    r"(?!\w)"
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
_ROMAN_NUMS = {
    w: n for n, w in enumerate(
        ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
         "xi", "xii", "xiii", "xiv", "xv", "xvi", "xvii", "xviii", "xix", "xx"], start=1,
    )
}
# U+2160..U+216B are Ⅰ..Ⅻ; .lower() maps them onto U+2170..U+217B.
_ROMAN_NUMS.update({chr(0x2170 + i): i + 1 for i in range(12)})

# Label words that name a CONTAINER of chapters, not a chapter. "Part IV" is
# the book's Part IV — using it as the chapter level is the exact wrong-altitude
# bug this module now guards against, and teaching roman numerals to the marker
# parser would otherwise have made these newly detectable. A container family is
# only ever used when no finer family produced a usable run.
_CONTAINER_LABELS = frozenset({"part", "section", "volume", "book"})


def _marker_number(token: str) -> Optional[int]:
    token = token.strip().lower()
    if token.isdigit():
        n = int(token)
        return n if 1 <= n <= 99 else None
    return _WORD_NUMS.get(token) or _ROMAN_NUMS.get(token)


def _better_family(candidate: list[dict], best: list[dict], fam: str, best_fam: str | None) -> bool:
    """Family ranking for the labelled/running-header detectors: a finer family
    (chapter/unit/lesson…) beats a CONTAINER family (part/volume…) even when the
    container found more entries — 7 Parts must never win over 21 Units. Within
    the same rank, the longer ascending run wins."""
    if len(candidate) < 3:
        return False
    if best_fam is not None and (fam in _CONTAINER_LABELS) != (best_fam in _CONTAINER_LABELS):
        if best_fam not in _CONTAINER_LABELS:
            return False  # never demote a finer family to a container
        # Promote finer over container only when the finer run is not a scrap.
        # Unconditional promotion would let three stray "Chapter N" strings beat
        # the 20 real "Part N" units of a workbook that genuinely numbers its
        # chapters as Parts — rare, but a total loss when it happens. A real
        # chapter family cannot be much shorter than the container family, since
        # every container holds at least one chapter; half the container's length
        # leaves room for partial detection and still excludes a scrap. (The
        # failure case this ranking exists for is 21 chapters vs 7 Parts — 3x
        # clear of the bar.)
        return len(candidate) * 2 >= len(best)
    return len(candidate) > len(best)


def _looks_like_file_bookmark(title: str) -> bool:
    """A bookmark that is a FILE name, not a chapter title.

    ``_FILENAME_RE`` alone requires a literal extension, which missed a whole
    production book whose five "chapters" were named
    ``esl_cie_asl_genpaper_1ed_tr_ch1.3_wksht1.3a_TOR`` — an export slug with
    the extension already stripped. Four-plus underscore-joined segments with
    no spaces is a file name; a real chapter title that long has spaces in it.
    """
    t = (title or "").strip()
    if not t:
        return False
    return bool(_FILENAME_RE.search(t)) or (" " not in t and t.count("_") >= 3)


def _toc_is_usable(toc: list[TOCItem], total_pages: int) -> bool:
    """Reject outlines that clearly aren't real chapters — a PDF stitched from a
    few source files whose only bookmarks are the file names, or (the opposite
    failure) an auto-generated bookmark per PAGE (some export tools do this),
    which would shred the book into one-page pseudo-chapters."""
    level1 = [t for t in toc if t.level == 1]
    if not level1:
        return False
    if any(_looks_like_file_bookmark(t.title or "") for t in level1):
        return False
    # Too few top-level entries for a sizeable book → probably not chapters.
    if total_pages > 40 and len(level1) < 3:
        return False
    # Too many entries → per-page/per-heading bookmarks, not chapters.
    if len(level1) > max(30, total_pages // 3):
        return False
    # Mostly bare-number / trivial titles (page labels) → not chapters.
    trivial = sum(1 for t in level1 if len((t.title or "").strip()) <= 2 or (t.title or "").strip().isdigit())
    if trivial * 2 > len(level1):
        return False
    return True


# ── Chapter map SHAPE ────────────────────────────────────────────────────────
# A detected "chapter" has to be a teaching unit. The count-only checks below
# could not tell one from the book's PART that CONTAINS the chapters: 8 units
# over 1,008 pages (median 124 pages, 20 lesson-parts each) sailed through, and
# so did an 11-chapter book whose last "chapter" was 229 of its 301 pages.
#
# Every threshold is calibrated against the 19-book production corpus. Note what
# CANNOT be done here, because an earlier draft of this block claimed it was:
# "a chapter is a fraction of the book" is not a usable rule. A map of N units
# covering the book gives each unit ~1/N of it whether those units are Parts or
# chapters — 8 Parts over 1,008 pages is 12.5% each, 5 real chapters over 300
# pages is 20% each, so every size-relative measure ranks the WRONG book worse.
# What separates a Part from a big chapter is absolute TEACHING LOAD, and that
# is why the page rule below is an absolute count. To stop that absolute count
# from throwing away ordinary books on its own, it now needs CORROBORATION from
# the independent load measure (estimated lesson-parts): a 300-page university
# textbook with five 60-page chapters is not a wrong-altitude map, and the
# earlier unaccompanied 55-page rule rejected it.
_SHAPE_MIN_PAGES = 120          # under this a book is about one unit's worth; the
                                # question "is this a chapter or a Part?" is moot
_SHAPE_MIN_CHAPTERS = 3
_MAX_MEDIAN_PAGES = 70          # largest LEGITIMATE median in the corpus is 41
                                # (Cambridge Primary Science Y7, 9 units / 344pp),
                                # and an ordinary university textbook runs to 60
                                # (300pp / 5 chapters). 70 clears both and is 1.8x
                                # below the failure case's 124-126
_MIN_PARTS_FOR_PAGE_RULE = 20   # the corroborating half of the page rule. 20
                                # lesson-parts is ~30,000 words, ~4h of video for
                                # one "chapter"; the failure case's 124-page units
                                # measured 20.5 parts each in production, while a
                                # 60-page chapter at the 250 w/page estimate is 10
_MIN_MEDIAN_PAGES = 4           # below this the units are sections, not chapters
                                # (the over-shoot guard for a deeper TOC depth)
_MAX_MEDIAN_PARTS = 30          # BACKSTOP that fires alone, so it is set where no
                                # real chapter can reach: 30 parts is ~45,000
                                # words, ~7h of video. It catches a unit that is
                                # page-light but text-heavy (dense two-column
                                # typesetting), which the pages rule cannot see.
                                # It must stay clear of a genuinely dense
                                # 60-page chapter, measured at 22 parts
_MAX_CHAPTER_SHARE = 0.25       # with the ratio test below: one unit that is both
                                # a quarter of the book AND 4x its siblings
_MAX_SPAN_RATIO = 4.0           # largest legitimate max/median span is ~1.6
                                # (Cambridge Science: 52 vs 41); the 229-page tail
                                # was 32x its siblings' median of 7. Measured
                                # AFTER the last-chapter bound is applied — see
                                # _bounded_last_chapter

# ── did the map actually reach the end of the book? ──────────────────────────
# This is the rule that has to exist BECAUSE the span ratio above is measured on
# the bounded map. _bounded_last_chapter clips the final unit to
# max(3x median, median + 20), which is under _MAX_SPAN_RATIO by construction
# for any median above ~7 pages — so once the bound is applied the span rule can
# no longer fire on the last chapter at all, and a book whose detection stopped a
# quarter of the way in was ACCEPTED and silently truncated instead of falling
# through to the Claude/vision rescue. Measured: 11 units of 7 pages in a
# 301-page book measured {'ok': False, 'reason': 'one unit is 77% of the book and
# 33x its siblings'} raw, and ok=True bounded, storing 11 chapters over pages
# 0-96 and dropping 204 of 301 pages at health "good".
#
# So the bound is judged by what it LEAVES BEHIND. It is a rescue when those
# pages are back matter and a symptom when they are the rest of the book, and
# the two shapes that must be told apart both fail the raw span test, so the raw
# test cannot separate them:
#
#   LEGITIMATE — 10 units of 20 pages in a 300-page book with 100 pages of
#   unbookmarked answers/glossary/index (Cambridge/NCERT-style). Raw: "one unit
#   is 40% of the book and 6x its siblings". The bound clips the final unit at 60
#   pages and leaves 60 unmapped = 20.0% of the book.
#
#   BROKEN — the 11x7 book above. The bound clips at 27 pages and leaves 204
#   unmapped = 67.8% of the book.
#
# Note what the tail canNOT be compared against: the median span. 60 unmapped is
# 3x a 20-page median and 204 is 29x a 7-page median, but that ratio tracks the
# chapter SIZE, not how much of the book was lost — the same error as measuring a
# chapter as a fraction of the book, one paragraph up. Against the book itself
# the two shapes read 20% and 68%, so 0.25 sits 5 points clear of the legitimate
# case and 2.7x below the broken one. In back-matter terms that legitimate book
# tolerates up to 120 pages of unbookmarked tail on its 200-page body before it
# trips, which is more back matter than any book in the corpus carries.
_MAX_UNMAPPED_TAIL_SHARE = 0.25

# Mirrors agent2_analysis.analyzer — the parts estimate has to model the REAL
# chunker or it measures nothing. build_chapter_parts closes a chunk when
# either budget would overflow, so parts ≈ max(chars/15000, words/1500).
# (tests/test_chapter_altitude.py asserts these stay in step.)
_PART_CHARS = 15000
_PART_WORDS = 1500
_EST_WORDS_PER_PAGE = 250       # same calibration as the worker's part-map
                                # estimator: reproduces a measured book's real
                                # 4 parts, and errs toward FEWER parts


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    return float(ordered[mid]) if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def _page_text_stats(items: list[DocItem]) -> dict[int, tuple[int, int]]:
    """{page_num: (chars, words)} — the raw material for the parts estimate."""
    stats: dict[int, tuple[int, int]] = {}
    for it in items or []:
        text = (getattr(it, "text", "") or "").strip()
        if not text:
            continue
        page = int(getattr(it, "page_num", 0) or 0)
        chars, words = stats.get(page, (0, 0))
        stats[page] = (chars + len(text), words + len(text.split()))
    return stats


def _chapter_pages(chapter: dict) -> int:
    try:
        start = int(chapter.get("start_page", 0))
        end = int(chapter.get("end_page", start))
    except (TypeError, ValueError):
        return 0
    return max(0, end - start + 1)


def _est_parts(chapter: dict, page_stats: dict | None = None) -> int:
    """How many lesson-parts this page range would split into.

    Measured from the extracted text when there is any, because a chapter's
    lesson count is what actually tells a Part from a chapter. CHARS are the
    load-bearing measure: the book that triggered this had a text layer with no
    word boundaries, so a words-only estimate would have under-counted it by an
    order of magnitude and hidden the very defect being looked for.
    """
    pages = _chapter_pages(chapter)
    chars = words = 0
    if page_stats:
        try:
            start = int(chapter.get("start_page", 0))
            end = int(chapter.get("end_page", start))
        except (TypeError, ValueError):
            start, end = 0, -1
        for page in range(start, end + 1):
            page_chars, page_words = page_stats.get(page, (0, 0))
            chars += page_chars
            words += page_words
    if chars <= 0:  # no text layer — fall back to the page estimate
        words = pages * _EST_WORDS_PER_PAGE
        chars = 0
    by_chars = -(-chars // _PART_CHARS)
    by_words = -(-words // _PART_WORDS)
    return max(1, by_chars, by_words)


def _unmapped_tail(chapters: list[dict], total_pages: int) -> int:
    """Pages past the last chapter's end that no unit covers. (book_health
    re-derives the same gap from the STORED map — keep the two in step.)"""
    ends: list[int] = []
    for c in chapters or []:
        try:
            ends.append(int(c["end_page"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not ends or total_pages <= 0:
        return 0
    return max(0, (total_pages - 1) - max(ends))


def _map_shape(chapters: list[dict], total_pages: int, page_stats: dict | None = None) -> dict:
    """Measure a chapter map: {ok, reason, median_pages, median_parts, max_share,
    unmapped_tail}.

    Only judges books big enough for the question to mean anything — a 40-page
    upload with 3 units is simply a short book, not a wrong altitude. The tail
    rule inherits that guard deliberately: below 120 pages a tail is a colophon,
    and rejecting a small book only buys it a paid detection pass.
    """
    spans = [_chapter_pages(c) for c in chapters]
    median_pages = _median(spans)
    tail = _unmapped_tail(chapters, total_pages)
    out = {
        "ok": True,
        "reason": None,
        "median_pages": median_pages,
        "median_parts": _median([_est_parts(c, page_stats) for c in chapters]),
        "max_share": round(max(spans) / total_pages, 3) if spans and total_pages > 0 else 0.0,
        "unmapped_tail": tail,
    }
    if total_pages < _SHAPE_MIN_PAGES or len(chapters) < _SHAPE_MIN_CHAPTERS:
        return out

    if median_pages > _MAX_MEDIAN_PAGES and out["median_parts"] >= _MIN_PARTS_FOR_PAGE_RULE:
        out["reason"] = f"units average {median_pages:.0f} pages — these look like the book's parts, not its chapters"
    elif out["median_parts"] >= _MAX_MEDIAN_PARTS:
        out["reason"] = f"units would each become ~{out['median_parts']:.0f} lessons"
    elif median_pages and median_pages < _MIN_MEDIAN_PAGES:
        out["reason"] = f"units average {median_pages:.1f} pages — these look like sections, not chapters"
    elif (
        len(chapters) >= 4
        and median_pages > 0
        and max(spans) > _MAX_SPAN_RATIO * median_pages
        and out["max_share"] > _MAX_CHAPTER_SHARE
    ):
        out["reason"] = f"one unit is {out['max_share']:.0%} of the book and {max(spans) / median_pages:.0f}x its siblings"
    elif tail > _MAX_UNMAPPED_TAIL_SHARE * total_pages:
        # Last, so that a map which is ALSO at the wrong altitude reports the
        # more diagnostic reason. See _MAX_UNMAPPED_TAIL_SHARE: this is the only
        # rule left that can see a truncated map once the last-chapter bound has
        # flattened the span ratio.
        out["reason"] = (
            f"the map stops {tail} pages ({tail / total_pages:.0%} of the book) short of the end "
            "— chapter detection stopped early"
        )
    out["ok"] = out["reason"] is None
    return out


def _ranges_valid(chapters: list[dict], total_pages: int) -> bool:
    """Corruption gate: in-range pages, end >= start, strictly increasing
    starts, unique chapter numbers. Applied to DETECTION output as well as to
    healed output — a production book was stored with a chapter running from
    page 115 to page 1, a NEGATIVE 113-page range, because nothing on this path
    ever checked.

    STRICTLY STRONGER than ``vision_chapters._validate_chapter_list``, which is
    the same gate minus ordering: that one asks only that starts be UNIQUE
    (``if s in starts``), this one that they ASCEND. So a map the heal path
    accepts can be rejected here, and any outline that lists an entry out of
    page order — a trailing Index or Glossary bookmark, a re-ordered export, two
    bookmarks landing on one page — fails. That is deliberate, because the
    ordering assumption is what everything downstream slices pages on; it is
    also why EVERY caller must run ``_repair_chapter_ranges`` first, which sorts
    and de-duplicates and so makes those shapes valid instead of fatal. Judging
    an unrepaired map against this gate is a bug, and has been one.
    """
    if not chapters:
        return False
    prev_start = -1
    nums: set[int] = set()
    for c in chapters:
        try:
            start = int(c["start_page"])
            end = int(c["end_page"])
            num = int(c.get("chapter_num", c.get("num")))
        except (KeyError, TypeError, ValueError):
            return False
        if not (0 <= start < total_pages) or not (0 <= end < total_pages) or end < start:
            return False
        if start <= prev_start or num in nums:
            return False
        prev_start = start
        nums.add(num)
    return True


def _repair_chapter_ranges(chapters: list[dict], total_pages: int) -> list[dict]:
    """Fix the range faults that can be fixed WITHOUT guessing, before anything
    trusts the map: drop out-of-range starts, order by start page, drop repeats,
    and derive any missing/backwards end from the next start. Deterministic and
    lossless for a map that was already sound."""
    if total_pages <= 0:
        return []
    kept: list[dict] = []
    for c in chapters:
        try:
            start = int(c.get("start_page", 0))
        except (TypeError, ValueError):
            continue
        if 0 <= start < total_pages:
            kept.append(dict(c))
    kept.sort(key=lambda c: int(c["start_page"]))

    out: list[dict] = []
    for c in kept:
        if out and int(c["start_page"]) == int(out[-1]["start_page"]):
            continue
        out.append(c)

    for i, c in enumerate(out):
        start = int(c["start_page"])
        boundary = (int(out[i + 1]["start_page"]) - 1) if i + 1 < len(out) else total_pages - 1
        try:
            end = int(c.get("end_page", boundary))
        except (TypeError, ValueError):
            end = boundary
        if end < start:
            end = boundary
        c["end_page"] = max(start, min(end, total_pages - 1))
        c["chapter_num"] = i
    return out


# The last chapter runs to the end of the book — but only as far as a real final
# chapter plausibly runs. Unbounded, this is how a book of 7-page chapters ended
# up with a chapter 10 spanning 229 of its 301 pages: detection stopped early and
# the tail was handed to the last unit. A genuine final chapter (summary,
# appendix) runs longer than its siblings but not unboundedly so; the widest
# max/median span in the production corpus is ~1.6x, so 3x is a generous ceiling.
# The `+ 20` floor keeps a short book with 5-page chapters able to absorb a
# normal tail.
_LAST_CHAPTER_SPAN_RATIO = 3.0
_LAST_CHAPTER_SPAN_FLOOR = 20
# Below this, unmapped tail pages are not worth reporting: a page or two of
# colophon at the end of a book is not content anybody is missing.
_MIN_REPORTABLE_UNMAPPED = 3


def _bounded_last_chapter(chapters: list[dict], total_pages: int) -> tuple[list[dict], int]:
    """``(map, unmapped_tail_pages)`` — the map with its final unit stretched to
    the end of the book, or to its plausible limit, whichever is smaller.

    Returns COPIES, because the plausibility check needs to measure the bounded
    shape without committing to it.
    """
    if not chapters or total_pages <= 0:
        return [dict(c) for c in chapters], 0
    out = [dict(c) for c in chapters]
    full = total_pages - 1
    last = out[-1]
    start = int(last["start_page"])
    median_span = _median([_chapter_pages(c) for c in out[:-1]])
    limit = (
        max(_LAST_CHAPTER_SPAN_RATIO * median_span, median_span + _LAST_CHAPTER_SPAN_FLOOR)
        if median_span else float("inf")
    )
    if full - start + 1 <= limit:
        last["end_page"] = full
        return out, 0
    # Detection stopped early. Give the last chapter its share and no more — the
    # remaining pages stay unmapped rather than being taught as part of a chapter
    # they do not belong to. The caller must SAY SO: pages that are silently
    # dropped are never extracted, never analysed and never taught, and that is a
    # worse failure than the over-long chapter this bound replaces.
    last["end_page"] = min(max(int(last["end_page"]), start), start + int(limit) - 1, full)
    return out, full - int(last["end_page"])


def _chapters_plausible(
    chapters: list[dict],
    total_pages: int,
    page_stats: dict | None = None,
) -> bool:
    """Sanity-check any detected chapter list before trusting it. Degenerate
    splits (a 'chapter' per page, digit-only titles), corrupt page ranges and
    wrong-ALTITUDE maps (the book's Parts detected instead of its chapters) all
    cost real generation money downstream — better to try another rung."""
    if not chapters:
        return False
    if len(chapters) > max(30, total_pages // 3):
        return False
    digit_titles = sum(1 for c in chapters if str(c.get("title", "")).strip().isdigit())
    if digit_titles * 2 > len(chapters):
        return False
    if not _ranges_valid(chapters, total_pages):
        return False
    # Measure the map as it will actually be STORED. The last-chapter bound runs
    # at the end of structure_book, so judging the unbounded map here threw away
    # the very shape the bound exists to rescue: a legitimate TOC whose final
    # bookmarked chapter absorbs 60-100 pages of unbookmarked answers, glossary
    # and index — routine in Cambridge/NCERT-style textbooks — measured as "one
    # unit is 40% of the book and 6x its siblings" and was discarded, sending the
    # book to a paid detection pass or shattering it into sections.
    #
    # What the bounded map must still be asked is whether the bound was a rescue
    # or a symptom — i.e. how much of the book it left behind. Without that
    # question this check silently ACCEPTS a truncated map, because the clip is
    # under _MAX_SPAN_RATIO by construction (see _MAX_UNMAPPED_TAIL_SHARE), and
    # a book whose chapters were only found for its first quarter never reaches
    # the Claude/vision rescue that used to catch it.
    bounded, _ = _bounded_last_chapter(chapters, total_pages)
    return bool(_map_shape(bounded, total_pages, page_stats)["ok"])


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


def _clean_title(text: str) -> str:
    """Bound an inferred title. Extraction sometimes glues a heading to the first
    body sentence ("Flowcharts A sequence of steps that make up…"); a chapter
    label must stay a label — cut at a sentence end when one lands early, else
    hard-wrap on a word boundary."""
    t = " ".join(text.split())
    if len(t) <= 64:
        return t
    m = re.match(r"^(.{8,63}?[.!?:])\s", t)
    if m:
        return m.group(1).rstrip(".!?:")
    return t[:56].rsplit(" ", 1)[0] + "…"


def _detect_running_header_chapters(items: list[DocItem], total_pages: int) -> list[dict]:
    """Detect chapters from RUNNING HEADERS — books (Cambridge, Oxford, …) that
    print "Unit 3: Selecting hardware and software" on every page of the unit.

    ``_detect_labeled_chapters`` (first occurrence per number) fails on these
    books: every unit's first occurrence is the printed Contents page, so its
    ascending run collapses and detection falls through to heading inference —
    which produces blurb titles and drifted boundaries. Here the signal is the
    opposite one: a label on MOST pages, forming dense per-unit page clusters
    whose transitions are the exact boundaries and whose modal inline title is
    the clean chapter title. Scans ALL items (running headers are small text,
    not level-1/2 headings)."""
    # family -> page -> set of (n, inline_title) seen on that page
    by_family: dict[str, dict[int, set[tuple[int, str]]]] = {}
    for idx, it in enumerate(items):
        m = _LABEL_RE.match(it.text.strip())
        if not m:
            continue
        n = _marker_number(m.group(2))
        if n is None:
            continue
        title = m.group(3).strip(" .:-–—")
        if not any(c.isalpha() for c in title) and idx + 1 < len(items):
            # Extraction often splits the header into spans — "Unit 3" then
            # ": Selecting hardware and software" — so peek at the neighbour.
            nxt = items[idx + 1]
            nt = nxt.text.strip().strip(" .:-–—")
            if nxt.page_num == it.page_num and nt and len(nt) <= 64 and any(c.isalpha() for c in nt):
                title = nt
        fam = by_family.setdefault(m.group(1).lower(), {})
        fam.setdefault(it.page_num, set()).add((n, title))

    best: list[dict] = []
    best_fam: str | None = None
    for fam_name, pages_map in by_family.items():
        # A page mentioning 3+ different unit numbers is a Contents/index page,
        # not a running header — drop it from the signal.
        page_n: dict[int, tuple[int, str]] = {}
        for pg, marks in pages_map.items():
            nums = {n for n, _ in marks}
            if len(nums) > 2:
                continue
            n = min(nums)
            title = next(
                (t for num, t in sorted(marks) if num == n and any(c.isalpha() for c in t)), ""
            )
            page_n[pg] = (n, title)
        # Running headers = the label appears on a large share of the book.
        if len(page_n) < max(8, total_pages // 5):
            continue

        # Per number: the dense page cluster (longest run with small gaps) is the
        # unit's true extent; stray cross-references ("see Unit 7") sit far from
        # their number's cluster and fall out of it.
        clusters: dict[int, tuple[int, int]] = {}
        titles: dict[int, dict[str, int]] = {}
        for n in sorted({v[0] for v in page_n.values()}):
            pgs = sorted(pg for pg, (num, _) in page_n.items() if num == n)
            runs: list[list[int]] = [[pgs[0]]]
            for pg in pgs[1:]:
                if pg - runs[-1][-1] <= 4:
                    runs[-1].append(pg)
                else:
                    runs.append([pg])
            main = max(runs, key=len)
            if len(main) < 3:  # too sparse to be a unit's running header
                continue
            clusters[n] = (main[0], main[-1])
            tcounts = titles.setdefault(n, {})
            for pg in main:
                t = page_n[pg][1]
                if t:
                    tcounts[t] = tcounts.get(t, 0) + 1

        # Consecutive numbering from 1, strictly increasing starts.
        chapters: list[dict] = []
        n = 1
        while n in clusters:
            start, _last = clusters[n]
            if chapters and start <= chapters[-1]["start_page"]:
                break
            tcounts = titles.get(n) or {}
            title = max(tcounts, key=tcounts.get) if tcounts else f"Chapter {n}"
            chapters.append({
                "chapter_num": len(chapters), "title": _clean_title(title),
                "start_page": start, "end_page": 0,
            })
            n += 1

        if _better_family(chapters, best, fam_name, best_fam):
            for i in range(len(chapters)):
                chapters[i]["end_page"] = (
                    chapters[i + 1]["start_page"] - 1 if i + 1 < len(chapters)
                    else clusters[len(chapters)][1]
                )
            best, best_fam = chapters, fam_name
    return best


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
    best_fam: str | None = None
    for fam_name, first in families.items():
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
        if _better_family(chapters, best, fam_name, best_fam):
            best, best_fam = chapters, fam_name

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
      1. The PDF's outline (TOC bookmarks), if it looks like real chapters — at
         whichever DEPTH holds the chapters, not always level 1 (see
         ``_pick_toc_depth``).
      2. Labelled markers — "Chapter 3" / "Unit 3" / "Lesson Three" / "Topic 3"…
      3. Bare-number heading markers ("1", "2", … ascending).
      4. Level-1 heading inference.
      5. When a ``client`` (Claude) is provided and the above found nothing:
         a text book gets an LLM pass over its headings digest; a SCANNED book
         (no text layer) gets a vision pass that READS the rendered pages —
         handling any labelling convention, since Claude reads pages like a
         person does.
      6. Whole document as a single chapter.

    Every candidate map is range-repaired and then shape-checked. A CORRUPT map
    is discarded so the next rung gets its chance; a map that is merely at the
    wrong ALTITUDE (the book's Parts where its chapters should be) is kept aside
    and restored if nothing finer validates — one whole-book chapter would be
    worse for the teacher than eight over-large ones.
    """
    used_known = bool(known_chapters) and all(
        "start_page" in c and "end_page" in c for c in known_chapters
    )
    # Text volume per page — what the shape checks measure a candidate map
    # against. Never computed for stored known_chapters: those are authoritative
    # and are not re-judged.
    page_stats = None if used_known else _page_text_stats(extraction.items)
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
        chapter_defs = _build_chapters_from_toc(extraction.toc, extraction.total_pages, page_stats)
    else:
        # No usable outline → running headers, labelled markers, bare numbers,
        # heading inference. Running headers go first: when a book prints its
        # unit on every page, that beats any first-occurrence heuristic.
        chapter_defs = _detect_running_header_chapters(extraction.items, extraction.total_pages)
        if not chapter_defs:
            chapter_defs = _detect_labeled_chapters(extraction.items, extraction.total_pages)
        if not chapter_defs:
            chapter_defs = _detect_numbered_chapters(extraction.items, extraction.total_pages)
        if not chapter_defs:
            chapter_defs = _infer_chapters_from_items(extraction.items, extraction.total_pages)

    # Sanity-check whatever the outline/heuristics produced: a degenerate split
    # (per-page pseudo-chapters, digit titles) must not be trusted — reset it so
    # the Claude fallback gets its chance.
    coarse_fallback: list[dict] = []
    if not used_known and chapter_defs:
        chapter_defs = _repair_chapter_ranges(chapter_defs, extraction.total_pages)
        if not _chapters_plausible(chapter_defs, extraction.total_pages, page_stats):
            # A map fails for one of two reasons, and they deserve different
            # treatment. A CORRUPT one (bad ranges, digit-only titles, a
            # chapter per page) is thrown away. A merely WRONG-ALTITUDE one —
            # the book's 8 Parts where its 21 chapters should be — is kept
            # aside: falling all the way back to ONE whole-document chapter
            # would be strictly worse for the teacher than 8 over-large units,
            # so it is restored below if nothing finer validates.
            if len(chapter_defs) > 1 and _ranges_valid(chapter_defs, extraction.total_pages):
                coarse_fallback = chapter_defs
            chapter_defs = []

    # Claude fallback: heuristics found nothing usable (0 or 1 pseudo-chapter).
    # Never second-guess stored known_chapters — the split must stay identical
    # to what indexing stored (and vision must not be re-billed per generation).
    if client is not None and not used_known and len(chapter_defs) <= 1:
        from agent1_ingestion.vision_chapters import (
            detect_chapters_from_text_llm,
            detect_chapters_vision,
            extraction_has_text,
        )

        smart: list[dict] = []
        if extraction_has_text(extraction):
            smart = detect_chapters_from_text_llm(extraction, client)
        # Vision reads the rendered pages, so it works whether or not a text
        # layer exists — use it whenever the text route came up empty.
        if not smart and pdf_path:
            smart = detect_chapters_vision(pdf_path, extraction.total_pages, client)
        if smart:
            chapter_defs = _repair_chapter_ranges(smart, extraction.total_pages)

    # Nothing finer validated — a coarse map beats a single whole-book chapter.
    if len(chapter_defs) <= 1 and coarse_fallback:
        chapter_defs = coarse_fallback

    # Final fallback: whole document as a single chapter
    if not chapter_defs:
        chapter_defs = [{
            "chapter_num": 0,
            "title": title or "Full Document",
            "start_page": 0,
            "end_page": extraction.total_pages - 1,
        }]

    # Ensure the last chapter covers all remaining pages — but never stretch
    # stored known_chapters bounds: they are authoritative (stretching would,
    # e.g., balloon a 12-page unit to the whole book when fewer chapters are
    # passed than the book has). See _bounded_last_chapter for the bound.
    if chapter_defs and not used_known:
        chapter_defs, unmapped = _bounded_last_chapter(chapter_defs, extraction.total_pages)
        if unmapped >= _MIN_REPORTABLE_UNMAPPED:
            # Say it out loud. compute_book_health re-derives the same gap from
            # the stored map and surfaces it to the teacher; this line is for
            # support triage, which reads worker logs, not books.health.
            logger.warning(
                "book %s: chapter detection stopped early — pages %d-%d (%d of %d) "
                "are not covered by any chapter and will not be taught",
                book_id, int(chapter_defs[-1]["end_page"]) + 1,
                extraction.total_pages - 1, unmapped, extraction.total_pages,
            )

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
