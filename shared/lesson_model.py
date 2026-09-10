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

    def located(self) -> list[str]:
        """Declared parts the artwork can actually place."""
        have = {str(k).strip().lower() for k in (self.regions or {})}
        return [p for p in self.parts if str(p).strip().lower() in have]


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
        first = next((t for sid, t in m.claims if sid == sec.id), "")
        # A claim longer than a breath is a paragraph that lost its full stop;
        # it belongs in the body, not in 20pt across the top of the slide.
        sec.key_idea = first if first and len(first) <= 150 else ""

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
            # The points ARE the chapter's own prose, one sentence each. They
            # become paragraphs on the deck rather than bullets; the storyboard
            # decides that, not this module.
            body_md="\n\n".join(pts),
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
