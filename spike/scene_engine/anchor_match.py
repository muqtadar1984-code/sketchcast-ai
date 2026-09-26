"""The anchor's own layer matcher — looser than the shared one, on purpose.

ANCHORS ONLY. `vector_assets.match_layer_ids` is the shared layer matcher:
asset subsetting, carried-state reveal and draw distribution all call it, and
"wall" meaning the same strokes everywhere is a property worth keeping. What
lives here is the anchor's last resort, so a looser reading can put an arrow
on a part without also changing which strokes get drawn. A previous session
put this narrowing in the shared matcher and a review test caught it; that
test (`tests/test_anchor_qualifiers.py`) still stands guard.

The rungs are tried in order and the first that yields exactly one region
wins. Every one of them refuses ambiguity rather than guessing: a name that
picks out two regions has identified a family, not a part, and the label
keeps its leader line. There is no similarity tier here for the same reason
`partnames` has none — a confident arrow on the wrong structure teaches a
child something false, which is worse than an unlabelled one.
"""

from __future__ import annotations

import re

__all__ = ["without_unknown_qualifiers", "by_token_subset", "by_process_endpoints",
           "anchor_layer_hits", "process_endpoints"]


def _tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", str(s).lower()) if t]


def without_unknown_qualifiers(available: list[str], layer: str) -> list[str]:
    """An anchor's layer name, retried without words the ARTWORK never uses.

    The plan and the picture are written by two different calls, and the plan
    qualifies a part the annotator named plainly. From the live Cells kit
    (2026-09-08), where 14 anchors resolved to nothing and their labels fell
    back to a stacked column of leader lines:

        plant_central_vacuole  vs  "large central vacuole"   ->  no match
        central_vacuole        vs  "large central vacuole"   ->  MATCHES
        golgi_sacs             vs  "golgi apparatus"         ->  no match
        golgi                  vs  "golgi apparatus"         ->  MATCHES

    One extra word loses a match the rest of the name makes perfectly. So drop
    it — but ONLY a word the artwork's whole vocabulary does not contain,
    which is what makes this safe rather than a similarity score. A word the
    picture DOES use somewhere is meaningful and is never discarded:
    `nucleus_membrane` keeps both its words, because the artwork knows
    "nucleus" and knows "cell membrane", and narrowing to "membrane" would put
    a nuclear-membrane label on the cell membrane.

    Ambiguity refuses too: a narrowed name matching more than one region has
    identified a family, not a part. `vacuole` against "vacuole column" and
    "large vacuole area" stays unresolved and the label keeps its leader line.

    Both halves of the comparison are inflection-tolerant, and that is not a
    nicety. From the live Cells kit (2026-09-09), the annotator wrote
    "mitochondrion" and the plan asked for "mitochondria_region":

      * the vocabulary test — a raw `t in vocabulary` decided the artwork does
        not know the word "mitochondria" at all, so nothing was droppable and
        the whole function bailed one line later;
      * the final lookup — `match_layer_ids` is exact-then-substring, and
        neither spelling contains the other.

    Every other name comparison in the engine already folds inflections
    (`annotate_regions` files through `norm_part`, `match_layer_ids` falls
    back to `resolve_part`, `_unasked_names` uses `same_part`). This was the
    one that did not, and it cost two anchors on a picture whose boxes were
    sitting in `regions` the whole time.
    """
    from .partnames import same_part
    from .vector_assets import match_layer_ids

    toks = _tokens(layer)
    vocabulary = {t for a in available for t in _tokens(a)}
    keep = [t for t in toks if any(same_part(t, v) for v in vocabulary)]
    if not keep or len(keep) == len(toks):
        return []                     # nothing droppable, or nothing left
    narrowed = " ".join(keep)
    # the shared matcher first, so anything that resolves today resolves the
    # same way; its inflection blind spot is covered only when it finds none
    hits = match_layer_ids(available, [narrowed]) or \
        [a for a in available if same_part(narrowed, a)]
    return hits if len(hits) == 1 else []


def by_token_subset(available: list[str], layer: str) -> list[str]:
    """A region whose words are ALL words of the anchor, in any order.

    Every other tier compares whole strings, so word order and function words
    defeat all of them: `wall_of_the_cell` finds neither "cell wall" by
    equality, nor by inflection, nor by containment. Here a candidate survives
    only if the anchor's tokens cover EVERY token of it — one direction only,
    candidate ⊆ anchor — which is why this cannot reopen the similarity tiers
    `partnames` rejected. `nucleolus`/`nucleus`, `neutron`/`neuron` and
    `meiosis`/`mitosis` are not token subsets of one another, and mere shared
    tokens are not enough because every token of the candidate must be
    covered.

    Of the survivors only the most specific group is considered, and only if
    it holds exactly one name. This is the LAST rung and must stay last: run
    ahead of the others it would answer names the qualifier rung answers
    better.
    """
    from .partnames import same_part

    toks = _tokens(layer)
    if not toks:
        return []
    covered = []
    for a in available:
        at = _tokens(a)
        if at and all(any(same_part(t, w) for w in toks) for t in at):
            covered.append((len(at), a))
    if not covered:
        return []
    best = max(n for n, _ in covered)
    hits = [a for n, a in covered if n == best]
    return hits if len(hits) == 1 else []


# A process label names what HAPPENS between two parts, and the picture can
# only ever show the arrow between them. States of Matter (2026-09-25): the
# director named solid, liquid and gas, the annotator boxed the three
# arrow pairs between them, and the labels "Evaporative Cooling" and
# "Sublimation" — synthesized from the narration — still fell to the edge of
# the picture, because no region is called "evaporative cooling". The word
# stem maps to its two ends; a region naming both ends is the arrow. Stems,
# not words, so "evaporative", "evaporation" and "evaporates" all read alike.
_PROCESS_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    # (stem, from, to) — longer stems first so "desublim" wins over "sublim"
    ("desublim", "gas", "solid"),
    ("deposit", "gas", "solid"),
    ("solidif", "liquid", "solid"),
    ("freez", "liquid", "solid"),
    ("fusion", "solid", "liquid"),
    ("melt", "solid", "liquid"),
    ("evaporat", "liquid", "gas"),
    ("vaporis", "liquid", "gas"),
    ("vaporiz", "liquid", "gas"),
    ("boil", "liquid", "gas"),
    ("condens", "gas", "liquid"),
    ("sublim", "solid", "gas"),
)
_ENDPOINT_WORDS: dict[str, frozenset[str]] = {
    "solid": frozenset({"solid", "solids", "ice"}),
    "liquid": frozenset({"liquid", "liquids", "water"}),
    "gas": frozenset({"gas", "gases", "gaseous", "vapour", "vapor", "steam"}),
}
_ARROW_WORDS = frozenset({"arrow", "arrows", "path", "paths", "transition",
                          "transitions", "change", "changes", "link", "edge"})


def process_endpoints(layer: str) -> tuple[str, str] | None:
    """(from, to) for a label that names a phase change, else None."""
    for t in _tokens(layer):
        for stem, a, b in _PROCESS_ENDPOINTS:
            if t.startswith(stem):
                return (a, b)
    return None


def by_process_endpoints(available: list[str], layer: str) -> list[str]:
    """A process anchor resolved to the ARROW between its two ends.

    Fires only for a name carrying a phase-change stem, and only onto a
    region whose own words name BOTH ends — "liquid gas arrows" for
    "evaporative cooling", "solid gas arrows" for "sublimation". Of several
    such regions the one whose word order runs from → to wins (the repair
    pass writes "solid to gas arrow" beside "gas to solid arrow"), then the
    one that calls itself an arrow or path. Two equally good candidates are
    a family, not a part, and are refused like every other rung.
    """
    ends = process_endpoints(layer)
    if not ends:
        return []
    src, dst = ends
    scored: list[tuple[int, str]] = []
    for a in available:
        at = _tokens(a)
        si = next((i for i, t in enumerate(at) if t in _ENDPOINT_WORDS[src]), None)
        di = next((i for i, t in enumerate(at) if t in _ENDPOINT_WORDS[dst]), None)
        if si is None or di is None:
            continue
        # an explicit "solid TO gas" outranks the bare pair "solid gas", which
        # outranks the reversed pair; calling itself an arrow breaks ties
        explicit = si < di and any(t in ("to", "into") for t in at[si + 1:di])
        score = ((4 if explicit else 0) + (2 if si < di else 0)
                 + (1 if any(t in _ARROW_WORDS for t in at) else 0))
        scored.append((score, a))
    if not scored:
        return []
    best = max(sc for sc, _ in scored)
    hits = [a for sc, a in scored if sc == best]
    return hits if len(hits) == 1 else []


def anchor_layer_hits(available: list[str], layer: str) -> list[str]:
    """THE anchor ladder: the shared matcher, then the anchor-only rungs.

    One definition, so the raster and vector branches of
    `_layer_instance_boxes` can never drift apart — they did, and an SVG asset
    (where `<g id>` groups ARE the parts) shipped without the qualifier
    tolerance at all.
    """
    from .vector_assets import match_layer_ids
    return (match_layer_ids(available, [layer])
            or without_unknown_qualifiers(available, layer)
            or by_token_subset(available, layer)
            # after the name rungs: a process word is only read as "the arrow
            # between its ends" once no region carries the name itself
            or by_process_endpoints(available, layer))


def resolves(available: list[str], layer: str) -> bool:
    """Would this anchor find a region? The single definition of "resolved",
    so a repair pass and the renderer agree on what needs repairing."""
    return bool(anchor_layer_hits(available, layer))
