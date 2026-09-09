"""The plan's promise, checked against the artwork's delivery.

`organelle_city` (Cells kit, 2026-09-09) was asked for four part names,
returned `regions: {}` for every one, and was published approved with
group_count 0 — while the same vision call answered `plant_cell_diagram` and
`hierarchy_ladder` minutes apart on the same run. A total miss is the flakiest
outcome, not the most certain one, and it was the single case the annotation
path's own re-ask refused to touch (`if missing and regions:`).

These tests pin the check, its subject-blindness, and the one property the
repair must not break: a name asked on a LOAD is still never re-bought, and
only a plan actually pointing at that name may go past the latch.
"""

from __future__ import annotations

from spike.scene_engine.anchor_repair import (plan_anchor_wants, scenes_of,
                                              unresolved_wants)


def _scene(asset: str, *layers: str, el: str = "pic") -> dict:
    return {
        "elements": [{"id": el, "type": "illustration", "asset": asset},
                     {"id": "lbl", "type": "text", "text": "x"}],
        "steps": [{"actions": [{"verb": "draw", "target": el}]}],
        "arrows": [{"id": f"arr_{l}", "head": {"el": el, "layer": l},
                    "tail": {"el": "lbl"}} for l in layers],
    }


class TestWhatThePlanPromised:
    def test_wants_are_keyed_by_ASSET_so_a_carried_board_is_one_repair(self):
        """A clear_and_redraw boundary copies the board forward as `prev__<id>`,
        renaming the element but carrying `asset` through verbatim. The live
        run logged the same two failures twice for that reason. One picture,
        one repair."""
        a = _scene("organelle_city", "nucleus_region", el="cell_city")
        b = _scene("organelle_city", "mitochondria_region", el="prev__cell_city")
        assert plan_anchor_wants([a, b]) == {
            "organelle_city": {"nucleus_region", "mitochondria_region"}}

    def test_an_anchor_on_a_non_illustration_is_not_a_want(self):
        """Only a picture has parts to point at."""
        s = _scene("pic_key", "some_part")
        s["arrows"].append({"id": "a2", "head": {"el": "lbl", "layer": "nope"}})
        assert plan_anchor_wants([s]) == {"pic_key": {"some_part"}}

    def test_anchors_are_found_structurally_not_by_a_list_of_keys(self):
        """The schema is extra="allow" and anchors ride on new element types
        over time; the walk must keep finding them."""
        s = _scene("pic_key")
        s["groups"] = [{"members": [{"whatever": {"el": "pic",
                                                  "layer": "deep_part"}}]}]
        assert plan_anchor_wants([s]) == {"pic_key": {"deep_part"}}


class TestWhatTheArtworkCanDeliver:
    def test_the_live_failure_is_reported(self):
        wants = {"organelle_city": {"nucleus_region", "mitochondria_region"}}
        assert unresolved_wants(wants, lambda k: []) == {
            "organelle_city": ["mitochondria_region", "nucleus_region"]}

    def test_a_part_the_matcher_can_already_reach_is_NOT_bought_again(self):
        """The check uses the renderer's own ladder, so a name that resolves
        through inflection or qualifier-narrowing costs nothing. Buying a box
        for `mitochondria_region` when the artwork says `mitochondrion` would
        be paying for what PR #51 already fixed for free."""
        regions = ["mitochondrion", "nucleus", "golgi apparatus"]
        wants = {"pic": {"mitochondria_region", "nucleus_region", "golgi_sacs"}}
        assert unresolved_wants(wants, lambda k: regions) == {}

    def test_a_picture_that_never_arrived_is_someone_elses_fault(self):
        """A deferred generation has no image to annotate. Reporting it as an
        anchor fault would buy a vision call against nothing."""
        wants = {"pic": {"part"}}
        assert unresolved_wants(wants, lambda k: None) == {}

    def test_it_is_the_same_check_outside_biology(self):
        """If this mechanism is ever made subject-specific, this fails."""
        assert unresolved_wants({"geo": {"mantle_region"}},
                                lambda k: ["upper mantle"]) == {}
        assert unresolved_wants({"chem": {"salt_bridges_area"}},
                                lambda k: ["salt bridge"]) == {}
        assert unresolved_wants({"hist": {"walls_of_the_city"}},
                                lambda k: ["city walls"]) == {}
        # ...and it reports a real absence just as readily, in any subject
        assert unresolved_wants({"geo": {"mantle_region"}},
                                lambda k: ["forum", "aqueduct"]) == {
            "geo": ["mantle_region"]}


class TestSubjectBlindness:
    def test_renaming_every_subject_token_changes_nothing(self):
        """The strongest guard available: a bijective rename of every domain
        noun must produce a structurally identical answer. This catches
        indirect hardcoding — a lookup keyed by subject, an ordering by string,
        a length check — that an AST scan cannot see."""
        real = _scene("organelle_city", "nucleus_region", "mitochondria_region")
        fake = _scene("q7f3a1", "k2b9_region", "z11_region")

        got_real = unresolved_wants(plan_anchor_wants([real]), lambda k: [])
        got_fake = unresolved_wants(plan_anchor_wants([fake]), lambda k: [])

        assert len(got_real) == len(got_fake) == 1
        assert sorted(map(len, got_real.values())) == \
            sorted(map(len, got_fake.values()))


class TestSceneAddressing:
    def test_scenes_are_addressed_the_way_the_warm_pass_addresses_them(self):
        """If these two ever disagree about which segments carry which scenes,
        the repair pass would silently check the wrong pictures."""
        slide = [{"segment_id": "s001"}, {"segment_id": "s002"},
                 {"segment_id": "s003"}]
        script = {"s001": {"scene": _scene("a", "p")},
                  "s003": {"scene": _scene("b", "q")}}
        assert len(scenes_of(slide, script)) == 2          # s002 has no scene
        assert plan_anchor_wants(scenes_of(slide, script)) == {
            "a": {"p"}, "b": {"q"}}
