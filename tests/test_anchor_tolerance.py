"""Anchor tolerance: a dangling arrow anchor is CONVERTED, never a lost scene.

Incident 2026-09-04 (generation c90bac1d, founder's "Cells" Part 1): the
director declared its root visual as ``plant_cell_diagram`` and pointed
``arrow_plant`` at ``plant_cell_box``. Nothing before the schema validator
looked at anchor refs, so vc_s003..vc_s006 were each rejected with "arrow
'arrow_plant' anchors to unknown element 'plant_cell_box'", fell to slides,
and the lesson shipped 9/9 whiteboard cards ("NO scenes").

All offline: no model, no network, no ffmpeg.
"""

from __future__ import annotations

import logging

import pytest

from spike.scene_engine.anchors import (anchor_key, resolve_anchor,
                                        resolve_roster_anchors,
                                        resolve_scene_anchors)
from spike.scene_engine.continuity import compile_plan, parse_visual_plan
from spike.scene_engine.director import parse_scene_response
from spike.scene_engine.schema import Scene
from spike.scene_engine.validate import validate_visual_language

_NARR = {"s001": "hook", "s002": "intro", "s003": "Here is a plant cell.",
         "s004": "The cell wall protects it.",
         "s005": "Chloroplasts make food.", "s006": "Look at the vacuole."}


def _incident_plan_raw():
    """The incident's shape: root declared under one id, the arrow anchored
    to another; four segments ride the arrow."""
    return {"chapters": [{
        "concept": "plant_cell", "transition": "clear_and_redraw",
        "assets": {"plant_cell": "A plant cell diagram. Name the layer groups "
                                 "exactly: cell_wall, chloroplasts, vacuole"},
        "elements": [
            {"id": "plant_cell_diagram", "type": "illustration",
             "asset": "plant_cell", "at": [640, 360], "scale": 1.0},
            {"id": "lbl_wall", "type": "text", "text": "Cell wall",
             "at": [95, 140]},
            {"id": "arrow_plant", "type": "arrow",
             "tail": {"el": "lbl_wall", "edge": "right"},
             "head": {"el": "plant_cell_box", "edge": "center"}},
        ],
        "steps": [
            {"segment": 3, "decision": "NEW_VISUAL",
             "actions": [{"verb": "draw", "target": "plant_cell_diagram"}]},
            {"segment": 4, "decision": "EXTEND",
             "actions": [{"verb": "write", "target": "lbl_wall"},
                         {"verb": "draw", "target": "arrow_plant"}]},
            {"segment": 5, "decision": "FOCUS",
             "actions": [{"verb": "zoom", "target": "lbl_wall", "scale": 1.4}]},
            {"segment": 6, "decision": "CONTINUE",
             "actions": [{"verb": "circle", "target": "lbl_wall"}]},
        ]}]}


def _compile(raw):
    plan = parse_visual_plan(raw)
    assert plan is not None
    return compile_plan(plan, _NARR, all_segments=list(_NARR), skip_hold=set())


class TestNormalisation:
    def test_kind_suffixes_fold_two_ids_for_one_thing(self):
        assert anchor_key("plant_cell_box") == "plant cell"
        assert anchor_key("Plant-Cell Diagram") == "plant cell"
        assert anchor_key("plant_cell_diagram") == anchor_key("plant_cell")
        assert anchor_key("lbl_nucleus_label") == "lbl nucleus"

    def test_a_bare_kind_word_is_not_stripped_to_nothing(self):
        assert anchor_key("box") == "box"
        assert anchor_key("diagram") == "diagram"


class TestResolveAnchor:
    ROSTER = {
        "plant_cell_diagram": {"id": "plant_cell_diagram",
                               "type": "illustration", "asset": "plant_cell"},
        "lbl_wall": {"id": "lbl_wall", "type": "text", "text": "Cell wall"},
        "lbl_vac": {"id": "lbl_vac", "type": "text", "text": "Vacuole"},
        "arr_x": {"id": "arr_x", "type": "arrow", "tail": [0, 0],
                  "head": [1, 1]},
    }

    def test_id_match_after_normalisation(self):
        el, layer, how = resolve_anchor("plant_cell_box", self.ROSTER,
                                        "plant_cell_diagram")
        assert (el, layer, how) == ("plant_cell_diagram", None, "id match")

    def test_asset_key_match(self):
        roster = {"root_visual": {"id": "root_visual", "type": "illustration",
                                  "asset": "plant_cell"},
                  "lbl_wall": self.ROSTER["lbl_wall"]}
        el, _, how = resolve_anchor("plant_cell", roster, None)
        assert (el, how) == ("root_visual", "asset match")

    def test_label_resolves_by_its_content(self):
        el, _, how = resolve_anchor("cell_wall_label", self.ROSTER, None)
        assert (el, how) == ("lbl_wall", "text match")

    def test_merged_handle_becomes_root_plus_layer(self):
        el, layer, how = resolve_anchor(
            "cell_nucleus", self.ROSTER, "plant_cell_diagram",
            aliases={"cell_nucleus": "nucleus"})
        assert (el, layer, how) == ("plant_cell_diagram", "nucleus",
                                    "merged handle")

    def test_part_name_used_as_an_id_becomes_root_plus_layer(self):
        el, layer, how = resolve_anchor(
            "chloroplasts", self.ROSTER, "plant_cell_diagram",
            part_names=["cell_wall", "chloroplasts"])
        assert (el, layer, how) == ("plant_cell_diagram", "chloroplasts",
                                    "part name")

    def test_anything_unlabel_like_falls_to_the_single_root(self):
        el, layer, how = resolve_anchor("mystery_thing", self.ROSTER,
                                        "plant_cell_diagram")
        assert (el, how) == ("plant_cell_diagram", "root visual")

    def test_an_unknown_label_ref_has_no_candidate(self):
        # two labels, neither matches: guessing would point at the WRONG one
        el, _, how = resolve_anchor("lbl_ghost", self.ROSTER,
                                    "plant_cell_diagram")
        assert el is None and how == "no candidate"

    def test_arrows_are_never_candidates(self):
        el, _, _ = resolve_anchor("arr_x", {"arr_x": self.ROSTER["arr_x"]},
                                  None)
        assert el is None


class TestDirectorGuard:
    """(a) re-anchored when a normalised match exists; (b) dropped with a
    warning when nothing matches, the rest intact; (c) genuinely malformed
    scenes still fail."""

    def _raw(self, tail, head, extra_actions=()):
        return {
            "id": "vc_s004",
            "elements": [
                {"id": "plant_cell_diagram", "type": "illustration",
                 "asset": "plant_cell", "at": [640, 360]},
                {"id": "lbl_wall", "type": "text", "text": "Cell wall",
                 "at": [95, 140]},
                {"id": "arrow_plant", "type": "arrow", "tail": tail,
                 "head": head},
            ],
            "actions": [{"verb": "draw", "target": "plant_cell_diagram"},
                        {"verb": "write", "target": "lbl_wall"},
                        {"verb": "draw", "target": "arrow_plant"},
                        *extra_actions],
        }

    def test_a_unknown_anchor_with_a_normalised_match_is_reanchored(self, caplog):
        raw = self._raw({"el": "lbl_wall", "edge": "right"},
                        {"el": "plant_cell_box", "edge": "center"})
        with caplog.at_level(logging.WARNING, logger="spike.scene_engine.director"):
            scene = parse_scene_response(raw, "The cell wall protects it.")
        assert scene is not None, "the whole scene fell to a slide"
        arrow = next(e for e in scene.elements if e.id == "arrow_plant")
        assert arrow.head.el == "plant_cell_diagram"
        assert arrow.head.edge == "center"           # the rest of the ref kept
        assert "REANCHORED arrow_plant.head 'plant_cell_box'" in caplog.text
        assert "vc_s004" in caplog.text

    def test_b_unresolvable_anchor_drops_that_arrow_only(self, caplog):
        raw = self._raw({"el": "lbl_ghost", "edge": "right"},
                        {"el": "plant_cell_diagram", "edge": "center"},
                        extra_actions=[{"verb": "circle",
                                        "target": "arrow_plant"}])
        # a second label so the ghost ref has no unique text to fall to
        raw["elements"].append({"id": "lbl_vac", "type": "text",
                                "text": "Vacuole", "at": [95, 220]})
        with caplog.at_level(logging.WARNING, logger="spike.scene_engine.director"):
            scene = parse_scene_response(raw, "n")
        assert scene is not None
        ids = [e.id for e in scene.elements]
        assert "arrow_plant" not in ids
        assert ids == ["plant_cell_diagram", "lbl_wall", "lbl_vac"]
        assert [a.verb for a in scene.actions] == ["draw", "write"]
        assert "DROPPED arrow arrow_plant (tail anchor 'lbl_ghost'" in caplog.text
        assert "vc_s004" in caplog.text

    def test_c_an_unknown_draw_target_still_fails(self):
        raw = self._raw([100, 140], [600, 360],
                        extra_actions=[{"verb": "draw", "target": "ghost"}])
        assert parse_scene_response(raw, "n") is None

    def test_c_the_schema_validator_is_untouched(self):
        with pytest.raises(Exception, match="anchors to unknown"):
            Scene.model_validate({
                "id": "x", "narration": "n",
                "elements": [{"id": "ar", "type": "arrow",
                              "tail": {"el": "ghost"}, "head": (5, 5)}],
                "actions": [{"verb": "draw", "target": "ar"}]})

    def test_a_text_chained_after_a_ghost_is_unchained_not_lost(self):
        raw = {"id": "t", "elements": [
            {"id": "a", "type": "text", "text": "x2 +", "at": [100, 200]},
            {"id": "b", "type": "text", "text": "5x", "at": [200, 200],
             "after": {"el": "ghost", "gap": 2}}],
            "actions": [{"verb": "write", "target": "a"},
                        {"verb": "write", "target": "b"}]}
        scene = parse_scene_response(raw, "n")
        assert scene is not None
        b = next(e for e in scene.elements if e.id == "b")
        assert b.after is None
        assert list(b.at) == [200, 200]        # its own point survives


class TestCompilerConversion:
    """(d) the plan report carries the conversion; (e) the incident shape
    parses on every segment it touched."""

    def test_e_incident_shape_parses_on_every_segment(self):
        scenes, _, report = _compile(_incident_plan_raw())
        assert set(scenes) >= {"s003", "s004", "s005", "s006"}
        for sid in ("s003", "s004", "s005", "s006"):
            assert parse_scene_response(scenes[sid], _NARR[sid]) is not None, \
                f"{sid} fell to a slide"
        arrow = next(e for e in scenes["s004"]["elements"]
                     if e["id"] == "arrow_plant")
        assert arrow["head"]["el"] == "plant_cell_diagram"
        assert arrow["tail"]["el"] == "lbl_wall"

    def test_d_report_records_the_conversion_like_anchored_lines(self):
        _, _, report = _compile(_incident_plan_raw())
        line = next((ln for ln in report if "| REANCHORED" in ln), None)
        assert line is not None, report
        assert line.startswith("CHAPTER plant_cell | REANCHORED arrow_plant.head "
                               "'plant_cell_box' -> plant_cell_diagram")
        # once per chapter, not once per segment the arrow rode into
        assert sum(1 for ln in report if "REANCHORED arrow_plant" in ln) == 1

    def test_d_validate_counts_a_reanchored_arrow_as_an_arrow(self):
        plan = parse_visual_plan(_incident_plan_raw())
        _, _, report = compile_plan(plan, _NARR, all_segments=list(_NARR),
                                    skip_hold=set())
        manifest = {"segments": [{"segment_id": s, "renderer": "scene",
                                  "audio_path": "a.mp3"} for s in _NARR]}
        r = validate_visual_language(
            manifest, {"plan": plan.model_dump(), "report": report})
        assert r["arrows_reanchored"] == 1
        assert r["arrows_dropped"] == []
        synthesized = sum(1 for ln in report if "| SYNTHESIZED" in ln)
        assert r["arrow_count"] == 1 + synthesized

    def test_d_validate_subtracts_a_dropped_arrow(self):
        raw = _incident_plan_raw()
        # tail names a label that never existed, beside a second real label
        raw["chapters"][0]["elements"].append(
            {"id": "lbl_vac", "type": "text", "text": "Vacuole", "at": [95, 220]})
        raw["chapters"][0]["elements"][2]["tail"] = {"el": "lbl_ghost",
                                                     "edge": "right"}
        plan = parse_visual_plan(raw)
        scenes, _, report = compile_plan(plan, _NARR, all_segments=list(_NARR),
                                         skip_hold=set())
        assert any("| DROPPED arrow arrow_plant (tail anchor 'lbl_ghost'" in ln
                   for ln in report), report
        for sid in ("s003", "s004", "s005", "s006"):
            assert parse_scene_response(scenes[sid], _NARR[sid]) is not None
        manifest = {"segments": [{"segment_id": s, "renderer": "scene",
                                  "audio_path": "a.mp3"} for s in _NARR]}
        r = validate_visual_language(
            manifest, {"plan": plan.model_dump(), "report": report})
        assert len(r["arrows_dropped"]) == 1
        synthesized = sum(1 for ln in report if "| SYNTHESIZED" in ln)
        assert r["arrow_count"] == synthesized       # the declared one is gone

    def test_a_merged_handle_anchor_follows_the_handle_into_the_root(self):
        raw = {"chapters": [{
            "concept": "cell", "assets": {"plant_cell": "a cell"},
            "elements": [
                {"id": "cell", "type": "illustration", "asset": "plant_cell",
                 "at": [640, 360]},
                {"id": "cell_nucleus", "type": "illustration",
                 "asset": "plant_cell", "at": [640, 360]},
                {"id": "lbl_nucleus", "type": "text", "text": "Nucleus",
                 "at": [95, 140]},
                {"id": "arr_nucleus", "type": "arrow",
                 "tail": {"el": "lbl_nucleus", "edge": "right"},
                 "head": {"el": "cell_nucleus", "edge": "center"}},
            ],
            "steps": [
                {"segment": 3, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "cell"}]},
                {"segment": 4, "decision": "EXTEND",
                 "actions": [{"verb": "draw", "target": "cell_nucleus"},
                             {"verb": "write", "target": "lbl_nucleus"},
                             {"verb": "draw", "target": "arr_nucleus"}]},
            ]}]}
        scenes, _, report = _compile(raw)
        assert any("MERGED handle 'cell_nucleus'" in ln for ln in report)
        arrow = next(e for e in scenes["s004"]["elements"]
                     if e["id"] == "arr_nucleus")
        assert arrow["head"]["el"] == "cell"
        assert arrow["head"].get("layer")
        assert parse_scene_response(scenes["s004"], _NARR["s004"]) is not None

    def test_an_anchor_to_a_label_not_yet_on_the_board_flattens(self):
        """An arrow drawn one step BEFORE its label is written: the label is
        not on that board, so the anchor cannot bind. Flatten it to the
        label's planned point rather than reveal the label early or lose the
        scene."""
        raw = {"chapters": [{
            "concept": "cell", "assets": {"plant_cell": "a cell"},
            "elements": [
                {"id": "cell", "type": "illustration", "asset": "plant_cell",
                 "at": [640, 360]},
                {"id": "lbl_wall", "type": "text", "text": "Cell wall",
                 "at": [95, 140]},
                {"id": "arr_wall", "type": "arrow",
                 "tail": {"el": "lbl_wall", "edge": "right", "dx": 6},
                 "head": {"el": "cell", "edge": "center"}},
            ],
            "steps": [
                {"segment": 3, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "cell"},
                             {"verb": "draw", "target": "arr_wall"}]},
                {"segment": 4, "decision": "EXTEND",
                 "actions": [{"verb": "write", "target": "lbl_wall"}]},
            ]}]}
        scenes, _, report = _compile(raw)
        s3 = next(e for e in scenes["s003"]["elements"] if e["id"] == "arr_wall")
        assert s3["tail"] == [101.0, 140.0]
        assert not any(e["id"] == "lbl_wall" for e in scenes["s003"]["elements"])
        assert any("SEGMENT s003 | FLATTENED arr_wall.tail 'lbl_wall'" in ln
                   for ln in report)
        assert parse_scene_response(scenes["s003"], _NARR["s003"]) is not None
        # once the label IS on the board the anchor is kept as written
        s4 = next(e for e in scenes["s004"]["elements"] if e["id"] == "arr_wall")
        assert s4["tail"]["el"] == "lbl_wall"
        assert parse_scene_response(scenes["s004"], _NARR["s004"]) is not None


class TestSemanticAdapter:
    """The semantic path had the same hole one layer up: _target() accepted
    any element name for an ARROW."""

    def test_e_unknown_arrow_target_anchors_to_the_root_and_is_reported(self):
        from spike.scene_engine.semantic import adapt_semantic_plan
        sem = {"chapters": [{
            "concept": "plant_cell", "transition": "clear_and_redraw",
            "assets": {"plant_cell": "A plant cell diagram"},
            "semantic_regions": ["cell_wall", "chloroplasts"],
            "elements": [
                {"id": "plant_cell_diagram", "type": "illustration",
                 "asset": "plant_cell", "role": "root_visual"},
                {"id": "lbl_wall", "type": "text", "text": "Cell wall"}],
            "steps": [
                {"segment": 3, "decision": "NEW_VISUAL", "actions": [
                    {"verb": "DRAW", "target": {"element": "plant_cell_diagram"},
                     "cue": "plant cell"}]},
                {"segment": 4, "decision": "EXTEND", "actions": [
                    {"verb": "WRITE", "target": {"element": "lbl_wall"},
                     "cue": "cell wall"},
                    {"verb": "ARROW", "target": {"element": "plant_cell_box",
                                                 "region": "cell_wall"},
                     "cue": "cell wall"}]}]}]}
        plan_raw, issues = adapt_semantic_plan(sem, _NARR)
        assert [i["code"] for i in issues] == ["UNKNOWN_ARROW_TARGET"]
        arrow = next(e for e in plan_raw["chapters"][0]["elements"]
                     if e["type"] == "arrow")
        assert arrow["head"]["el"] == "plant_cell_diagram"
        assert arrow["head"]["layer"] == "cell_wall"
        scenes, _, _ = _compile(plan_raw)
        for sid in ("s003", "s004"):
            assert parse_scene_response(scenes[sid], _NARR[sid]) is not None

    def test_strict_mode_still_refuses_it(self):
        from spike.scene_engine.semantic import AdapterError, adapt_semantic_plan
        sem = {"chapters": [{
            "concept": "c", "assets": {"a": "p"},
            "elements": [{"id": "root", "type": "illustration", "asset": "a"}],
            "steps": [{"segment": 3, "decision": "NEW_VISUAL", "actions": [
                {"verb": "ARROW", "target": {"element": "ghost",
                                             "region": "r"}}]}]}]}
        with pytest.raises(AdapterError):
            adapt_semantic_plan(sem, _NARR, strict=True)


class TestSceneGuardShape:
    def test_a_clean_scene_is_untouched(self):
        scene = {"elements": [
            {"id": "cell", "type": "illustration", "asset": "a", "at": [1, 2]},
            {"id": "ar", "type": "arrow", "tail": [0, 0],
             "head": {"el": "cell"}}],
            "actions": [{"verb": "draw", "target": "ar"}]}
        before = [dict(e) for e in scene["elements"]]
        assert resolve_scene_anchors(scene) == []
        assert scene["elements"] == before

    def test_roster_pass_reports_and_mutates_in_place(self):
        roster = {
            "cell": {"id": "cell", "type": "illustration", "asset": "a"},
            "ar": {"id": "ar", "type": "arrow", "tail": [0, 0],
                   "head": {"el": "cell_box"}}}
        notes, dropped = resolve_roster_anchors(roster, "cell")
        assert dropped == []
        assert roster["ar"]["head"]["el"] == "cell"
        assert notes == ["REANCHORED ar.head 'cell_box' -> cell (id match)"]
