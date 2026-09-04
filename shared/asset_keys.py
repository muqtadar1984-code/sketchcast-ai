"""The ONE canonical identity of a visual asset.

There were two. ``spike/scene_engine/raster_assets.canonical_key`` folded away
"cell", "figure" and friends; ``shared/visual_library.canonical_key`` did not.
They therefore disagreed about every *_cell key in the biology curriculum, and
the disagreement was silent and expensive:

    visual_library.hydrate() downloaded a library hit and filed it under
    cache/cell_ciliated/, while the renderer only ever reads
    cache/ciliated/. Every cell-key library hit landed where nobody looks and
    the picture was generated again — red_blood_cell was HIT at 17:52:10,
    GENERATED at 17:52:22 and rejected as a duplicate on publish, adding paid
    calls to the very 429 burst that then cost the lesson two blank boards.

One function, imported by both, so a fold can never drift again. Stored
``canonical_key`` values on existing rows keep working: they are read only for
the avatar exact match (avatar keys fold identically under both old
functions) and for search, while publish idempotency is by content hash.
"""

from __future__ import annotations

import re

# Words that decorate a subject without changing which picture it is. The key
# is free text a MODEL invented, so one chapter produced ciliated_epithelium,
# ciliated_epithelium_cells and ciliated_epithelium_diagram as three
# separately-paid generations of one image.
#
# Deliberately conservative: "outline" and "view" are NOT here, because
# plant_cell_outline and plant_cell_diagram are different pictures and merging
# them would serve the wrong art — which is worse than paying twice.
#
# Split in two because "noise" turns out to mean two different things. A word
# that names the MEDIUM ("diagram", "figure") could decorate any picture ever
# drawn, so two keys sharing one says nothing at all. A word like "cell" is
# noise for CACHE IDENTITY -- ciliated_cell and ciliated are one picture -- yet
# it still narrows the subject: a cell diagram is not a volcano diagram. Only
# the first kind is barred from carrying a match (see `distinguishes`).
GENERIC_NOISE = frozenset({
    "diagram", "diagrams", "illustration", "illustrations",
    "image", "images", "picture", "pictures", "figure", "figures", "drawing",
    "drawings", "asset", "assets", "visual", "visuals", "graphic", "graphics",
    "sketch", "art", "of", "the", "a", "an", "and",
})
SUBJECT_NOISE = frozenset({"cell", "cells"})
KEY_NOISE = GENERIC_NOISE | SUBJECT_NOISE

_SPLIT = re.compile(r"[^a-z0-9]+")


def tokens(value: str) -> list[str]:
    return [t for t in _SPLIT.split(str(value).lower()) if t]


def distinguishes(token: str) -> bool:
    """Whether ONE token says anything about which picture this is.

    Not a medium word, and not a bare numeral. The numeral clause is the
    reason `figure_3` and `diagram_3` are two keys and not one: both reduce to
    the single core token "3", so before this they shared a canonical key and
    therefore a cache directory -- and in the SHARED cross-book library that
    means one book's figure 3 is served for another book's. A number is an
    index into a document nobody else can see; it names nothing on its own.
    """
    t = str(token)
    return bool(t) and t not in GENERIC_NOISE and not t.isdigit()


def _names_a_subject(token: str) -> bool:
    """`distinguishes`, but for CACHE identity, where "cell" is folded away."""
    t = str(token)
    return bool(t) and t not in KEY_NOISE and not t.isdigit()


def core_tokens(value: str) -> set[str]:
    """The tokens that say WHICH picture this is.

    Falls back to every token when a key names no subject at all
    ("cell_diagram", "figure_3"), because a key with no distinguishing token
    still has to be comparable to another one -- and, for the numeral case,
    because the number alone must not become the whole identity.

    The fallback tests for a SUBJECT, while the set it returns keeps numerals:
    `stage_3` and `stage_2` are two pictures and must stay two cache entries,
    but `figure_3` has nothing but the number and so keeps "figure" too.
    """
    toks = tokens(value)
    if not any(_names_a_subject(t) for t in toks):
        return set(toks)
    return {t for t in toks if t not in KEY_NOISE}


def all_noise(value: str) -> bool:
    """True when a key carries no token saying WHICH picture it is.

    `core_tokens` keeps the noise words for such a key so it stays comparable
    at all, which means callers cannot tell "cell_diagram" (nothing to go on)
    from "ciliated_cell" (a real subject) by looking at the result. They have
    to ask. The visual library's key guard does: a request with no
    distinguishing token has nothing to assert about a candidate, and refusing
    every row on that basis turned matches the library serves correctly today
    into paid regenerations.
    """
    toks = tokens(value)
    return bool(toks) and not any(_names_a_subject(t) for t in toks)


def canonical_key(value: str) -> str:
    """The cache identity of an asset, independent of how it was named.

    Measured on a real cache: folds 71 directories into 62, saving 9 paid
    image generations from one chapter, with no two distinct pictures
    colliding.
    """
    return "_".join(sorted(core_tokens(value))) or "asset"
