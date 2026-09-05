"""ONE sanitisation pass, run to a FIXED POINT, on every road to a scene.

Four adversarial rounds each closed one dangling-reference path and found the
next: an arrow to an unknown element, then a group naming the arrow the guard
had just dropped, then a text chained behind the group it had just emptied,
then an arrow anchored to that group — plus a HOLD board whose single-pass
seal kept refs to the very arrows and groups it had itself removed. Every one
of them cost the WHOLE board at schema validation: "group references
unknown", "text chains after unknown element", "arrow anchors to unknown
element". They are one family, not four bugs, so this file tests the family.

A small generator of dangling SHAPES (below), every shape alone and every
pair of them, on all three roads a scene travels — ``director.
parse_scene_response``, the compiler's per-segment emission, and the
HOLD / carry-out board (``board_now``) — asserting that each scene

  * is SEALED: no anchor, ``after`` chain, group child, action target or
    morph ``into`` names something the scene does not contain; and
  * KEEPS what the shape did not name: the picture, the labels, and every
    innocent element beside the dangling one.

All offline: no model, no network, no ffmpeg.
"""

from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass, field

import pytest

from spike.scene_engine.continuity import compile_plan, parse_visual_plan
from spike.scene_engine.director import parse_scene_response
from spike.scene_engine.schema import CAMERA_VERBS

_NARR = {"s001": "hook", "s002": "intro", "s003": "Here is a plant cell.",
         "s004": "The wall protects it.", "s005": "Chloroplasts make food.",
         "s006": "Look at the vacuole."}

# the innocent board every shape is dropped onto: a picture and TWO labels,
# so a ghost ref never has a unique text to fall to
_BASE = [
    {"id": "cell", "type": "illustration", "asset": "plant_cell",
     "at": [640, 360]},
    {"id": "lbl_alpha", "type": "text", "text": "Alpha", "at": [95, 140]},
    {"id": "lbl_beta", "type": "text", "text": "Beta", "at": [95, 220]},
]
_BASE_IDS = ("cell", "lbl_alpha", "lbl_beta")
_BASE_ACTIONS = [{"verb": "draw", "target": "cell"},
                 {"verb": "write", "target": "lbl_alpha"},
                 {"verb": "write", "target": "lbl_beta"}]
_ASSETS = {"plant_cell": "A plant cell diagram."}


@dataclass(frozen=True)
class Shape:
    """One way a scene refers to something it will not contain."""
    name: str
    elements: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    survivors: tuple = ()      # ids the pass must KEEP
    gone: tuple = ()           # ids the pass must remove


def _ghost(n: int) -> dict:
    """An arrow nothing can re-anchor: its tail names a LABEL that does not
    exist, and a label ref never falls to the root picture."""
    return {"id": f"arr{n}", "type": "arrow",
            "tail": {"el": f"ghost{n}_label"}, "head": {"el": "cell"}}


def _shapes() -> list[Shape]:
    """The generator. Each shape is built around its own numbered ids so any
    combination of them can share one board."""
    out: list[Shape] = []

    n = 1                                   # 1. the original incident shape
    out.append(Shape(
        "arrow to an unknown element",
        [_ghost(n)], [{"verb": "draw", "target": f"arr{n}"}],
        gone=(f"arr{n}",)))

    n = 2                                   # 2. round 4, finding 1
    out.append(Shape(
        "arrow anchored to a group the guard empties",
        [_ghost(n), {"id": f"grp{n}", "type": "group",
                     "children": [f"arr{n}"]},
         {"id": f"ptr{n}", "type": "arrow", "tail": {"el": "lbl_alpha"},
          "head": {"el": f"grp{n}"}}],
        [{"verb": "draw", "target": f"arr{n}"},
         {"verb": "draw", "target": f"ptr{n}"}],
        gone=(f"arr{n}", f"grp{n}", f"ptr{n}")))

    n = 3                                   # 3. round 3, finding 2
    out.append(Shape(
        "text chained after a group the guard empties",
        [_ghost(n), {"id": f"grp{n}", "type": "group",
                     "children": [f"arr{n}"]},
         {"id": f"cap{n}", "type": "text", "text": "Caption", "at": [95, 300],
          "after": {"el": f"grp{n}", "gap": 8}}],
        [{"verb": "draw", "target": f"arr{n}"},
         {"verb": "write", "target": f"cap{n}"}],
        survivors=(f"cap{n}",), gone=(f"arr{n}", f"grp{n}")))

    n = 4                                   # 4. a chain onto a LATER element
    out.append(Shape(
        "text chained after a later element",
        [{"id": f"cap{n}", "type": "text", "text": "Caption", "at": [95, 300],
          "after": {"el": f"trailer{n}", "gap": 8}},
         {"id": f"trailer{n}", "type": "text", "text": "Trailer",
          "at": [95, 380]}],
        [{"verb": "write", "target": f"cap{n}"},
         {"verb": "write", "target": f"trailer{n}"}],
        survivors=(f"cap{n}", f"trailer{n}")))

    n = 5                                   # 5. a morph's destination
    out.append(Shape(
        "morph into a dropped element",
        [_ghost(n)],
        [{"verb": "draw", "target": f"arr{n}"},
         {"verb": "morph", "target": "lbl_alpha", "into": f"arr{n}",
          "duration": 1.0}],
        gone=(f"arr{n}",)))

    n = 6                                   # 6. round 3, finding 1
    out.append(Shape(
        "a group whose children are all dropped",
        [_ghost(n), {"id": f"grp{n}", "type": "group",
                     "children": [f"arr{n}"]}],
        [{"verb": "draw", "target": f"arr{n}"},
         {"verb": "draw", "target": f"grp{n}"}],
        gone=(f"arr{n}", f"grp{n}")))

    n = 7                                   # 7. the cascade, three deep
    out.append(Shape(
        "nested groups, emptied from the inside out",
        [_ghost(n), {"id": f"grp{n}", "type": "group",
                     "children": [f"arr{n}"]},
         {"id": f"mid{n}", "type": "group", "children": [f"grp{n}"]},
         {"id": f"top{n}", "type": "group", "children": [f"mid{n}"]}],
        [{"verb": "draw", "target": f"arr{n}"},
         {"verb": "draw", "target": f"top{n}"}],
        gone=(f"arr{n}", f"grp{n}", f"mid{n}", f"top{n}")))

    n = 8                                   # 8. the pass must not OVER-drop
    out.append(Shape(
        "a group that keeps its innocent child, and the arrow on it",
        [_ghost(n), {"id": f"grp{n}", "type": "group",
                     "children": [f"arr{n}", "lbl_beta"]},
         {"id": f"ptr{n}", "type": "arrow", "tail": {"el": "lbl_alpha"},
          "head": {"el": f"grp{n}"}}],
        [{"verb": "draw", "target": f"arr{n}"},
         {"verb": "draw", "target": f"grp{n}"},
         {"verb": "draw", "target": f"ptr{n}"}],
        survivors=(f"grp{n}", f"ptr{n}"), gone=(f"arr{n}",)))

    return out


SHAPES = _shapes()
_BY_NAME = {s.name: s for s in SHAPES}
_PAIRS = list(itertools.combinations([s.name for s in SHAPES], 2))


# ── the property ─────────────────────────────────────────────────────────

def _assert_sealed(scene: dict, where: str) -> None:
    """The pass's contract, checked directly on the scene dict: nothing in it
    names something it does not contain. Every one of these was a real lost
    board — the schema raises on each, and a raise costs the whole scene."""
    els = [e for e in scene.get("elements") or [] if isinstance(e, dict)]
    ids = {e["id"] for e in els if isinstance(e.get("id"), str)}
    order = {e["id"]: i for i, e in enumerate(els)
             if isinstance(e.get("id"), str)}
    arrows = {e["id"] for e in els
              if e.get("type") == "arrow" and isinstance(e.get("id"), str)}
    for e in els:
        eid = e.get("id")
        if e.get("type") == "arrow":
            for end in ("tail", "head"):
                ref = e.get(end)
                if isinstance(ref, dict) and isinstance(ref.get("el"), str):
                    assert ref["el"] in ids, f"{where}: {eid}.{end} -> {ref['el']}"
                    assert ref["el"] not in arrows, \
                        f"{where}: {eid}.{end} anchors to an arrow"
        elif e.get("type") == "text" and isinstance(e.get("after"), dict):
            ref = e["after"].get("el")
            assert ref in ids, f"{where}: text {eid} after {ref!r}"
            assert order[ref] < order[eid], \
                f"{where}: text {eid} chains after the LATER {ref!r}"
        elif e.get("type") == "group":
            for c in e.get("children") or []:
                assert c in ids, f"{where}: group {eid} names {c!r}"
    for a in scene.get("actions") or []:
        if not isinstance(a, dict) or a.get("verb") in CAMERA_VERBS:
            continue
        assert a.get("target") in ids, f"{where}: {a.get('verb')} -> {a.get('target')}"
        if a.get("verb") == "morph":
            assert a.get("into") in ids, f"{where}: morph into {a.get('into')}"


def _assert_kept(scene: dict, shapes: list[Shape], where: str, *,
                 check_gone: bool = True) -> None:
    ids = {e["id"] for e in scene.get("elements") or []
           if isinstance(e, dict) and isinstance(e.get("id"), str)}
    for b in _BASE_IDS:
        assert b in ids, f"{where}: the guard cost the board {b}"
    if not check_gone:
        return
    for s in shapes:
        for g in s.gone:
            assert g not in ids, f"{where}: {g} survived ({s.name})"


# ── road 1: the director ─────────────────────────────────────────────────

def _director_raw(shapes: list[Shape], compiled: bool = False) -> dict:
    els = [copy.deepcopy(e) for e in _BASE]
    acts = [dict(a) for a in _BASE_ACTIONS]
    for s in shapes:
        els += [copy.deepcopy(e) for e in s.elements]
        acts += [copy.deepcopy(a) for a in s.actions]
    raw = {"id": "vc_s004", "elements": els, "actions": acts}
    if compiled:
        raw["compiled"] = True
    return raw


def _check_director(shapes: list[Shape], compiled: bool = False) -> None:
    raw = _director_raw(shapes, compiled)
    scene = parse_scene_response(raw, _NARR["s004"])
    names = " + ".join(s.name for s in shapes)
    assert scene is not None, f"director: the board fell to a slide ({names})"
    # the director hands back a Scene, so the sanitised board is what it
    # parsed — dump it and hold THAT to the contract
    sanitised = scene.model_dump()
    _assert_sealed(sanitised, f"director [{names}]")
    _assert_kept(sanitised, shapes, f"director [{names}]")
    ids = {e.id for e in scene.elements}
    for s in shapes:
        for k in s.survivors:
            assert k in ids, f"director: {k} was lost with the dangle ({s.name})"


@pytest.mark.parametrize("name", [s.name for s in SHAPES])
def test_director_seals_every_shape(name):
    _check_director([_BY_NAME[name]])


@pytest.mark.parametrize("a,b", _PAIRS)
def test_director_seals_every_pair(a, b):
    """Two shapes on one board: the pairs are where a single-pass guard breaks
    — one shape's drop is the other's dangling reference."""
    _check_director([_BY_NAME[a], _BY_NAME[b]])


def test_director_seals_all_shapes_at_once():
    # a compiled board carries the whole persistent scene, so it skips the
    # raw-scene size clamps — the road every compiler scene actually takes
    _check_director(SHAPES, compiled=True)


# ── road 2: the compiler's per-segment emission ──────────────────────────

def _compile(raw: dict):
    plan = parse_visual_plan(raw)
    assert plan is not None
    return compile_plan(plan, _NARR, all_segments=list(_NARR), skip_hold=set())


def _plan_raw(shapes: list[Shape], steps: list[dict]) -> dict:
    els = [copy.deepcopy(e) for e in _BASE]
    for s in shapes:
        els += [copy.deepcopy(e) for e in s.elements]
    return {"chapters": [{"concept": "cell", "assets": dict(_ASSETS),
                          "elements": els, "steps": steps}]}


def _check_compiler(shapes: list[Shape]) -> None:
    extra = [copy.deepcopy(a) for s in shapes for a in s.actions]
    raw = _plan_raw(shapes, [
        {"segment": 3, "decision": "NEW_VISUAL",
         "actions": [dict(a) for a in _BASE_ACTIONS]},
        {"segment": 4, "decision": "EXTEND", "actions": extra}])
    scenes, _, report = _compile(raw)
    names = " + ".join(s.name for s in shapes)
    assert set(scenes) >= {"s003", "s004"}, report
    seen: set[str] = set()
    for sid, scene in scenes.items():
        where = f"compiler {sid} [{names}]"
        _assert_sealed(scene, where)
        _assert_kept(scene, shapes, where)
        seen |= {e["id"] for e in scene["elements"]}
        assert parse_scene_response(copy.deepcopy(scene),
                                    _NARR[sid]) is not None, where
    for s in shapes:
        for k in s.survivors:
            assert k in seen, f"compiler: {k} never reached a board ({s.name})"


@pytest.mark.parametrize("name", [s.name for s in SHAPES])
def test_compiler_seals_every_shape(name):
    _check_compiler([_BY_NAME[name]])


def test_compiler_seals_all_shapes_at_once():
    _check_compiler(SHAPES)


# ── road 3: the HOLD / carry-out board ───────────────────────────────────
#
# board_now() never called the guard: it sealed the board with a pass of its
# own, single-pass, so an arrow it dropped stayed named by a group, a group
# it emptied stayed named by an arrow and by a text chain, and the HOLD
# scene the student would have seen was thrown away whole. Here the dangle
# is made the way a real board makes it — the anchor's target is ERASED, so
# it is off the board with no planned point to flatten to.

_RING = {"id": "ring", "type": "shape", "shape": "ellipse",
         "center": [640, 360], "rx": 40, "ry": 40}


def _board_shapes() -> list[Shape]:
    out: list[Shape] = []

    def vanishing(n: int) -> dict:
        return {"id": f"arr{n}", "type": "arrow", "tail": [100, 100],
                "head": {"el": "ring", "edge": "center"}}

    n = 1
    out.append(Shape("arrow onto the erased shape",
                     [vanishing(n)], [{"verb": "draw", "target": f"arr{n}"}],
                     gone=(f"arr{n}",)))
    n = 2
    out.append(Shape(
        "group holding the arrow that leaves the board",
        [vanishing(n), {"id": f"grp{n}", "type": "group",
                        "children": [f"arr{n}"]}],
        [{"verb": "draw", "target": f"arr{n}"},
         {"verb": "draw", "target": f"grp{n}"}],
        gone=(f"arr{n}", f"grp{n}")))
    n = 3
    out.append(Shape(
        "nested groups over an arrow that leaves the board",
        [vanishing(n), {"id": f"grp{n}", "type": "group",
                        "children": [f"arr{n}"]},
         {"id": f"top{n}", "type": "group", "children": [f"grp{n}"]}],
        [{"verb": "draw", "target": f"arr{n}"},
         {"verb": "draw", "target": f"top{n}"}],
        gone=(f"arr{n}", f"grp{n}", f"top{n}")))
    n = 4
    out.append(Shape(
        "text chained after a group the seal empties",
        [vanishing(n), {"id": f"grp{n}", "type": "group",
                        "children": [f"arr{n}"]},
         {"id": f"cap{n}", "type": "text", "text": "Caption", "at": [95, 300],
          "after": {"el": f"grp{n}", "gap": 8}}],
        [{"verb": "draw", "target": f"arr{n}"},
         {"verb": "draw", "target": f"grp{n}"},
         {"verb": "write", "target": f"cap{n}"}],
        survivors=(f"cap{n}",), gone=(f"arr{n}", f"grp{n}")))
    n = 5
    out.append(Shape(
        "arrow anchored to a group the seal empties",
        [vanishing(n), {"id": f"grp{n}", "type": "group",
                        "children": [f"arr{n}"]},
         {"id": f"ptr{n}", "type": "arrow", "tail": {"el": "lbl_alpha"},
          "head": {"el": f"grp{n}"}}],
        [{"verb": "draw", "target": f"arr{n}"},
         {"verb": "draw", "target": f"grp{n}"},
         {"verb": "draw", "target": f"ptr{n}"}],
        gone=(f"arr{n}", f"grp{n}", f"ptr{n}")))
    return out


BOARD_SHAPES = _board_shapes()
_BOARD_BY_NAME = {s.name: s for s in BOARD_SHAPES}


def _check_hold(shapes: list[Shape]) -> None:
    els = [copy.deepcopy(e) for e in _BASE] + [copy.deepcopy(_RING)]
    draws = [dict(a) for a in _BASE_ACTIONS] + [{"verb": "draw",
                                                 "target": "ring"}]
    for s in shapes:
        els += [copy.deepcopy(e) for e in s.elements]
        draws += [copy.deepcopy(a) for a in s.actions]
    raw = {"chapters": [{"concept": "cell", "assets": dict(_ASSETS),
                         "elements": els, "steps": [
        {"segment": 3, "decision": "NEW_VISUAL", "actions": draws},
        {"segment": 4, "decision": "EXTEND",
         "actions": [{"verb": "erase", "target": "ring"}]},
        {"segment": 6, "decision": "CONTINUE",
         "actions": [{"verb": "circle", "target": "cell"}]}]}]}
    scenes, _, report = _compile(raw)
    names = " + ".join(s.name for s in shapes)
    assert any("SEGMENT s005 | chapter: cell | decision: HOLD" in ln
               for ln in report), report
    for sid, scene in scenes.items():
        where = f"hold {sid} [{names}]"
        _assert_sealed(scene, where)
        # the shapes are legitimate boards until the erase: only the HELD
        # scene (and everything after it) is asked to have lost them
        _assert_kept(scene, shapes, where, check_gone=sid >= "s005")
        assert parse_scene_response(copy.deepcopy(scene),
                                    _NARR[sid]) is not None, where
    held = {e["id"] for e in scenes["s005"]["elements"]}
    for s in shapes:
        for k in s.survivors:
            assert k in held, f"hold: {k} left the board with the dangle"


@pytest.mark.parametrize("name", [s.name for s in BOARD_SHAPES])
def test_hold_board_seals_every_shape(name):
    _check_hold([_BOARD_BY_NAME[name]])


def test_hold_board_seals_all_shapes_at_once():
    _check_hold(BOARD_SHAPES)


def test_the_hold_seal_still_says_what_it_lost():
    """The report vocabulary is the acceptance report's only record of these
    losses (validate.py parses these very words), so the one pass keeps each
    call site's wording."""
    _, _, report = _compile({"chapters": [{
        "concept": "cell", "assets": dict(_ASSETS),
        "elements": [copy.deepcopy(e) for e in _BASE] + [copy.deepcopy(_RING),
            {"id": "arr", "type": "arrow", "tail": [100, 100],
             "head": {"el": "ring", "edge": "center"}},
            {"id": "grp", "type": "group", "children": ["arr"]},
            {"id": "cap", "type": "text", "text": "Caption", "at": [95, 300],
             "after": {"el": "grp", "gap": 8}}],
        "steps": [
            {"segment": 3, "decision": "NEW_VISUAL",
             "actions": [dict(a) for a in _BASE_ACTIONS]
             + [{"verb": "draw", "target": "ring"},
                {"verb": "draw", "target": "arr"},
                {"verb": "draw", "target": "grp"},
                {"verb": "write", "target": "cap"}]},
            {"segment": 4, "decision": "EXTEND",
             "actions": [{"verb": "erase", "target": "ring"}]},
            {"segment": 6, "decision": "CONTINUE",
             "actions": [{"verb": "circle", "target": "cell"}]}]}]})
    assert ("SEGMENT s005 | DROPPED arrow arr (head anchor 'ring' is not on "
            "the board)") in report
    assert ("SEGMENT s005 | DROPPED group grp (every child left the board)"
            ) in report
    assert ("SEGMENT s005 | UNCHAINED text cap (after 'grp' is not on the "
            "board)") in report


def test_the_carry_out_board_crosses_the_boundary_sealed():
    """The exported board becomes the NEXT chapter's fade-out, renamed
    prev__*. A dangling ref that crosses that boundary fails the boundary
    scene — and it is reported as LEFT BEHIND, never DROPPED, so the arrow
    accounting can tell a boundary from a loss."""
    raw = {"chapters": [
        {"concept": "cell", "assets": dict(_ASSETS),
         "elements": [copy.deepcopy(e) for e in _BASE] + [copy.deepcopy(_RING),
             {"id": "arr", "type": "arrow", "tail": [100, 100],
              "head": {"el": "ring", "edge": "center"}},
             {"id": "grp", "type": "group", "children": ["arr"]},
             {"id": "cap", "type": "text", "text": "Caption", "at": [95, 300],
              "after": {"el": "grp", "gap": 8}}],
         "steps": [
             {"segment": 3, "decision": "NEW_VISUAL",
              "actions": [dict(a) for a in _BASE_ACTIONS]
              + [{"verb": "draw", "target": "ring"},
                 {"verb": "draw", "target": "arr"},
                 {"verb": "draw", "target": "grp"},
                 {"verb": "write", "target": "cap"}]},
             {"segment": 4, "decision": "EXTEND",
              "actions": [{"verb": "erase", "target": "ring"}]}]},
        {"concept": "leaf", "transition": "clear_and_redraw",
         "assets": {"leaf": "A leaf diagram."},
         "elements": [{"id": "leaf", "type": "illustration", "asset": "leaf",
                       "at": [640, 360]}],
         "steps": [{"segment": 5, "decision": "NEW_VISUAL",
                    "actions": [{"verb": "draw", "target": "leaf"}]}]}]}
    scenes, _, report = _compile(raw)
    assert ("CHAPTER cell | CARRY-OUT | LEFT BEHIND arrow arr (head anchor "
            "'ring' is not on the board)") in report
    assert ("CHAPTER cell | CARRY-OUT | LEFT BEHIND group grp (every child "
            "left the board)") in report
    for sid, scene in scenes.items():
        _assert_sealed(scene, f"carry-out {sid}")
        assert parse_scene_response(copy.deepcopy(scene),
                                    _NARR[sid]) is not None, sid
    # the boundary scene carries the previous board, renamed and sealed
    boundary = {e["id"] for e in scenes["s005"]["elements"]}
    assert "prev__cell" in boundary and "prev__arr" not in boundary


def test_a_raising_pass_leaves_the_scene_exactly_as_it_was():
    """The guard may never be the loss. It writes nothing back until it has
    finished, so a bug inside it costs the scene its sanitisation and nothing
    else — and the compiler's chapter pass works on a COPY for the same
    reason, since it mutates a roster everything downstream holds."""
    import spike.scene_engine.anchors as _a

    scene = _director_raw([_BY_NAME["arrow to an unknown element"]])
    before = copy.deepcopy(scene)
    orig = _a._sanitize_roster

    def _boom(roster, *a, **k):
        orig(roster, *a, **k)          # do the work...
        raise KeyError("arr1")         # ...then die before it is written back

    _a._sanitize_roster = _boom
    try:
        with pytest.raises(KeyError):
            _a.sanitize_scene(scene)
    finally:
        _a._sanitize_roster = orig
    assert scene == before


# ── the silent repair must still be written back ────────────────────────────
class TestASilentRepairIsWrittenBack:
    """Pruning a dropped child out of a group emits no note. Gating the
    write-back on notes alone computed that prune and discarded it, so the
    group went on naming an element the scene no longer had and the schema
    rejected the whole board (adversarial pass, 2026-09-05)."""

    def _scene(self):
        # `grp` names a child that the roster does not contain: no arrow is
        # dropped here, so the pass has nothing to say — it only prunes.
        return {
            "id": "s001", "narration": "look at the cell",
            "elements": [
                {"id": "cell", "type": "illustration", "asset": "plant_cell",
                 "at": [600, 380], "scale": 2.0},
                {"id": "grp", "type": "group", "children": ["cell", "ghost"]},
            ],
            "actions": [{"verb": "draw", "target": "cell", "duration": 1.0}],
        }

    def test_the_group_loses_the_child_the_scene_does_not_have(self):
        from spike.scene_engine.anchors import sanitize_scene
        scene = self._scene()
        sanitize_scene(scene, "cell", part_names=[], aliases={})
        grp = next(e for e in scene["elements"] if e.get("id") == "grp")
        assert "ghost" not in (grp.get("children") or []), grp
        assert "cell" in (grp.get("children") or []), "the innocent child was lost"

    def test_the_scene_validates_afterwards(self):
        from spike.scene_engine.anchors import sanitize_scene
        from spike.scene_engine.schema import Scene
        scene = self._scene()
        sanitize_scene(scene, "cell", part_names=[], aliases={})
        Scene.model_validate(scene)   # must not raise

    def test_a_scene_needing_nothing_is_left_alone(self):
        from spike.scene_engine.anchors import sanitize_scene
        scene = self._scene()
        scene["elements"][1]["children"] = ["cell"]
        before = [dict(e) for e in scene["elements"]]
        notes = sanitize_scene(scene, "cell", part_names=[], aliases={})
        assert notes == []
        assert [dict(e) for e in scene["elements"]] == before
