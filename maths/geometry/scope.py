"""Is this chapter one the geometry engine should draw for?

The figure call (maths.geometry.items.geometry_items) is told in its prompt
that "a chapter on fractions or equations has no diagram" and should answer
with an empty list. The model does not honour that: Variables, Constants and
Algebraic Expressions (2026-10-12, kits bac9327b / f188658e) opened its video
on "Evaluate the perimeter of the shape shown below" and "Identify which of
the following shapes are polygons" — verified geometry, drawn beautifully,
and nothing the article teaches. A prompt rule the model may ignore is not a
gate; this is the gate, in code, decided BEFORE the call is made.

The judgment is made on what names the chapter — the topic title, the
chapter title, its section headings and the analysis's concept NAMES — and
never on the chapter's prose or the concept definitions: an algebra chapter
legitimately says "the perimeter of a rectangle is 2l + 2w" in a word
problem without being a chapter about rectangles. A chapter none of whose
names mention a shape, an angle, a coordinate or a measure of one gets no
figure call at all, and its ladder (verified by algebra) fills the lesson.
"""

from __future__ import annotations

import re
from typing import Iterable

# Word STEMS, matched at a word start, case-insensitively, over the texts
# the chapter is named by. Deliberately absent: "square" and "cube" (square
# numbers, completing the square, cube roots), "translat" (Translating Words
# into Algebra), "construct" (constructing formulae), "line" and "graph" on
# their own (number lines, bar graphs), "similar", "net", "plane".
_STEMS = (
    "angle", "triangle", "polygon", "quadrilateral", "parallelogram", "rhomb", "trapez", "kite",
    "rectang", "pentagon", "hexagon", "heptagon", "octagon", "decagon", "isosceles", "equilateral",
    "scalene", "circle", "circumference", "chord", "tangent", "sector", "arc\\b", "perimeter", "area",
    "volume", "surface", "prism", "cuboid", "cylinder", "cone", "sphere", "pyramid", "polyhedr", "solid",
    "shape", "geometr", "symmetr", "congruen", "transformation", "reflect", "rotat", "enlarge",
    "tessellat", "pythagor", "hypotenuse", "trigonometr", "sine", "cosine", "bearing", "compass",
    "bisect", "locus", "loci", "coordinate", "cartesian", "axes", "plot", "gradient", "midpoint",
    "intercept", "parallel", "perpendicular", "vertex", "vertices", "diagonal", "protractor",
    "straight[ -]line", "linear graph", "distance[ -]time", "2d", "3d",
)
_GEOMETRY_RE = re.compile(r"(?<![a-z])(?:" + "|".join(_STEMS) + ")", re.IGNORECASE)


def geometry_words(texts: Iterable[str | None]) -> list[str]:
    """The geometry words found in ``texts`` (deduplicated, lower-cased, in
    order of first appearance) — empty when the chapter names none."""
    seen: list[str] = []
    for text in texts:
        for m in _GEOMETRY_RE.finditer(str(text or "")):
            word = m.group(0).lower()
            if word not in seen:
                seen.append(word)
    return seen


def figures_wanted(texts: Iterable[str | None]) -> bool:
    """True when the chapter's names mention geometry — the one condition
    under which the geometry engine is asked for figure questions."""
    return bool(geometry_words(texts))


def chapter_names(*, topic: str | None, analysis: dict | None, episode: dict | None = None,
                  chapter: dict | None = None) -> list[str]:
    """What a chapter is named by: the topic (the lesson's title), the
    analysis's chapter title, the episode's sections (or the chapter's
    section titles for a document) and the concept names the coverage gate
    would measure against — scoped to the episode when one is given."""
    names: list[str] = [str(topic or "")]
    if isinstance(analysis, dict):
        names.append(str(analysis.get("chapter_title") or ""))
        try:
            from shared.coverage import chapter_topics  # local: shared.coverage imports nothing of maths
            names.extend(str(t.get("name") or "") for t in chapter_topics(analysis, episode))
        except Exception:  # noqa: BLE001 — a malformed analysis names nothing more
            pass
    if isinstance(episode, dict):
        names.append(str(episode.get("title") or ""))
        names.extend(str(s) for s in (episode.get("sections_covered") or []))
    if isinstance(chapter, dict):
        names.append(str(chapter.get("title") or ""))
        for s in chapter.get("sections") or []:
            if isinstance(s, dict):
                names.append(str(s.get("section_title") or s.get("heading") or s.get("title") or ""))
    return [n for n in names if n.strip()]


__all__ = ["geometry_words", "figures_wanted", "chapter_names"]
