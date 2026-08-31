"""Visual continuity: the persistent-whiteboard plan compiler + carry-over
rendering. All offline (authored vector assets, no ffmpeg, no network)."""

from __future__ import annotations

import pytest

from spike.scene_engine.continuity import (compile_plan, parse_visual_plan,
                                           plan_stats)
from spike.scene_engine.render import SceneRenderer
from spike.scene_engine.schema import parse_scene


def _plan():
    return parse_visual_plan({
        "chapters": [
            {"concept": "plant_cell_structure",
             "assets": {"plant_cell": "a cell"},
             "elements": [
                 {"id": "cell", "type": "illustration", "asset": "plant_cell",
                  "at": [600, 380], "scale": 0.9},
                 {"id": "lbl", "type": "text", "text": "Cell wall",
                  "at": [70, 150]},
             ],
             "steps": [
                 {"segment": 1, "decision": "NEW_VISUAL",
                  "actions": [{"verb": "draw", "target": "cell",
                               "layers": ["wall"]}]},
                 {"segment": 2, "decision": "EXTEND",
                  "actions": [{"verb": "write", "target": "lbl"},
                              {"verb": "draw", "target": "cell",
                               "layers": ["nucleus"]}]},
                 {"segment": 3, "decision": "FOCUS",
                  "actions": [{"verb": "zoom", "target": "lbl",
                               "scale": 1.5}]},
             ]},
            {"concept": "next_topic",
             "elements": [{"id": "mem", "type": "illustration",
                           "asset": "membrane_section", "at": [640, 360]}],
             "steps": [{"segment": 4, "decision": "CLEAR_AND_REDRAW",
                        "actions": [{"verb": "draw", "target": "mem"}]}]},
        ]})


_NARR = {"s001": "a plant cell", "s002": "the wall protects",
         "s003": "focus here", "s004": "now the membrane"}


class TestCompiler:
    def test_segment_index_normalizes_to_ids(self):
        scenes, _, _ = compile_plan(_plan(), _NARR)
        assert set(scenes) == {"s001", "s002", "s003", "s004"}

    def test_board_state_accumulates_across_steps(self):
        scenes, _, _ = compile_plan(_plan(), _NARR)
        cell_s2 = next(e for e in scenes["s002"]["elements"] if e["id"] == "cell")
        assert cell_s2["drawn_layers"] == ["wall"]      # s001's work carried in
        # by s003 both drawn layers carry — and ONLY those: a layer-drawn
        # asset never "completes" into showing layers nobody drew
        cell_s3 = next(e for e in scenes["s003"]["elements"] if e["id"] == "cell")
        assert set(cell_s3["drawn_layers"]) == {"wall", "nucleus"}
        # the label written in s002 appears in s003 WITHOUT an introducer
        assert any(e["id"] == "lbl" for e in scenes["s003"]["elements"])
        assert not any(a.get("target") == "lbl" and a["verb"] == "write"
                       for a in scenes["s003"]["actions"])

    def test_raster_slices_apportioned_across_segments(self):
        scenes, _, _ = compile_plan(_plan(), _NARR)
        d1 = next(a for a in scenes["s001"]["actions"] if a["verb"] == "draw")
        d2 = next(a for a in scenes["s002"]["actions"] if a["verb"] == "draw")
        assert d1["slice"] == (0.0, 0.5) and d2["slice"] == (0.5, 0.5)
        cell_s2 = next(e for e in scenes["s002"]["elements"] if e["id"] == "cell")
        assert cell_s2["drawn_frac"] == 0.5

    def test_camera_chains_and_boundary_resets_on_screen(self):
        scenes, _, _ = compile_plan(_plan(), _NARR)
        assert scenes["s003"]["camera_start"]["scale"] == 1.0   # zoom happens IN s003
        s4 = scenes["s004"]
        assert s4["camera_start"]["scale"] == 1.5   # arrives zoomed from s003...
        verbs = [a["verb"] for a in s4["actions"]]
        assert verbs[:2] == ["fade", "camera_reset"]  # ...and resets ON SCREEN

    def test_boundary_carries_and_fades_previous_board(self):
        # carried elements arrive RENAMED (prev__*) so a reused id can never
        # collide with the new chapter's roster
        scenes, _, _ = compile_plan(_plan(), _NARR)
        ids = {e["id"] for e in scenes["s004"]["elements"]}
        assert {"prev__cell", "prev__lbl", "prev__board", "mem"} <= ids
        fade = scenes["s004"]["actions"][0]
        assert fade["target"] == "prev__board" and fade["to"] == 0.0

    def test_boundary_survives_id_reuse_across_chapters(self):
        p2 = parse_visual_plan({
            "chapters": [
                {"concept": "a",
                 "elements": [{"id": "title", "type": "text", "text": "One",
                               "at": [100, 100]}],
                 "steps": [{"segment": 1, "decision": "NEW_VISUAL",
                            "actions": [{"verb": "write", "target": "title"}]}]},
                {"concept": "b",
                 "elements": [{"id": "title", "type": "text", "text": "Two",
                               "at": [100, 100]}],
                 "steps": [{"segment": 2, "decision": "CLEAR_AND_REDRAW",
                            "actions": [{"verb": "write", "target": "title"}]}]},
            ]})
        scenes, _, _ = compile_plan(p2, {"s001": "x", "s002": "y"})
        sc = parse_scene(scenes["s002"])      # duplicate ids would fail here
        assert {"prev__title", "title"} <= {e.id for e in sc.elements}

    def test_every_compiled_scene_is_schema_valid(self):
        scenes, _, _ = compile_plan(_plan(), _NARR)
        for sid, sc in scenes.items():
            parse_scene(sc)

    def test_assets_are_cumulative_so_carried_boards_resolve(self):
        _, assets, _ = compile_plan(_plan(), _NARR)
        assert assets["s001"] == {"plant_cell": "a cell"}
        # the boundary scene carries chapter 1's cell — its asset must resolve
        assert assets["s004"] == {"plant_cell": "a cell"}

    def test_stats_and_report(self):
        plan = _plan()
        st = plan_stats(plan)
        assert st == {"segments_planned": 4, "visual_chapters": 2,
                      "root_visuals": 1, "extensions": 1,
                      "focus_transform": 1, "full_redraws": 2}
        _, _, report = compile_plan(plan, _NARR)
        assert len(report) == 4 and "CLEAR_AND_REDRAW" in report[3]

    def test_garbage_plan_rejected_not_fatal(self):
        assert parse_visual_plan("nope") is None
        assert parse_visual_plan({"chapters": [{"concept": "x", "elements": [],
                                                "steps": []}]}) is None


class TestCarryOverRendering:
    def test_carried_layers_visible_at_t0(self):
        scenes, _, _ = compile_plan(_plan(), _NARR)
        r = SceneRenderer(parse_scene(scenes["s002"]))
        r.compile(8.0)
        st = r._state_at(0.0)["cell"]
        assert st.visible
        wall_idx = r._layer_flat_indices(r.bound["cell"], ["wall"])
        nuc_idx = r._layer_flat_indices(r.bound["cell"], ["nucleus"])
        assert wall_idx and all(st.reveal.get(i) == 1.0 for i in wall_idx)
        assert all(st.reveal.get(i, 0.0) == 0.0 for i in nuc_idx)

    def test_completed_text_renders_full_without_introducer(self):
        scenes, _, _ = compile_plan(_plan(), _NARR)
        r = SceneRenderer(parse_scene(scenes["s003"]))
        r.compile(8.0)
        st = r._state_at(0.0)["lbl"]
        assert st.visible and st.text_frac == 1.0

    def test_camera_start_honoured(self):
        scenes, _, _ = compile_plan(_plan(), _NARR)
        r = SceneRenderer(parse_scene(scenes["s004"]))
        r.compile(10.0)
        assert r.cam.state_at(0.0).scale == pytest.approx(1.5)
        assert r.cam.state_at(9.5).scale == pytest.approx(1.0)  # reset ran

    def test_explicit_draw_slice_overrides_scene_split(self):
        scenes, _, _ = compile_plan(_plan(), _NARR)
        sc = parse_scene(scenes["s002"])
        draw = next(a for a in sc.actions if a.verb == "draw")
        assert draw.slice == (0.5, 0.5)

class TestHoldsAndDegradation:
    def test_gap_segment_inside_chapter_gets_hold_scene(self):
        p2 = parse_visual_plan({
            "chapters": [{"concept": "c",
                "elements": [{"id": "cell", "type": "illustration",
                              "asset": "plant_cell", "at": [600, 380]}],
                "steps": [
                    {"segment": 1, "decision": "NEW_VISUAL",
                     "actions": [{"verb": "draw", "target": "cell",
                                  "layers": ["wall"]}]},
                    {"segment": 3, "decision": "FOCUS",
                     "actions": [{"verb": "zoom", "target": "cell",
                                  "scale": 1.4}]},
                ]}]})
        scenes, _, report = compile_plan(
            p2, {"s001": "a", "s002": "b", "s003": "c"},
            all_segments=["s001", "s002", "s003"])
        assert "s002" in scenes                       # the HOLD
        hold = scenes["s002"]
        assert hold["actions"] == []
        cell = next(e for e in hold["elements"] if e["id"] == "cell")
        assert cell["drawn_layers"] == ["wall"]       # board exactly as left
        assert any("HOLD" in l for l in report)
        sc = parse_scene(hold)
        r = SceneRenderer(sc); r.compile(5.0)
        st = r._state_at(1.0)["cell"]
        assert st.visible                              # renders, statically

    def test_interactive_segments_are_not_held(self):
        p2 = parse_visual_plan({
            "chapters": [{"concept": "c",
                "elements": [{"id": "cell", "type": "illustration",
                              "asset": "plant_cell", "at": [600, 380]}],
                "steps": [
                    {"segment": 1, "decision": "NEW_VISUAL",
                     "actions": [{"verb": "draw", "target": "cell"}]},
                    {"segment": 3, "decision": "CONTINUE",
                     "actions": [{"verb": "circle", "target": "cell"}]},
                ]}]})
        scenes, _, _ = compile_plan(
            p2, {"s001": "a", "s002": "quiz!", "s003": "c"},
            all_segments=["s001", "s002", "s003"], skip_hold={"s002"})
        assert "s002" not in scenes                   # quiz keeps its slide

    def test_unknown_asset_drops_element_scene_survives(self):
        sc = parse_scene({
            "id": "x", "narration": "look at the mystery thing here",
            "elements": [
                {"id": "ghost", "type": "illustration", "asset": "volcano_9x",
                 "at": [400, 300]},
                {"id": "lbl", "type": "text", "text": "Label", "at": [700, 200]},
            ],
            "actions": [{"verb": "draw", "target": "ghost"},
                        {"verb": "write", "target": "lbl"}]})
        r = SceneRenderer(sc)          # must NOT raise
        tl = r.compile(10.0)
        st = r._state_at(tl[-1].end + 0.1)
        assert st["lbl"].text_frac == 1.0             # the rest still teaches


    def test_unknown_decision_clamps_never_kills_chapter(self):
        # the model leaked "GHOST_ONLY" (visual_action vocabulary) into a
        # decision once — and chapter-level salvage dropped the whole plan
        p2 = parse_visual_plan({
            "chapters": [{"concept": "c",
                "elements": [{"id": "cell", "type": "illustration",
                              "asset": "plant_cell", "at": [600, 380]}],
                "steps": [
                    {"segment": 1, "decision": "NEW_VISUAL",
                     "actions": [{"verb": "draw", "target": "cell"}]},
                    {"segment": 2, "decision": "GHOST_ONLY",
                     "actions": [{"verb": "circle", "target": "cell"}]},
                    {"segment": 3, "decision": "invalid_stuff", "actions": []},
                ]}]})
        assert p2 is not None
        assert [st.decision for st in p2.chapters[0].steps] ==             ["NEW_VISUAL", "CONTINUE", "CONTINUE"]


    def test_one_root_visual_per_chapter_enforced(self):
        p2 = parse_visual_plan({
            "chapters": [{"concept": "c",
                "elements": [
                    {"id": "cell", "type": "illustration",
                     "asset": "plant_cell", "at": [600, 380]},
                    {"id": "extra_wall", "type": "illustration",
                     "asset": "membrane_section", "at": [600, 380]},
                    {"id": "lbl", "type": "text", "text": "L", "at": [70, 100]},
                ],
                "steps": [
                    {"segment": 1, "decision": "NEW_VISUAL",
                     "actions": [{"verb": "draw", "target": "cell"}]},
                    {"segment": 2, "decision": "EXTEND",
                     "actions": [{"verb": "draw", "target": "extra_wall"},
                                 {"verb": "write", "target": "lbl"}]},
                ]}]})
        scenes, _, report = compile_plan(p2, {"s001": "a", "s002": "b"})
        ids2 = {e["id"] for e in scenes["s002"]["elements"]}
        assert "extra_wall" not in ids2               # second visual dropped
        assert "cell" in ids2 and "lbl" in ids2       # root + label intact
        verbs2 = [(a["verb"], a.get("target")) for a in scenes["s002"]["actions"]]
        assert ("draw", "extra_wall") not in verbs2   # its action dropped too
        assert ("write", "lbl") in verbs2
        assert any("one root visual" in l for l in report)
        parse_scene(scenes["s002"])                   # still schema-valid


    def test_big_compiled_boards_survive_the_response_parser(self):
        # the persistent board legitimately outgrows the raw-scene cap; the
        # parser must never amputate a compiled scene (s023 once lost its
        # 13th element and failed validation over its own action)
        from spike.scene_engine.director import parse_scene_response
        els = [{"id": f"lbl{i}", "type": "text", "text": f"L{i}",
                "at": [40, 30 + 40 * i]} for i in range(14)]
        acts = [{"verb": "write", "target": "lbl13"}]
        sc = parse_scene_response({"id": "vc_s099", "compiled": True,
                                   "elements": els, "actions": acts}, "narr")
        assert sc is not None and len(sc.elements) == 14

    def test_raw_scene_clamp_stays_coherent(self):
        from spike.scene_engine.director import parse_scene_response
        els = [{"id": f"lbl{i}", "type": "text", "text": f"L{i}",
                "at": [40, 30 + 40 * i]} for i in range(14)]
        acts = [{"verb": "write", "target": "lbl0"},
                {"verb": "write", "target": "lbl13"}]   # will be clamped away
        sc = parse_scene_response({"id": "raw", "elements": els,
                                   "actions": acts}, "narr")
        assert sc is not None and len(sc.elements) == 12
        assert [a.target for a in sc.actions] == ["lbl0"]  # no dangling refs
