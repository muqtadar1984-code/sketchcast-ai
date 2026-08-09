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
