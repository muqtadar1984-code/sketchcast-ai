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

from shared.lesson_model import (Figure, LessonModel, Section, display_part,
                                 parse_body, strip_emphasis)

from . import metrics as mx
from .deck_strings import T

TITLE = "title"
OBJECTIVES = "objectives"
SECTION = "section"
DIAGRAM = "diagram"
MISCONCEPTIONS = "misconceptions"
WORKED = "worked_example"
GLOSSARY = "glossary"
CHECK = "check"
FOCUS = "focus"
COMPARE = "compare"
QUIZ = "quiz"
TAKEAWAYS = "takeaways"
SHAPES = "shapes"          # flow / cycle / hierarchy, drawn as native shapes
ICONS = "icons"
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


# A section that HAS a figure shows it beside its opening prose, at this width,
# before the full labelled version gets a slide of its own. That is how a
# textbook does it, and it costs nothing: the artwork already exists.
_ILLUSTRATED_BODY_W_IN = 7.30
# Focus slides zoom one part of a figure. Capped because a deck of zooms is a
# deck with no lesson in it, and only offered where the ARTICLE supplies the
# words — a crop with nothing to say beside it is decoration.
_FOCUS_PER_FIGURE = 1
_FOCUS_PER_DECK = 3
# A list item wider than this is a paragraph wearing a bullet. In em, not
# characters: 140 Latin characters is one idea, 140 Devanagari characters is
# a paragraph and 140 ideographs is a page.
_POINT_MAX_EM = 140 * 0.48


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
    figure_b: Optional[Figure] = None
    illustration: Optional[Figure] = None
    body_w_in: float = 0.0
    parts: list[str] = field(default_factory=list)
    section_id: str = ""
    continued: bool = False
    visual: Optional[dict] = None
    label: str = ""


def _t(v) -> str:
    return " ".join(str(v or "").split())


def visual_slides(sec: Section, notes: str, lang: str = "en") -> tuple[str, list[dict], list[Slide]]:
    """What a section's authored visual contributes: a key idea, extra body
    blocks, and slides of its own.

    definition -> the term's meaning becomes the key-idea band
    compare    -> a native two-column table joins the body
    quiz       -> a check slide (options native, answer in the notes)
    takeaways  -> a numbered recap slide
    flow/cycle/hierarchy -> a slide of native shapes and connectors
    icons      -> a slide of small icon tiles with native labels

    A visual that fails its own minimum (a flow with one node, a quiz with
    one option) contributes nothing — the same refusal the video path makes,
    rather than drawing something that teaches a hole.
    """
    vis = sec.visual or {}
    kind = _t(vis.get("kind")).lower()
    caption = _t(vis.get("caption"))
    nodes = [_t(n) for n in (vis.get("nodes") or []) if _t(n)]
    key_idea, blocks, slides = "", [], []
    if kind == "definition" and _t(vis.get("body")):
        key_idea = _t(vis.get("body"))
    elif kind == "compare":
        groups = [g for g in (vis.get("groups") or []) if isinstance(g, dict)][:2]
        cols = [[_t(i) for i in (g.get("items") or []) if _t(i)] for g in groups]
        if len(groups) == 2 and all(cols):
            n = max(len(c) for c in cols)
            blocks.append({"kind": "table",
                           "header": [_t(g.get("heading")) or f"Option {i + 1}"
                                      for i, g in enumerate(groups)],
                           "rows": [[cols[0][r] if r < len(cols[0]) else "",
                                     cols[1][r] if r < len(cols[1]) else ""] for r in range(n)]})
    elif kind == "quiz":
        options = [_t(o) for o in (vis.get("options") or []) if _t(o)][:4]
        ans = vis.get("answer")
        if len(options) >= 2:
            right = options[ans] if isinstance(ans, int) and 0 <= ans < len(options) else ""
            slides.append(Slide(kind=QUIZ, kicker=T(lang, "check"), heading=sec.heading,
                                items=options, section_id=sec.id,
                                notes=(f"{T(lang, 'answer')}: {right}" if right else T(lang, "answer_not_given"))
                                      + (f"\n\n{notes}" if notes else "")))
    elif kind == "takeaways" and len(nodes) >= 2:
        slides.append(Slide(kind=TAKEAWAYS, kicker=T(lang, "remember"), heading=sec.heading,
                            items=nodes, section_id=sec.id, notes=notes))
    elif kind in ("flow", "cycle", "hierarchy") and len(nodes) >= 2:
        slides.append(Slide(kind=SHAPES, heading=sec.heading, items=nodes,
                            subtitle=caption, section_id=sec.id, notes=notes,
                            visual={"kind": kind}))
    elif kind == "icons":
        items = [(_t(i.get("icon")), _t(i.get("label"))) for i in (vis.get("items") or [])
                 if isinstance(i, dict) and _t(i.get("label"))][:6]
        if len(items) >= 2:
            slides.append(Slide(kind=ICONS, heading=sec.heading, items=items,
                                subtitle=caption, section_id=sec.id, notes=notes))
    return key_idea, blocks, slides


def labels_per_slide(label_pt: float = mx.LABEL_PT, gutter_in: float = _GUTTER_IN) -> int:
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


# Sentence ends, by script: the Latin full stop, the Arabic question mark,
# the Devanagari danda, the CJK ideographic full stop (which takes no space).
_SENTENCE_ENDS = (". ", "؟ ", "। ", "。", "！", "？")


def _split_sentence(text: str) -> tuple[str, str, str]:
    """The first sentence of `text`, its terminator, and the rest."""
    best = None
    for end in _SENTENCE_ENDS:
        i = text.find(end)
        if i >= 0 and (best is None or i < best[0]):
            best = (i, end)
    if best is None:
        return text, "", ""
    i, end = best
    return text[:i], end, text[i + len(end):]


def _paginate(blocks: list[dict], budget_in: float,
              width_in: float = mx.CONTENT_W_IN) -> list[list[dict]]:
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
            sentence, sep, rest = _split_sentence(tail)
            piece = sentence + (sep or "")
            h = mx.text_height_in(head + piece, width_in, mx.BODY_PT)
            if head and h > room:
                break
            head, tail, used_h = head + piece, rest, h
        return head.strip(), tail.strip()

    def leads_in(b: dict) -> bool:
        return b["kind"] in ("para", "heading") and (b.get("text") or "").rstrip().endswith(":")

    i, n = 0, len(blocks)
    while i < n:
        b = blocks[i]
        h = mx.block_height_in(b, width_in)

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
            need += (mx.list_item_height_in((nxt.get("items") or [""])[0], width_in)
                     if nxt["kind"] == "list" else mx.block_height_in(nxt, width_in))
        # ...but a list is never pre-flushed on its FULL height: it is about to
        # be split, and turning the page first is what put a lone lead-in and
        # five inches of white space on slide 5.
        if cur and b["kind"] != "list" and used + need > budget_in:
            flush()

        if b["kind"] == "list" and used + h > budget_in:
            items = list(b.get("items") or [])
            while items:
                room = budget_in - used
                if cur and room < mx.list_item_height_in(items[0], width_in):
                    flush()
                    room = budget_in
                take: list[str] = []
                for it in items:
                    ih = mx.list_item_height_in(it, width_in)
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
                used += (sum(mx.list_item_height_in(x, width_in) for x in take)
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
                used += mx.text_height_in(head, width_in, mx.BODY_PT) + mx.PARA_GAP_IN
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


def section_content(sec: Section) -> tuple[list[dict], str]:
    """What the section's slide shows, and what its notes say.

    The slide shows POINTS: the article's own claims for the section, plus
    any list or table the prose already contained (those are point-form by
    nature — a table is a summary, a list is one). The paragraphs go to the
    speaker notes, whole, so the teacher has the full text under the slide
    rather than on it. A section with no claims and no structure falls back
    to its prose, with the key-idea restatement removed, because a slide
    that is only a heading is worse than one with a paragraph.
    """
    blocks = _clean_blocks(sec.body_md)
    tables = [b for b in blocks if b["kind"] == "table"]
    lists = [b for b in blocks if b["kind"] == "list"]
    # A list whose items are paragraphs is prose in disguise. The Cells
    # article's organelle list — "Cell membrane (Plasma membrane): This outer
    # boundary of the cell is a selectively permeable barrier that..." — went
    # up verbatim as bullets and filled two slides with the same definitions
    # the glossary slides already carry. Short items are points and stay;
    # long ones join the prose in the notes.
    short_lists = [b for b in lists
                   if max((mx.text_width_em(it) for it in b["items"]), default=0) <= _POINT_MAX_EM]
    prose_bits = [b["text"] for b in blocks if b["kind"] == "para"]
    prose_bits += ["; ".join(b["items"]) for b in lists if b not in short_lists]
    notes = sec.narration or "\n\n".join(prose_bits)
    if sec.points:
        return ([{"kind": "list", "items": list(sec.points)}] + short_lists + tables), notes
    if short_lists or tables:
        return short_lists + tables, notes
    return drop_restatement(blocks, sec.key_idea), sec.narration


def _table_pages(pairs, header, first_col: float, budget_in: float) -> list[list]:
    """Split table rows across slides by MEASURED height, not a row count.

    Eight glossary rows per slide was fine at 12pt. At 16pt a definition
    wraps to three lines and eight of them are a foot of table on a slide
    with five inches of room; the row count has to give way to the ruler.
    """
    cols = mx.table_col_widths_in(len(header), mx.CONTENT_W_IN, first_col)
    head = mx.table_row_height_in(header, cols, mx.TABLE_HEAD_PT)
    pages, cur, used = [], [], head
    for pair in pairs:
        h = mx.table_row_height_in(list(pair), cols)
        if cur and used + h > budget_in:
            pages.append(cur)
            cur, used = [], head
        cur.append(pair)
        used += h
    if cur:
        pages.append(cur)
    return pages


def _chunk(seq, n):
    return [seq[i:i + n] for i in range(0, len(seq), n)] or []


def _diagram_slides(model: LessonModel, sec: Section, capacity: int) -> list[Slide]:
    slides: list[Slide] = []
    lang = model.language
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
                notes=(f"{fig.caption}\n\n{T(lang, 'labelled_here')} " + ", ".join(display_part(p) for p in parts)),
            ))
    return slides


def storyboard(model: LessonModel, label_pt: float = mx.LABEL_PT) -> list[Slide]:
    """The whole deck, in order. Every list that is empty simply omits its
    slide — a teacher's deck is SHORTER than a catalogue one, not hollower."""
    capacity = labels_per_slide(label_pt)
    lang = model.language
    out: list[Slide] = [Slide(kind=TITLE, heading=model.title or "Lesson",
                              subtitle=model.subtitle)]

    if model.objectives:
        out.append(Slide(kind=OBJECTIVES, kicker=T(lang, "objectives"),
                         heading=T(lang, "by_end"),
                         items=list(model.objectives)))

    for sec in model.sections:
        body, notes = section_content(sec)
        v_key, v_blocks, v_slides = visual_slides(sec, notes, lang)
        key_idea = sec.key_idea or v_key
        body = body + v_blocks
        # A quiz or a diagram segment often has nothing but its heading and
        # narration: the visual slide IS the section, and a plain slide before
        # it would be a heading over the spoken text twice. On the video and
        # book routes the body IS the narration (no points are authored), so
        # "nothing but narration" is the common case, not the empty one —
        # the first live teacher deck paired every diagram, quiz and recap
        # with a slide of the words the teacher was about to say.
        only_narration = not sec.body_md or sec.body_md == sec.narration
        if v_slides and not sec.points and not v_blocks and only_narration:
            body = []
        budget = mx.BODY_H_IN - mx.key_idea_height_in(key_idea)
        # A section that owns artwork shows it beside its OPENING prose. The
        # first page is therefore measured against a narrower column and the
        # rest against the full width — paginating everything narrow would
        # leave the later, unillustrated pages needlessly cramped.
        art = next((f for f in model.figures_for(sec) if f.annotatable), None)
        if art:
            first = _paginate(body, budget, _ILLUSTRATED_BODY_W_IN)[0]
            rest = body[len(first):]
            pages = [first] + ([] if not rest else _paginate(rest, mx.BODY_H_IN))
        else:
            pages = _paginate(body, budget)
        for i, blocks in enumerate(pages):
            if not blocks and len(pages) == 1 and not art and (v_slides or not sec.narration):
                continue                  # nothing to say and nothing to show
            out.append(Slide(kind=SECTION, kicker="",
                             heading=sec.heading + (f"  {T(lang, 'continued')}" if i > 0 else ""),
                             illustration=art if i == 0 else None,
                             body_w_in=_ILLUSTRATED_BODY_W_IN if (art and i == 0)
                             else mx.CONTENT_W_IN,
                             # The key idea leads the section, so it belongs on
                             # the FIRST page only; repeating it above a
                             # continuation reads as the slide having restarted.
                             key_idea=key_idea if i == 0 else "",
                             blocks=blocks, section_id=sec.id, continued=i > 0,
                             notes=notes))
        out.extend(v_slides)
        out.extend(_diagram_slides(model, sec, capacity))

    mis_header = [T(lang, "learners_think"), T(lang, "in_fact")]
    for group in _table_pages(model.misconceptions, mis_header,
                              0.40, mx.BODY_H_IN - 0.1 - mx.TABLE_GAP_IN):
        cont = bool(out) and out[-1].kind == MISCONCEPTIONS
        out.append(Slide(kind=MISCONCEPTIONS, kicker=T(lang, "watch_out"),
                         heading=T(lang, "misunderstandings") + (f"  {T(lang, 'continued')}" if cont else ""),
                         items=group, label="|".join(mis_header)))

    for i, (problem, solution) in enumerate(model.worked_examples, 1):
        # The PROBLEM is not a heading. It was one, and a four-part classify
        # question ran past the 120-character cap and was cut off mid-clause —
        # a slide asking a question it does not finish asking. It goes in the
        # band instead, which wraps and is sized to be read from the back.
        out.append(Slide(kind=WORKED, kicker=T(lang, "worked_example"),
                         heading=(T(lang, "worked_example_n", n=i) if len(model.worked_examples) > 1
                                  else T(lang, "worked_example")),
                         key_idea=problem,
                         blocks=_paginate(_clean_blocks(solution),
                                          mx.BODY_H_IN
                                          - mx.key_idea_height_in(problem))[0]))

    # Zoom on the parts the ARTICLE has words for. `Picture.crop_*` makes a
    # focus view the same image with different crop fractions, so these cost
    # no artwork at all — but a crop with nothing beside it is decoration, so
    # a part with neither a glossary entry nor a claim does not get one.
    focused = 0
    for fig in model.figures.values():
        if not fig.annotatable or focused >= _FOCUS_PER_DECK:
            continue
        made = 0
        for part in fig.zoomable():
            if made >= _FOCUS_PER_FIGURE or focused >= _FOCUS_PER_DECK:
                break
            definition = model.definition_of(part)
            says = model.claims_about(part)
            if not definition and len(says) < 2:
                continue
            body = ([{"kind": "para", "text": definition}] if definition else [])
            if says:
                body.append({"kind": "list", "items": says})
            out.append(Slide(kind=FOCUS, kicker=T(lang, "zoom_in"), heading=display_part(part),
                             subtitle=fig.caption, figure=fig, parts=[part],
                             blocks=body))
            made += 1
            focused += 1

    a, b, common = model.shared_parts()
    if a is not None and b is not None:
        only_b = [p for p in b.located()
                  if p.lower() not in {q.lower() for q in a.located()}]
        # The captions are full sentences ("A typical animal cell showing key
        # organelles."); two of them joined by "vs" is not a heading, it is a
        # paragraph in bold. They already label their own picture below.
        out.append(Slide(kind=COMPARE, kicker=T(lang, "compare"),
                         heading=T(lang, "side_by_side"),
                         figure=a, figure_b=b, items=[display_part(p) for p in only_b],
                         label=T(lang, "only_second_has"),
                         subtitle=(T(lang, "shared") + " " + ", ".join(display_part(p) for p in common[:8]))))

    # The check comes before the glossary: it is the last thing the class does,
    # and the glossary is a reference they keep.
    check = next((f for f in model.figures.values() if f.annotatable), None)
    if check:
        asked = split_parts(check, capacity)
        if asked:
            out.append(Slide(kind=CHECK, kicker=T(lang, "check"), heading=T(lang, "name_each"),
                             figure=check, parts=asked[0],
                             notes=T(lang, "answers") + " " + "; ".join(
                                 f"{n}. {display_part(p)}"
                                 for n, p in enumerate(asked[0], 1))))

    gl_header = [T(lang, "term"), T(lang, "meaning")]
    for group in _table_pages(model.glossary, gl_header,
                              0.26, mx.BODY_H_IN - 0.1 - mx.TABLE_GAP_IN):
        cont = bool(out) and out[-1].kind == GLOSSARY
        out.append(Slide(kind=GLOSSARY, kicker=T(lang, "key_terms"),
                         heading=T(lang, "words_to_know") + (f"  {T(lang, 'continued')}" if cont else ""),
                         items=group, label="|".join(gl_header)))

    out.append(Slide(kind=CLOSING, heading=T(lang, "ready"), subtitle=model.title or "Lesson",
                     label=T(lang, "notes_line")))
    return out


def summarise(slides: list[Slide]) -> dict[str, int]:
    """Slide counts by kind — what a test asserts and a log line prints."""
    counts: dict[str, int] = {}
    for s in slides:
        counts[s.kind] = counts.get(s.kind, 0) + 1
    return counts
