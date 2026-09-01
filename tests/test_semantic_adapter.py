"""Semantic-plan adapter: every supported element, verb, transition, target
form, cue mapping and avatar moment — plus an end-to-end multi-chapter
regression that renders through the real compiler.

The adapter is the joint between the intelligence layer and the rendering
layer, so the bar here is higher than "it parses": each case asserts what
actually reaches the board, and the strict mode asserts that nothing is ever
dropped SILENTLY.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spike.scene_engine.continuity import compile_plan, parse_visual_plan
from spike.scene_engine.schema import Scene
from spike.scene_engine.semantic import (AdapterError, adapt_semantic_plan)


NARR = {
    "s001": "Here is a right-angled triangle. The hypotenuse is the longest side.",
    "s002": "The base sits at the bottom. Every triangle has three sides.",
    "s003": "Now look at the whole shape again.",
}


def _plan(**over):
    ch = {
        "id": "chapter_1", "concept": "triangle_sides", "transition": "continue",
        "assets": {"triangle": "A right-angled triangle drawn in outline"},
        "semantic_regions": ["hypotenuse", "base"],
        "elements": [
            {"id": "tri", "type": "illustration", "asset": "triangle",
             "role": "root_visual"},
            {"id": "lbl_hyp", "type": "text", "text": "Hypotenuse", "role": "label"},
            {"id": "lbl_base", "type": "text", "text": "Base", "role": "label"},
        ],
        "steps": [
            {"segment": 1, "decision": "EXTEND", "reason": "introduce the shape",
             "actions": [
                 {"verb": "DRAW", "target": {"element": "tri"},
                  "cue": "a right-angled triangle"},
                 {"verb": "WRITE", "target": {"element": "lbl_hyp"},
                  "cue": "The hypotenuse"},
                 {"verb": "ARROW",
                  "target": {"asset": "triangle", "region": "hypotenuse"},
                  "cue": "the longest side"}]},
            {"segment": 2, "decision": "FOCUS", "reason": "move attention",
             "actions": [
                 {"verb": "HIGHLIGHT", "target": {"element": "tri"},
                  "cue": "The base"}]},
        ],
    }
    ch.update(over)
    return {"chapters": [ch]}


class TestTargets:
    def test_element_target_becomes_an_element_id(self):
        plan, issues = adapt_semantic_plan(_plan(), NARR, strict=True)
        a = plan["chapters"][0]["steps"][0]["actions"][0]
        assert a == {"verb": "draw", "target": "tri",
                     "at": {"phrase": "a right-angled triangle"}}

    def test_asset_region_target_resolves_to_the_root_element_plus_layer(self):
        plan, _ = adapt_semantic_plan(_plan(), NARR, strict=True)
        arrow = next(e for e in plan["chapters"][0]["elements"]
                     if e.get("type") == "arrow")
        assert arrow["head"] == {"el": "tri", "layer": "hypotenuse",
                                 "edge": "center"}
        # tail hooks the label that names the same region
        assert arrow["tail"]["el"] == "lbl_hyp"

    def test_bare_string_target_is_accepted(self):
        p = _plan()
        p["chapters"][0]["steps"][0]["actions"][0]["target"] = "tri"
        plan, _ = adapt_semantic_plan(p, NARR, strict=True)
        assert plan["chapters"][0]["steps"][0]["actions"][0]["target"] == "tri"

    def test_unresolvable_target_is_reported_not_silently_dropped(self):
        p = _plan()
        p["chapters"][0]["steps"][0]["actions"][0]["target"] = {}
        _, issues = adapt_semantic_plan(p, NARR)
        assert any(i["code"] == "UNRESOLVED_TARGET" for i in issues)
        with pytest.raises(AdapterError):
            adapt_semantic_plan(p, NARR, strict=True)

    def test_draw_with_a_region_becomes_narration_ordered(self):
        p = _plan()
        p["chapters"][0]["steps"][0]["actions"][0]["target"] = {
            "asset": "triangle", "region": "hypotenuse"}
        plan, _ = adapt_semantic_plan(p, NARR, strict=True)
        assert plan["chapters"][0]["steps"][0]["actions"][0]["region"] == "hypotenuse"
        # and it must SURVIVE the trust boundary, not be stripped there
        parsed = parse_visual_plan(plan)
        act = parsed.chapters[0].steps[0].actions[0]
        assert act["region"] == "hypotenuse"
        scenes, _, _ = compile_plan(parsed, NARR, all_segments=["s001", "s002"],
                                    skip_hold=set())
        drawn = next(a for a in scenes["s001"]["actions"]
                     if a["verb"] == "draw" and a["target"] == "tri")
        assert drawn.get("region") == "hypotenuse"


class TestVerbs:
    @pytest.mark.parametrize("semantic,engine", [
        ("DRAW", "draw"), ("WRITE", "write"), ("POINT", "circle"),
        ("HIGHLIGHT", "highlight"), ("CIRCLE", "circle"),
        ("UNDERLINE", "underline"), ("ZOOM", "zoom"), ("ERASE", "erase"),
    ])
    def test_each_semantic_verb_maps(self, semantic, engine):
        p = _plan()
        p["chapters"][0]["steps"][0]["actions"] = [
            {"verb": semantic, "target": {"element": "tri"},
             "cue": "a right-angled triangle"}]
        plan, _ = adapt_semantic_plan(p, NARR, strict=True)
        assert plan["chapters"][0]["steps"][0]["actions"][0]["verb"] == engine

    def test_transform_with_into_becomes_morph(self):
        p = _plan()
        p["chapters"][0]["steps"][0]["actions"] = [
            {"verb": "TRANSFORM", "target": {"element": "tri"}, "into": "lbl_base",
             "cue": "a right-angled triangle"}]
        plan, _ = adapt_semantic_plan(p, NARR, strict=True)
        a = plan["chapters"][0]["steps"][0]["actions"][0]
        assert a["verb"] == "morph" and a["into"] == "lbl_base"

    def test_transform_without_into_emphasises_and_reports(self):
        p = _plan()
        p["chapters"][0]["steps"][0]["actions"] = [
            {"verb": "TRANSFORM", "target": {"element": "tri"},
             "cue": "a right-angled triangle"}]
        plan, issues = adapt_semantic_plan(p, NARR)
        assert plan["chapters"][0]["steps"][0]["actions"][0]["verb"] == "pulse"
        assert any(i["code"] == "TRANSFORM_WITHOUT_TARGET_FORM" for i in issues)

    def test_clear_and_redraw_as_an_action_becomes_the_decision(self):
        p = _plan()
        p["chapters"][0]["steps"][0]["actions"] = [
            {"verb": "CLEAR_AND_REDRAW", "reason": "new concept"}]
        plan, _ = adapt_semantic_plan(p, NARR, strict=True)
        st = plan["chapters"][0]["steps"][0]
        assert st["decision"] == "CLEAR_AND_REDRAW" and st["actions"] == []

    def test_unknown_verb_is_reported(self):
        p = _plan()
        p["chapters"][0]["steps"][0]["actions"] = [
            {"verb": "SPARKLE", "target": {"element": "tri"}}]
        _, issues = adapt_semantic_plan(p, NARR)
        assert any(i["code"] == "UNSUPPORTED_VERB" for i in issues)
        with pytest.raises(AdapterError):
            adapt_semantic_plan(p, NARR, strict=True)

    def test_arrow_without_a_region_is_reported(self):
        p = _plan()
        p["chapters"][0]["steps"][0]["actions"] = [
            {"verb": "ARROW", "target": {"element": "tri"}}]
        _, issues = adapt_semantic_plan(p, NARR)
        assert any(i["code"] == "ARROW_WITHOUT_REGION" for i in issues)


class TestElements:
    def test_illustration_and_labels_get_geometry(self):
        plan, _ = adapt_semantic_plan(_plan(), NARR, strict=True)
        els = {e["id"]: e for e in plan["chapters"][0]["elements"]}
        assert els["tri"]["at"] == [600.0, 380.0] and els["tri"]["scale"] == 1.0
        assert els["lbl_hyp"]["at"][0] == 95.0
        # labels stack, they do not overlap at birth
        assert els["lbl_base"]["at"][1] > els["lbl_hyp"]["at"][1]

    def test_a_full_label_column_clears_the_avatar_zone(self):
        """Seven organelle labels on the old fixed 78px pitch ran to y=608.
        The student avatar's keep-out starts at 424, so the renderer shoved
        the last three up into a column that was already full and they
        clamped on top of each other at the safe-area top — the founder's
        "1 label overwriting another", reported by the new TEXT_OVERLAP
        audit on a real render."""
        from spike.scene_engine.semantic import (_LABEL_FLOOR, _LABEL_H,
                                                 LABEL_COLUMN_CAPACITY)
        p = _plan()
        p["chapters"][0]["elements"] += [
            {"id": f"lbl_{i}", "type": "text", "text": f"Part {i}",
             "role": "label"} for i in range(5)]      # 5 + the plan's 2 = 7
        plan, issues = adapt_semantic_plan(p, NARR)
        ys = sorted(e["at"][1] for e in plan["chapters"][0]["elements"]
                    if e.get("role") == "label")
        assert len(ys) == LABEL_COLUMN_CAPACITY
        assert ys[-1] + _LABEL_H <= _LABEL_FLOOR, \
            f"the column still reaches the avatar: bottom {ys[-1] + _LABEL_H}"
        assert all(b - a >= 40.0 for a, b in zip(ys, ys[1:])), \
            f"labels packed too tightly to read: {ys}"
        assert not [i for i in issues if i["code"] == "LABEL_COLUMN_OVERFLOW"]

    def test_more_labels_than_fit_are_reported_not_silently_stacked(self):
        p = _plan()
        p["chapters"][0]["elements"] += [
            {"id": f"lbl_{i}", "type": "text", "text": f"Part {i}",
             "role": "label"} for i in range(10)]
        _, issues = adapt_semantic_plan(p, NARR)
        assert [i for i in issues if i["code"] == "LABEL_COLUMN_OVERFLOW"], \
            "an overflowing label column must be named, not quietly overlapped"

    def test_renderer_owned_types_are_dropped_with_a_note(self):
        p = _plan()
        p["chapters"][0]["elements"] += [
            {"id": "stu", "type": "character", "role": "student"},
            {"id": "bub", "type": "speech_bubble", "role": "dialogue"}]
        plan, issues = adapt_semantic_plan(p, NARR)
        ids = {e["id"] for e in plan["chapters"][0]["elements"]}
        assert "stu" not in ids and "bub" not in ids
        assert sum(1 for i in issues
                   if i["code"] == "RENDERER_OWNED_ELEMENT") == 2

    def test_reserved_ids_are_refused(self):
        p = _plan()
        p["chapters"][0]["elements"].append(
            {"id": "__teach_av", "type": "illustration", "asset": "triangle"})
        _, issues = adapt_semantic_plan(p, NARR)
        assert any(i["code"] == "RESERVED_ID" for i in issues)

    def test_title_role_places_at_the_top(self):
        p = _plan()
        p["chapters"][0]["elements"].append(
            {"id": "t", "type": "text", "text": "Triangles", "role": "title"})
        plan, _ = adapt_semantic_plan(p, NARR, strict=True)
        t = next(e for e in plan["chapters"][0]["elements"] if e["id"] == "t")
        assert t["anchor"] == "mt" and t["at"][1] < 200


class TestChapterSplitOnRedraw:
    """A real 41-segment lesson ("animal cell vs plant cell") declared TEN
    teaching visuals and switched between them with CLEAR_AND_REDRAW at
    eleven steps, all inside ONE chapter. One root per chapter is a hard rule
    downstream, so the compiler discarded the animal-cell diagram, the
    cheek-cell prep sequence, the microscope setup and the summary table —
    while the narration went on talking about them."""

    @staticmethod
    def _multi_root():
        return {"chapters": [{
            "concept": "animal_vs_plant", "transition": "clear_and_redraw",
            "assets": {"tbl": "a comparison table",
                       "animal": "an animal cell",
                       "scope": "a microscope"},
            "semantic_regions": ["plant_col", "animal_col"],
            "elements": [
                {"id": "compare", "type": "illustration", "asset": "tbl",
                 "role": "root_visual"},
                {"id": "animal_img", "type": "illustration", "asset": "animal"},
                {"id": "scope_img", "type": "illustration", "asset": "scope"},
                {"id": "lbl_n", "type": "text", "text": "Nucleus",
                 "role": "label"}],
            "steps": [
                {"segment": 1, "decision": "CLEAR_AND_REDRAW", "actions": [
                    {"verb": "DRAW", "target": {"element": "compare"},
                     "cue": "the hypotenuse"}]},
                {"segment": 2, "decision": "CLEAR_AND_REDRAW", "actions": [
                    {"verb": "DRAW", "target": {"element": "animal_img"},
                     "cue": "the hypotenuse"}]},
                {"segment": 3, "decision": "CLEAR_AND_REDRAW", "actions": [
                    {"verb": "DRAW", "target": {"element": "scope_img"},
                     "cue": "the hypotenuse"}]}]}]}

    def test_every_declared_visual_survives(self):
        narr = {f"s{i:03d}": "the hypotenuse is the longest side"
                for i in (1, 2, 3)}
        plan, issues = adapt_semantic_plan(self._multi_root(), narr)
        roots = [e["id"] for c in plan["chapters"] for e in c["elements"]
                 if e.get("type") == "illustration"]
        assert sorted(roots) == ["animal_img", "compare", "scope_img"], \
            f"a declared teaching visual was lost: {roots}"
        assert len(plan["chapters"]) == 3
        for c in plan["chapters"]:
            n = sum(1 for e in c["elements"]
                    if e.get("type") == "illustration")
            assert n == 1, f"{c['concept']} still carries {n} root visuals"
        assert any(i["code"] == "CHAPTER_SPLIT_ON_REDRAW" for i in issues), \
            "the split must be reported, not silent"

    def test_each_chapter_only_carries_the_asset_it_uses(self):
        """Assets are generated per chapter, so handing every chapter the
        full asset map would pay for images nothing draws."""
        narr = {f"s{i:03d}": "the hypotenuse is the longest side"
                for i in (1, 2, 3)}
        plan, _ = adapt_semantic_plan(self._multi_root(), narr)
        for c in plan["chapters"]:
            used = {e.get("asset") for e in c["elements"]
                    if e.get("type") == "illustration"}
            assert set(c["assets"]) == used, \
                f"{c['concept']} carries unused assets {set(c['assets']) - used}"

    def test_region_names_do_not_travel_to_a_different_picture(self):
        """semantic_regions describe the ORIGINAL root's asset. Handing them
        to a sibling names parts of a different image and sends the vision
        annotator hunting for regions that were never drawn."""
        narr = {f"s{i:03d}": "the hypotenuse is the longest side"
                for i in (1, 2, 3)}
        plan, _ = adapt_semantic_plan(self._multi_root(), narr)
        tail = "name the layer groups exactly"
        for c in plan["chapters"]:
            root = next(e for e in c["elements"]
                        if e.get("type") == "illustration")
            prompt = c["assets"].get(root["asset"], "")
            if root["id"] != "compare":
                assert tail not in prompt.lower(), \
                    f"{c['concept']} inherited another picture's region names"

    def test_a_draw_addressed_BY_ASSET_still_splits(self):
        """A real lesson drew its comparison table as {"asset": ...} rather
        than {"element": ...}. An element-only reading missed the root change,
        so the table was discarded and segment 12 narrated "how animal cells
        differ from plant cells" over a redrawn cheek-cell prep diagram."""
        p = self._multi_root()
        p["chapters"][0]["steps"][1]["actions"][0]["target"] = {"asset": "animal"}
        p["chapters"][0]["steps"][2]["actions"][0]["target"] = {"asset": "scope"}
        narr = {f"s{i:03d}": "the hypotenuse is the longest side"
                for i in (1, 2, 3)}
        plan, _ = adapt_semantic_plan(p, narr)
        roots = sorted(e["id"] for c in plan["chapters"] for e in c["elements"]
                       if e.get("type") == "illustration")
        assert roots == ["animal_img", "compare", "scope_img"], \
            f"an asset-addressed visual was lost: {roots}"

    def test_a_region_draw_on_the_same_picture_is_not_a_split(self):
        """The counterpart: building ONE diagram region by region is the
        desired pattern and must stay a single chapter."""
        p = self._multi_root()
        p["chapters"][0]["elements"] = [
            e for e in p["chapters"][0]["elements"]
            if e["id"] not in ("animal_img", "scope_img")]
        for i, region in enumerate(("cheek", "slide", "coverslip")):
            p["chapters"][0]["steps"][i]["actions"][0]["target"] = {
                "asset": "tbl", "region": region}
        narr = {f"s{i:03d}": "the hypotenuse is the longest side"
                for i in (1, 2, 3)}
        plan, issues = adapt_semantic_plan(p, narr)
        assert len(plan["chapters"]) == 1
        assert not [i for i in issues
                    if i["code"] == "CHAPTER_SPLIT_ON_REDRAW"]

    def test_a_single_root_chapter_is_left_alone(self):
        plan, issues = adapt_semantic_plan(_plan(), NARR, strict=True)
        assert len(plan["chapters"]) == 1
        assert not [i for i in issues
                    if i["code"] == "CHAPTER_SPLIT_ON_REDRAW"]

    def test_redrawing_the_same_visual_is_not_a_split(self):
        """Clearing and redrawing the SAME picture is a legitimate reset, not
        a new chapter — splitting there would multiply chapters for nothing."""
        p = self._multi_root()
        for s in p["chapters"][0]["steps"]:
            s["actions"][0]["target"] = {"element": "compare"}
        plan, issues = adapt_semantic_plan(
            p, {f"s{i:03d}": "the hypotenuse is the longest side"
                for i in (1, 2, 3)})
        assert len(plan["chapters"]) == 1
        assert not [i for i in issues
                    if i["code"] == "CHAPTER_SPLIT_ON_REDRAW"]


class TestTransitionsAndRegions:
    def test_continue_maps_to_carry(self):
        plan, _ = adapt_semantic_plan(_plan(), NARR, strict=True)
        assert plan["chapters"][0]["transition"] == "carry"

    def test_clear_and_redraw_passes_through(self):
        plan, _ = adapt_semantic_plan(_plan(transition="clear_and_redraw"),
                                      NARR, strict=True)
        assert plan["chapters"][0]["transition"] == "clear_and_redraw"

    def test_invalid_transition_is_reported(self):
        _, issues = adapt_semantic_plan(_plan(transition="keep"), NARR)
        assert any(i["code"] == "INVALID_TRANSITION" for i in issues)

    def test_semantic_regions_feed_the_vision_annotator(self):
        plan, _ = adapt_semantic_plan(_plan(), NARR, strict=True)
        prompt = plan["chapters"][0]["assets"]["triangle"]
        assert "name the layer groups exactly: hypotenuse, base" in prompt.lower()


class TestCues:
    def test_verbatim_cue_becomes_a_phrase_cue(self):
        plan, _ = adapt_semantic_plan(_plan(), NARR, strict=True)
        assert plan["chapters"][0]["steps"][0]["actions"][1]["at"] == {
            "phrase": "The hypotenuse"}

    def test_paraphrased_cue_is_reported_and_dropped(self):
        p = _plan()
        p["chapters"][0]["steps"][0]["actions"][0]["cue"] = "the sloping side"
        plan, issues = adapt_semantic_plan(p, NARR)
        assert any(i["code"] == "CUE_NOT_IN_NARRATION" for i in issues)
        assert "at" not in plan["chapters"][0]["steps"][0]["actions"][0]
        with pytest.raises(AdapterError):
            adapt_semantic_plan(p, NARR, strict=True)

    def test_missing_cue_is_allowed(self):
        p = _plan()
        p["chapters"][0]["steps"][0]["actions"][0].pop("cue")
        plan, _ = adapt_semantic_plan(p, NARR, strict=True)
        assert "at" not in plan["chapters"][0]["steps"][0]["actions"][0]


class TestMoments:
    def test_human_teaching_moment_becomes_a_step_moment(self):
        p = _plan()
        p["chapters"][0]["steps"][1]["actions"].append(
            {"verb": "HUMAN_TEACHING_MOMENT", "role": "student",
             "line": "So it's always the longest?"})
        plan, _ = adapt_semantic_plan(p, NARR, strict=True)
        assert plan["chapters"][0]["steps"][1]["moment"] == {
            "role": "student", "text": "So it's always the longest?"}

    def test_moment_without_a_line_is_reported(self):
        p = _plan()
        p["chapters"][0]["steps"][1]["actions"].append(
            {"verb": "HUMAN_TEACHING_MOMENT", "role": "student"})
        _, issues = adapt_semantic_plan(p, NARR)
        assert any(i["code"] == "MOMENT_WITHOUT_LINE" for i in issues)


class TestEndToEnd:
    """The adapter's output must survive the REAL pipeline: parse_visual_plan
    -> compile_plan -> Scene validation, across multiple chapters, with
    visuals present in every segment (not just the first)."""

    def _multi(self):
        return {"chapters": [
            {"id": "c1", "concept": "triangle", "transition": "clear_and_redraw",
             "assets": {"triangle": "A right-angled triangle"},
             "semantic_regions": ["hypotenuse", "base"],
             "elements": [
                 {"id": "tri", "type": "illustration", "asset": "triangle",
                  "role": "root_visual"},
                 {"id": "lbl_hyp", "type": "text", "text": "Hypotenuse"},
                 {"id": "lbl_base", "type": "text", "text": "Base"}],
             "steps": [
                 {"segment": 1, "decision": "NEW_VISUAL", "reason": "open",
                  "actions": [
                      {"verb": "DRAW", "target": {"element": "tri"},
                       "cue": "a right-angled triangle"},
                      {"verb": "WRITE", "target": {"element": "lbl_hyp"},
                       "cue": "The hypotenuse"},
                      {"verb": "ARROW",
                       "target": {"asset": "triangle", "region": "hypotenuse"},
                       "cue": "the longest side"}]},
                 {"segment": 2, "decision": "EXTEND", "reason": "second side",
                  "actions": [
                      {"verb": "WRITE", "target": {"element": "lbl_base"},
                       "cue": "The base"},
                      {"verb": "ARROW",
                       "target": {"asset": "triangle", "region": "base"},
                       "cue": "at the bottom"}]},
                 {"segment": 3, "decision": "CONTINUE", "reason": "hold",
                  "actions": []}]},
            {"id": "c2", "concept": "circles", "transition": "clear_and_redraw",
             "assets": {"circle": "A circle with a radius line"},
             "semantic_regions": ["radius"],
             "elements": [
                 {"id": "circ", "type": "illustration", "asset": "circle",
                  "role": "root_visual"},
                 {"id": "lbl_r", "type": "text", "text": "Radius"}],
             "steps": [
                 {"segment": 4, "decision": "CLEAR_AND_REDRAW", "reason": "new idea",
                  "actions": [
                      {"verb": "DRAW", "target": {"element": "circ"},
                       "cue": "a circle"},
                      {"verb": "WRITE", "target": {"element": "lbl_r"},
                       "cue": "the radius"}]}]},
        ]}

    def test_multi_chapter_plan_reaches_the_board_in_every_segment(self):
        narr = dict(NARR)
        narr["s004"] = "Now a circle. Every point sits the same distance from the centre — that is the radius."
        plan, issues = adapt_semantic_plan(self._multi(), narr, strict=True)
        assert issues == []

        parsed = parse_visual_plan(plan)
        assert parsed is not None, "adapter output must satisfy the trust boundary"
        scenes, assets, report = compile_plan(
            parsed, narr, all_segments=["s001", "s002", "s003", "s004"],
            skip_hold=set())

        # EVERY segment renders, not just the first
        assert set(scenes) == {"s001", "s002", "s003", "s004"}
        for sid, sc in scenes.items():
            Scene.model_validate(sc)               # renderer-schema valid
            assert sc["elements"], f"{sid} has an empty board"

        # the triangle persists across its chapter and the circle replaces it
        assert any(e["id"] == "tri" for e in scenes["s003"]["elements"])
        assert any(e["id"] == "circ" for e in scenes["s004"]["elements"])
        # arrows were synthesized for both regions and anchored semantically
        arrows = [e for e in scenes["s002"]["elements"]
                  if e.get("type") == "arrow"]
        assert {a["head"]["layer"] for a in arrows} == {"hypotenuse", "base"}
        # cue timing survived the whole trip
        draw = next(a for a in scenes["s001"]["actions"]
                    if a["verb"] == "draw" and a["target"] == "tri")
        assert draw["at"]["phrase"] == "a right-angled triangle"

    def test_a_hostile_plan_never_takes_the_lesson_down(self):
        """Production mode: garbage in, a reduced but VALID lesson out, with
        every problem reported rather than hidden."""
        hostile = {"chapters": [{
            "concept": "broken", "transition": "sideways",
            "assets": {"a": "an asset"},
            "elements": [
                {"id": "ok", "type": "illustration", "asset": "a"},
                {"id": "__nb_x", "type": "text", "text": "squatting"},
                {"type": "text", "text": "no id"},
                {"id": "weird", "type": "hologram"}],
            "steps": [{"segment": 1, "decision": "TELEPORT", "actions": [
                {"verb": "SPARKLE", "target": {"element": "ok"}},
                {"verb": "DRAW", "target": {"element": "ok"},
                 "cue": "a right-angled triangle"},
                "not-an-object"]}]}]}
        plan, issues = adapt_semantic_plan(hostile, NARR)
        codes = {i["code"] for i in issues}
        assert {"INVALID_TRANSITION", "RESERVED_ID", "MALFORMED_ELEMENT",
                "UNSUPPORTED_ELEMENT_TYPE", "INVALID_DECISION",
                "UNSUPPORTED_VERB", "MALFORMED_ACTION"} <= codes
        parsed = parse_visual_plan(plan)
        assert parsed is not None
        scenes, _, _ = compile_plan(parsed, NARR, all_segments=["s001"],
                                    skip_hold=set())
        Scene.model_validate(scenes["s001"])
        with pytest.raises(AdapterError):
            adapt_semantic_plan(hostile, NARR, strict=True)
