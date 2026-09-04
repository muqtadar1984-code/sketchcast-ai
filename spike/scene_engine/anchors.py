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

ONE PASS, RUN TO A FIXED POINT. ``sanitize_scene`` is the whole guard, and
every road that produces a scene calls it immediately before validation: the
compiler's per-segment emission, the exported/HOLD board (``board_now``), and
``director.parse_scene_response``. It used to be a scatter of per-call-site
guards, and each one could create the dangling reference the next would have
had to catch — four review rounds found four of those in turn: an arrow to an
unknown element, a group naming a dropped arrow, a text chained behind a
dropped group, an arrow anchored to a group the guard itself emptied. So the
pass SWEEPS UNTIL A SWEEP REMOVES NOTHING, and its contract is a whole-scene
one: when it returns, no element, action, anchor, group child, ``after`` chain
or morph ``into`` names something the scene does not contain.

Every conversion is reported the way ANCHORED / SYNTHESIZED lines are, so
validate.py's arrow accounting stays truthful — the vocabulary is a ``Words``
tuple per call site, not a second implementation. The pydantic validator keeps
its check as the last line of defence — it is simply no longer reachable for
this class of plan.
"""

from __future__ import annotations

import re
from typing import Callable, NamedTuple, Optional

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


# ── the report vocabulary ────────────────────────────────────────────────
# ONE pass serves three call sites, and each says WHERE a reference went in
# its own words. The chapter roster and a scene about to be validated say a
# ref "names no element"; an exported board says it "is not on the board";
# a carry-out board says LEFT BEHIND instead of DROPPED on purpose, so
# validate.py's arrow accounting can tell a boundary from a loss. The
# wording is data, not a second implementation.

class Words(NamedTuple):
    drop_word: str = "DROPPED"                       # DROPPED | LEFT BEHIND
    missing: str = "names no element"                # an arrow end's ref
    group_gone: str = "every child was dropped"
    after_gone: str = "names no earlier element"     # a text chain's ref
    flattened: str = "not on the board"              # the `place` fallback


WORDS_PLAN = Words()
# the compiler's per-segment emission: a ref to a roster element this board
# has not drawn YET flattens to its planned point and rides on
WORDS_SEGMENT = Words(flattened="not on the board yet")
# board_now(): the board as exported, where "gone" means "not on it"
WORDS_BOARD = Words(missing="is not on the board",
                    group_gone="every child left the board",
                    after_gone="is not on the board")


def _rechain_text(eid: str, e: dict, roster: dict[str, dict],
                  pos: dict[str, int], notes: list[str], *,
                  resolve: bool = True, words: Words = WORDS_PLAN) -> None:
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
    if resolve and not is_carried_id(eid):
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
        # a ref that is HERE but later is a different sentence from one that
        # is not here at all — an exported board says "is not on the board"
        # only about the second
        why = (words.after_gone
               if not isinstance(ref, str) or ref not in roster
               else WORDS_PLAN.after_gone)
        notes.append(f"UNCHAINED text {eid} (after {ref!r} {why})")
    roster[eid] = fixed


def _sweep(roster: dict[str, dict], root_id: Optional[str], part_names,
           aliases: Optional[dict[str, str]], resolve: bool,
           place: Optional[Callable], words: Words,
           notes: list[str]) -> list[str]:
    """ONE sweep of the roster. Returns the ids removed by THIS sweep.

    A sweep is not enough on its own — removing a group orphans the texts
    chained behind it and the arrows anchored to it, removing an arrow
    empties the groups that held it — which is why the only caller runs
    sweeps to a fixed point.
    """
    removed: list[str] = []
    pos = {k: i for i, k in enumerate(roster)}
    for eid in list(roster):
        e = roster.get(eid)
        if not isinstance(e, dict):
            continue
        t = e.get("type")
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
                # 1. the caller may know where the missing thing WOULD be
                #    (its planned point): a flattened end keeps the arrow
                pt = place(eid, end, ref) if place is not None else None
                if pt is not None:
                    fixed[end] = pt
                    changed = True
                    notes.append(f"FLATTENED {eid}.{end} {tgt!r} -> {pt} "
                                 f"({words.flattened})")
                    continue
                if is_carried_id(eid):
                    # the previous chapter's arrow: whatever it pointed at is
                    # gone with that board; the NEW chapter's elements are
                    # never what it meant, however alike their names
                    notes.append(f"{words.drop_word} arrow {eid} ({end} anchor "
                                 f"{tgt!r} stayed behind in the previous "
                                 f"chapter)")
                    kill = True
                    break
                # 2. re-anchor to what the ref must have meant
                new_el = layer = how = None
                if resolve:
                    new_el, layer, how = resolve_anchor(
                        tgt, roster, root_id, part_names, aliases, end=end)
                # 3. nothing resolves: THIS arrow goes, never the board
                if new_el is None or new_el == eid:
                    notes.append(f"{words.drop_word} arrow {eid} ({end} anchor "
                                 f"{tgt!r} {words.missing})")
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
                    notes.append(f"{words.drop_word} arrow {eid} (both ends "
                                 f"resolve to {_w}; it would draw nothing)")
                    kill = True
            if kill:
                del roster[eid]
                removed.append(eid)
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
            _rechain_text(eid, e, roster, pos, notes,
                          resolve=resolve, words=words)
        elif t == "group":
            # a group naming something the scene does not contain fails the
            # schema ("group references unknown") — the guard must never make
            # the failure it exists to prevent. Prune the child; a group left
            # empty goes too, and the sweep after this one takes the group
            # whose only child was THAT group.
            kids = [c for c in (e.get("children") or [])
                    if isinstance(c, str) and c in roster and c != eid]
            if len(kids) == len(e.get("children") or []):
                continue
            if kids:
                roster[eid] = {**e, "children": kids}
                continue
            del roster[eid]
            removed.append(eid)
            notes.append(f"{words.drop_word} group {eid} ({words.group_gone})")
    return removed


def _sanitize_roster(roster: dict[str, dict], root_id: Optional[str], *,
                     part_names=None, aliases: Optional[dict[str, str]] = None,
                     resolve: bool = True, place: Optional[Callable] = None,
                     words: Words = WORDS_PLAN,
                     ) -> tuple[list[str], list[str]]:
    """Sweep the roster to a FIXED POINT. Returns (notes, dropped_ids).

    Each removal can orphan another reference — a dropped arrow empties the
    group that held it, an emptied group orphans the arrows anchored to it
    and the texts chained behind it — so one sweep can hand the schema the
    dangling reference the next sweep would have caught. Three review passes
    each fixed one such path and found the next; this loop closes the family
    instead: sweep until a sweep removes nothing.
    """
    notes: list[str] = []
    dropped: list[str] = []
    if resolve and root_id is None:
        root_id = _single_root(roster)
    for _ in range(len(roster) + 2):     # each sweep removes >= 1 or stops
        gone = _sweep(roster, root_id, part_names, aliases, resolve, place,
                      words, notes)
        dropped.extend(gone)
        if not gone:
            break
    return notes, dropped


def resolve_roster_anchors(roster: dict[str, dict], root_id: Optional[str],
                           *, part_names=None,
                           aliases: Optional[dict[str, str]] = None,
                           ) -> tuple[list[str], list[str]]:
    """The chapter roster's form of the one pass: fix or drop dangling
    anchors IN PLACE (the caller hands it a COPY — see continuity.py —
    because an exception mid-pass would otherwise leave a half-sanitised
    roster behind a report line claiming nothing changed).
    Returns (notes, dropped_ids).

    Notes are report fragments ("REANCHORED arrow_plant.head 'plant_cell_box'
    -> plant_cell_diagram (root visual)"); the caller prefixes the chapter or
    segment. Actions that targeted a dropped arrow are the caller's to drop —
    the compiler's dangling-reference filter already does that.
    """
    return _sanitize_roster(roster, root_id, part_names=part_names,
                            aliases=aliases)


def sanitize_scene(scene: dict, root_id: Optional[str] = None, *,
                   part_names=None, aliases: Optional[dict[str, str]] = None,
                   resolve: bool = True, place: Optional[Callable] = None,
                   words: Words = WORDS_PLAN) -> list[str]:
    """THE sanitisation pass — run it immediately before a scene dict is
    validated, on every road that produces one.

    Contract: when it returns, nothing in ``scene`` names something the
    scene does not contain — no arrow anchor, no text ``after``, no group
    child, and no action target or morph ``into`` that this pass removed.
    (An action naming an element that was never there is left for the schema:
    the guard fixes what IT and the plan broke; it does not paper over a
    director that targeted a ghost.)

    ``place(el_id, end, ref) -> point | None`` lets a caller keep an arrow
    whose anchor is merely not on THIS board by flattening that end to the
    element's planned point; ``resolve=False`` turns off re-anchoring for a
    board where "not here" means "not drawn yet", not "misnamed". Nothing is
    written back to ``scene`` until the pass has finished, so a guard that
    raises leaves the scene exactly as it was.
    """
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
    notes, dropped = _sanitize_roster(
        roster, root_id, part_names=part_names, aliases=aliases,
        resolve=resolve, place=place, words=words)
    if not notes:
        return []
    # rebuild from the ORIGINAL list: a fixed element replaces the entry it
    # was made from, everything else passes through untouched — a duplicate
    # id or an id-less entry still reaches the schema to be rejected there,
    # exactly as it would without a dangling anchor beside it. The pass
    # prunes GROUPS in place, so a pruned group comes back through `roster`
    # and one left empty through `dropped` — and an id-less group can never
    # enter the roster and take every target-less camera action down with it.
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
    actions = scene.get("actions")
    if gone and isinstance(actions, list):
        kept = []
        for a in actions:
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
        actions = kept
    scene["elements"] = rebuilt
    if isinstance(actions, list):
        scene["actions"] = actions
    return notes


def resolve_scene_anchors(scene: dict, root_id: Optional[str] = None,
                          part_names=None) -> list[str]:
    """The scene-shaped name the director calls the one pass by:
    ``sanitize_scene`` with the planning vocabulary."""
    return sanitize_scene(scene, root_id, part_names=part_names)
