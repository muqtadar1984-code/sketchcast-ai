"""A process label points at the arrow between its two ends, and an
engine-invented arrow that still finds nothing becomes a caption.

States of Matter (2026-09-25, kit 56b9b04d, second render): the director
named solid, liquid and gas; the annotator boxed the three arrow pairs
between them; and the two labels continuity synthesized from the narration,
"Evaporative Cooling" and "Sublimation", still drew leaders to the edge of
the phase triangle — six ANCHOR_EDGE_FALLBACKs across s009–s012 — because no
region is called "evaporative cooling".
"""

from __future__ import annotations

from spike.scene_engine import anchor_match as am
from spike.scene_engine.raster_assets import RasterAsset
from spike.scene_engine.render import SceneRenderer
from spike.scene_engine.schema import Scene
from tests.test_scene_engine_quality import _cell_asset, _resolver

# the exact vocabulary the annotator wrote for phase_change_triangle
TRIANGLE = ["gas", "liquid", "liquid gas arrows", "solid",
            "solid gas arrows", "solid liquid arrows"]


class TestAProcessNamesTheArrowBetweenItsEnds:
    def test_the_two_live_misses_now_resolve(self):
        assert am.anchor_layer_hits(TRIANGLE, "evaporative cooling") == ["liquid gas arrows"]
        assert am.anchor_layer_hits(TRIANGLE, "sublimation") == ["solid gas arrows"]

    def test_every_phase_change_has_its_ends(self):
        assert am.process_endpoints("melting_path") == ("solid", "liquid")
        assert am.process_endpoints("Freezing") == ("liquid", "solid")
        assert am.process_endpoints("evaporation") == ("liquid", "gas")
        assert am.process_endpoints("boiling point") == ("liquid", "gas")
        assert am.process_endpoints("condensation") == ("gas", "liquid")
        assert am.process_endpoints("sublimation") == ("solid", "gas")
        assert am.process_endpoints("deposition") == ("gas", "solid")
        assert am.process_endpoints("desublimation") == ("gas", "solid")
        assert am.process_endpoints("nucleus") is None

    def test_direction_picks_between_the_repair_passs_per_direction_keys(self):
        regions = TRIANGLE + ["solid to gas arrow", "gas to solid arrow"]
        assert am.by_process_endpoints(regions, "sublimation") == ["solid to gas arrow"]
        assert am.by_process_endpoints(regions, "deposition") == ["gas to solid arrow"]

    def test_two_equally_good_candidates_are_refused(self):
        assert am.by_process_endpoints(["solid gas arrow a", "solid gas arrow b"],
                                       "sublimation") == []

    def test_a_region_naming_only_one_end_is_not_the_arrow(self):
        assert am.by_process_endpoints(["solid", "gas"], "sublimation") == []

    def test_a_region_literally_named_for_the_process_still_wins(self):
        assert am.anchor_layer_hits(TRIANGLE + ["sublimation"], "sublimation") == ["sublimation"]

    def test_a_name_without_a_process_word_is_untouched(self):
        assert am.by_process_endpoints(TRIANGLE, "nucleus") == []
        assert am.anchor_layer_hits(TRIANGLE, "gas") == ["gas"]


def _scene(arrow_id: str, layer: str) -> Scene:
    return Scene.model_validate({
        "id": "anch", "narration": "the ice sublimes straight to vapour",
        "elements": [
            {"id": "cell", "type": "illustration", "asset": "plant_cell",
             "at": [640, 360], "scale": 1.0},
            {"id": "lbl", "type": "text", "text": "Sublimation",
             "at": [80, 120], "role": "label", "anchor": "lt"},
            {"id": arrow_id, "type": "arrow", "curve": 0,
             "tail": {"el": "lbl", "edge": "right", "dx": 6},
             "head": {"el": "cell", "layer": layer, "edge": "center"}},
        ],
        "actions": [{"verb": "draw", "target": "cell"},
                    {"verb": "write", "target": "lbl"},
                    {"verb": "draw", "target": arrow_id}],
    })


class TestAnInventedArrowThatFindsNothingBecomesACaption:
    def test_the_label_moves_under_the_picture_and_the_arrow_draws_nothing(self):
        r = SceneRenderer(_scene("arr_auto_lbl", "evaporative cooling"),
                          asset_resolver=_resolver(_cell_asset()))
        warns = r.audit()["warnings"]
        assert any(w.startswith("LABEL_CAPTIONED lbl") for w in warns), warns
        assert not any(w.startswith("ANCHOR_EDGE_FALLBACK") for w in warns), warns
        art = r._ink_box(r.bound["cell"]) or r.bound["cell"].box
        lbl = r.bound["lbl"].box
        assert lbl[1] >= art[3], (lbl, art)                      # below the picture
        assert abs((lbl[0] + lbl[2]) / 2 - (art[0] + art[2]) / 2) < 2.0   # centred on it
        assert r.bound["arr_auto_lbl"].layers == [] and "arr_auto_lbl" in r._dropped

    def test_a_director_declared_arrow_keeps_the_edge_leader(self):
        r = SceneRenderer(_scene("ar", "evaporative cooling"),
                          asset_resolver=_resolver(_cell_asset()))
        warns = r.audit()["warnings"]
        assert any(w.startswith("ANCHOR_EDGE_FALLBACK") for w in warns), warns
        assert not any(w.startswith("LABEL_CAPTIONED") for w in warns)
        assert r.bound["ar"].layers and r.bound["ar"].layers[0].strokes

    def test_when_the_picture_has_the_arrow_the_label_points_at_it(self):
        asset = _cell_asset()
        asset.regions = {"solid": [(20.0, 20.0, 60.0, 60.0)],
                         "gas": [(140.0, 20.0, 180.0, 60.0)],
                         "solid gas arrows": [(70.0, 30.0, 130.0, 50.0)]}
        r = SceneRenderer(_scene("arr_auto_lbl", "sublimation"),
                          asset_resolver=_resolver(asset))
        warns = r.audit()["warnings"]
        assert not any(w.startswith(("LABEL_CAPTIONED", "ANCHOR_EDGE_FALLBACK",
                                     "UNRESOLVED_ANCHOR")) for w in warns), warns
        head = r.bound["arr_auto_lbl"].head_pt
        # asset centred at (640,360), 200px wide -> the arrow box in world is
        # (610,290)-(670,310); the head lands on its boundary
        assert 605.0 <= head[0] <= 675.0 and 285.0 <= head[1] <= 315.0, head


class TestTheGateCountsIt:
    def test_captioned_labels_are_reported_on_their_own_line(self):
        from spike.scene_engine.validate import validate_visual_language
        manifest = {"segments": [{"segment_id": "s001", "renderer": "scene",
                                  "audio_path": "a.mp3",
                                  "scene_audit": ["LABEL_CAPTIONED lbl (cell.sublimation not in the picture)"]}]}
        r = validate_visual_language(manifest, None)
        assert r["labels_captioned"] == ["s001: LABEL_CAPTIONED lbl (cell.sublimation not in the picture)"]
        assert r["anchor_edge_fallbacks"] == []
