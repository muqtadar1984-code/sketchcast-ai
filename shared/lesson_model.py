"""One shape for "what this lesson teaches", filled from either source.

The deck and the video are meant to share a lesson and differ in how they
show it. They cannot share anything today: the deck is built from
``deck_slides`` — heading, bullets, narration, PNG — which is assembled two
lossy hops downstream of whatever the lesson actually came from.

This is that missing shape. It is deliberately NOT the article: a catalogue
kit has an article and a teacher's chapter kit never will, and a model that
only one of them can fill would leave the people the founder cares most about
on the old path forever.

WHAT EACH SOURCE CAN FILL

    field             article (catalogue)      analysis + script (teachers)
    ----------------------------------------------------------------------
    title             title                    episode_title
    objectives        objectives[]             — (see below)
    sections          sections[] with body_md  script segments, heading + prose
    glossary          glossary[]               concepts[] {name, definition}
    misconceptions    misconceptions[]         —
    worked_examples   worked_examples[]        —
    figures           article_figures + art    —

An empty list means the storyboard omits that slide, never that it renders an
empty one. That is the fail-over-degrade rule at deck scale: a teacher gets
FEWER slides than a catalogue lesson, each of which is real, rather than the
same count with three of them hollow.

OBJECTIVES ARE NOT INFERRED. `analysis.concepts[].importance == "foundational"`
is tempting and wrong: turning "Cell membrane" into "Explain the cell
membrane" invents a teaching objective the curriculum never set, prints it on
a slide under the school's name, and reads exactly like one a human wrote.
The objectives slide is simply absent on that path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# A word boundary, spelled out. Written as r"\b" it has now twice been
# flattened to a literal backspace by a shell heredoc, and a regex with a
# control character in it matches nothing and says nothing about why.
_WORD = chr(92) + "b"

SOURCE_ARTICLE = "article"
SOURCE_ANALYSIS = "analysis"


@dataclass
class Figure:
    """A planned diagram and, if it was ever rendered, the artwork for it."""

    key: str
    caption: str = ""
    parts: list[str] = field(default_factory=list)
    png: Optional[Path] = None
    regions: dict = field(default_factory=dict)
    w: float = 0.0
    h: float = 0.0

    @property
    def annotatable(self) -> bool:
        """Can this figure carry native labels?

        All four must hold, and the last is the one that bites: `vision.w`/`h`
        are the frame the region boxes were MEASURED in, so without them a box
        is a set of numbers with no scale and every label lands somewhere
        arbitrary. A figure that fails this is still usable as a plain picture.
        """
        return bool(self.png and Path(self.png).exists()
                    and self.regions and self.w > 0 and self.h > 0)

    # A part whose box is most of the frame is not a detail you can zoom to:
    # it IS the frame. `cell membrane` spans [12,14,690,598] of a 704x613
    # picture, so "cropping" to it returns the whole cell — a focus slide
    # headed "cell membrane" showing the entire diagram, which teaches the
    # reader nothing and looks like the crop silently failed. Same threshold
    # the renderer uses to decide how to aim a leader at one.
    ENCLOSER_AREA = 0.55

    def encloses(self, part: str) -> bool:
        boxes = part_boxes(self.regions, part)
        if not boxes or not (self.w and self.h):
            return False
        biggest = max(abs((b[2] - b[0]) * (b[3] - b[1])) for b in boxes)
        return biggest / (self.w * self.h) >= self.ENCLOSER_AREA

    def zoomable(self) -> list[str]:
        """Located parts small enough that a crop to them means something."""
        return [p for p in self.located() if not self.encloses(p)]

    def located(self) -> list[str]:
        """Declared parts the artwork can actually place.

        Delegates to `part_boxes` rather than comparing names itself. It DID
        compare them itself, with a plain case-fold, and so did the renderer —
        two copies of one rule, and fixing the renderer's copy left this one
        answering "no parts" for `climate_impact`, which silently cost that
        figure its whole slide while the renderer was ready to draw it.
        """
        return [p for p in self.parts if part_boxes(self.regions, p)]


@dataclass
class Section:
    id: str
    heading: str
    body_md: str = ""
    figure_keys: list[str] = field(default_factory=list)
    narration: str = ""
    # The one sentence this section exists to establish. Taken from the
    # article's own `claims`, which are written to be "precise and checkable"
    # because QUESTIONS are generated from them — which makes them the best
    # single-sentence statements in the whole article, and the deck has never
    # looked at them. Empty on the teacher path, where the slide simply has
    # no key-idea band rather than a fabricated one.
    key_idea: str = ""
    # What the slide SHOWS. The prose is what the article says; the points are
    # what a class should take from it, and a slide is for the second. From
    # the article these are its own `claims` for the section — one sentence
    # each, written to be precise because questions are generated from them —
    # so no summarising call is made and nothing on the slide was invented.
    # The prose is not lost: it becomes the speaker notes.
    points: list[str] = field(default_factory=list)


@dataclass
class LessonModel:
    title: str = ""
    subtitle: str = ""
    objectives: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    glossary: list[tuple[str, str]] = field(default_factory=list)
    misconceptions: list[tuple[str, str]] = field(default_factory=list)
    worked_examples: list[tuple[str, str]] = field(default_factory=list)
    figures: dict[str, Figure] = field(default_factory=dict)
    claims: list[tuple[str, str]] = field(default_factory=list)   # (section_id, text)
    source: str = SOURCE_ARTICLE

    def figures_for(self, section: Section) -> list[Figure]:
        return [self.figures[k] for k in section.figure_keys if k in self.figures]

    def definition_of(self, part: str) -> str:
        """The article's own glossary entry for a part, if it wrote one."""
        want = norm_part(part)
        for term, meaning in self.glossary:
            if norm_part(term) == want:
                return meaning
        return ""

    def claims_about(self, part: str, limit: int = 3) -> list[str]:
        """Claims that name this part, as whole words.

        Whole words matter: a substring match on "orbit" would also pull in
        "orbital", and on "sun" every "sunlight" in the article.
        """
        want = norm_part(part)
        if not want:
            return []
        pat = re.compile(_WORD + re.escape(want) + _WORD, re.I)
        out = [t for _sid, t in self.claims if pat.search(norm_part(t))]
        return out[:limit]

    def shared_parts(self) -> tuple[Optional[Figure], Optional[Figure], list[str]]:
        """The two figures with the most parts in common, if that is worth a
        slide. Below three shared parts a comparison teaches nothing — two
        diagrams that both happen to contain a sun are not a contrast."""
        figs = [f for f in self.figures.values() if f.annotatable]
        best: tuple[float, Optional[Figure], Optional[Figure], list[str]] = (0, None, None, [])
        for i, a in enumerate(figs):
            for b in figs[i + 1:]:
                bn = {norm_part(p) for p in b.located()}
                common = [p for p in a.located() if norm_part(p) in bn]
                if len(common) > best[0]:
                    best = (len(common), a, b, common)
        return (best[1], best[2], best[3]) if best[0] >= 3 else (None, None, [])


# ── naming the same part twice ────────────────────────────────────────

def norm_part(s: str) -> str:
    """A part name with its separators levelled.

    THE FIGURE AND THE ARTWORK ARE NAMED BY DIFFERENT HANDS. `spec.parts` is
    written by the article author as identifiers — `axis_tilt`,
    `incoming_radiation` — while the vision pass names what it SAW, in
    English: `axis tilt`, `incoming radiation`. Measured on the live Weather
    figures, a plain case-fold matched NONE of `climate_impact`'s five parts,
    so that figure produced no slide at all and the other three would have
    rendered one or two labels instead of five. Row counts agreed throughout
    — five declared, five stored — because the counts were right and the join
    was never tested.
    """
    return " ".join(str(s or "").replace("_", " ").replace("-", " ").lower().split())


def display_part(part: str) -> str:
    """A part name as a slide should show it: `thirty_year_calendar` becomes
    `thirty year calendar`. Case is left alone — `Golgi apparatus` is spelled
    that way on purpose."""
    return " ".join(str(part or "").replace("_", " ").replace("-", " ").split())


def part_boxes(regions: dict, part: str) -> list[tuple[float, float, float, float]]:
    """Every stored instance of `part`, over three widening tiers.

    Exact, then separator-levelled, then the shared `same_part` matcher (which
    knows singular/plural). The last tier REFUSES an ambiguous hit: a name
    that could be two different regions is not a name, and a leader drawn to
    the wrong one of them is a confident arrow at the wrong structure — the
    failure this whole path exists to avoid.
    """
    if not isinstance(regions, dict):
        return []

    def unpack(boxes):
        out = []
        for b in boxes or []:
            if isinstance(b, (list, tuple)) and len(b) >= 4:
                x0, y0, x1, y1 = (float(v) for v in b[:4])
                out.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
        return out

    raw = str(part or "").strip().lower()
    want = norm_part(part)
    if not want:
        return []
    for name, boxes in regions.items():
        if str(name).strip().lower() == raw:
            return unpack(boxes)
    for name, boxes in regions.items():
        if norm_part(name) == want:
            return unpack(boxes)
    try:
        from spike.scene_engine.partnames import same_part
    except Exception:                       # noqa: BLE001 — tiers 1-2 still stand
        return []
    hits = [b for n, b in regions.items() if same_part(want, norm_part(n))]
    return unpack(hits[0]) if len(hits) == 1 else []


# ── helpers ───────────────────────────────────────────────────────────

def _text(v) -> str:
    return " ".join(str(v or "").split())


def _pairs(rows, a: str, b: str) -> list[tuple[str, str]]:
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        x, y = _text(r.get(a)), _text(r.get(b))
        if x and y:
            out.append((x, y))
    return out


# ── the catalogue path ────────────────────────────────────────────────

def from_article(article: dict, figure_rows: list[dict] | None = None,
                 art: Optional[Callable[[dict], Optional[dict]]] = None) -> LessonModel:
    """Build the model from an approved article and its figure rows.

    ``art(figure_row)`` is the caller's IO hook: given an ``article_figures``
    row it returns ``{"png": Path, "regions": {...}, "w": int, "h": int}`` or
    None. Kept as a callback so this module stays pure and testable — and so
    the storyboard can be exercised with no network at all.
    """
    m = LessonModel(source=SOURCE_ARTICLE)
    m.title = _text(article.get("title"))
    m.objectives = [_text(o.get("text")) for o in (article.get("objectives") or [])
                    if isinstance(o, dict) and _text(o.get("text"))]
    for s in article.get("sections") or []:
        if not isinstance(s, dict):
            continue
        heading = _text(s.get("heading"))
        if not heading:
            continue
        m.sections.append(Section(
            id=str(s.get("id") or f"s{len(m.sections) + 1}"),
            heading=heading,
            body_md=str(s.get("body_md") or ""),
            figure_keys=[str(k) for k in (s.get("figure_keys") or []) if str(k).strip()],
        ))
    for c in article.get("claims") or []:
        if isinstance(c, dict) and _text(c.get("text")):
            m.claims.append((str(c.get("section_id") or ""), _text(c.get("text"))))
    for sec in m.sections:
        mine = [t for sid, t in m.claims if sid == sec.id]
        first = mine[0] if mine else ""
        # A claim longer than a breath is a paragraph that lost its full stop;
        # it belongs in the body, not in 20pt across the top of the slide.
        sec.key_idea = first if first and len(first) <= 150 else ""
        sec.points = [t for t in mine if t != sec.key_idea]

    m.glossary = _pairs(article.get("glossary"), "term", "definition")
    m.misconceptions = _pairs(article.get("misconceptions"), "misconception", "correction")
    m.worked_examples = _pairs(article.get("worked_examples"), "problem", "solution_md")

    for row in figure_rows or []:
        if not isinstance(row, dict):
            continue
        key = _text(row.get("figure_key"))
        if not key:
            continue
        spec = row.get("spec") if isinstance(row.get("spec"), dict) else {}
        fig = Figure(key=key, caption=_text(row.get("caption")),
                     parts=[_text(p) for p in (spec.get("parts") or []) if _text(p)])
        got = art(row) if art else None
        if got:
            fig.png = Path(got["png"]) if got.get("png") else None
            fig.regions = got.get("regions") or {}
            fig.w = float(got.get("w") or 0)
            fig.h = float(got.get("h") or 0)
        m.figures[key] = fig
    return m


# ── the teacher / parent path ─────────────────────────────────────────

def from_analysis(analysis: dict, script: dict) -> LessonModel:
    """Build the model from a chapter analysis and its script.

    Sections come from the SCRIPT rather than from
    ``difficulty_assessments[].section_title``, because the script's segments
    are what the lesson was actually taught in — the deck and the video then
    still walk the same ground even though they draw it differently.

    The glossary is the real gain here and it costs nothing: `concepts[]`
    already carries a name and a learner-pitched definition for every concept
    in the chapter, and the deck has been throwing all of it away.
    """
    m = LessonModel(source=SOURCE_ANALYSIS)
    episodes = script.get("episodes") if isinstance(script.get("episodes"), list) else None
    ep = (episodes or [script])[0] if (episodes or script) else {}
    m.title = _text(ep.get("episode_title"))

    from shared.text_clean import strip_ssml
    for i, seg in enumerate(ep.get("segments") or [], 1):
        if not isinstance(seg, dict):
            continue
        heading = _text(seg.get("slide_heading")) or m.title
        narration = _text(strip_ssml(str(seg.get("text") or "")))
        pts = [_text(p) for p in (seg.get("slide_points") or []) if _text(p)]
        m.sections.append(Section(
            id=str(seg.get("segment_id") or f"s{i:03d}"),
            heading=heading,
            # `slide_points` already ARE the points; the narration is the prose.
            points=pts,
            narration=narration,
        ))

    seen: set[str] = set()
    for c in ((analysis.get("concepts") or {}).get("concepts")
              if isinstance(analysis.get("concepts"), dict) else analysis.get("concepts")) or []:
        if not isinstance(c, dict):
            continue
        term, definition = _text(c.get("name")), _text(c.get("definition"))
        if not (term and definition) or term.lower() in seen:
            continue
        seen.add(term.lower())
        m.glossary.append((term, definition))
    return m


# ── markdown, only as much as a slide needs ───────────────────────────

_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_HEADING = re.compile(r"^\s*#{1,6}\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")


def parse_body(body_md: str) -> list[dict]:
    """Split a section body into blocks the renderer can lay out natively.

    Blocks are ``{"kind": "para"|"list"|"table"|"heading", ...}``. A markdown
    TABLE becomes a real PowerPoint table rather than a picture of one, which
    is the single biggest editability win available in a body: a table is
    exactly the thing a teacher wants to retype for their own class.
    """
    blocks: list[dict] = []
    lines = (body_md or "").replace("\r\n", "\n").split("\n")
    i, n = 0, len(lines)
    para: list[str] = []
    items: list[str] = []

    def flush_para():
        if para:
            blocks.append({"kind": "para", "text": _text(" ".join(para))})
            para.clear()

    def flush_list():
        if items:
            blocks.append({"kind": "list", "items": list(items)})
            items.clear()

    while i < n:
        line = lines[i]
        if not line.strip():
            flush_para(); flush_list(); i += 1; continue
        h = _HEADING.match(line)
        if h:
            flush_para(); flush_list()
            blocks.append({"kind": "heading", "text": _text(h.group(1))})
            i += 1
            continue
        # A table needs its separator row on the NEXT line; a lone pipe in
        # prose ("either | or") must not swallow the paragraph.
        if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            flush_para(); flush_list()
            rows = []
            while i < n and "|" in lines[i]:
                if not _TABLE_SEP.match(lines[i]):
                    cells = [_text(c) for c in lines[i].strip().strip("|").split("|")]
                    rows.append(cells)
                i += 1
            if rows:
                blocks.append({"kind": "table", "header": rows[0], "rows": rows[1:]})
            continue
        b = _BULLET.match(line) or _NUMBERED.match(line)
        if b:
            flush_para()
            items.append(_text(b.group(1)))
            i += 1
            continue
        flush_list()
        para.append(line.strip())
        i += 1
    flush_para(); flush_list()
    return blocks


def strip_emphasis(s: str) -> str:
    """`**bold**` and `*italic*` to plain text — PowerPoint runs carry the
    weight instead, and a literal asterisk on a slide reads as a typo."""
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s or "")
    s = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", s)
    return re.sub(r"`(.+?)`", r"\1", s)
