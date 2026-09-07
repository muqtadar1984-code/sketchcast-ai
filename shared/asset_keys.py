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
# The ONE spelling a retained subject-noise token is written in. `core_tokens`
# keeps the word when dropping it would leave a single token carrying the whole
# identity, and a kept word has to fold its own plural or `plant_cell` and
# `plant_cells` become two cache directories for one picture.
SUBJECT_CANONICAL = "cell"
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

    "cell" is dropped only while something ELSE still names the subject. The
    rule is the one `cells_to_tissue` already established for the connective
    "to" (TestTheConnectiveStillSeparatesTwoCacheEntries): a token may not be
    folded away when folding it files a compound and a plain word in ONE cache
    directory, because those are two different pictures. Measured on the live
    library 2026-09-07 -- of 49 cell-keyed assets, the 10 that had folded held
    an ANIMAL CELL cutaway under the bare key `factory` (from `cell_factory`),
    a plant CELL under `plant`, and an animal CELL under `animal`. A topic
    asking for a real factory, a real plant or a real animal would have been
    served a cell diagram out of `_asset_dir`, which is `canonical_key` alone:
    no score, no threshold, no guard. The same collapse also walks straight
    through `_guard_refusal`, whose first clause passes any row whose
    canonical key equals the query's -- "same cache identity, same picture, by
    definition" -- so the fold made that definition false.

    The survivors decide, not a dictionary. Where "cell" leaves TWO or more
    tokens the compound still names itself (`red_blood_cell` -> blood_red,
    `cell_membrane_selectivity` -> membrane_selectivity) and the fold is kept:
    that is the measured saving, one chapter's ciliated_epithelium,
    ciliated_epithelium_cells and ciliated_epithelium_diagram paid for three
    times. Where it leaves ONE, that token is carrying the whole identity
    alone and must not be silently equated with a picture OF it --
    `palisade_cell` is a leaf cell and a palisade is a fence. The dedup those
    single-token keys used to get is recovered at LOOKUP by `lookup_variants`,
    where a miss costs a retry instead of the wrong picture.
    """
    toks = tokens(value)
    if not any(_names_a_subject(t) for t in toks):
        return set(toks)
    kept = {t for t in toks if t not in KEY_NOISE}
    if len(kept) < 2 and any(t in SUBJECT_NOISE for t in toks):
        # Retained in ONE spelling. Keeping the word as written would split
        # `Ciliated Cells Diagram` (cells_ciliated) from `ciliated_cell`
        # (cell_ciliated) -- two cache directories for one picture, which is
        # the paid regeneration this whole fold exists to prevent, arriving
        # through the exemption. Caught by
        # TestDeferralReplacesTheLadder::test_deferral_is_by_picture_not_by_
        # spelling, which defers a plural and asks with a singular.
        return {SUBJECT_CANONICAL if t in SUBJECT_NOISE else t
                for t in toks if t not in GENERIC_NOISE}
    return kept


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


# ── one picture, spelled two ways, is not two assets ─────────────────────────
# `canonical_key` above may NEVER learn this fold: its output is pinned
# byte-for-byte against the app (catalogue_key_cases.json, sha in both repos)
# and re-keying would orphan the 684 assets already published under it. So the
# alias happens at LOOKUP instead — a key that misses is asked again in the
# other orthography, and only the stored key stays as it was.
#
# Measured on the live Cells kit (2026-09-07): part 1 asked
# `levels_of_organization` and part 2 asked `levels_of_organisation`. Two
# canonical keys, two cache directories, one picture — part 1 hit its cache,
# part 2 found nothing, was rate-limited, and shipped a scene with no diagram.
#
# `respell` above cannot serve here. It is one-directional and deliberately
# broad (40+ rules), and a lookup key becomes a PATH: a word it rewrites
# wrongly files an asset where nobody reads it, which is the exact failure
# canonical_key was consolidated to end. This list is short and explicit.

# Whole-token pairs, used wherever a morpheme rule would over-reach: "hem" ->
# "haem" anywhere turns hemisphere into haemisphere, and "e" -> "oe" at the
# start turns electron into oelectron.
_SPELLING_WORDS: tuple[tuple[str, str], ...] = (
    ("haemoglobin", "hemoglobin"),
    ("oesophagus", "esophagus"),
)

# (British, American, where). "end" is anchored at the end of a TOKEN, so
# `levels_of_organisation` folds on `organisation` alone; the plural forms come
# first because the first matching rule is the only one applied.
_SPELLING_MORPHEMES: tuple[tuple[str, str, str], ...] = (
    ("isations", "izations", "end"),
    ("isation", "ization", "end"),
    ("yses", "yzes", "end"),
    ("yse", "yze", "end"),
    ("colour", "color", "any"),
    ("centre", "center", "any"),
    ("fibre", "fiber", "any"),
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _swap_token(token: str, to_american: bool) -> str:
    """One token in the other orthography, or the token unchanged."""
    t = token.lower()
    for br, us in _SPELLING_WORDS:
        src, dst = (br, us) if to_american else (us, br)
        if t == src:
            return dst
    for br, us, where in _SPELLING_MORPHEMES:
        src, dst = (br, us) if to_american else (us, br)
        if where == "any":
            if src in t:
                return t.replace(src, dst)
        elif t.endswith(src) and len(t) > len(src):
            return t[: len(t) - len(src)] + dst
    return token


def spelling_variants(key: str) -> list[str]:
    """``key`` first, then the same key written in the other orthography.

    The key itself is always element 0, so a caller retries with
    ``spelling_variants(k)[1:]`` and a key this list does not cover comes back
    as ``[key]`` — unchanged, and with nothing to retry.
    """
    raw = str(key or "")
    out = [raw]
    for to_american in (True, False):
        alt = _TOKEN_RE.sub(
            lambda m, us=to_american: _swap_token(m.group(0), us), raw)
        if alt not in out:
            out.append(alt)
    return out


def lookup_variants(key: str) -> list[str]:
    """``key`` first, then every other name the SAME picture may be filed under.

    Two folds are recovered here rather than in `canonical_key`, for the same
    reason and by the same route: a fold inside the canonical key merges two
    cache directories permanently and cannot tell a compound from the plain
    word it contains, while a fold here costs one extra lookup on a MISS and
    can never serve the wrong picture, because whatever it finds was stored
    under a key that really does exist.

      * the other orthography (`spelling_variants`) -- `organisation` for a
        stored `organization`;
      * the cell-stripped form, for the single-token keys `core_tokens` stopped
        folding on 2026-09-07. `ciliated_cell` is filed as `cell_ciliated`
        now, so a library that still holds `ciliated` would be missed; asking
        again without the word finds it.

    ONE DIRECTION ONLY, and the asymmetry is the entire point. Stripping is
    safe because the request said "cell" and the stored key did not: a cell
    picture is a fair answer to a request for a cell. ADDING it would ask
    `factory` for `cell_factory` and serve the animal-cell cutaway to the
    Industrial Revolution -- the bug this change exists to close, walking back
    in through the retry. So a key with no cell token gets no cell variant,
    ever.

    The key itself is always element 0, so a caller retries with
    ``lookup_variants(k)[1:]`` and a key with nothing to vary comes back as
    ``[key]``. Order is deliberate: exact first, spelling before the cell fold,
    because a spelling variant is the same word and the cell fold is a weaker
    claim about the same subject.
    """
    raw = str(key or "")
    out: list[str] = []
    # Deduped by CANONICAL key, not by spelling. Both callers turn a variant
    # into `canonical_key(alt)` — a cache directory in the renderer, a fresh
    # whole-library scan in `find` — so two variants that canonicalise to one
    # key are one question asked twice. `cells_to_tissue` strips to
    # `to_tissue` and `specialised_animal_cells_table` to
    # `specialised_animal_table`, and both fold straight back onto their own
    # primary: without this each would cost a redundant scan of the library on
    # every miss.
    # `raw` is element 0 unconditionally, even when it is empty: the contract
    # above is that a caller can always slice [1:] off its own key.
    out.append(raw)
    seen: set[str] = {canonical_key(raw)}

    def _add(candidate: str) -> None:
        ck = canonical_key(candidate)
        if candidate and ck not in seen:
            seen.add(ck)
            out.append(candidate)

    for spelled in spelling_variants(raw)[1:]:
        _add(spelled)
    for base in list(out):
        toks = tokens(base)
        if not any(t in SUBJECT_NOISE for t in toks):
            continue
        stripped = [t for t in toks if t not in SUBJECT_NOISE]
        # Only when something is LEFT to ask for: a bare "cells" stripped of
        # "cells" is not a weaker question, it is no question at all.
        if not stripped or not any(_names_a_subject(t) for t in stripped):
            continue
        _add("_".join(stripped))
    return out


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
