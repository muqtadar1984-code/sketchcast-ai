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

    def test_a_generic_picture_ref_falls_to_the_single_root(self):
        el, layer, how = resolve_anchor("the_diagram", self.ROSTER,
                                        "plant_cell_diagram")
        assert (el, how) == ("plant_cell_diagram", "root visual")
        # ...as does one that names the picture by one of its own words
        el, _, how = resolve_anchor("cell_thing", self.ROSTER,
                                    "plant_cell_diagram")
        assert (el, how) == ("plant_cell_diagram", "root visual")
        # but a name sharing nothing with it is a guess, not a binding
        el, _, how = resolve_anchor("mystery_thing", self.ROSTER,
                                    "plant_cell_diagram")
        assert el is None and how == "no candidate"

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


class TestReviewFindings:
    """Adversarial review of the guard (a780f88): eight ways it still lost an
    arrow, bound the wrong thing, or mis-counted. Each pinned here, numbered
    as the review numbered them."""

    _CELL = {"id": "cell", "type": "illustration", "asset": "plant_cell",
             "at": [640, 360]}
    _LBL = {"id": "lbl_wall", "type": "text", "text": "Cell wall",
            "at": [95, 140]}
    _ARR = {"id": "arr_wall", "type": "arrow",
            "tail": {"el": "lbl_wall", "edge": "right", "dx": 6},
            "head": {"el": "cell", "edge": "center"}}

    @staticmethod
    def _manifest():
        return {"segments": [{"segment_id": s, "renderer": "scene",
                              "audio_path": "a.mp3"} for s in _NARR]}

    def _chapter(self, steps, **extra):
        return {"concept": "cell", "assets": {"plant_cell": "a cell"},
                "elements": [dict(self._CELL), dict(self._LBL), dict(self._ARR)],
                "steps": steps, **extra}

    # ── 1: validate.py accounting ───────────────────────────────────────
    def test_1_a_scene_level_drop_is_listed_not_subtracted(self):
        """The root erased under its arrow: s005 and s006 each drop the arrow
        from THEIR scene. One arrow, two lines — it used to be counted twice
        and the total went to -1."""
        raw = {"chapters": [self._chapter([
            {"segment": 3, "decision": "NEW_VISUAL",
             "actions": [{"verb": "draw", "target": "cell"},
                         {"verb": "write", "target": "lbl_wall"},
                         {"verb": "draw", "target": "arr_wall"}]},
            {"segment": 4, "decision": "EXTEND",
             "actions": [{"verb": "erase", "target": "cell"}]},
            {"segment": 5, "decision": "CONTINUE",
             "actions": [{"verb": "circle", "target": "lbl_wall"}]},
            {"segment": 6, "decision": "CONTINUE",
             "actions": [{"verb": "pulse", "target": "lbl_wall"}]}])]}
        plan = parse_visual_plan(raw)
        scenes, _, report = compile_plan(plan, _NARR, all_segments=list(_NARR),
                                         skip_hold=set())
        seg_drops = [ln for ln in report if ln.startswith("SEGMENT")
                     and "| DROPPED arrow arr_wall" in ln]
        assert len(seg_drops) == 2, report
        for sid in ("s003", "s004", "s005", "s006"):
            assert parse_scene_response(scenes[sid], _NARR[sid]) is not None
        r = validate_visual_language(
            self._manifest(), {"plan": plan.model_dump(), "report": report})
        synthesized = sum(1 for ln in report if "| SYNTHESIZED" in ln)
        assert r["arrows_dropped"] == []
        assert r["arrow_scene_drops"] == seg_drops
        assert r["arrow_count"] == 1 + synthesized   # drawn in s003 and s004
        assert r["arrow_count"] >= 0

    def test_1_chapter_drops_count_distinct_arrows_and_never_go_negative(self):
        plan = {"chapters": [{"elements": [{"id": "a1", "type": "arrow"}]}]}
        line = "CHAPTER c | DROPPED arrow a1 (tail anchor 'x' names no element)"
        r = validate_visual_language(self._manifest(), {
            "plan": plan,
            "report": [line, line, line,
                       "SEGMENT s003 | DROPPED arrow a1 (head anchor 'y' "
                       "names no element)"]})
        assert r["arrows_dropped"] == [line]
        assert r["arrow_count"] == 0
        assert len(r["arrow_scene_drops"]) == 1
        # the same id dropped in TWO chapters is two arrows gone
        plan2 = {"chapters": [{"elements": [{"id": "a1", "type": "arrow"}]},
                              {"elements": [{"id": "a1", "type": "arrow"}]}]}
        r2 = validate_visual_language(self._manifest(), {
            "plan": plan2,
            "report": ["CHAPTER c1 | DROPPED arrow a1 (tail anchor 'x' names "
                       "no element)",
                       "CHAPTER c2 | DROPPED arrow a1 (tail anchor 'x' names "
                       "no element)"]})
        assert len(r2["arrows_dropped"]) == 2 and r2["arrow_count"] == 0
        # a carry-out line is neither: the arrow had its scenes
        r3 = validate_visual_language(self._manifest(), {
            "plan": plan,
            "report": ["CHAPTER c | CARRY-OUT | LEFT BEHIND arrow a1 (head "
                       "anchor 'y' is not on the board)"]})
        assert r3["arrows_dropped"] == [] and r3["arrow_count"] == 1
        assert r3["arrow_scene_drops"] == []

    # ── 2: no cross-chapter re-binding ──────────────────────────────────
    def test_2_a_carried_arrow_never_binds_to_the_new_chapters_label(self):
        """Chapter 1's arrow was drawn before its label; chapter 2 has a label
        with a similar name. The fading old arrow once re-anchored to the
        NEW label being written in that very scene ('kind match')."""
        raw = {"chapters": [
            self._chapter([{"segment": 2, "decision": "NEW_VISUAL",
                            "actions": [{"verb": "draw", "target": "cell"},
                                        {"verb": "draw", "target": "arr_wall"}]}],
                          transition="clear_and_redraw"),
            {"concept": "leaf", "transition": "clear_and_redraw",
             "assets": {"leaf": "a leaf"},
             "elements": [{"id": "leaf", "type": "illustration",
                           "asset": "leaf", "at": [640, 360]},
                          {"id": "lbl_wall_leaf", "type": "text",
                           "text": "Leaf wall", "at": [95, 140]}],
             "steps": [{"segment": 4, "decision": "NEW_VISUAL",
                        "actions": [{"verb": "draw", "target": "leaf"},
                                    {"verb": "write", "target": "lbl_wall_leaf"}]}]}]}
        scenes, _, report = _compile(raw)
        prev = next(e for e in scenes["s004"]["elements"]
                    if e["id"] == "prev__arr_wall")
        assert prev["tail"] == [101.0, 140.0]           # sealed at carry-out
        assert prev["head"]["el"] == "prev__cell"
        assert not any("REANCHORED prev__" in ln for ln in report), report
        assert any(ln.startswith("CHAPTER cell | CARRY-OUT | FLATTENED "
                                 "arr_wall.tail 'lbl_wall'") for ln in report), report
        assert parse_scene_response(scenes["s004"], _NARR["s004"]) is not None

    def test_2_the_resolver_itself_refuses_to_rebind_a_carried_ref(self):
        from spike.scene_engine.anchors import is_carried_id
        assert is_carried_id("prev__x") and is_carried_id("_prev__x")
        assert not is_carried_id("preview") and not is_carried_id("__hud")
        roster = {
            "leaf": {"id": "leaf", "type": "illustration", "asset": "leaf"},
            "lbl_wall_leaf": {"id": "lbl_wall_leaf", "type": "text",
                              "text": "Leaf wall"},
            "prev__cell": {"id": "prev__cell", "type": "illustration",
                           "asset": "plant_cell"},
            "prev__arr_wall": {"id": "prev__arr_wall", "type": "arrow",
                               "tail": {"el": "lbl_wall"},
                               "head": {"el": "prev__cell"}},
        }
        notes, dropped = resolve_roster_anchors(roster, "leaf")
        assert dropped == ["prev__arr_wall"]
        assert "prev__arr_wall" not in roster
        assert notes == ["DROPPED arrow prev__arr_wall (tail anchor 'lbl_wall' "
                         "stayed behind in the previous chapter)"]

    # ── 3: the root-visual rung ─────────────────────────────────────────
    def test_3_a_tail_never_falls_to_the_root_visual(self):
        roster = TestResolveAnchor.ROSTER
        for ref in ("mystery_thing", "cell_thing", "the_diagram"):
            el, _, how = resolve_anchor(ref, roster, "plant_cell_diagram",
                                        end="tail")
            assert (el, how) == (None, "no candidate"), ref

    def test_3_a_text_like_ref_never_falls_to_the_root_visual(self):
        roster = {
            "plant_cell_diagram": {"id": "plant_cell_diagram",
                                   "type": "illustration", "asset": "plant_cell"},
            "title_1": {"id": "title_1", "type": "text", "text": "The plant cell"},
            "lbl_wall": {"id": "lbl_wall", "type": "text", "text": "Cell wall"},
        }
        parts = ["cell_wall", "nucleus"]
        for ref in ("title", "title_text", "heading", "eq1", "formula_2",
                    "step_2", "caption_a", "nucleus_label", "cell_caption",
                    "lbl_nucleus_label"):
            el, layer, how = resolve_anchor(ref, roster, "plant_cell_diagram",
                                            part_names=parts)
            assert el != "plant_cell_diagram", (ref, el, layer, how)
        # 'title' still finds the one title on the board — by kind, as text
        assert resolve_anchor("title", roster, "plant_cell_diagram")[0] == "title_1"

    def test_3_a_head_needs_a_shared_word_or_a_generic_picture_ref(self):
        roster = TestResolveAnchor.ROSTER
        ok = ("the_diagram", "diagram", "main_visual", "cell_thing",
              "plant_thing")
        for ref in ok:
            assert resolve_anchor(ref, roster, "plant_cell_diagram") == \
                ("plant_cell_diagram", None, "root visual"), ref
        for ref in ("mystery_thing", "organelle", "wall_box"):
            el, _, _ = resolve_anchor(ref, roster, "plant_cell_diagram")
            assert el is None, ref
        # a part name counts as one of the picture's own words
        el, layer, how = resolve_anchor("wall_thing", roster,
                                        "plant_cell_diagram",
                                        part_names=["cell_wall"])
        assert (el, how) == ("plant_cell_diagram", "root visual")

    def test_3_the_director_never_makes_a_root_to_root_arrow(self):
        raw = {"id": "vc_s004", "compiled": True, "elements": [
            {"id": "plant_cell_diagram", "type": "illustration",
             "asset": "plant_cell", "at": [640, 360]},
            {"id": "title_1", "type": "text", "text": "The plant cell",
             "at": [640, 60]},
            {"id": "arrow_plant", "type": "arrow",
             "tail": {"el": "title", "edge": "bottom"},
             "head": {"el": "plant_cell_diagram", "edge": "center"}}],
            "actions": [{"verb": "draw", "target": "plant_cell_diagram"},
                        {"verb": "write", "target": "title_1"},
                        {"verb": "draw", "target": "arrow_plant"}]}
        scene = parse_scene_response(raw, "n")
        assert scene is not None
        arrow = next(e for e in scene.elements if e.id == "arrow_plant")
        assert arrow.tail.el == "title_1"
        assert arrow.tail.el != arrow.head.el
        # with NO text to mean, the arrow goes — its tail never lands on the
        # picture it points at
        raw2 = {"id": "vc_s004", "compiled": True,
                "elements": [raw["elements"][0], raw["elements"][2]],
                "actions": [raw["actions"][0], raw["actions"][2]]}
        scene2 = parse_scene_response(raw2, "n")
        assert scene2 is not None
        assert [e.id for e in scene2.elements] == ["plant_cell_diagram"]

    # ── 4: HOLD scenes ──────────────────────────────────────────────────
    def test_4_a_hold_scene_flattens_instead_of_dropping(self, caplog):
        """Arrow drawn in s003 before its label, s004 unplanned (HOLD), label
        written in s005. The arrow used to be visible, gone, back."""
        raw = {"chapters": [self._chapter([
            {"segment": 3, "decision": "NEW_VISUAL",
             "actions": [{"verb": "draw", "target": "cell"},
                         {"verb": "draw", "target": "arr_wall"}]},
            {"segment": 5, "decision": "EXTEND",
             "actions": [{"verb": "write", "target": "lbl_wall"}]}])]}
        scenes, _, report = _compile(raw)
        assert any("SEGMENT s004 | chapter: cell | decision: HOLD" in ln
                   for ln in report), report
        tails = {sid: next(e for e in scenes[sid]["elements"]
                           if e["id"] == "arr_wall")["tail"]
                 for sid in ("s003", "s004", "s005")}
        assert tails["s003"] == [101.0, 140.0]
        assert tails["s004"] == [101.0, 140.0]
        assert tails["s005"] == {"el": "lbl_wall", "edge": "right", "dx": 6}
        assert not any("DROPPED arrow" in ln for ln in report), report
        assert any(ln.startswith("SEGMENT s004 | FLATTENED arr_wall.tail "
                                 "'lbl_wall'") for ln in report), report
        with caplog.at_level(logging.WARNING, logger="spike.scene_engine.director"):
            for sid in ("s003", "s004", "s005"):
                assert parse_scene_response(scenes[sid], _NARR[sid]) is not None
        assert "DROPPED" not in caplog.text

    # ── 5: the part-name rung ───────────────────────────────────────────
    def test_5_the_part_rung_uses_the_layer_matcher(self):
        roster = TestResolveAnchor.ROSTER
        parts = ["cell_wall", "chloroplasts", "nucleus"]
        for ref, layer in (("chloroplast", "chloroplasts"),
                           ("wall_box", "cell_wall"),
                           ("nucleus_membrane", "nucleus"),
                           ("Nucleus", "nucleus")):
            assert resolve_anchor(ref, roster, "plant_cell_diagram",
                                  part_names=parts) == \
                ("plant_cell_diagram", layer, "part name"), ref

    def test_5_an_ambiguous_part_match_binds_no_layer(self):
        roster = {"diagram_root": {"id": "diagram_root", "type": "illustration",
                                   "asset": "plant_cell"}}
        el, layer, how = resolve_anchor("cell", roster, "diagram_root",
                                        part_names=["cell_wall", "cell_membrane"])
        assert (el, layer, how) == ("diagram_root", None, "root visual")

    # ── 6: the director guard is consistent ─────────────────────────────
    def test_6_a_duplicate_id_is_rejected_even_beside_a_dangling_anchor(self):
        dup = [{"id": "cell", "type": "illustration", "asset": "a", "at": [1, 2]},
               {"id": "cell", "type": "illustration", "asset": "b", "at": [5, 5]}]
        with_anchor = {"id": "x", "elements": dup + [
            {"id": "ar", "type": "arrow", "tail": [0, 0],
             "head": {"el": "cell_box"}}],
            "actions": [{"verb": "draw", "target": "cell"},
                        {"verb": "draw", "target": "ar"}]}
        without = {"id": "x", "elements": list(dup),
                   "actions": [{"verb": "draw", "target": "cell"}]}
        assert parse_scene_response(without, "n") is None
        assert parse_scene_response(with_anchor, "n") is None
        # the guard itself keeps both entries for the schema to see
        scene = {"elements": dup + [{"id": "ar", "type": "arrow",
                                     "tail": [0, 0], "head": {"el": "cell_box"}}],
                 "actions": []}
        notes = resolve_scene_anchors(scene)
        assert notes == ["REANCHORED ar.head 'cell_box' -> cell (id match)"]
        assert [e["id"] for e in scene["elements"]] == ["cell", "cell", "ar"]
        assert scene["elements"][1]["asset"] == "b"       # not the first's copy

    def test_6_an_id_less_element_still_reaches_the_schema(self):
        scene = {"elements": [
            {"id": "cell", "type": "illustration", "asset": "a", "at": [1, 2]},
            {"type": "text", "text": "orphan", "at": [3, 4]},
            {"id": "ar", "type": "arrow", "tail": [0, 0],
             "head": {"el": "cell_box"}}], "actions": []}
        resolve_scene_anchors(scene)
        assert len(scene["elements"]) == 3
        assert scene["elements"][1] == {"type": "text", "text": "orphan",
                                        "at": [3, 4]}

    # ── 7: after-chains ─────────────────────────────────────────────────
    def test_7_an_after_naming_a_later_text_is_unchained_not_rejected(self):
        raw = {"id": "t", "elements": [
            {"id": "b", "type": "text", "text": "5x", "at": [200, 200],
             "after": {"el": "eq_a_text", "gap": 2}},
            {"id": "eq_a", "type": "text", "text": "x2 +", "at": [100, 200]}],
            "actions": [{"verb": "write", "target": "b"},
                        {"verb": "write", "target": "eq_a"}]}
        scene = parse_scene_response(raw, "n")
        assert scene is not None
        b = next(e for e in scene.elements if e.id == "b")
        assert b.after is None and list(b.at) == [200, 200]
        # the same ref with the match EARLIER is re-anchored, not unchained
        raw2 = {"id": "t", "elements": list(reversed(raw["elements"])),
                "actions": list(reversed(raw["actions"]))}
        scene2 = parse_scene_response(raw2, "n")
        assert scene2 is not None
        b2 = next(e for e in scene2.elements if e.id == "b")
        assert b2.after is not None and b2.after.el == "eq_a"

    # ── 8: an arrow ahead of its picture ────────────────────────────────
    def test_8_an_arrow_to_a_picture_drawn_next_step_flattens_and_rides_on(self):
        raw = {"chapters": [self._chapter([
            {"segment": 3, "decision": "NEW_VISUAL",
             "actions": [{"verb": "write", "target": "lbl_wall"},
                         {"verb": "draw", "target": "arr_wall"}]},
            {"segment": 4, "decision": "EXTEND",
             "actions": [{"verb": "draw", "target": "cell"}]}])]}
        scenes, _, report = _compile(raw)
        s3 = next(e for e in scenes["s003"]["elements"] if e["id"] == "arr_wall")
        assert s3["head"] == [640.0, 360.0]
        assert s3["tail"]["el"] == "lbl_wall"
        assert not any(e["id"] == "cell" for e in scenes["s003"]["elements"])
        s4 = next(e for e in scenes["s004"]["elements"] if e["id"] == "arr_wall")
        assert s4["head"] == {"el": "cell", "edge": "center"}
        assert not any("DROPPED" in ln for ln in report), report
        assert any(ln.startswith("SEGMENT s003 | FLATTENED arr_wall.head 'cell'")
                   for ln in report)
        for sid in ("s003", "s004"):
            assert parse_scene_response(scenes[sid], _NARR[sid]) is not None

    # ── found while fixing 2/4: a group must not outlive a dropped child ──
    def test_a_group_naming_a_dropped_arrow_is_pruned_not_left_dangling(self):
        base = [{"id": "cell", "type": "illustration", "asset": "a", "at": [1, 2]},
                {"id": "lbl_a", "type": "text", "text": "A", "at": [3, 4]},
                {"id": "lbl_b", "type": "text", "text": "B", "at": [3, 8]},
                {"id": "ar", "type": "arrow", "tail": {"el": "lbl_ghost"},
                 "head": {"el": "cell"}}]
        # (i) the group loses only that child
        scene = {"id": "g1", "elements": base + [
            {"id": "g", "type": "group", "children": ["ar", "cell"]}],
            "actions": [{"verb": "draw", "target": "g"}]}
        notes = resolve_scene_anchors(scene)
        assert "DROPPED arrow ar (tail anchor 'lbl_ghost' names no element)" in notes
        g = next(e for e in scene["elements"] if e["id"] == "g")
        assert g["children"] == ["cell"]
        assert scene["actions"] == [{"verb": "draw", "target": "g"}]
        assert parse_scene_response(scene, "n") is not None
        # (ii) a group left empty goes, with the actions that addressed it
        scene = {"id": "g2", "elements": base + [
            {"id": "g", "type": "group", "children": ["ar"]}],
            "actions": [{"verb": "draw", "target": "cell"},
                        {"verb": "draw", "target": "g"},
                        {"verb": "circle", "target": "g"}]}
        notes = resolve_scene_anchors(scene)
        assert "DROPPED group g (every child was dropped)" in notes
        assert [e["id"] for e in scene["elements"]] == ["cell", "lbl_a", "lbl_b"]
        assert scene["actions"] == [{"verb": "draw", "target": "cell"}]
        assert parse_scene_response(scene, "n") is not None

    def test_a_hold_scene_prunes_a_group_whose_arrow_lost_its_target(self):
        """An arrow to a SHAPE (no planned point to flatten to), grouped;
        the shape is erased, then an unplanned segment holds the board. The
        held scene once carried the arrow (dangling) inside its group: the
        director dropped the arrow, the group then named a ghost, and the
        schema threw the whole board away."""
        raw = {"chapters": [{
            "concept": "cell", "assets": {"plant_cell": "a cell"},
            "elements": [
                dict(self._CELL),
                {"id": "ring", "type": "shape", "shape": "ellipse",
                 "center": [640, 360], "rx": 40, "ry": 40},
                {"id": "arr", "type": "arrow", "tail": [100, 100],
                 "head": {"el": "ring", "edge": "center"}},
                {"id": "g", "type": "group", "children": ["arr"]}],
            "steps": [
                {"segment": 3, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "cell"},
                             {"verb": "draw", "target": "ring"},
                             {"verb": "draw", "target": "g"}]},
                {"segment": 4, "decision": "EXTEND",
                 "actions": [{"verb": "erase", "target": "ring"}]},
                {"segment": 6, "decision": "CONTINUE",
                 "actions": [{"verb": "circle", "target": "cell"}]}]}]}
        scenes, _, report = _compile(raw)
        assert any("SEGMENT s005 | chapter: cell | decision: HOLD" in ln
                   for ln in report), report
        ids = [e["id"] for e in scenes["s005"]["elements"]
               if not e["id"].startswith("__")]
        assert ids == ["cell"], ids
        assert "SEGMENT s005 | DROPPED arrow arr (head anchor 'ring' is not " \
               "on the board)" in report
        assert "SEGMENT s005 | DROPPED group g (every child left the board)" \
            in report
        for sid in ("s003", "s004", "s005", "s006"):
            assert parse_scene_response(scenes[sid], _NARR[sid]) is not None, sid


class TestThirdReviewFindings:
    """Third adversarial pass over the guard: five more ways it lost a board,
    bound the wrong thing, or reported half the truth. Numbered as the review
    numbered them."""

    _CELL = {"id": "cell", "type": "illustration", "asset": "plant_cell",
             "at": [640, 360]}

    @staticmethod
    def _manifest():
        return {"segments": [{"segment_id": s, "renderer": "scene",
                              "audio_path": "a.mp3"} for s in _NARR]}

    @staticmethod
    def _ghost_roster(**extra):
        """A chapter roster whose arrow tail names nothing: two labels, so no
        unique text to fall to, and the ref reads like a label so it never
        reaches the picture."""
        roster = {
            "cell": {"id": "cell", "type": "illustration",
                     "asset": "plant_cell", "at": [640, 360]},
            "lbl_a": {"id": "lbl_a", "type": "text", "text": "A",
                      "at": [95, 140]},
            "lbl_b": {"id": "lbl_b", "type": "text", "text": "B",
                      "at": [95, 220]},
            "arr": {"id": "arr", "type": "arrow",
                    "tail": {"el": "lbl_ghost"}, "head": {"el": "cell"}},
        }
        roster.update(extra)
        return roster

    # ── 1: a chapter-level drop must prune the groups that named it ──────
    def test_1_a_chapter_group_loses_only_the_dropped_child(self):
        roster = self._ghost_roster(
            g={"id": "g", "type": "group", "children": ["arr", "cell"]})
        notes, dropped = resolve_roster_anchors(roster, "cell")
        assert dropped == ["arr"]
        assert roster["g"]["children"] == ["cell"]
        assert ("DROPPED arrow arr (tail anchor 'lbl_ghost' names no element)"
                in notes)

    def test_1_a_chapter_group_left_empty_is_dropped_and_cascades(self):
        roster = self._ghost_roster(
            g={"id": "g", "type": "group", "children": ["arr"]},
            gg={"id": "gg", "type": "group", "children": ["g"]})
        notes, dropped = resolve_roster_anchors(roster, "cell")
        assert dropped == ["arr", "g", "gg"]
        assert "g" not in roster and "gg" not in roster
        assert notes[-2:] == ["DROPPED group g (every child was dropped)",
                              "DROPPED group gg (every child was dropped)"]

    def test_1_the_guards_own_drop_no_longer_costs_the_whole_scene(self):
        """End to end: the arrow the CHAPTER guard drops was named by a group,
        so every scene the group rode into failed the schema with "group
        references unknown" — the guard making the failure it exists to
        prevent."""
        raw = {"chapters": [{
            "concept": "cell", "assets": {"plant_cell": "a cell"},
            "elements": [
                dict(self._CELL),
                {"id": "lbl_a", "type": "text", "text": "A", "at": [95, 140]},
                {"id": "lbl_b", "type": "text", "text": "B", "at": [95, 220]},
                {"id": "arr", "type": "arrow", "tail": {"el": "lbl_ghost"},
                 "head": {"el": "cell", "edge": "center"}},
                {"id": "g", "type": "group", "children": ["arr", "lbl_a"]}],
            "steps": [
                {"segment": 3, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "cell"},
                             {"verb": "write", "target": "lbl_b"}]},
                {"segment": 4, "decision": "EXTEND",
                 "actions": [{"verb": "draw", "target": "g"}]},
                {"segment": 5, "decision": "CONTINUE",
                 "actions": [{"verb": "circle", "target": "g"}]}]}]}
        scenes, _, report = _compile(raw)
        assert ("CHAPTER cell | DROPPED arrow arr (tail anchor 'lbl_ghost' "
                "names no element)") in report
        for sid in ("s003", "s004", "s005"):
            assert parse_scene_response(scenes[sid], _NARR[sid]) is not None, sid
            for e in scenes[sid]["elements"]:
                if e.get("type") == "group":
                    assert "arr" not in e["children"], (sid, e)

    def test_1_a_chapter_group_whose_only_child_went_is_reported(self):
        raw = {"chapters": [{
            "concept": "cell", "assets": {"plant_cell": "a cell"},
            "elements": [
                dict(self._CELL),
                {"id": "lbl_a", "type": "text", "text": "A", "at": [95, 140]},
                {"id": "lbl_b", "type": "text", "text": "B", "at": [95, 220]},
                {"id": "arr", "type": "arrow", "tail": {"el": "lbl_ghost"},
                 "head": {"el": "cell", "edge": "center"}},
                {"id": "g", "type": "group", "children": ["arr"]}],
            "steps": [
                {"segment": 3, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "cell"}]},
                {"segment": 4, "decision": "EXTEND",
                 "actions": [{"verb": "draw", "target": "g"},
                             {"verb": "write", "target": "lbl_a"}]}]}]}
        scenes, _, report = _compile(raw)
        assert ("CHAPTER cell | DROPPED group g (every child was dropped)"
                in report)
        # g is gone from the roster, so the step that drew it drew nothing
        # (the only groups left are the compiler's own overlays)
        assert not any(e["id"] == "g" or "arr" in (e.get("children") or [])
                       for e in scenes["s004"]["elements"])
        assert ("SEGMENT s004 | DROPPED draw->g (not on the board, not "
                "introduced this step)") in report
        for sid in ("s003", "s004"):
            assert parse_scene_response(scenes[sid], _NARR[sid]) is not None, sid

    # ── 2: an arrow may never bind both ends to one element ──────────────
    def test_2_a_head_tries_the_pictures_parts_before_the_label(self):
        roster = {"cell": {"id": "cell", "type": "illustration",
                           "asset": "plant_cell"},
                  "lbl_nuc": {"id": "lbl_nuc", "type": "text",
                              "text": "Nucleus"}}
        parts = ["nucleus", "cell_wall"]
        # the HEAD points AT the picture: its part wins over the label whose
        # words are the same...
        assert resolve_anchor("nucleus", roster, "cell", part_names=parts,
                              end="head") == ("cell", "nucleus", "part name")
        # ...while the TAIL, which comes FROM the writing, still finds it
        assert resolve_anchor("nucleus", roster, "cell", part_names=parts,
                              end="tail") == ("lbl_nuc", None, "text match")

    def test_2_both_ends_on_one_element_drops_the_arrow(self):
        roster = {"cell": {"id": "cell", "type": "illustration",
                           "asset": "plant_cell"},
                  "lbl_nuc": {"id": "lbl_nuc", "type": "text",
                              "text": "Nucleus"},
                  "arr": {"id": "arr", "type": "arrow",
                          "tail": {"el": "nucleus_label"},
                          "head": {"el": "nucleus_text"}}}
        notes, dropped = resolve_roster_anchors(roster, "cell")
        assert dropped == ["arr"] and "arr" not in roster
        assert notes[-1] == ("DROPPED arrow arr (both ends resolve to "
                             "lbl_nuc; it would draw nothing)")

    def test_2_root_to_root_is_dropped_too(self):
        roster = {"cell": {"id": "cell", "type": "illustration",
                           "asset": "plant_cell"},
                  "arr": {"id": "arr", "type": "arrow",
                          "tail": {"el": "cell_box"},
                          "head": {"el": "cell_diagram"}}}
        notes, dropped = resolve_roster_anchors(roster, "cell")
        assert dropped == ["arr"]
        assert notes[-1].startswith("DROPPED arrow arr (both ends resolve to "
                                    "cell;")

    def test_2_two_parts_of_one_picture_still_make_an_arrow(self):
        roster = {"diagram_root": {"id": "diagram_root", "type": "illustration",
                                   "asset": "plant_cell"},
                  "arr": {"id": "arr", "type": "arrow",
                          "tail": {"el": "cell_wall"},
                          "head": {"el": "nucleus"}}}
        notes, dropped = resolve_roster_anchors(
            roster, "diagram_root", part_names=["cell_wall", "nucleus"])
        assert dropped == [], notes
        arr = roster["arr"]
        assert arr["tail"] == {"el": "diagram_root", "layer": "cell_wall"}
        assert arr["head"] == {"el": "diagram_root", "layer": "nucleus"}

    def test_2_the_director_drops_a_self_pointing_arrow_and_keeps_the_board(
            self, caplog):
        raw = {"id": "vc_s004", "elements": [
            {"id": "cell", "type": "illustration", "asset": "plant_cell",
             "at": [640, 360]},
            {"id": "lbl_nuc", "type": "text", "text": "Nucleus",
             "at": [95, 140]},
            {"id": "arr", "type": "arrow", "tail": {"el": "nucleus_label"},
             "head": {"el": "nucleus_text"}}],
            "actions": [{"verb": "draw", "target": "cell"},
                        {"verb": "write", "target": "lbl_nuc"},
                        {"verb": "draw", "target": "arr"}]}
        with caplog.at_level(logging.WARNING,
                             logger="spike.scene_engine.director"):
            scene = parse_scene_response(raw, "n")
        assert scene is not None
        assert [e.id for e in scene.elements] == ["cell", "lbl_nuc"]
        assert [a.verb for a in scene.actions] == ["draw", "write"]
        assert "both ends resolve to lbl_nuc" in caplog.text

    # ── 3: the kind rung matches on token boundaries ─────────────────────
    def test_3_a_substring_is_not_a_match(self):
        # 'ion' is not 'region': the old rung bound any same-kind element
        # whose key merely CONTAINED the ref
        roster = {"region_map": {"id": "region_map", "type": "illustration",
                                 "asset": "map"}}
        assert resolve_anchor("ion", roster, None)[0] is None

    def test_3_a_trailing_numeral_must_be_the_same_number(self):
        roster = {"label_10": {"id": "label_10", "type": "text",
                               "text": "Ten"}}
        assert resolve_anchor("label_1", roster, None)[0] is None
        roster["label_1"] = {"id": "label_1", "type": "text", "text": "One"}
        assert resolve_anchor("label_1", roster, None)[0] == "label_1"

    def test_3_a_whole_token_run_still_matches(self):
        roster = {"cell_wall_shape": {"id": "cell_wall_shape",
                                      "type": "illustration", "asset": "w"}}
        el, _, how = resolve_anchor("wall", roster, None)
        assert (el, how) == ("cell_wall_shape", "kind match")

    # ── 4: honest arrow accounting ───────────────────────────────────────
    def test_4_the_report_counts_arrows_not_lines(self):
        plan = {"chapters": [{"elements": [
            {"id": f"arr{i}", "type": "arrow"} for i in range(1, 5)]}]}
        report = [
            "CHAPTER cell | REANCHORED arr1.head 'cell_box' -> cell (id match)",
            "SEGMENT s003 | REANCHORED arr2.tail 'x' -> lbl_a (kind match)",
            "SEGMENT s004 | REANCHORED arr2.tail 'x' -> lbl_a (kind match)",
            "SEGMENT s005 | REANCHORED arr2.tail 'x' -> lbl_a (kind match)",
            "SEGMENT s003 | REANCHORED t1.after 'ghost' -> t0 (text match)",
            "SEGMENT s003 | FLATTENED arr3.tail 'lbl_a' -> [1.0, 2.0] (not on "
            "the board yet)",
            "SEGMENT s004 | FLATTENED arr3.tail 'lbl_a' -> [1.0, 2.0] (not on "
            "the board yet)",
            "CHAPTER cell | CARRY-OUT | LEFT BEHIND arrow arr4 (head anchor "
            "'ring' is not on the board)",
            "SEGMENT s006 | UNCHAINED text t2 (after 'ghost' names no earlier "
            "element)",
        ]
        r = validate_visual_language(self._manifest(),
                                     {"plan": plan, "report": report})
        # arr1 once, arr2 once however many boards it rode into
        assert r["arrows_reanchored"] == 2
        # a text chain is not an arrow
        assert r["texts_rechained"] == 1
        assert len(r["texts_unchained"]) == 1
        assert len(r["arrows_flattened"]) == 1
        assert r["arrows_left_behind"] == [report[7]]
        assert r["arrow_count"] == 4        # nothing here removes an arrow

    def test_4_a_real_compile_shows_its_flattened_ends(self):
        raw = {"chapters": [{
            "concept": "cell", "assets": {"plant_cell": "a cell"},
            "elements": [
                dict(self._CELL),
                {"id": "lbl_wall", "type": "text", "text": "Cell wall",
                 "at": [95, 140]},
                {"id": "arr_wall", "type": "arrow",
                 "tail": {"el": "lbl_wall", "edge": "right", "dx": 6},
                 "head": {"el": "cell", "edge": "center"}}],
            "steps": [
                {"segment": 3, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "cell"},
                             {"verb": "draw", "target": "arr_wall"}]},
                {"segment": 5, "decision": "EXTEND",
                 "actions": [{"verb": "write", "target": "lbl_wall"}]}]}]}
        plan = parse_visual_plan(raw)
        _, _, report = compile_plan(plan, _NARR, all_segments=list(_NARR),
                                    skip_hold=set())
        r = validate_visual_language(self._manifest(),
                                     {"plan": plan.model_dump(),
                                      "report": report})
        # s003 and s004 both flatten arr_wall.tail: one arrow, one entry
        assert len([ln for ln in report
                    if "| FLATTENED arr_wall.tail" in ln]) == 2
        assert len(r["arrows_flattened"]) == 1
        assert r["arrows_flattened"][0].startswith("SEGMENT s003 | FLATTENED")

    # ── 5: a morph names its destination in `into`, not `target` ─────────
    def test_5_a_morph_into_a_dropped_arrow_goes_with_it(self):
        scene = {"id": "m", "elements": [
            dict(self._CELL),
            {"id": "lbl_a", "type": "text", "text": "A", "at": [95, 140]},
            {"id": "lbl_b", "type": "text", "text": "B", "at": [95, 220]},
            {"id": "arr", "type": "arrow", "tail": {"el": "lbl_ghost"},
             "head": {"el": "cell"}}],
            "actions": [{"verb": "draw", "target": "cell"},
                        {"verb": "morph", "target": "cell", "into": "arr"}]}
        notes = resolve_scene_anchors(scene)
        assert "DROPPED morph into arr (that element was dropped)" in notes
        assert scene["actions"] == [{"verb": "draw", "target": "cell"}]
        # ...and the board survives: the morph used to reach the schema as
        # "morph into unknown element" and cost the whole scene
        assert parse_scene_response(scene, "n") is not None

    def test_5_the_compiler_drops_a_morph_into_a_dropped_arrow(self):
        raw = {"chapters": [{
            "concept": "cell", "assets": {"plant_cell": "a cell"},
            "elements": [
                dict(self._CELL),
                {"id": "lbl_a", "type": "text", "text": "A", "at": [95, 140]},
                {"id": "lbl_b", "type": "text", "text": "B", "at": [95, 220]},
                {"id": "arr", "type": "arrow", "tail": {"el": "lbl_ghost"},
                 "head": {"el": "cell", "edge": "center"}}],
            "steps": [
                {"segment": 3, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "cell"},
                             {"verb": "write", "target": "lbl_a"}]},
                {"segment": 4, "decision": "EXTEND",
                 "actions": [{"verb": "morph", "target": "cell",
                              "into": "arr"},
                             {"verb": "write", "target": "lbl_b"}]}]}]}
        scenes, _, report = _compile(raw)
        assert ("SEGMENT s004 | DROPPED morph into arr (not on the board, not "
                "introduced this step)") in report
        assert not any(a.get("verb") == "morph"
                       for a in scenes["s004"]["actions"])
        for sid in ("s003", "s004"):
            assert parse_scene_response(scenes[sid], _NARR[sid]) is not None, sid
