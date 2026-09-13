"""Three things the founder saw in the first published catalogue lessons
(Heat Transfer, 2026-09-13), one mechanism each, all subject-blind.

1. Blank boards. At a chapter boundary the old board fades out by ~1 s and
   the new board's first `draw` waits for the director's cue phrase — 45 s of
   speech bubbles before the bathtub, then 12–20 s blanks at three later
   chapter openings. The compiler now marks a scene that opens on an empty
   board; the timeline pulls its first real drawing to FIRST_INK_SECS.
2. A sketch drawn over labels. Margin slots are chosen at plan time against
   the illustrations only; the labels do not exist yet. The renderer moves a
   sketch off any text it lands on.
3. A leader line from nowhere. The director gave two thermos arrows a bare
   tail point and no label anywhere. The compiler synthesizes the label the
   arrow names and points the tail at it.

Offline: authored vector assets, fake raster, no ffmpeg, no network.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from spike.scene_engine import timing as T
from spike.scene_engine.continuity import compile_plan, parse_visual_plan
from spike.scene_engine.schema import Scene


# ── 1. the first ink on a fresh board ────────────────────────────────────────

def _scene(actions, fresh: bool | None):
    d = {
        "id": "z", "narration": "we talk for a while and then draw the picture",
        "elements": [
            {"id": "prev__old", "type": "text", "text": "old", "at": [200, 200]},
            {"id": "prev__board", "type": "group", "children": ["prev__old"]},
            {"id": "pic", "type": "illustration", "asset": "plant_cell", "at": [600, 380]},
            {"id": "sk_z_0", "type": "illustration", "asset": "membrane_section",
             "at": [136, 142], "scale": 0.3, "hud": True},
            {"id": "ttl", "type": "text", "text": "Title", "at": [600, 60], "role": "title"},
        ],
        "actions": actions,
    }
    if fresh is not None:
        d["fresh_board"] = fresh
    return Scene.model_validate(d)


def _board(scene, audio):
    return [t for t in T.compile_timeline(scene, audio) if not T._is_caption(t.action)]


class TestTheFirstInkOnAFreshBoard:
    def test_a_late_cued_draw_is_pulled_to_the_opening(self):
        acts = [
            {"verb": "fade", "target": "prev__board", "to": 0.0, "duration": 0.9, "at": {"sec": 0.15}},
            {"verb": "camera_reset", "duration": 0.9, "at": {"sec": 0.15}},
            {"verb": "draw", "target": "pic", "at": {"sec": 14.0}},
        ]
        tl = _board(_scene(acts, fresh=True), 30.0)
        draw = next(t for t in tl if t.action.verb == "draw" and t.action.target == "pic")
        assert draw.start == pytest.approx(T.FIRST_INK_SECS)
        assert T.take_ink_pulls() == [f"pic: 14.0s -> {T.FIRST_INK_SECS:.1f}s"]

    def test_a_carried_board_keeps_its_cue(self):
        """EXTEND / FOCUS scenes are not blank: the picture is already there."""
        acts = [{"verb": "draw", "target": "pic", "at": {"sec": 14.0}}]
        tl = _board(_scene(acts, fresh=False), 30.0)
        assert tl[0].start == pytest.approx(14.0)
        assert T.take_ink_pulls() == []

    def test_an_unmarked_scene_behaves_as_before(self):
        acts = [{"verb": "draw", "target": "pic", "at": {"sec": 14.0}}]
        tl = _board(_scene(acts, fresh=None), 30.0)
        assert tl[0].start == pytest.approx(14.0)

    def test_an_early_draw_is_left_alone(self):
        acts = [{"verb": "draw", "target": "pic", "at": {"sec": 0.8}}]
        tl = _board(_scene(acts, fresh=True), 30.0)
        assert tl[0].start == pytest.approx(0.8)
        assert T.take_ink_pulls() == []

    def test_a_title_or_a_corner_sketch_does_not_count_as_ink(self):
        """A board with a title and a doodle in the corner still reads blank."""
        acts = [
            {"verb": "write", "target": "ttl", "at": {"sec": 0.3}},
            {"verb": "draw", "target": "sk_z_0", "at": {"sec": 0.6}},
            {"verb": "draw", "target": "pic", "at": {"sec": 14.0}},
        ]
        tl = _board(_scene(acts, fresh=True), 30.0)
        by = {t.action.target: t.start for t in tl}
        assert by["ttl"] == pytest.approx(0.3) and by["sk_z_0"] == pytest.approx(0.6)
        assert by["pic"] == pytest.approx(T.FIRST_INK_SECS)

    def test_the_fading_old_board_is_not_the_first_ink(self):
        acts = [
            {"verb": "draw", "target": "prev__old", "at": {"sec": 0.2}},
            {"verb": "draw", "target": "pic", "at": {"sec": 14.0}},
        ]
        tl = _board(_scene(acts, fresh=True), 30.0)
        assert next(t for t in tl if t.action.target == "pic").start == pytest.approx(T.FIRST_INK_SECS)

    def test_only_the_first_drawing_is_pulled_later_cues_hold(self):
        acts = [
            {"verb": "draw", "target": "pic", "at": {"sec": 14.0}},
            {"verb": "write", "target": "ttl", "at": {"sec": 20.0}},
        ]
        tl = _board(_scene(acts, fresh=True), 30.0)
        assert tl[0].start == pytest.approx(T.FIRST_INK_SECS)
        assert tl[1].start == pytest.approx(20.0), "a later cue is an anchor and does not move"


def _plan():
    return parse_visual_plan({
        "chapters": [
            {"concept": "plant_cell_structure",
             "assets": {"plant_cell": "a cell"},
             "elements": [
                 {"id": "cell", "type": "illustration", "asset": "plant_cell",
                  "at": [600, 380], "scale": 0.9},
                 {"id": "lbl", "type": "text", "text": "Cell wall", "at": [70, 150]},
             ],
             "steps": [
                 {"segment": 1, "decision": "NEW_VISUAL",
                  "actions": [{"verb": "draw", "target": "cell", "layers": ["wall"]}]},
                 {"segment": 2, "decision": "EXTEND",
                  "actions": [{"verb": "write", "target": "lbl"}]},
                 {"segment": 3, "decision": "FOCUS",
                  "actions": [{"verb": "zoom", "target": "lbl", "scale": 1.5}]},
             ]},
            {"concept": "next_topic",
             "elements": [{"id": "mem", "type": "illustration",
                           "asset": "membrane_section", "at": [640, 360]}],
             "steps": [{"segment": 4, "decision": "CLEAR_AND_REDRAW",
                        "actions": [{"verb": "draw", "target": "mem"}]}]},
        ]})


_NARR = {"s001": "a plant cell", "s002": "the wall protects",
         "s003": "focus here", "s004": "now the membrane"}


class TestTheCompilerMarksFreshBoards:
    def test_first_chapter_and_boundary_are_fresh_the_rest_are_not(self):
        scenes, _, _ = compile_plan(_plan(), _NARR)
        assert scenes["s001"]["fresh_board"] is True, "the lesson opens on an empty board"
        assert scenes["s002"]["fresh_board"] is False, "EXTEND carries the picture"
        assert scenes["s003"]["fresh_board"] is False, "FOCUS carries the picture"
        assert scenes["s004"]["fresh_board"] is True, "the old board is fading out"

    def test_the_flag_survives_schema_validation(self):
        scenes, _, _ = compile_plan(_plan(), _NARR)
        sc = Scene.model_validate(scenes["s004"])
        assert getattr(sc, "fresh_board", None) is True


# ── 3. an arrow with no label anywhere ───────────────────────────────────────

class TestAnArrowWithoutALabelGetsOne:
    def _plan(self, with_label: bool):
        els = [{"id": "cell", "type": "illustration", "asset": "plant_cell",
                "at": [600, 380], "scale": 0.9},
               {"id": "arr_wall", "type": "arrow",
                "head": {"el": "cell", "layer": "wall", "edge": "center"},
                "tail": [215.0, 140.0]}]
        acts = [{"verb": "draw", "target": "cell"},
                {"verb": "draw", "target": "arr_wall",
                 "at": {"phrase": "the wall"}}]
        if with_label:
            els.append({"id": "lbl_wall", "type": "text", "text": "Cell wall",
                        "at": [95, 140], "role": "label"})
            acts.insert(1, {"verb": "write", "target": "lbl_wall"})
        # the prompt tail is what gives a chapter its part names — every
        # real plan carries it; without it there is nothing to anchor to
        return parse_visual_plan({"chapters": [
            {"concept": "plant_cell_structure",
             "assets": {"plant_cell": "a plant cell. Name the layer groups exactly: wall, nucleus"},
             "elements": els,
             "steps": [{"segment": 1, "decision": "NEW_VISUAL", "actions": acts}]}]})

    # The narration must NOT name the part: when it does, the older pass
    # already synthesizes a label from the narration and this arm never
    # runs. The live failure was exactly this — "double-walled container
    # with a vacuum" never said "vacuum gap".
    _NARR = {"s001": "look at the outer layer of this cell"}

    def test_the_label_is_synthesized_written_first_and_the_tail_points_at_it(self):
        scenes, _, report = compile_plan(self._plan(with_label=False), self._NARR)
        sc = scenes["s001"]
        ids = {e["id"]: e for e in sc["elements"]}
        assert "lbl_auto_wall" in ids, report
        lbl = ids["lbl_auto_wall"]
        assert lbl["type"] == "text" and lbl["role"] == "label" and lbl["text"] == "Wall"
        arrow = ids["arr_wall"]
        assert arrow["tail"]["el"] == "lbl_auto_wall", "the line now starts at its words"
        verbs = [(a["verb"], a["target"]) for a in sc["actions"]]
        assert verbs.index(("write", "lbl_auto_wall")) < verbs.index(("draw", "arr_wall"))
        write = next(a for a in sc["actions"] if a["target"] == "lbl_auto_wall")
        assert write.get("at") == {"phrase": "the wall"}, "the words land with the line"
        assert any("SYNTHESIZED lbl_auto_wall (label for arr_wall)" in l for l in report)

    def test_an_arrow_that_already_has_a_label_is_not_given_another(self):
        scenes, _, report = compile_plan(self._plan(with_label=True), self._NARR)
        ids = {e["id"] for e in scenes["s001"]["elements"]}
        assert "lbl_auto_wall" not in ids
        assert not any("label for arr_wall" in l for l in report)

    def test_a_narration_synthesized_label_is_also_written_before_its_arrow(self):
        """The older pass appended the label's write to the END of the step,
        after the arrow's draw — a leader from an empty spot until the words
        caught up. Same step, the words go first."""
        scenes, _, report = compile_plan(self._plan(with_label=False),
                                         {"s001": "look at the wall of the cell"})
        sc = scenes["s001"]
        assert any("SYNTHESIZED lbl_auto_wall (narration names" in l for l in report)
        verbs = [(a["verb"], a["target"]) for a in sc["actions"]]
        assert verbs.index(("write", "lbl_auto_wall")) < verbs.index(("draw", "arr_wall"))

    def test_the_compiled_scene_is_schema_valid(self):
        scenes, _, _ = compile_plan(self._plan(with_label=False), self._NARR)
        Scene.model_validate(scenes["s001"])


# ── 2. a sketch never covers a label ─────────────────────────────────────────

def _disc_asset():
    from spike.scene_engine.raster_assets import RasterAsset
    from spike.scene_engine.trace import drawing_order
    ink = Image.new("RGBA", (200, 150), (0, 0, 0, 0))
    d = ImageDraw.Draw(ink)
    d.ellipse([30, 30, 170, 120], outline=(20, 20, 20, 255), width=6)
    trace = drawing_order(np.asarray(ink.getchannel("A")))
    return RasterAsset("disc", ink, trace, 4.0, 2.0)


class TestASketchNeverCoversALabel:
    def _renderer(self, label_at, sketch_at=(1114, 142)):
        from spike.scene_engine.render import SceneRenderer
        scene = Scene.model_validate({
            "id": "z", "narration": "temperature is the average energy of the particles",
            "elements": [
                # the production root fits 700x520 about (600,380); this
                # disc at 2.0 is about that size, leaving the margins free
                {"id": "big", "type": "illustration", "asset": "disc", "at": [600, 400], "scale": 2.0},
                {"id": "lbl_temp", "type": "text", "text": "Temperature: Average Energy",
                 "at": list(label_at), "role": "label", "size": 26},
                {"id": "sk_z_0", "type": "illustration", "asset": "disc",
                 "at": list(sketch_at), "scale": 0.4, "hud": True},
            ],
            "actions": [
                {"verb": "draw", "target": "big", "duration": 1.0},
                {"verb": "write", "target": "lbl_temp", "duration": 0.8},
                {"verb": "draw", "target": "sk_z_0", "duration": 0.8},
            ],
        })
        asset = _disc_asset()
        r = SceneRenderer(scene, asset_resolver=lambda k: ("raster", asset))
        r.compile(6.0)
        return r

    @staticmethod
    def _hits(a, b):
        return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]

    def test_a_sketch_on_a_label_moves_to_a_free_slot(self):
        r = self._renderer(label_at=(980, 130))       # the director's label, top-right
        sk, lbl = r.bound["sk_z_0"], r.bound["lbl_temp"]
        assert not self._hits(sk.box, lbl.box), (sk.box, lbl.box)
        assert any(w.startswith("SKETCH_MOVED_OFF_TEXT sk_z_0") for w in r._warned), r._warned
        assert sk.raster is not None and sk.raster.at[0] < 400, "it took the other margin slot"
        # the box and the raster centre moved TOGETHER — the frame loop reads both
        assert (sk.box[0] + sk.box[2]) / 2 == pytest.approx(sk.raster.at[0], abs=1.0)

    def test_a_sketch_clear_of_every_label_stays_put(self):
        r = self._renderer(label_at=(80, 620))
        sk = r.bound["sk_z_0"]
        assert sk.raster.at == (1114, 142)
        assert not any(w.startswith("SKETCH_") for w in r._warned)

    def test_the_label_keeps_its_column_the_sketch_is_what_yields(self):
        """The label relayout owns where labels go (right column first); the
        sketch pass must not undo that. The words stay in their column and
        the doodle is the thing that leaves."""
        r = self._renderer(label_at=(980, 130))
        lbl, sk = r.bound["lbl_temp"], r.bound["sk_z_0"]
        assert lbl.box[0] >= 900, "the label is still in the right-hand column"
        assert sk.box[2] < 400, "the sketch went to the far side"

    def test_a_board_with_no_free_slot_says_so_instead_of_covering_the_words(self):
        from spike.scene_engine.render import SceneRenderer
        scene = Scene.model_validate({
            "id": "z", "narration": "two labels, two corners",
            "elements": [
                {"id": "big", "type": "illustration", "asset": "disc", "at": [600, 400], "scale": 2.0},
                {"id": "l1", "type": "text", "text": "Temperature: Average Energy", "at": [980, 130], "role": "label", "size": 26},
                {"id": "l2", "type": "text", "text": "Heat: Total Energy", "at": [40, 130], "role": "term", "size": 26},  # a TERM, not a label: the label relayout leaves it in its corner
                {"id": "sk_z_0", "type": "illustration", "asset": "disc", "at": [1114, 142], "scale": 0.4, "hud": True},
            ],
            "actions": [{"verb": "draw", "target": "big"}, {"verb": "write", "target": "l1"},
                        {"verb": "write", "target": "l2"}, {"verb": "draw", "target": "sk_z_0"}],
        })
        r = SceneRenderer(scene, asset_resolver=lambda k: ("raster", _disc_asset()))
        r.compile(6.0)
        assert any(w.startswith("SKETCH_OVER_TEXT sk_z_0") for w in r._warned), r._warned
