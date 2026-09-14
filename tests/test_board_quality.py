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

    def test_a_board_with_no_free_slot_drops_the_sketch_instead_of_covering_the_words(self):
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
        assert any(w.startswith("SKETCH_DROPPED_OVER_TEXT sk_z_0") for w in r._warned), r._warned
        sk = r.bound["sk_z_0"]
        assert sk.raster is None and sk.layers == [] and r._flat["sk_z_0"] == [],             "dropped means nothing is drawn — not drawn somewhere else"
        assert sk.box[0] == sk.box[2] and sk.box[1] == sk.box[3]
        # and the frame loop survives a draw action on the dropped sketch
        last = None
        for last in r.frames(6.0):
            pass
        assert last is not None


# ── 1b. an opening step that draws no picture ────────────────────────────────

class TestAnEmptyOpeningStepDrawsThePicture:
    """Heat Transfer, third render: the director's first step introduced
    nothing, so the compiler dropped it as an empty scene and the segment
    played 50 s over a title and speech bubbles. The chapter HAD a picture —
    it was drawn in step two. The teacher now starts with it."""

    def _plan(self, first_actions):
        return parse_visual_plan({"chapters": [
            {"concept": "temperature_and_thermal_energy",
             "assets": {"plant_cell": "a plant cell. Name the layer groups exactly: wall, nucleus"},
             "elements": [
                 {"id": "cell", "type": "illustration", "asset": "plant_cell", "at": [600, 380]},
                 {"id": "ttl", "type": "text", "text": "Hot Spoon", "at": [600, 60], "role": "title"},
             ],
             "steps": [
                 {"segment": 1, "decision": "NEW_VISUAL", "actions": first_actions},
                 {"segment": 2, "decision": "EXTEND",
                  "actions": [{"verb": "draw", "target": "cell"}]},
             ]}]})

    _NARR = {"s001": "have you ever noticed a hot spoon", "s002": "now look at the cell"}

    def test_an_empty_first_step_gets_the_chapter_s_picture(self):
        scenes, _, report = compile_plan(self._plan([]), self._NARR)
        assert "s001" in scenes, report
        verbs = [(a["verb"], a["target"]) for a in scenes["s001"]["actions"]]
        assert ("draw", "cell") in verbs
        assert any("OPENING STEP DRAWS cell" in l for l in report)
        assert not any("SKIPPED empty scene" in l for l in report)

    def test_a_first_step_that_only_writes_the_title_also_gets_it(self):
        scenes, _, report = compile_plan(self._plan([{"verb": "write", "target": "ttl"}]),
                                         self._NARR)
        verbs = [(a["verb"], a["target"]) for a in scenes["s001"]["actions"]]
        assert verbs.index(("draw", "cell")) < verbs.index(("write", "ttl")), \
            "the picture comes first; the title follows"

    def test_a_first_step_that_already_draws_is_left_alone(self):
        scenes, _, report = compile_plan(self._plan([{"verb": "draw", "target": "cell"}]),
                                         self._NARR)
        draws = [a for a in scenes["s001"]["actions"] if a["verb"] == "draw" and a["target"] == "cell"]
        assert len(draws) == 1
        assert not any("OPENING STEP DRAWS" in l for l in report)

    def test_the_second_step_still_compiles_and_validates(self):
        scenes, _, _ = compile_plan(self._plan([]), self._NARR)
        for sid in ("s001", "s002"):
            Scene.model_validate(scenes[sid])
        assert scenes["s001"]["fresh_board"] is True and scenes["s002"]["fresh_board"] is False


# ── 4. a zoom never crops what it is pointing at ─────────────────────────────

class TestAZoomNeverCropsItsTarget:
    """Structure of the Atom, 2026-09-13: `zoom 1.6` on a diagram that already
    filled the board, held through two CONTINUE segments — 50 s of the
    gold-foil picture running off every edge. The renderer measures the
    target's ink and caps the zoom at what still fits."""

    def _camera_end_scale(self, big_scale: float, zoom: float):
        from spike.scene_engine.render import SceneRenderer
        scene = Scene.model_validate({
            "id": "z", "narration": "we zoom in on the nucleus of the gold atom",
            "elements": [
                {"id": "pic", "type": "illustration", "asset": "disc", "at": [600, 380], "scale": big_scale},
            ],
            "actions": [
                {"verb": "draw", "target": "pic", "duration": 1.0},
                {"verb": "zoom", "target": "pic", "scale": zoom, "duration": 1.0},
            ],
        })
        r = SceneRenderer(scene, asset_resolver=lambda k: ("raster", _disc_asset()))
        r.compile(8.0)
        return r, r.cam.state_at(8.0).scale

    def test_a_zoom_on_a_board_filling_picture_is_clamped(self):
        r, s = self._camera_end_scale(big_scale=3.0, zoom=1.6)
        assert s < 1.6, "the zoom did not run at the director's 1.6x"
        assert any(w.startswith("ZOOM_CLAMPED pic 1.60->") for w in r._warned), r._warned

    def test_the_clamped_target_still_fits_the_frame(self):
        from spike.scene_engine.schema import WORLD_H, WORLD_W
        r, s = self._camera_end_scale(big_scale=3.0, zoom=1.6)
        ink = r._ink_box(r.bound["pic"]) or r.bound["pic"].box
        assert (ink[2] - ink[0]) * s <= WORLD_W and (ink[3] - ink[1]) * s <= WORLD_H

    def test_a_zoom_on_a_small_picture_runs_as_directed(self):
        r, s = self._camera_end_scale(big_scale=0.8, zoom=1.6)
        assert s == pytest.approx(1.6)
        assert not any(w.startswith("ZOOM_CLAMPED") for w in r._warned)

    def test_the_camera_obeys_a_cap_and_never_zooms_out(self):
        from spike.scene_engine.camera import CameraTrack
        from spike.scene_engine.schema import Scene as _S
        from spike.scene_engine import timing as T
        scene = _S.model_validate({
            "id": "c", "narration": "x",
            "elements": [{"id": "t", "type": "text", "text": "x", "at": [600, 380]}],
            "actions": [{"verb": "zoom", "target": "t", "scale": 2.0, "duration": 1.0}],
        })
        tl = T.compile_timeline(scene, 5.0)
        i = next(k for k, ta in enumerate(tl) if ta.action.verb == "zoom")
        assert CameraTrack(tl, {i: (600, 380)}, scale_cap={i: 1.3}).state_at(5.0).scale == pytest.approx(1.3)
        assert CameraTrack(tl, {i: (600, 380)}, scale_cap={i: 0.5}).state_at(5.0).scale == pytest.approx(1.0), \
            "a cap below 1 means no zoom, never a zoom OUT"
        assert CameraTrack(tl, {i: (600, 380)}).state_at(5.0).scale == pytest.approx(2.0)


# ── 5. the director is told what an illustration is ──────────────────────────

class TestTheDirectorIsToldNotToAskForTables:
    def test_the_prompt_forbids_tables_as_assets(self):
        import inspect

        from spike.scene_engine import director
        src = inspect.getsource(director)
        assert "never a\n  table, chart, grid" in src or "never a table, chart, grid" in src.replace("\n  ", " ")


# ── 6. the lesson never opens on a board with nothing drawn ──────────────────

class TestATextOnlyOpeningChapterIsFoldedIntoTheNext:
    """Structure of the Atom, 2026-09-13: the director's first chapter was a
    title and two lines, no illustration — 36 s of words on a blank board
    before the atom in chapter two. The opening chapter folds into the next
    one, whose picture the opening-step injection then draws on step one."""

    _NARR = {"s001": "what is everything made of", "s002": "every object is atoms",
             "s003": "look at the atom", "s004": "the nucleus sits in the middle"}

    def _plan(self, first_has_picture=False, second_has_picture=True,
              collide=False):
        first_els = [{"id": "ttl", "type": "text", "text": "Matter", "at": [600, 60],
                      "role": "title"},
                     {"id": "l1", "type": "text", "text": "everything is atoms",
                      "at": [600, 300], "role": "term"}]
        if first_has_picture:
            first_els.append({"id": "rod", "type": "illustration",
                              "asset": "metal_rod", "at": [600, 380]})
        lbl = "ttl" if collide else "lbl_n"
        second_els = [{"id": "atom", "type": "illustration", "asset": "atom",
                       "at": [600, 380]},
                      {"id": lbl, "type": "text", "text": "nucleus",
                       "at": [600, 60], "role": "label"}]
        if not second_has_picture:
            second_els = second_els[1:]
        first_actions = [{"verb": "write", "target": "ttl"}]
        if first_has_picture:
            first_actions.append({"verb": "draw", "target": "rod"})
        third_actions = ([{"verb": "draw", "target": "atom"}] if second_has_picture
                         else [{"verb": "write", "target": lbl}])
        return parse_visual_plan({"chapters": [
            {"concept": "matter",
             "assets": {"metal_rod": "a metal rod"} if first_has_picture else {},
             "elements": first_els,
             "steps": [
                 {"segment": 1, "decision": "NEW_VISUAL", "actions": first_actions},
                 {"segment": 2, "decision": "EXTEND",
                  "actions": [{"verb": "write", "target": "l1"}]},
             ]},
            {"concept": "the_atom", "transition": "clear_and_redraw",
             "assets": {"atom": "an atom. Name the layer groups exactly: nucleus, shell"},
             "elements": second_els,
             "steps": [
                 {"segment": 3, "decision": "NEW_VISUAL", "actions": third_actions},
                 {"segment": 4, "decision": "EXTEND",
                  "actions": [{"verb": "write", "target": lbl}]},
             ]},
        ]})

    def test_the_first_segment_draws_the_next_chapter_s_picture(self):
        plan = self._plan()
        scenes, _, report = compile_plan(plan, self._NARR)
        assert any("| FOLDED into the_atom" in r for r in report), report
        s1 = scenes["s001"]
        verbs = [(a["verb"], a["target"]) for a in s1["actions"]]
        assert ("draw", "atom") in verbs, verbs
        assert ("write", "ttl") in verbs, "the title is still written"
        assert verbs.index(("draw", "atom")) < verbs.index(("write", "ttl"))
        assert len(plan.chapters) == 1

    def test_the_title_and_the_picture_share_one_board(self):
        scenes, _, _ = compile_plan(self._plan(), self._NARR)
        ids = {e["id"] for e in scenes["s003"]["elements"]}
        assert {"atom", "ttl", "l1"} <= ids, ids
        assert not any(i.startswith("prev__") for i in ids), "no wipe between them"

    def test_an_opening_chapter_with_its_own_picture_is_left_alone(self):
        plan = self._plan(first_has_picture=True)
        _, _, report = compile_plan(plan, self._NARR)
        assert not any("FOLDED" in r for r in report)
        assert len(plan.chapters) == 2

    def test_a_colliding_text_id_is_renamed_and_its_action_follows(self):
        plan = self._plan(collide=True)
        scenes, _, report = compile_plan(plan, self._NARR)
        s1 = scenes["s001"]
        assert ("write", "c1_ttl") in [(a["verb"], a["target"]) for a in s1["actions"]]
        assert {e["id"] for e in s1["elements"]} >= {"c1_ttl", "atom"}
        # the later chapter's "ttl" (the nucleus label) is the one that keeps the id
        ttl = next(e for e in scenes["s004"]["elements"] if e["id"] == "ttl")
        assert ttl["text"] == "nucleus"

    def test_a_plan_with_no_art_anywhere_is_reported_not_repaired(self):
        plan = self._plan(second_has_picture=False)
        _, _, report = compile_plan(plan, self._NARR)
        assert any(r.startswith("PLAN | NO ART") for r in report), report
        assert len(plan.chapters) == 2

    def test_the_folded_scenes_are_schema_valid(self):
        scenes, _, _ = compile_plan(self._plan(collide=True), self._NARR)
        for sc in scenes.values():
            Scene.model_validate(sc)


class TestTheDirectorIsToldToOpenOnADrawing:
    def test_the_prompt_requires_a_picture_in_the_first_chapter(self):
        import inspect

        from spike.scene_engine import director
        assert "THE LESSON OPENS ON A DRAWING" in inspect.getsource(director)


# ── 7. a position is not a part, and a grey page is still a page ─────────────

class TestAPositionalPartNameIsNeverLabelled:
    """Periodic table, 2026-09-13: the layer groups were left_side, right_side,
    rows, columns, staircase; the narration named them all, so four labels
    saying WHERE went down the margin with four arrows converging on one
    corner of the grid."""

    _NARR = {"s001": "the periodic table has metals on the left side and "
                     "non metals on the right side, in rows and columns, "
                     "split by the metalloid staircase"}

    def _plan(self):
        return parse_visual_plan({"chapters": [
            {"concept": "periodic_table_structure",
             "assets": {"periodic_table_grid": "A block layout of the periodic table. "
                        "Name the layer groups exactly: left_side, right_side, "
                        "rows, columns, staircase"},
             "elements": [
                 {"id": "table_grid", "type": "illustration",
                  "asset": "periodic_table_grid", "at": [600, 380]},
             ],
             "steps": [
                 {"segment": 1, "decision": "NEW_VISUAL",
                  "actions": [{"verb": "draw", "target": "table_grid"}]},
             ]}]})

    def test_positions_are_skipped_and_the_thing_is_still_labelled(self):
        scenes, _, report = compile_plan(self._plan(), self._NARR)
        ids = {e["id"] for e in scenes["s001"]["elements"]}
        assert "lbl_auto_staircase" in ids, report
        for pos in ("left_side", "right_side", "rows", "columns"):
            assert f"lbl_auto_{pos}" not in ids, (pos, sorted(ids))
        skipped = [r for r in report if "NOT LABELLED" in r]
        assert len(skipped) == 4, skipped

    @pytest.mark.parametrize("name", ["left side", "right_side", "rows", "columns",
                                      "upper half", "top", "background", "outline"])
    def test_what_counts_as_a_position(self, name):
        from spike.scene_engine.continuity import _is_positional
        assert _is_positional(name) is True

    @pytest.mark.parametrize("name", ["outer electron", "left ventricle",
                                      "nucleus", "staircase", "top soil", ""])
    def test_a_thing_named_by_where_it_is_is_still_a_thing(self, name):
        from spike.scene_engine.continuity import _is_positional
        assert _is_positional(name) is False


class TestAGreyPageIsCutLikeAWhiteOne:
    """The potassium atom came back on a light grey page (lum ~195); every
    paper pixel sat just under the white cut and the board showed a grey
    rectangle behind the drawing."""

    @staticmethod
    def _page(bg: int, stroke: int = 20):
        a = np.full((200, 300, 3), bg, np.uint8)
        a[80:120, 100:200] = stroke
        return Image.fromarray(a, "RGB")

    @pytest.mark.parametrize("bg", [255, 235, 210, 195, 175])
    def test_the_page_goes_and_the_stroke_stays(self, bg):
        from spike.scene_engine.raster_assets import to_ink
        img = to_ink(self._page(bg))
        alpha = np.asarray(img.getchannel("A"))
        assert alpha.max() == 255
        # cropped to the stroke plus its 12 px pad: no page survived the cut
        assert img.size == (100 + 24, 40 + 24 - 1) or img.size[0] <= 124, img.size
        assert int(alpha[0, 0]) == 0

    def test_a_white_page_cuts_exactly_as_before(self):
        from spike.scene_engine.raster_assets import _ink_threshold
        lum = np.full((100, 100), 255, np.int32)
        assert _ink_threshold(lum) == 215

    def test_art_to_the_edge_is_not_mistaken_for_a_dark_page(self):
        from spike.scene_engine.raster_assets import _ink_threshold, to_ink
        lum = np.full((100, 100), 90, np.int32)
        assert _ink_threshold(lum) == 215, "a dark border is drawing, not paper"
        img = to_ink(self._page(120, stroke=120))
        assert np.asarray(img.getchannel("A")).min() > 150, "nothing was cut away"


# ── 8. every segment belongs to a chapter ────────────────────────────────────

class TestEverySegmentBelongsToAChapter:
    """Joints, second render (2026-09-14): steps on segments 3-6 and 9-10 of
    twelve. The hook, the two segments between the pulley and the arm, and
    the two closing segments fell to the whiteboard fallback — five title
    cards in a lesson whose pictures were all there."""

    _ALL = [f"s{i:03d}" for i in range(1, 9)]
    _NARR = {"s001": "have you ever wondered how your arm bends",
             "s002": "if our skeleton were one solid bone",
             "s003": "a joint is where two bones meet",
             "s004": "cartilage keeps them from grinding",
             "s005": "because muscles can only pull they work in pairs",
             "s006": "here is a puzzle for you",
             "s007": "to bend your arm the biceps contracts",
             "s008": "next time you run feel your muscles"}

    def _plan(self):
        return parse_visual_plan({"chapters": [
            {"concept": "joint_structure",
             "assets": {"joint": "a synovial joint. Name the layer groups exactly: cartilage"},
             "elements": [
                 {"id": "joint", "type": "illustration", "asset": "joint", "at": [600, 380]},
                 {"id": "lbl_c", "type": "text", "text": "Cartilage", "at": [95, 140],
                  "role": "label"}],
             "steps": [
                 {"segment": 3, "decision": "NEW_VISUAL",
                  "actions": [{"verb": "draw", "target": "joint"}]},
                 {"segment": 4, "decision": "EXTEND",
                  "actions": [{"verb": "write", "target": "lbl_c"}]}]},
            {"concept": "muscle_pairs", "transition": "clear_and_redraw",
             "assets": {"arm": "a bent arm. Name the layer groups exactly: biceps"},
             "elements": [
                 {"id": "arm", "type": "illustration", "asset": "arm", "at": [600, 380]}],
             "steps": [
                 {"segment": 7, "decision": "NEW_VISUAL",
                  "actions": [{"verb": "draw", "target": "arm"}]}]},
        ]})

    def _compile(self, skip_hold=None):
        return compile_plan(self._plan(), self._NARR, all_segments=self._ALL,
                            skip_hold=skip_hold)

    def test_the_hook_opens_on_the_first_chapter_s_picture(self):
        scenes, _, report = self._compile()
        assert "s001" in scenes, sorted(scenes)
        assert any(a.get("verb") == "draw" and a.get("target") == "joint"
                   for a in scenes["s001"]["actions"]), scenes["s001"]["actions"]
        assert any("OPENING PULLED s003 -> s001" in r for r in report), report
        # the segment the step was written for now simply holds the board
        assert "s003" in scenes and "joint" in {e["id"] for e in scenes["s003"]["elements"]}

    def test_the_segments_between_two_chapters_hold_the_earlier_board(self):
        scenes, _, _ = self._compile()
        for sid in ("s005", "s006"):
            assert sid in scenes, sid
            ids = {e["id"] for e in scenes[sid]["elements"]}
            assert "joint" in ids and "arm" not in ids, (sid, ids)

    def test_the_closing_segment_holds_the_last_board(self):
        scenes, _, _ = self._compile()
        assert "s008" in scenes
        assert "arm" in {e["id"] for e in scenes["s008"]["elements"]}

    def test_a_question_hook_still_keeps_its_own_visual(self):
        scenes, _, _ = self._compile(skip_hold={"s006"})
        assert "s006" not in scenes
        assert "s005" in scenes

    def test_every_segment_is_covered_and_valid(self):
        scenes, _, _ = self._compile()
        assert set(scenes) == set(self._ALL), sorted(set(self._ALL) - set(scenes))
        for sc in scenes.values():
            Scene.model_validate(sc)

    def test_without_a_segment_list_the_old_span_rule_stands(self):
        scenes, _, report = compile_plan(self._plan(), self._NARR)
        assert "s001" not in scenes and not any("OPENING PULLED" in r for r in report)


class TestTheDirectorIsToldEverySegmentHasAChapter:
    def test_the_prompt_says_so(self):
        import inspect

        from spike.scene_engine import director
        assert "EVERY SEGMENT BELONGS TO A CHAPTER" in inspect.getsource(director)
