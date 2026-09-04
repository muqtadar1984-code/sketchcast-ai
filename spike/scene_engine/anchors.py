"""Anchor tolerance: a dangling anchor is CONVERTED, never a lost scene.

Incident (2026-09-04, founder's "Cells" Part 1): the director declared its
root visual as ``plant_cell_diagram`` and pointed ``arrow_plant`` at
``plant_cell_box`` — two ids for one thing. Nothing between the plan and the
schema validator looked at anchor refs, so ``Scene._integrity`` raised
"arrow 'arrow_plant' anchors to unknown element 'plant_cell_box'" for every
segment the arrow rode into (vc_s003..vc_s006), each fell to a slide, and the
lesson shipped 9/9 whiteboard cards with "NO scenes" in its acceptance report.
One arrow head cost four boards.

The compiler's principle is that it CONVERTS model anti-patterns and never
drops. So an anchor whose ``el`` names no element is resolved here, before
validation:

  1. deterministically — the element whose id matches after normalisation
     (case, separators, and naming suffixes such as ``_box`` / ``_diagram`` /
     ``_visual`` / ``_root`` / ``_label``), the illustration whose ASSET key
     matches, the text whose CONTENT matches, a merged per-part handle (root
     + layer), a part name used as if it were an element (root + layer, the
     same layer matcher the draw distribution uses), or the unique element of
     the kind the id implies;
  2. else, for the HEAD of an arrow only (arrows point AT pictures), a ref
     that does not read like text and either shares a word with the root
     illustration (its id, asset key or a part name) or is a generic "the
     diagram" reference, binds to the scene's single root illustration;
  3. else THAT arrow is dropped and the scene proceeds — with the actions
     that TARGET it, the morph actions whose ``into`` names it, the groups
     that listed it (a group left empty goes too), and the text chains that
     ran behind any of them, or the schema rejects the very board this guard
     exists to save.

A text ``after`` is held to the same rule the schema states: it must name an
element that is on the board AND earlier than the text. Anything else — a
name that is gone, a name further down the board — is re-chained to an
earlier text or cut loose (the text still carries its own ``at``).

An arrow whose two ends resolve to ONE element is dropped as well: it renders
as nothing and the label it came from loses its leader line. A HEAD tries the
picture's PARTS before the label that carries the same words, so an arrow from
"Nucleus" to the nucleus does not collapse onto its own tail.

A ref is never re-bound across a chapter boundary: an element carried from the
previous chapter (``prev__*``) whose anchor names something that never made
it onto the exported board is dropped, not re-anchored to whatever the NEW
chapter happens to call the same thing — the compiler seals exported boards
so this stays a last line of defence.

Every conversion is reported the way ANCHORED / SYNTHESIZED lines are, so
validate.py's arrow accounting stays truthful. The pydantic validator keeps
its check as the last line of defence — it is simply no longer reachable for
this class of plan.
"""

from __future__ import annotations

import re
from typing import Optional

# naming suffixes a director bolts onto an id that name the KIND of thing,
# not the thing: plant_cell_box == plant_cell_diagram == plant_cell
_KIND_SUFFIXES = frozenset({
    "box", "diagram", "visual", "root", "image", "img", "illustration",
    "picture", "pic", "fig", "figure", "drawing", "sketch", "asset", "art",
    "obj", "objs", "object", "el", "elem", "element", "shape", "item",
    "label", "labels", "lbl", "text", "txt", "name", "tag", "caption",
    "part", "graphic", "icon",
})
# a ref that READS like text: a naming prefix or suffix, or a word that only
# ever names writing (a title, a heading, an equation, a step number ...)
_TEXT_PREFIXES = ("lbl_", "label_", "lb_", "txt_", "text_", "caption_",
                  "title_", "heading_", "term_", "note_")
_TEXT_SUFFIXES = ("_label", "_labels", "_lbl", "_text", "_txt", "_title",
                  "_caption", "_heading", "_term", "_note")
_TEXT_WORDS = frozenset({
    "title", "heading", "header", "subtitle", "caption", "label", "labels",
    "lbl", "text", "txt", "term", "note", "word", "words", "eq", "equation",
    "formula", "step", "bullet", "definition", "question",
})
_TEXT_WORD_RE = re.compile(r"(eq|equation|formula|step|q|question|bullet)\d+")
# words that mean "the picture" and nothing more specific
_GENERIC_PICTURE = frozenset({
    "diagram", "visual", "root", "image", "img", "illustration", "picture",
    "pic", "fig", "figure", "drawing", "sketch", "asset", "art", "graphic",
    "icon", "object", "obj", "main", "the", "whole", "full", "entire",
    "scene", "board", "model",
})
_ANCHORABLE = ("illustration", "text", "shape", "particles")


def _tokens(s) -> list[str]:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).split()


def _token_run(a: str, b: str) -> bool:
    """Do two anchor keys match on TOKEN boundaries?

    One key's tokens must appear as a CONTIGUOUS RUN inside the other's, and
    where BOTH end in a numeral it must be the SAME number. Raw substring
    containment bound an unknown ref to any same-kind element whose key
    merely contained it: 'label 1' matched 'label 10', 'ion' matched
    'region', and the arrow landed on a neighbour of the thing it named. A
    bare name still finds its numbered element ('title' -> 'title 1'): that
    is one name, not two numbers disagreeing.
    """
    ta, tb = a.split(), b.split()
    if not ta or not tb:
        return False
    if len(ta) > len(tb):
        ta, tb = tb, ta
    if not any(tb[i:i + len(ta)] == ta for i in range(len(tb) - len(ta) + 1)):
        return False
    return not (ta[-1].isdigit() and tb[-1].isdigit()) or ta[-1] == tb[-1]


def anchor_key(s) -> str:
    """'Plant_Cell_Box' / 'plant-cell diagram' / 'plant_cell' -> 'plant cell'."""
    words = _tokens(s)
    while len(words) > 1 and words[-1] in _KIND_SUFFIXES:
        words.pop()
    return " ".join(words)


def _looks_like_text(ref: str) -> bool:
    raw = str(ref).lower()
    if raw.startswith(_TEXT_PREFIXES) or raw.endswith(_TEXT_SUFFIXES):
        return True
    return any(t in _TEXT_WORDS or _TEXT_WORD_RE.fullmatch(t)
               for t in _tokens(raw))


def is_carried_id(eid) -> bool:
    """An element the compiler carried across a chapter boundary
    (``prev__x``, ``_prev__x`` when the plain name collided)."""
    return str(eid or "").lstrip("_").startswith("prev__")


def resolve_anchor(ref: str, elements: dict[str, dict],
                   root_id: Optional[str], part_names=None,
                   aliases: Optional[dict[str, str]] = None,
                   *, end: str = "head",
                   ) -> tuple[Optional[str], Optional[str], str]:
    """Where should an anchor that says ``ref`` really point?

    Returns (element_id, layer, how) — ``how`` is a short reason for the
    report — or (None, None, reason) when nothing deterministic exists.
    ``aliases`` maps ids the compiler merged away (per-part handles) to the
    part name they stood for. ``end`` is which end of the arrow asks: only a
    HEAD may fall back to the root illustration.
    """
    ref = str(ref)
    if aliases and ref in aliases and root_id:
        return root_id, aliases[ref], "merged handle"
    key = anchor_key(ref)
    cands = {eid: e for eid, e in elements.items()
             if isinstance(e, dict) and e.get("type") in _ANCHORABLE}
    if key:
        exact = [eid for eid in cands if anchor_key(eid) == key]
        if len(exact) == 1:
            return exact[0], None, "id match"
        by_asset = [eid for eid, e in cands.items()
                    if e.get("type") == "illustration"
                    and anchor_key(e.get("asset")) == key]
        if len(by_asset) == 1:
            return by_asset[0], None, "asset match"

        def _part_rung():
            # THE layer matcher (vector_assets.match_layer_ids): exact wins,
            # else containment — so 'chloroplast' finds 'chloroplasts' and
            # 'wall' finds 'cell_wall' here exactly as it does when the draw
            # distribution looks the same name up. Ambiguity binds nothing,
            # and a ref that reads like a LABEL is never a part of the picture.
            if not (root_id and part_names and len(key) >= 3
                    and not _looks_like_text(ref)):
                return None
            from .vector_assets import match_layer_ids
            hits = list(dict.fromkeys(match_layer_ids(
                [str(p) for p in part_names],
                [ref.lower(), key, key.replace(" ", "_")])))
            if len(hits) == 1:
                return root_id, str(hits[0]), "part name"
            return None

        # An arrow HEAD points AT the picture, so the picture's own PARTS are
        # tried before the label that happens to carry the same words. The
        # other order bound a head named 'nucleus' to the label 'Nucleus'
        # its own TAIL was already on: one element, both ends, an arrow that
        # draws nothing under a label with no leader line.
        if end == "head":
            hit = _part_rung()
            if hit:
                return hit
        by_text = [eid for eid, e in cands.items()
                   if e.get("type") == "text"
                   and anchor_key(e.get("text")) == key]
        if len(by_text) == 1:
            return by_text[0], None, "text match"
        kind = "text" if _looks_like_text(ref) else "illustration"
        loose = [eid for eid, e in cands.items()
                 if e.get("type") == kind and anchor_key(eid)
                 and _token_run(key, anchor_key(eid))]
        if len(loose) == 1:
            return loose[0], None, "kind match"
        if end != "head":
            hit = _part_rung()
            if hit:
                return hit
    if (end == "head" and root_id and root_id in cands
            and not _looks_like_text(ref)):
        # arrows point AT pictures: a head that names the picture by one of
        # its own words (or just says "the diagram") means the root. A ref
        # sharing nothing with it is a guess, and a guess drew a zero-length
        # root->root arrow once ('title' as a tail).
        toks = set(_tokens(ref))
        root_toks = (set(_tokens(root_id))
                     | set(_tokens(cands[root_id].get("asset")))
                     | {t for p in (part_names or []) for t in _tokens(p)})
        if toks and (toks & root_toks or toks <= _GENERIC_PICTURE):
            return root_id, None, "root visual"
    return None, None, "no candidate"


def _single_root(elements: dict[str, dict]) -> Optional[str]:
    roots = [eid for eid, e in elements.items()
             if isinstance(e, dict) and e.get("type") == "illustration"
             and not eid.startswith("__") and not is_carried_id(eid)
             and not e.get("hud")]
    return roots[0] if len(roots) == 1 else None


def _rechain_text(eid: str, e: dict, roster: dict[str, dict],
                  pos: dict[str, int], notes: list[str]) -> None:
    """Re-resolve — or cut — ONE text's dangling ``after``, in place.

    A chain runs BEHIND an earlier element (schema rule), so the candidates
    are the texts ahead of this one in the roster AND still on it. Reading
    them out of a pre-drop snapshot instead raised ``KeyError`` for any
    arrow dropped earlier in the same pass, and the exception escaped the
    guard to kill the whole chapter compile: zero scenes, the exact loss
    this module exists to prevent.
    """
    ref = e["after"].get("el")
    fixed = dict(e)
    new_el = how = None
    if not is_carried_id(eid):
        here = pos.get(eid, len(pos))
        texts = {k: v for k, v in roster.items()
                 if pos.get(k, len(pos)) < here and isinstance(v, dict)
                 and v.get("type") == "text"}
        new_el, _, how = resolve_anchor(str(ref), texts, None, end="after")
    if new_el is not None:
        fixed["after"] = {**e["after"], "el": new_el}
        notes.append(f"REANCHORED {eid}.after {ref!r} -> {new_el} ({how})")
    else:
        fixed.pop("after", None)   # it still has its own `at`
        notes.append(f"UNCHAINED text {eid} (after {ref!r} names no "
                     f"earlier element)")
    roster[eid] = fixed


def resolve_roster_anchors(roster: dict[str, dict], root_id: Optional[str],
                           *, part_names=None,
                           aliases: Optional[dict[str, str]] = None,
                           ) -> tuple[list[str], list[str]]:
    """Fix or drop dangling anchors IN PLACE. Returns (notes, dropped_ids).

    Notes are report fragments ("REANCHORED arrow_plant.head 'plant_cell_box'
    -> plant_cell_diagram (root visual)"); the caller prefixes the chapter or
    segment. Actions that targeted a dropped arrow are the caller's to drop —
    the compiler's dangling-reference filter already does that.
    """
    notes: list[str] = []
    dropped: list[str] = []
    if root_id is None:
        root_id = _single_root(roster)
    pos = {k: i for i, k in enumerate(roster)}
    for eid, e in list(roster.items()):
        if not isinstance(e, dict):
            continue
        t = e.get("type")
        carried = is_carried_id(eid)
        if t == "arrow":
            fixed = dict(e)
            changed = kill = False
            for end in ("tail", "head"):
                ref = fixed.get(end)
                if not (isinstance(ref, dict) and isinstance(ref.get("el"), str)):
                    continue
                tgt = ref["el"]
                if tgt in roster and tgt != eid \
                        and roster[tgt].get("type") != "arrow":
                    continue
                if carried:
                    # the previous chapter's arrow: whatever it pointed at is
                    # gone with that board; the NEW chapter's elements are
                    # never what it meant, however alike their names
                    notes.append(f"DROPPED arrow {eid} ({end} anchor {tgt!r} "
                                 f"stayed behind in the previous chapter)")
                    kill = True
                    break
                new_el, layer, how = resolve_anchor(
                    tgt, roster, root_id, part_names, aliases, end=end)
                if new_el is None or new_el == eid:
                    notes.append(f"DROPPED arrow {eid} ({end} anchor {tgt!r} "
                                 f"names no element)")
                    kill = True
                    break
                nref = {**ref, "el": new_el}
                if layer and not nref.get("layer"):
                    nref["layer"] = layer
                fixed[end] = nref
                changed = True
                notes.append(f"REANCHORED {eid}.{end} {tgt!r} -> {new_el}"
                             + (f".{layer}" if layer else "") + f" ({how})")
            if not kill and changed:
                _t, _h = fixed.get("tail"), fixed.get("head")
                if isinstance(_t, dict) and isinstance(_h, dict) \
                        and isinstance(_t.get("el"), str) \
                        and _t.get("el") == _h.get("el") \
                        and _t.get("layer") == _h.get("layer"):
                    # both ends on one element (a label pointing at itself,
                    # root to root): it renders as nothing AND the label it
                    # came from loses its leader line. Better no arrow than
                    # an invisible one nobody can see is wrong.
                    _w = str(_t["el"]) + (f".{_t['layer']}" if _t.get("layer")
                                          else "")
                    notes.append(f"DROPPED arrow {eid} (both ends resolve to "
                                 f"{_w}; it would draw nothing)")
                    kill = True
            if kill:
                del roster[eid]
                dropped.append(eid)
            elif changed:
                roster[eid] = fixed
        elif t == "text" and isinstance(e.get("after"), dict):
            ref = e["after"].get("el")
            # sound already? the schema asks a chain to name an element that
            # is BOTH on the board and EARLIER than this one. A ref to a
            # LATER element passed this test and was then thrown out by the
            # validator ("must chain after an EARLIER element") — a whole
            # board lost for a chain the guard could simply have cut.
            if (isinstance(ref, str) and ref != eid and ref in roster
                    and pos.get(ref, len(pos)) < pos.get(eid, len(pos))):
                continue
            _rechain_text(eid, e, roster, pos, notes)
    if dropped:
        # a group naming a dropped arrow would fail the schema ("group
        # references unknown") — the guard must never make the failure it
        # exists to prevent. Prune the child; a group left empty goes too,
        # and a group whose only child was THAT group goes with it.
        gone = set(dropped)
        again = True
        while again:
            again = False
            for gid, g in list(roster.items()):
                if not isinstance(g, dict) or g.get("type") != "group":
                    continue
                kids = [c for c in (g.get("children") or []) if c not in gone]
                if len(kids) == len(g.get("children") or []):
                    continue
                if kids:
                    roster[gid] = {**g, "children": kids}
                    continue
                del roster[gid]
                gone.add(gid)
                dropped.append(gid)
                notes.append(f"DROPPED group {gid} (every child was dropped)")
                again = True
        # ...and a TEXT chained after something this pass removed is the same
        # self-inflicted loss one rung down: "text chains after unknown
        # element" costs the board whether the id was an arrow dropped
        # further along the roster or a group emptied only now that `gone` is
        # final. The main loop saw neither — both happened after it looked.
        for tid, te in list(roster.items()):
            if not isinstance(te, dict) or te.get("type") != "text":
                continue
            aft = te.get("after")
            if (isinstance(aft, dict)
                    and isinstance(aft.get("el"), str)
                    and aft["el"] in gone):
                _rechain_text(tid, te, roster, pos, notes)
    return notes, dropped


def resolve_scene_anchors(scene: dict, root_id: Optional[str] = None,
                          part_names=None) -> list[str]:
    """The per-scene form of the same guard, for a scene dict about to be
    validated: elements list + actions list. Dropped arrows take their
    actions with them (camera verbs never target). Returns report notes."""
    els = scene.get("elements")
    if not isinstance(els, list):
        return []
    roster: dict[str, dict] = {}
    original: dict[str, dict] = {}
    for e in els:
        if isinstance(e, dict) and isinstance(e.get("id"), str) \
                and e["id"] not in roster:
            roster[e["id"]] = e
            original[e["id"]] = e
    notes, dropped = resolve_roster_anchors(roster, root_id,
                                            part_names=part_names)
    if not notes:
        return []
    # rebuild from the ORIGINAL list: a fixed element replaces the entry it
    # was made from, everything else passes through untouched — a duplicate
    # id or an id-less entry still reaches the schema to be rejected there,
    # exactly as it would without a dangling anchor beside it. The roster
    # pass prunes the GROUPS that named a dropped arrow in place, so a pruned
    # group comes back through `roster` and one left empty through `dropped`
    # — and an id-less group can never enter the set and take every
    # target-less camera action down with it.
    gone = {d for d in dropped if isinstance(d, str)}
    rebuilt = []
    for e in els:
        eid = e.get("id") if isinstance(e, dict) else None
        if isinstance(eid, str) and eid in gone:
            continue
        if eid in roster and original.get(eid) is e:
            rebuilt.append(roster[eid])
        else:
            rebuilt.append(e)
    scene["elements"] = rebuilt
    if gone and isinstance(scene.get("actions"), list):
        kept = []
        for a in scene["actions"]:
            if not isinstance(a, dict):
                kept.append(a)
                continue
            if a.get("target") in gone:
                notes.append(f"DROPPED {a.get('verb')}->{a.get('target')} "
                             f"(its arrow was dropped)")
                continue
            if a.get("into") in gone:
                # a morph names its destination in `into`, not `target`:
                # dropping only the targeting actions left "morph into
                # unknown element" and cost the whole scene anyway
                notes.append(f"DROPPED {a.get('verb')} into {a.get('into')} "
                             f"(that element was dropped)")
                continue
            kept.append(a)
        scene["actions"] = kept
    return notes
