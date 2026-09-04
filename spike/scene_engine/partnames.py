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
The fuzzy tier is guarded (same initial letter, single best, 0.80 ratio)
because a confident arrow to the wrong structure teaches worse than no arrow.
"""

from __future__ import annotations

import difflib
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

    # 4. nearest by name. Guarded hard: a wrong leader line is worse than
    #    none, so the candidate must start with the same letter and clear a
    #    high similarity bar, and only a SINGLE best survives.
    best, best_score = None, 0.0
    wt = _tokens(want)
    for an, a in by_norm.items():
        if not an or an[0] != w[0]:
            continue
        ratio = difflib.SequenceMatcher(None, w, an).ratio()
        at = _tokens(a)
        jacc = len(wt & at) / len(wt | at) if (wt | at) else 0.0
        score = max(ratio if ratio >= 0.80 else 0.0,
                    jacc if jacc >= 0.5 else 0.0)
        if score > best_score:
            best, best_score = a, score
    if best is not None:
        return best, "nearest"
    return None, None


def same_part(a: str, b: str) -> bool:
    """Do these two names mean the same part? (exact/plural tiers only.)"""
    key, how = resolve_part(a, [b])
    return key is not None and how in ("exact", "plural")
