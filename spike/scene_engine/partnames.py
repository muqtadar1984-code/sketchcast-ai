"""Part-name resolution: the ONE place that decides whether two names for a
structure are the same structure.

An arrow head names a part ("chloroplasts"); the vision annotator returns a
region keyed by whatever it wrote back ("chloroplast"). Until this module the
only matcher was exact-then-substring (vector_assets.match_layer_ids), which
is blind three ways — measured by running it:

    'mitochondria' vs 'mitochondrion' -> no match
    'nuclei'       vs 'nucleus'       -> no match
    'cell_wall'    vs 'cell wall'     -> no match

Every one of those leaves the label floating with no leader line, and — when
the asset IS annotated — suppresses the arrow entirely (render.py PASS 2). The
founder's killed Cells Part 2 attempt logged 'layer anchor
plant_cell_diagram.chloroplasts unresolved' five times against an image whose
own prompt had named 'chloroplasts'.

Deliberately a LAST-RESORT tier: exact and substring keep their current
meaning and run first, so nothing that matched before matches differently now.
The last tier matches on shared WORDS, never on spelling: a character-ratio
tier was measured binding nucleolus->nucleus, neutron->neuron and
meiosis->mitosis, and a confident arrow to the wrong structure teaches worse
than no arrow.
"""

from __future__ import annotations

import re

__all__ = ["norm_part", "resolve_part", "same_part"]


def norm_part(s: str) -> str:
    """Separator style is model whim, never semantics: 'Cell_Wall', 'cell
    wall' and 'CELL-WALL' are one name."""
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


# Latin plurals earn their own table: biology diagrams are full of them and a
# regex over -s alone gets every one of them wrong.
_ENDINGS = (
    ("ia", ("ion", "ium")),      # mitochondria -> mitochondrion / bacterium
    ("ae", ("a",)),              # trachaeae -> trachaea
    ("i", ("us",)),              # nuclei -> nucleus
    ("a", ("um", "on")),         # flagella -> flagellum
    ("ion", ("ia",)),
    ("ium", ("ia",)),
    ("us", ("i",)),
    ("um", ("a",)),
    ("es", ("", "is")),          # analyses -> analysis
    ("s", ("",)),
)


def _forms(name: str) -> set[str]:
    """A name and its singular/plural variants, normalized. Only the LAST word
    inflects — 'cell walls' is one wall's plural, not a plural 'cell'."""
    n = norm_part(name)
    if not n:
        return set()
    out = {n}
    words = n.split()
    head, last = " ".join(words[:-1]), words[-1]
    for suffix, repls in _ENDINGS:
        if not last.endswith(suffix) or len(last) - len(suffix) < 2:
            continue
        stem = last[:len(last) - len(suffix)]
        for r in repls:
            if stem + r:
                out.add((head + " " + stem + r).strip())
    for extra in (last + "s", last + "es"):
        out.add((head + " " + extra).strip())
    return out


def _tokens(name: str) -> set[str]:
    return {t for t in norm_part(name).split() if t}


def resolve_part(want: str, available: list[str]) -> tuple[str | None, str | None]:
    """The key in `available` that names the same part as `want`.

    Returns (key, how) with `how` in {exact, plural, substring, nearest}, or
    (None, None). Tiers are tried in order and never blend: the first tier
    that produces a candidate decides.
    """
    w = norm_part(want)
    if not w or not available:
        return None, None
    by_norm: dict[str, str] = {}
    for a in available:
        by_norm.setdefault(norm_part(a), a)

    # 1. exact, once separators and case stop mattering
    if w in by_norm:
        return by_norm[w], "exact"

    # 2. singular/plural, English and Latin
    wf = _forms(want)
    for an, a in by_norm.items():
        if an in wf or wf & _forms(a):
            return a, "plural"

    # 3. containment — unchanged from match_layer_ids, so 'vacuole' still
    #    finds 'sap vacuole' and 'membrane' still loses to a literal
    #    'membrane' key above before it can bleed into 'nucleus membrane'
    contained = [a for an, a in by_norm.items() if an in w or w in an]
    if contained:
        return min(contained, key=lambda a: (len(norm_part(a)), a)), "substring"

    # 4. WORD ORDER only: 'wall cell' is 'cell wall'. Evidence is shared
    #    TOKENS, never spelling — a character-similarity tier was measured
    #    binding real, distinct structures to each other at ratios it called
    #    confident: nucleolus->nucleus 0.875, neutron->neuron 0.923,
    #    meiosis->mitosis 0.857, chromoplast->chloroplast 0.818,
    #    proton->photon, stalactite->stalagmite, radius->radium,
    #    carpal->carpel. Every one of those draws a barbed arrow into the
    #    wrong region with no warning, and this module exists on the
    #    principle that a confident arrow to the wrong structure teaches
    #    worse than no arrow. So spelling is out; only a multi-word name
    #    whose words are the SAME words qualifies.
    wt = _tokens(want)
    if len(wt) > 1:
        best, best_score = None, 0.0
        for an, a in by_norm.items():
            at = _tokens(a)
            if not at:
                continue
            jacc = len(wt & at) / len(wt | at)
            if jacc >= 0.5 and jacc > best_score:
                best, best_score = a, jacc
        if best is not None:
            return best, "nearest"
    return None, None


def same_part(a: str, b: str) -> bool:
    """Do these two names mean the same part? (exact/plural tiers only.)"""
    key, how = resolve_part(a, [b])
    return key is not None and how in ("exact", "plural")
