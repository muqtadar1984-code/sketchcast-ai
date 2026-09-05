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
from functools import lru_cache

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


# ── one word, written two ways, is not two claims ────────────────────────────
# Keys are free text a MODEL invented, over a product that ships Cambridge,
# CBSE and US curricula in ten locales, so the same picture arrives spelled
# more than one way: `leaves_cross_section` for a stored `leaf_cross_section`,
# `extraction_of_aluminum` for `extraction_of_aluminium`. The library itself is
# not consistent — it holds `addition_polymerisation_of_ethene`, `muscle_fibre`
# and `fertilisation_oviduct` beside `copper_sulfate`, `fetus_uterus` and
# `organization_hierarchy`.
#
# This lives here, beside `canonical_key`, and not in the renderer's
# `partnames` module, for two reasons. Layering: `shared` may not import
# `spike.scene_engine`, whose package __init__ installs the visual-library
# wrapper and reindexes the local asset cache — a side effect no pure
# predicate should carry. And scope: partnames answers "is this annotator
# region the part that arrow head names", and its docstring refuses any
# spelling tier on purpose, because a wrong guess there puts a confident label
# on the wrong structure. The question HERE is narrower and safer — two single
# tokens of a cache key, where the cost of missing an equivalence is a paid
# regeneration.
#
# The two live in different modules and answer different questions, which is
# exactly how this codebase ended up with two canonical_key functions that
# drifted. So `TestTheFoldIsSpellingsAndInflectionsAndNothingElse` in
# tests/test_visual_library_reuse.py pins that `same_word` folds everything
# `same_part` folds: they may not disagree about a plural.

# British/American, most productive rule first; word-specific rules run before
# the morpheme rules they would otherwise mangle (practise -> practice, not
# "practize"). Position: "any" anywhere in the token, "start"/"end" anchored.
_ORTHOGRAPHY: tuple[tuple[str, str, str], ...] = (
    ("practis", "practic", "any"),
    ("defence", "defense", "any"),
    ("offence", "offense", "any"),
    ("licence", "license", "any"),
    ("pretence", "pretense", "any"),
    ("aluminium", "aluminum", "any"),
    ("sulph", "sulf", "any"),
    ("foet", "fet", "start"),
    ("haem", "hem", "start"),
    ("anaem", "anem", "any"),
    ("paed", "ped", "start"),
    ("caes", "ces", "start"),
    ("oe", "e", "start"),          # oesophagus, oestrogen, oedema
    ("colour", "color", "any"),
    ("vapour", "vapor", "any"),
    ("behaviour", "behavior", "any"),
    ("neighbour", "neighbor", "any"),
    ("mould", "mold", "any"),
    ("smoulder", "smolder", "any"),
    ("plough", "plow", "any"),
    ("draught", "draft", "any"),
    ("grey", "gray", "any"),
    ("sceptic", "skeptic", "any"),
    ("ageing", "aging", "any"),
    ("judgement", "judgment", "any"),
    ("programme", "program", "end"),
    ("tyre", "tire", "end"),
    ("metre", "meter", "any"),     # voltmetre, millimetre, thermometre
    ("litre", "liter", "any"),
    ("fibre", "fiber", "any"),
    ("centre", "center", "any"),
    ("theatre", "theater", "any"),
    ("calibre", "caliber", "any"),
    ("spectre", "specter", "any"),
    ("logue", "log", "end"),       # catalogue, dialogue, analogue
    ("isation", "ization", "end"),
    ("isations", "izations", "end"),
    ("ised", "ized", "end"),
    ("ises", "izes", "end"),
    ("ising", "izing", "end"),
    ("iser", "izer", "end"),
    ("ise", "ize", "end"),
    ("ysed", "yzed", "end"),
    ("ysing", "yzing", "end"),
    ("yse", "yze", "end"),
    ("lled", "led", "end"),        # labelled, modelled
    ("lling", "ling", "end"),
    ("ller", "ler", "end"),
)

# Plurals no ending rule reaches. English -f/-fe -> -ves is here rather than in
# the ending table because the stem changes, and the rest are simply irregular.
_IRREGULAR_PLURALS = {
    "leaves": "leaf", "halves": "half", "shelves": "shelf",
    "wolves": "wolf", "calves": "calf", "hooves": "hoof", "loaves": "loaf",
    "thieves": "thief", "lives": "life", "knives": "knife",
    "wives": "wife", "wharves": "wharf", "scarves": "scarf",
    "teeth": "tooth", "feet": "foot", "geese": "goose", "mice": "mouse",
    "lice": "louse", "men": "man", "women": "woman", "children": "child",
    "people": "person", "oxen": "ox", "stomata": "stoma",
}

# Inflections, in the shape partnames uses: expand BOTH names to every form
# they could be written in and intersect, rather than picking a single
# canonical direction that would have to be right about which is the singular.
# The Latin rows mirror `spike/scene_engine/partnames._ENDINGS`; the -ies row
# is the regular English rule that table is missing.
_ENDINGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ies", ("y",)),             # arteries -> artery, bodies -> body
    ("y", ("ies",)),
    ("ia", ("ion", "ium")),      # mitochondria -> mitochondrion / bacterium
    ("ae", ("a",)),
    ("i", ("us",)),              # nuclei -> nucleus
    ("a", ("um", "on")),
    ("ion", ("ia",)),
    ("ium", ("ia",)),
    ("us", ("i",)),
    ("um", ("a",)),
    ("es", ("", "is")),          # analyses -> analysis
    ("s", ("",)),
)


@lru_cache(maxsize=4096)
def respell(token: str) -> str:
    """One token in a single orthography. Idempotent for already-US spelling.

    Cached: the guard asks this once per token PAIR per candidate row, so a
    single guarded scan of the library runs it thousands of times over a
    vocabulary of a few hundred words."""
    t = str(token or "").lower()
    for pat, repl, where in _ORTHOGRAPHY:
        if where == "any":
            t = t.replace(pat, repl)
        elif where == "start" and t.startswith(pat):
            t = repl + t[len(pat):]
        elif where == "end" and t.endswith(pat) and len(t) > len(pat):
            t = t[:len(t) - len(pat)] + repl
    return t


def word_forms(token: str) -> set[str]:
    """A token, respelled, with its singular/plural variants."""
    return set(_word_forms(token))


@lru_cache(maxsize=4096)
def _word_forms(token: str) -> frozenset[str]:
    t = respell(token)
    if not t:
        return frozenset()
    out = {t, _IRREGULAR_PLURALS.get(t, t)}
    for base in tuple(out):
        for suffix, repls in _ENDINGS:
            if not base.endswith(suffix) or len(base) - len(suffix) < 2:
                continue
            stem = base[:len(base) - len(suffix)]
            out.update(stem + r for r in repls if stem + r)
        out.update((base + "s", base + "es"))
    return frozenset(out)


@lru_cache(maxsize=8192)
def same_word(a: str, b: str) -> bool:
    """Whether two key tokens are the same word differently written.

    Deliberately NOT a similarity measure. Every fold is a spelling rule or an
    inflection, so the pairs a similarity score gets wrong survive it:
    meiosis/mitosis, nucleolus/nucleus, neutron/neuron, endothermic/exothermic
    are four different pictures and stay four different words.
    """
    ra, rb = respell(a), respell(b)
    if not ra or not rb:
        return False
    return ra == rb or bool(_word_forms(ra) & _word_forms(rb))


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


def is_avatar_key(key: str) -> bool:
    """Avatar identity from the asset key alone.

    Keys are the durable signal here: the roster is named avatar_* by the
    renderer (spike/scene_engine/whiteboard.py), and a key is available
    everywhere, including for rows written before asset_type was populated.

    Lives beside `canonical_key` because BOTH retrieval domains ask it and the
    answer must not differ: the visual library uses it to keep the persistent
    characters out of educational reuse, and the renderer uses it to keep an
    unresolvable avatar out of the placeholder tier -- a stand-in frame is a
    board that lost its diagram, whereas a missing teacher is simply a teacher
    who is not there.
    """
    return str(key or "").strip().lower().startswith("avatar")
