"""Part labels — the ONE place that decides how "part k of a chapter" is named.

The founder's rule, verbatim: *every part must carry the chapter name and the
part number, not just "Part 1", "Part 2"*.

Two things defeated that before. The stored label was composed as
``book · chapter · Part k of n`` with no room for the part's own heading, and
the heading it DID have in hand was usually the placeholder the structurer
emits when a chapter's page range yields no headings at all — the literal
string "Content", which was 47% of every part in production and read as the
part's NAME on the card.

Lives in shared/ because the app mirrors these rules in TypeScript
(``src/app/dashboard/lesson-card.tsx``). They must be one tested pair or they
drift, which is how the worker and the Library came to disagree in the first
place.
"""

from __future__ import annotations

import re

# Exact-match only, and deliberately SHORT. "Content" is the structurer's
# no-headings-found placeholder (agent1_ingestion/structurer.py) and the single
# most common part "title" in production; the others are the equivalent empties
# other extractors produce. A substring rule would eat real headings like
# "Content of the cell", and a media-design textbook really can have a section
# called "Content" — suppressing that one costs a heading in a rare book and
# saves a wrong one in half of them.
_PLACEHOLDER_RE = re.compile(r"^(content|contents|text|body|untitled|chapter|section)$", re.I)

# "3", "3.", "(4)", "1 - " … a heading that is only a number names nothing.
_BARE_NUMERAL_RE = re.compile(r"^[\d.)(\s\-–—]+$")


def _fold(s: str) -> str:
    """Case/space-insensitive comparison key."""
    return " ".join((s or "").split()).casefold()


def names_nothing(title, chapter_title: str = "") -> bool:
    """Whether a section heading identifies nothing — blank, a bare numeral, one
    of the extractors' placeholders, or a bare echo of the chapter title.

    Public because the coverage gate (shared/coverage.py) has to make exactly the
    same judgement for a completely different reason: a heading that names
    nothing is also a heading no generated script can be measured against, and
    "Content" is the only heading 47% of production parts have. Two copies of
    this rule would drift, which is the mistake this module was created to fix.
    """
    heading = " ".join(str(title or "").split())
    if not heading or _BARE_NUMERAL_RE.match(heading) or _PLACEHOLDER_RE.match(heading):
        return True
    return _fold(heading) == _fold(chapter_title) if chapter_title else False


def part_heading(titles, chapter_title: str = "") -> str | None:
    """The first section heading in ``titles`` worth showing as a part's name.

    Drops blanks, bare numerals, the placeholders above, and any heading that is
    just an echo of the chapter title (which tells the teacher nothing the row
    above it doesn't already say). Returns None when nothing survives — callers
    then fall back to the ordinal, never to a placeholder.
    """
    for raw in titles or []:
        heading = " ".join(str(raw).split())
        if names_nothing(heading, chapter_title):
            continue
        return heading
    return None


def clean_part_titles(titles, chapter_title: str = "", limit: int = 3) -> list[str]:
    """The showable subset of a part's section headings, in order.

    Used at INDEX time so the placeholder never reaches storage: the app renders
    ``titles[0] || <ordinal>``, so an empty list degrades to the ordinal while a
    stored "Content" renders as the part's name.
    """
    out: list[str] = []
    for raw in titles or []:
        heading = " ".join(str(raw).split())
        if names_nothing(heading, chapter_title) or heading in out:
            continue
        out.append(heading)
        if len(out) >= limit:
            break
    return out


def part_label(
    chapter_title: str,
    part: int | None = None,
    total: int | None = None,
    titles=None,
) -> str:
    """The chapter-anchored name of one part.

        "Feedback Loops · Part 3 of 7 — Balancing loops"   (heading available)
        "Feedback Loops · Part 3 of 7"                     (no usable heading)
        "Feedback Loops"                                   (single-part chapter)

    The chapter name always travels with the ordinal, so a label that leaves the
    Library — the parent portal, the three diary views, the staff console, every
    .docx running head — still says WHAT the part is part of. A single-part
    chapter degrades to the plain chapter name: "Part 1 of 1" is noise, and a
    re-index that shrinks a part map used to emit exactly that.
    """
    name = " ".join((chapter_title or "").split()) or "Lesson"
    try:
        part_n = int(part) if part is not None else 0
        total_n = int(total) if total is not None else 0
    except (TypeError, ValueError):
        return name
    if part_n < 1 or total_n <= 1:
        return name
    label = f"{name} · Part {part_n} of {total_n}"
    heading = part_heading(titles, chapter_title)
    return f"{label} — {heading}" if heading else label


# Below this much real chapter text, a "measurement" is not one. Mirrors the
# worker's own `section_chars < 200` test for "this chapter has no usable text",
# so the two agree on when a scanned chapter counts as transcribed.
_MIN_MEASURABLE_CHARS = 200


def measured_parts_for(chapter: dict, previous: list | None = None) -> list[dict] | None:
    """The part map a chapter's REAL text produces, or None to keep what is stored.

    A scanned book has no text at index time, so its map is inferred from page
    count at EST_WORDS_PER_PAGE = 250 — while a real scanned, illustrated
    textbook page measures about 145 words. The estimate therefore OVER-OFFERS:
    on the reference book, 44 parts advertised against 29 that could be built,
    and a teacher clicking part 8 of a 5-part chapter was told "part 8 does not
    exist. If the book's chapters changed, re-index it" — advice that cannot fix
    an estimate.

    Once the chapter has been transcribed there is nothing left to guess. Call
    this with the chapter dict whose ``sections`` hold the OCR; generation later
    runs build_chapter_parts on that same dict, so the stored count and the
    buildable count agree BY CONSTRUCTION instead of by calibration.

    Returns None — meaning leave the stored map alone — when the text yields
    nothing real, or when the stored map already says the same thing. Lives here
    rather than in the worker because everything it does is pure, and worker/
    cannot be imported without the supabase package.
    """
    from agent2_analysis.analyzer import build_chapter_parts

    # Is there REAL text here? Ask the chapter's own content, never the chunker's
    # output. build_chapter_parts prefixes each unit with "## <section title>",
    # so a chapter whose OCR came back EMPTY still yields one part of two words
    # ("##", "Content") — enough to look like a measurement and overwrite a
    # perfectly good 8-part map with a single junk row. A failed transcription
    # must leave the stored map exactly as it found it.
    content_chars = sum(
        len((sec.get("content") or ""))
        + sum(len((sub.get("content") or "")) for sub in (sec.get("subsections") or []))
        for sec in (chapter.get("sections") or [])
    )
    if content_chars < _MIN_MEASURABLE_CHARS:
        return None

    measured = [
        {"titles": clean_part_titles(p.get("section_titles"), chapter.get("title", "")),
         "words": int(p.get("words", 0))}
        for p in build_chapter_parts(chapter)
    ]
    if not measured or not any(p["words"] for p in measured):
        return None

    old = [p for p in (previous or []) if isinstance(p, dict)]

    # A gated book's PAGE RANGES are what health called suspect, and measuring
    # words inside a suspect range does not make the range right — so
    # low_confidence rides along if it was there. The count is real now; its
    # provenance is not.
    if any(p.get("low_confidence") for p in old):
        for p in measured:
            p["low_confidence"] = True

    already = (
        len(old) == len(measured)
        and not any(p.get("estimated") for p in old)
        and [p.get("words") for p in old] == [p["words"] for p in measured]
    )
    return None if already else measured
