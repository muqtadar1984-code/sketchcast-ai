"""An approved knowledge ARTICLE as the chapter the lesson pipeline reads.

Catalogue Phase 3 (2026-09-06). Every stage after ingestion — the analyzer,
the script generator, the document builders, the deck — takes a chapter dict
shaped like ``agent1_ingestion.models.ChapterContent``. A catalogue kit has no
book and no PDF: its source of truth is the topic's approved article
(``topic_articles``, 0112). This module is the ONE place that translation
happens, and the shape is pinned by a test that validates the result through
the pydantic model itself, so a drift in either the article row or the
chapter contract fails here rather than three agents later.

The mapping (decision 1 of the Phase 3 spec):

  chapter_num   -1 — the marker ``worker/process.py`` already uses for a
                synthetic chapter (revision papers, exams): nothing keyed by
                (book, chapter) may be cached or persisted for it.
  start/end     0 — there are no pages.
  sections      one ``body`` section per article section: ``heading`` is the
                section title, ``body_md`` (markdown stripped to plain
                prose) is the content. Sections carry the RENDERED figures'
                captions as trailing "Figure: …" lines, so the analysis
                knows which diagrams the article planned and its visual
                opportunities line up with assets that already exist.
  key_boxes     glossary → ``definition`` boxes; misconceptions →
                ``misconception`` boxes (title = the misconception, content
                = its correction); worked examples → ``example`` boxes;
                claims → one ``key_points`` box per section (the section's
                claims, one per line) so the analyzer's concept extraction
                sees the article's own factual spine.
  images        [] — the visual pipeline is fed by the scene engine and the
                visual library, never by extracted page images.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

SYNTHETIC_CHAPTER_NUM = -1
SECTION_TYPE_BODY = "body"
BOX_DEFINITION = "definition"
BOX_MISCONCEPTION = "misconception"
BOX_EXAMPLE = "example"
BOX_KEY_POINTS = "key_points"
FIGURE_RENDERED = "rendered"

# Inline markdown the article body may carry (bold, italics, code, headings).
# The narration is read aloud and the documents typeset their own emphasis,
# so the asterisks would only ever be spoken or printed literally. Single
# underscores are left alone: they are far more often part of a word
# (H_2O, snake_case) than italics in science prose.
_MD_EMPHASIS = re.compile(r"(\*\*|__|\*|`+)")
_MD_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*", re.M)
_MD_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+", re.M)
_SPACES = re.compile(r"[ \t]{2,}")
_BLANK_LINES = re.compile(r"\n{3,}")


def plain_text(md: object) -> str:
    """Markdown body → plain prose. Pure. Bullets become plain lines, headings
    lose their hashes, emphasis loses its markers; paragraph breaks stay."""
    text = str(md or "")
    text = _MD_HEADING.sub("", text)
    text = _MD_BULLET.sub("", text)
    text = _MD_EMPHASIS.sub("", text)
    text = "\n".join(_SPACES.sub(" ", ln).rstrip() for ln in text.splitlines())
    return _BLANK_LINES.sub("\n\n", text).strip()


def _s(value: object) -> str:
    return " ".join(str(value or "").split())


def _items(row: dict, key: str) -> list[dict]:
    raw = row.get(key)
    return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []


def rendered_captions(figure_rows: Optional[Iterable[dict]]) -> dict[str, str]:
    """``figure_key → caption`` for the figures that actually RENDERED. A draft
    figure (no asset) is not mentioned: promising the model a diagram that
    does not exist would seed a visual opportunity nothing can draw."""
    out: dict[str, str] = {}
    for f in figure_rows or []:
        if not isinstance(f, dict) or str(f.get("status") or "") != FIGURE_RENDERED:
            continue
        key, cap = _s(f.get("figure_key")), _s(f.get("caption"))
        if key and cap and key not in out:
            out[key] = cap
    return out


def section_dicts(article_row: dict, figure_rows: Optional[Iterable[dict]] = None) -> list[dict]:
    """The chapter's sections, in article order. A section with no heading is
    titled by its position so the part map and the slides always have a
    label; a section with no body is kept (its heading is still content) but
    contributes no text."""
    captions = rendered_captions(figure_rows)
    out: list[dict] = []
    for i, sec in enumerate(_items(article_row, "sections"), start=1):
        heading = _s(sec.get("heading")) or f"Section {i}"
        body = plain_text(sec.get("body_md"))
        keys = sec.get("figure_keys") if isinstance(sec.get("figure_keys"), list) else []
        figs = [f"Figure: {captions[_s(k)]}" for k in keys if _s(k) in captions]
        content = "\n\n".join(p for p in [body, *figs] if p)
        out.append({
            "section_title": heading,
            "section_type": SECTION_TYPE_BODY,
            "content": content,
            "page_num": 0,
            "subsections": [],
        })
    return out


def key_boxes(article_row: dict) -> list[dict]:
    """Glossary, misconceptions, worked examples and per-section claims as
    ``KeyBox`` dicts, in that order (definitions first: the analyzer's
    concept pass reads boxes after the sections)."""
    boxes: list[dict] = []
    for g in _items(article_row, "glossary"):
        term, definition = _s(g.get("term")), plain_text(g.get("definition"))
        if term and definition:
            boxes.append({"type": BOX_DEFINITION, "title": term, "content": definition, "page_num": 0})
    for m in _items(article_row, "misconceptions"):
        wrong, right = plain_text(m.get("misconception")), plain_text(m.get("correction"))
        if wrong and right:
            boxes.append({"type": BOX_MISCONCEPTION, "title": wrong, "content": right, "page_num": 0})
    for w in _items(article_row, "worked_examples"):
        problem, solution = plain_text(w.get("problem")), plain_text(w.get("solution_md"))
        if problem and solution:
            boxes.append({"type": BOX_EXAMPLE, "title": problem, "content": solution, "page_num": 0})
    # Claims grouped per section, in section order; claims whose section id
    # matches nothing are grouped under the article title so none is lost.
    headings = {_s(s.get("id")): (_s(s.get("heading")) or f"Section {i}")
                for i, s in enumerate(_items(article_row, "sections"), start=1)}
    grouped: dict[str, list[str]] = {}
    for c in _items(article_row, "claims"):
        text = plain_text(c.get("text"))
        if not text:
            continue
        title = headings.get(_s(c.get("section_id"))) or _s(article_row.get("title")) or "Key points"
        grouped.setdefault(title, []).append(text)
    for title, claims in grouped.items():
        boxes.append({"type": BOX_KEY_POINTS, "title": title, "content": "\n".join(claims), "page_num": 0})
    return boxes


def section_ids_by_heading(article_row: dict) -> dict[str, str]:
    """``casefolded heading → section id`` — how a video chapter (grouped by
    slide heading) is tied back to the article section it teaches."""
    out: dict[str, str] = {}
    for sec in _items(article_row, "sections"):
        heading, sid = _s(sec.get("heading")), _s(sec.get("id"))
        if heading and sid:
            out.setdefault(heading.casefold(), sid)
    return out


def article_to_chapter(article_row: dict, figure_rows: Optional[Iterable[dict]] = None) -> dict:
    """The article as a ``ChapterContent``-shaped dict (see the module doc).
    Pure; validated against the pydantic model in tests/test_catalogue_loader.py."""
    return {
        "chapter_num": SYNTHETIC_CHAPTER_NUM,
        "title": _s(article_row.get("title")) or "Untitled",
        "start_page": 0,
        "end_page": 0,
        "sections": section_dicts(article_row, figure_rows),
        "images": [],
        "key_boxes": key_boxes(article_row),
    }


__all__ = ["SYNTHETIC_CHAPTER_NUM", "article_to_chapter", "key_boxes", "plain_text",
           "rendered_captions", "section_dicts", "section_ids_by_heading"]
