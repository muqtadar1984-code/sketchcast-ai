"""Three arrows pointed at a picture nobody drew.

Cells kit, 2026-09-08, last three segments of the video: the labels "Tissue",
"Organ System" and "Specialized Cells" wrote themselves and drew three arrows
at `hierarchy` — an illustration the chapter roster declares and no step ever
draws. The compiler flattened all three ends to its PLANNED point and they
converged on empty board:

    SEGMENT s012 | FLATTENED arr_tissue_level.head 'hierarchy' -> [600.0, 380.0]
                   (not on the board yet)

"Yet" is the assumption that fails. Flattening is right when the picture
arrives a step later; when it never arrives the arrows outlive their target.
The roster HAS the element, with its asset and position, so the honest repair
is to draw it.
"""

from __future__ import annotations

from spike.scene_engine.continuity import compile_plan, parse_visual_plan

_NARR = {"s001": "cells make tissues", "s002": "and tissues make organs"}


def _plan(draw_the_picture: bool):
    """A chapter whose arrow names an illustration the steps may or may not
    introduce — the live shape, minus everything irrelevant to it."""
    steps = [
        {"segment": 1, "decision": "NEW_VISUAL",
         "actions": [{"verb": "write", "target": "lbl_tissue"},
                     {"verb": "draw", "target": "arr_tissue"}]},
        {"segment": 2, "decision": "FOCUS", "actions": []},
    ]
    if draw_the_picture:
        steps[0]["actions"].insert(0, {"verb": "draw", "target": "hierarchy"})
    return parse_visual_plan({"chapters": [{
        "concept": "organisation",
        "assets": {"hierarchy_organism": "cells to tissues to organs"},
        "elements": [
            {"id": "hierarchy", "type": "illustration",
             "asset": "hierarchy_organism", "at": [600, 380], "scale": 0.9},
            {"id": "lbl_tissue", "type": "text", "text": "Tissue", "at": [70, 150]},
            {"id": "arr_tissue", "type": "arrow",
             "tail": {"el": "lbl_tissue"}, "head": {"el": "hierarchy"}},
        ],
        "steps": steps,
    }]})


def _ids(scene) -> set[str]:
    return {e["id"] for e in scene["elements"]}


class TestAnArrowTargetIsAlwaysDrawn:
    def test_the_picture_is_materialised_when_no_step_draws_it(self):
        scenes, _assets, report = compile_plan(_plan(draw_the_picture=False), _NARR)
        assert "hierarchy" in _ids(scenes["s001"]), \
            "the arrow's target has to be on the board it points into"
        assert any("MATERIALISED hierarchy" in line for line in report), report

    def test_and_the_end_is_never_flattened_to_a_planned_point(self):
        """The symptom, stated as the thing that must not appear: a flattened
        head is an arrow aimed at a coordinate rather than at a picture."""
        _, _assets, report = compile_plan(_plan(draw_the_picture=False), _NARR)
        assert not [l for l in report if "FLATTENED" in l and "hierarchy" in l], report

    def test_a_plan_that_draws_its_own_target_is_untouched(self):
        """No materialising when the plan already did the right thing — and
        no second draw action either."""
        scenes, _assets, report = compile_plan(_plan(draw_the_picture=True), _NARR)
        assert "hierarchy" in _ids(scenes["s001"])
        assert not [l for l in report if "MATERIALISED" in l], report

    def test_the_repair_only_draws_things_that_CAN_be_drawn(self):
        """Text and arrows are introduced by the steps that write them; an
        arrow anchored to another arrow is a different defect, and the
        sanitisation pass owns it. Only the illustration is materialised."""
        scenes, _assets, report = compile_plan(_plan(draw_the_picture=False), _NARR)
        made = [l for l in report if "MATERIALISED" in l]
        assert len(made) == 1 and "hierarchy" in made[0], made
        # the label is introduced by its own write, exactly as before
        assert "lbl_tissue" in _ids(scenes["s001"])
