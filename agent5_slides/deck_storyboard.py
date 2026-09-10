"""What slides this lesson's deck consists of — decided before anything is drawn.

Pure data in, pure data out: a ``LessonModel`` becomes a list of ``Slide``
records and nothing here opens a file or touches python-pptx. That is the
point. Every judgement the deck makes — how many labels fit before a diagram
has to split, which sections earn a diagram at all, what a teacher's deck
loses when there is no article — is decided here and can be asserted on
without rendering a single slide.

THE DECK IS NOT THE VIDEO'S SLIDE LIST. Today it is: one deck slide per script
segment, each one a picture of that segment. The article knows things the
script threw away — the objectives a teacher writes on the board, the
misconceptions the class will arrive with, worked examples, a full glossary —
and every one of them is a better slide than a bullet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from shared.lesson_model import (Figure, LessonModel, Section, parse_body,
                                 strip_emphasis)

from . import metrics as mx

TITLE = "title"
OBJECTIVES = "objectives"
SECTION = "section"
DIAGRAM = "diagram"
MISCONCEPTIONS = "misconceptions"
WORKED = "worked_example"
GLOSSARY = "glossary"
CHECK = "check"
CLOSING = "closing"

# Vertical room the label gutters have, in inches, on the standard content
# frame. Capacity is derived from this rather than fixed, so raising the label
# size cannot silently overflow a diagram — it reduces the count instead.
_GUTTER_IN = 5.15
_LABEL_GAP_IN = 0.07
# A label wraps to two lines often enough (`endoplasmic reticulum` does, at
# 12pt in a 2.55in gutter) that budgeting for one line is how you get two
# labels on top of each other.
_LABEL_LINES = 2
# Six leaders a side. Not a measurement — see `labels_per_slide`.
_LEADER_CLUTTER = 12

# A section body is prose, not a summary, so a long one PAGINATES rather than
# shrinking to fit: 9pt body text on a projected slide is not editable
# content, it is a screenshot of a document. Everything below measures in
# inches through `metrics`, which the renderer imports too — see that module
# for why one shared estimate matters more than a good one.

# Rows a misconception pair and a glossary row occupy in their tables.
_MISCONCEPTIONS_PER_SLIDE = 4
_GLOSSARY_PER_SLIDE = 8


@dataclass
class Slide:
    kind: str
    heading: str = ""
    kicker: str = ""
    subtitle: str = ""
    notes: str = ""
    blocks: list[dict] = field(default_factory=list)
    items: list = field(default_factory=list)
    key_idea: str = ""
    figure: Optional[Figure] = None
    parts: list[str] = field(default_factory=list)
    section_id: str = ""
    continued: bool = False


def labels_per_slide(label_pt: float = 12.0, gutter_in: float = _GUTTER_IN) -> int:
    """How many labels a diagram slide can carry legibly, both gutters.

    Two limits, and the smaller wins.

    The first is arithmetic: how many label boxes fit down a gutter without
    touching. `_LABEL_LINES` is the wrap allowance — budgeting one line per
    label is how two of them end up stacked, and the validator would then
    refuse a slide the storyboard had promised.

    The second is `_LEADER_CLUTTER`, and it is a judgement rather than a
    measurement. At 12pt the arithmetic allows ten labels per side; ten
    leaders per side is a thicket of lines across the artwork that no
    overlap check would ever complain about, because none of them overlap.
    The rendered nine-label animal cell sat at five and four and read
    cleanly. Six a side is the most this layout should be asked for; past
    that the figure wants splitting, which is what the caller does with it.
    """
    pitch = (label_pt * 1.22 * _LABEL_LINES) / 72.0 + _LABEL_GAP_IN
    fits = 2 * int(gutter_in // pitch)
    return max(2, min(fits, _LEADER_CLUTTER))


def split_parts(fig: Figure, capacity: int) -> list[list[str]]:
    """Divide a figure's labels across as many slides as legibility needs.

    LARGEST FIRST, and that ordering is the teaching order as well as the
    subject-blind one: the outline and the big structures on the first slide,
    the fine detail on the next. A learner who has not yet found the cell
    membrane cannot place a ribosome inside it.

    The groups are balanced rather than filled — 14 parts at a capacity of 10
    become 7 and 7, not 10 and 4, because a slide holding four labels beside
    the same artwork looks like something went wrong.
    """
    parts = fig.located()
    if not parts:
        return []
    if len(parts) <= capacity:
        return [parts]

    def area(p: str) -> float:
        boxes = [b for name, bs in (fig.regions or {}).items()
                 if str(name).strip().lower() == p.strip().lower()
                 for b in (bs or []) if isinstance(b, (list, tuple)) and len(b) >= 4]
        return max((abs((b[2] - b[0]) * (b[3] - b[1])) for b in boxes), default=0.0)

    ordered = sorted(parts, key=lambda p: (-area(p), p.lower()))
    n = math.ceil(len(ordered) / capacity)
    per = math.ceil(len(ordered) / n)
    return [ordered[i:i + per] for i in range(0, len(ordered), per)]


def _paginate(blocks: list[dict], budget_in: float) -> list[list[dict]]:
    """Break a section body across slides so that every page actually fits.

    Two rules beyond "fill until full", both learned from the rendered Cells
    deck:

    A LIST IS NOT ATOMIC. Treating one as a single block put nine organelle
    definitions on one slide and ran the last three off the bottom edge. A
    list splits between items, and never leaves a single item stranded.

    A LEAD-IN STAYS WITH WHAT IT INTRODUCES. "Structures unique to plant
    cells:" is not a paragraph, it is the first line of the list below it,
    and breaking after it produced a page holding one colon and five inches
    of white space. A block ending in a colon is kept with its successor.
    """
    pages: list[list[dict]] = []
    cur: list[dict] = []
    used = 0.0

    def flush():
        nonlocal cur, used
        if cur:
            pages.append(cur)
        cur, used = [], 0.0

    def split_para(text: str, room: float) -> tuple[str, str]:
        """Break one paragraph at a sentence boundary near `room`.

        A paragraph taller than a whole slide had nowhere to go: only lists
        could be broken, so it was placed whole and ran off the bottom. Real
        article paragraphs are 300-600 characters and never hit this, which is
        precisely why it would have waited for one unusual article to appear.
        """
        head, tail, used_h = "", text, 0.0
        while tail:
            sentence, sep, rest = tail.partition(". ")
            piece = sentence + (sep or "")
            h = mx.text_height_in(head + piece, mx.CONTENT_W_IN, mx.BODY_PT)
            if head and h > room:
                break
            head, tail, used_h = head + piece, rest, h
        return head.strip(), tail.strip()

    def leads_in(b: dict) -> bool:
        return b["kind"] in ("para", "heading") and (b.get("text") or "").rstrip().endswith(":")

    i, n = 0, len(blocks)
    while i < n:
        b = blocks[i]
        h = mx.block_height_in(b)

        # A lead-in reserves room for what follows, so it can never be the last
        # thing on a page. How much room depends on whether the successor can
        # be broken: a list only needs its FIRST item to fit, because the rest
        # will flow onto the next page anyway, while a table cannot split at
        # all and has to be reserved whole. Reserving a nominal inch for the
        # table left "Differences between plant and animal cells:" stranded at
        # the foot of a page with its table overleaf.
        need = h
        if leads_in(b) and i + 1 < n:
            nxt = blocks[i + 1]
            need += (mx.list_item_height_in((nxt.get("items") or [""])[0])
                     if nxt["kind"] == "list" else mx.block_height_in(nxt))
        # ...but a list is never pre-flushed on its FULL height: it is about to
        # be split, and turning the page first is what put a lone lead-in and
        # five inches of white space on slide 5.
        if cur and b["kind"] != "list" and used + need > budget_in:
            flush()

        if b["kind"] == "list" and used + h > budget_in:
            items = list(b.get("items") or [])
            while items:
                room = budget_in - used
                if cur and room < mx.list_item_height_in(items[0]):
                    flush()
                    room = budget_in
                take: list[str] = []
                for it in items:
                    ih = mx.list_item_height_in(it)
                    if take and ih > room:
                        break
                    take.append(it)
                    room -= ih
                # A lone orphan on the next page reads as a mistake — but
                # PULLING IT BACK is how you overflow the very page you were
                # protecting: eight items filled 4.88in of 5.17in and the
                # ninth made it 5.49in. Push one FORWARD instead, so the last
                # page carries two and every page still fits.
                if len(items) - len(take) == 1 and len(take) > 1:
                    take.pop()
                cur.append({"kind": "list", "items": take})
                used += (sum(mx.list_item_height_in(x) for x in take)
                         + mx.LIST_TAIL_IN)
                items = items[len(take):]
            i += 1
            continue

        if b["kind"] == "para" and h > budget_in:
            text = b.get("text") or ""
            while text:
                room = budget_in - used - mx.PARA_GAP_IN
                if cur and room < 0.6:
                    flush()
                    room = budget_in - mx.PARA_GAP_IN
                head, text = split_para(text, room)
                if not head:                      # one unbreakable sentence
                    head, text = text, ""
                cur.append({"kind": "para", "text": head})
                used += mx.text_height_in(head, mx.CONTENT_W_IN,
                                          mx.BODY_PT) + mx.PARA_GAP_IN
                if text:
                    flush()
            i += 1
            continue

        cur.append(b)
        used += h
        i += 1
    flush()
    return pages or [[]]


def _clean_blocks(body_md: str) -> list[dict]:
    out = []
    for b in parse_body(body_md):
        b = dict(b)
        if b["kind"] in ("para", "heading"):
            b["text"] = strip_emphasis(b.get("text") or "")
            if not b["text"]:
                continue
        elif b["kind"] == "list":
            b["items"] = [strip_emphasis(x) for x in (b.get("items") or []) if strip_emphasis(x)]
            if not b["items"]:
                continue
        elif b["kind"] == "table":
            b["header"] = [strip_emphasis(x) for x in (b.get("header") or [])]
            b["rows"] = [[strip_emphasis(x) for x in r] for r in (b.get("rows") or [])]
            if not b["header"]:
                continue
        out.append(b)
    return out


def _norm(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


def drop_restatement(blocks: list[dict], key_idea: str) -> list[dict]:
    """Remove the sentence the key-idea band is already saying.

    The article's `claims` are EXTRACTED from the body, so a section's first
    claim is very often its first sentence word for word — and the slide then
    prints it twice, once at 20pt in a tinted band and again three lines
    below. Once is the point of the band; twice looks like a bug, because it
    is one.

    Only a leading sentence is dropped, and only on an exact normalised match:
    a claim that merely resembles the prose is a genuine summary and both
    earn their place.
    """
    if not key_idea or not blocks:
        return blocks
    want = _norm(key_idea)
    out = []
    for b in blocks:
        if b["kind"] == "para" and b.get("text"):
            text = b["text"]
            head, sep, tail = text.partition(". ")
            if _norm(head + ".") == want and sep:
                b = {**b, "text": tail.strip()}
                if not b["text"]:
                    continue
            elif _norm(text) == want:
                continue
        out.append(b)
    return out


def _chunk(seq, n):
    return [seq[i:i + n] for i in range(0, len(seq), n)] or []


def _diagram_slides(model: LessonModel, sec: Section, capacity: int) -> list[Slide]:
    slides: list[Slide] = []
    for fig in model.figures_for(sec):
        if not fig.annotatable:
            continue                      # a plain picture; the body slide keeps it
        groups = split_parts(fig, capacity)
        for i, parts in enumerate(groups):
            slides.append(Slide(
                kind=DIAGRAM, kicker=sec.heading if i == 0 else "",
                heading=fig.caption or sec.heading,
                subtitle=fig.caption if i == 0 else "",
                figure=fig, parts=parts, section_id=sec.id, continued=i > 0,
                notes=(f"{fig.caption}\n\nLabelled here: " + ", ".join(parts)
                       + (f"\n(Part {i + 1} of {len(groups)} — the remaining labels "
                          f"are on the next slide.)" if len(groups) > 1 else "")),
            ))
    return slides


def storyboard(model: LessonModel, label_pt: float = 12.0) -> list[Slide]:
    """The whole deck, in order. Every list that is empty simply omits its
    slide — a teacher's deck is SHORTER than a catalogue one, not hollower."""
    capacity = labels_per_slide(label_pt)
    out: list[Slide] = [Slide(kind=TITLE, heading=model.title or "Lesson",
                              subtitle=model.subtitle)]

    if model.objectives:
        out.append(Slide(kind=OBJECTIVES, kicker="Objectives",
                         heading="By the end of this lesson",
                         items=list(model.objectives),
                         notes="Read these out, or write them on the board, before you start."))

    for sec in model.sections:
        pages = _paginate(drop_restatement(_clean_blocks(sec.body_md), sec.key_idea),
                          mx.BODY_H_IN - mx.key_idea_height_in(sec.key_idea))
        for i, blocks in enumerate(pages):
            if not blocks and len(pages) == 1 and not sec.narration:
                continue                  # nothing to say and nothing to show
            out.append(Slide(kind=SECTION, kicker="", heading=sec.heading,
                             # The key idea leads the section, so it belongs on
                             # the FIRST page only; repeating it above a
                             # continuation reads as the slide having restarted.
                             key_idea=sec.key_idea if i == 0 else "",
                             blocks=blocks, section_id=sec.id, continued=i > 0,
                             notes=sec.narration))
        out.extend(_diagram_slides(model, sec, capacity))

    for group in _chunk(model.misconceptions, _MISCONCEPTIONS_PER_SLIDE):
        out.append(Slide(kind=MISCONCEPTIONS, kicker="Watch out for",
                         heading="Common misunderstandings", items=group,
                         continued=len(out) and out[-1].kind == MISCONCEPTIONS,
                         notes="Ask the class which of these they believed before "
                               "you give the correction."))

    for i, (problem, solution) in enumerate(model.worked_examples, 1):
        out.append(Slide(kind=WORKED, kicker="Worked example",
                         heading=problem, blocks=_clean_blocks(solution),
                         notes="Work through this on the board before showing the solution."))

    # The check comes before the glossary: it is the last thing the class does,
    # and the glossary is a reference they keep.
    check = next((f for f in model.figures.values() if f.annotatable), None)
    if check:
        asked = split_parts(check, capacity)
        if asked:
            out.append(Slide(kind=CHECK, kicker="Check", heading="Name each structure",
                             figure=check, parts=asked[0],
                             notes="Answers: " + "; ".join(
                                 f"{n}. {p}" for n, p in enumerate(asked[0], 1))))

    for group in _chunk(model.glossary, _GLOSSARY_PER_SLIDE):
        out.append(Slide(kind=GLOSSARY, kicker="Key terms", heading="Words to know",
                         items=group,
                         continued=len(out) and out[-1].kind == GLOSSARY))

    out.append(Slide(kind=CLOSING, heading=model.title or "Lesson",
                     subtitle="Every slide's speaker notes carry the narration."))
    return out


def summarise(slides: list[Slide]) -> dict[str, int]:
    """Slide counts by kind — what a test asserts and a log line prints."""
    counts: dict[str, int] = {}
    for s in slides:
        counts[s.kind] = counts.get(s.kind, 0) + 1
    return counts
