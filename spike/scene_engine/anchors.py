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
     + layer), a part name used as if it were an element (root + layer), or
     the unique element of the kind the id implies;
  2. else, for anything that does not look like a label, the scene's single
     root illustration;
  3. else THAT arrow is dropped (with its actions) and the scene proceeds.

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
_TEXT_PREFIXES = ("lbl_", "label_", "lb_", "txt_", "text_", "caption_")
_ANCHORABLE = ("illustration", "text", "shape", "particles")


def anchor_key(s) -> str:
    """'Plant_Cell_Box' / 'plant-cell diagram' / 'plant_cell' -> 'plant cell'."""
    words = re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).split()
    while len(words) > 1 and words[-1] in _KIND_SUFFIXES:
        words.pop()
    return " ".join(words)


def _looks_like_text(ref: str) -> bool:
    return str(ref).lower().startswith(_TEXT_PREFIXES)


def resolve_anchor(ref: str, elements: dict[str, dict],
                   root_id: Optional[str], part_names=None,
                   aliases: Optional[dict[str, str]] = None,
                   ) -> tuple[Optional[str], Optional[str], str]:
    """Where should an anchor that says ``ref`` really point?

    Returns (element_id, layer, how) — ``how`` is a short reason for the
    report — or (None, None, reason) when nothing deterministic exists.
    ``aliases`` maps ids the compiler merged away (per-part handles) to the
    part name they stood for.
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
        by_text = [eid for eid, e in cands.items()
                   if e.get("type") == "text"
                   and anchor_key(e.get("text")) == key]
        if len(by_text) == 1:
            return by_text[0], None, "text match"
        kind = "text" if _looks_like_text(ref) else "illustration"
        loose = []
        for eid, e in cands.items():
            if e.get("type") != kind:
                continue
            k = anchor_key(eid)
            if k and (key in k or k in key):
                loose.append(eid)
        if len(loose) == 1:
            return loose[0], None, "kind match"
        if root_id and part_names:
            for p in part_names:
                if anchor_key(p) == key:
                    return root_id, str(p), "part name"
    if root_id and root_id in cands and not _looks_like_text(ref):
        return root_id, None, "root visual"
    return None, None, "no candidate"


def _single_root(elements: dict[str, dict]) -> Optional[str]:
    roots = [eid for eid, e in elements.items()
             if isinstance(e, dict) and e.get("type") == "illustration"
             and not eid.startswith("__") and not eid.startswith("prev__")
             and not e.get("hud")]
    return roots[0] if len(roots) == 1 else None


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
    for eid, e in list(roster.items()):
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
                new_el, layer, how = resolve_anchor(
                    tgt, roster, root_id, part_names, aliases)
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
            if kill:
                del roster[eid]
                dropped.append(eid)
            elif changed:
                roster[eid] = fixed
        elif t == "text" and isinstance(e.get("after"), dict):
            ref = e["after"].get("el")
            if isinstance(ref, str) and ref in roster and ref != eid:
                continue
            texts = {k: v for k, v in roster.items()
                     if isinstance(v, dict) and v.get("type") == "text"
                     and k != eid}
            new_el, _, how = resolve_anchor(str(ref), texts, None)
            fixed = dict(e)
            if new_el is not None:
                fixed["after"] = {**e["after"], "el": new_el}
                notes.append(f"REANCHORED {eid}.after {ref!r} -> {new_el} "
                             f"({how})")
            else:
                fixed.pop("after", None)   # it still has its own `at`
                notes.append(f"UNCHAINED text {eid} (after {ref!r} names no "
                             f"element)")
            roster[eid] = fixed
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
    order: list[str] = []
    for e in els:
        if isinstance(e, dict) and isinstance(e.get("id"), str) \
                and e["id"] not in roster:
            roster[e["id"]] = e
            order.append(e["id"])
    notes, dropped = resolve_roster_anchors(roster, root_id,
                                            part_names=part_names)
    if not notes:
        return []
    scene["elements"] = [roster[eid] for eid in order if eid in roster]
    if dropped and isinstance(scene.get("actions"), list):
        gone = set(dropped)
        kept = []
        for a in scene["actions"]:
            if isinstance(a, dict) and a.get("target") in gone:
                notes.append(f"DROPPED {a.get('verb')}->{a.get('target')} "
                             f"(its arrow was dropped)")
                continue
            kept.append(a)
        scene["actions"] = kept
    return notes
