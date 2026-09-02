"""Quality-pass regression tests (video quality pass, §1-33).

Covers the five defect classes the frame audit of the first full lesson found:
arrows converging on one eyeballed point, labels clipped at the canvas edge,
labels appearing before their structures, char-midpoint-only cue timing, and
zero human teaching moments in a full lesson.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from spike.scene_engine.continuity import (compile_plan, parse_visual_plan,
                                           seed_moment)
from spike.scene_engine.raster_assets import RasterAsset, part_names_from_prompt
from spike.scene_engine.render import SceneRenderer, _region_ordered_trace
from spike.scene_engine.schema import Scene
from spike.scene_engine.timing import resolve_cue
from spike.scene_engine.schema import Cue


# ── synthetic annotated raster ───────────────────────────────────────────────

NUCLEUS = [120.0, 120.0, 160.0, 160.0]
CHLORO_A = [20.0, 20.0, 60.0, 60.0]
CHLORO_B = [20.0, 140.0, 60.0, 180.0]


def _cell_asset(key: str = "plant_cell") -> RasterAsset:
    ink = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    trace = []
    # base: a border walk (outside every region)
    for i in range(40):
        trace.append((5.0 + i * 4.8, 5.0))
    # nucleus points
    for i in range(20):
        trace.append((125.0 + (i % 5) * 7.0, 125.0 + (i // 5) * 8.0))
    # chloroplast instance A then B
    for i in range(10):
        trace.append((25.0 + (i % 5) * 7.0, 25.0 + (i // 5) * 8.0))
    for i in range(10):
        trace.append((25.0 + (i % 5) * 7.0, 145.0 + (i // 5) * 8.0))
    return RasterAsset(key=key, ink=ink, trace=trace, stamp_r=4.0,
                       world_scale=1.0,
                       regions={"nucleus": [NUCLEUS],
                                "chloroplast": [CHLORO_A, CHLORO_B]})


def _resolver(asset: RasterAsset):
    return lambda k: ("raster", asset) if k == asset.key else None


# ── region-ordered trace ─────────────────────────────────────────────────────

class TestRegionTrace:
    def test_base_folds_into_first_region_then_named_regions_in_order(self):
        a = _cell_asset()
        new, spans = _region_ordered_trace(a.trace, a.regions,
                                           ["nucleus", "chloroplast"])
        assert len(new) == len(a.trace)          # nothing lost
        # base (outline etc.) rides INSIDE the first region: no separate
        # base span, so the first part draw starts with the outline instead
        # of scattered leftover specks
        assert spans["__base"] == (0.0, 0.0)
        n_lo, n_hi = spans["nucleus"]
        c_lo, c_hi = spans["chloroplast"]
        assert n_lo == 0.0 and abs(n_hi - 0.75) < 0.01     # 40 base + 20 nuc
        assert abs(c_lo - n_hi) < 1e-9 and abs(c_hi - 1.0) < 1e-9
        # the tail of the nucleus span is the actual nucleus points
        k1 = int(n_hi * len(new))
        for p in new[40:k1]:
            assert NUCLEUS[0] <= p[0] <= NUCLEUS[2]
            assert NUCLEUS[1] <= p[1] <= NUCLEUS[3]


# ── layer anchors + arrow routing ────────────────────────────────────────────

def _anchor_scene(layer: str) -> Scene:
    return Scene.model_validate({
        "id": "anch", "narration": "the chloroplasts catch the light",
        "elements": [
            {"id": "cell", "type": "illustration", "asset": "plant_cell",
             "at": [640, 360], "scale": 1.0},
            {"id": "lbl", "type": "text", "text": "Chloroplast",
             "at": [80, 120], "role": "label", "anchor": "lt"},
            {"id": "ar", "type": "arrow", "curve": 0,
             "tail": {"el": "lbl", "edge": "right", "dx": 6},
             "head": {"el": "cell", "layer": layer, "edge": "center"}},
        ],
        "actions": [{"verb": "draw", "target": "cell"},
                    {"verb": "write", "target": "lbl"},
                    {"verb": "draw", "target": "ar"}],
    })


class TestLayerAnchors:
    def test_head_lands_on_nearest_instance_boundary(self):
        r = SceneRenderer(_anchor_scene("chloroplast"),
                          asset_resolver=_resolver(_cell_asset()))
        head = r.bound["ar"].head_pt
        # asset is centered at (640,360), 200px wide -> world box starts (540,260)
        # nearest chloroplast instance to a label at the top-left is CHLORO_A:
        # world box (560,280)-(600,320)
        assert 555.0 <= head[0] <= 605.0 and 275.0 <= head[1] <= 325.0
        # ...and NOT the element centre (the converging-arrows defect)
        assert ((head[0] - 640) ** 2 + (head[1] - 360) ** 2) ** 0.5 > 40.0

    def test_unresolved_layer_suppresses_the_arrow(self):
        r = SceneRenderer(_anchor_scene("ribosome"),
                          asset_resolver=_resolver(_cell_asset()))
        assert any(w.startswith("ARROW_SUPPRESSED")
                   for w in r.audit()["warnings"])
        # a label with no arrow beats a confident arrow to the wrong part
        assert not r._flat["ar"]
        assert "ar" not in r.audit()["arrow_heads"]

    def test_two_arrows_to_distinct_parts_do_not_converge(self):
        s = Scene.model_validate({
            "id": "two", "narration": "nucleus and chloroplast",
            "elements": [
                {"id": "cell", "type": "illustration", "asset": "plant_cell",
                 "at": [640, 360], "scale": 1.0},
                {"id": "l1", "type": "text", "text": "Nucleus", "at": [80, 100]},
                {"id": "l2", "type": "text", "text": "Chloroplast", "at": [80, 500]},
                {"id": "a1", "type": "arrow",
                 "tail": {"el": "l1", "edge": "right"},
                 "head": {"el": "cell", "layer": "nucleus", "edge": "center"}},
                {"id": "a2", "type": "arrow",
                 "tail": {"el": "l2", "edge": "right"},
                 "head": {"el": "cell", "layer": "chloroplast", "edge": "center"}},
            ],
            "actions": [{"verb": "draw", "target": "cell"}],
        })
        r = SceneRenderer(s, asset_resolver=_resolver(_cell_asset()))
        audit = r.audit()
        assert not any(w.startswith("ARROWS_CONVERGE")
                       for w in audit["warnings"])
        h1, h2 = audit["arrow_heads"]["a1"], audit["arrow_heads"]["a2"]
        assert ((h1[0] - h2[0]) ** 2 + (h1[1] - h2[1]) ** 2) ** 0.5 > 60.0


# ── label safe area ──────────────────────────────────────────────────────────

class TestSafeArea:
    def test_right_edge_label_is_pulled_inside(self):
        s = Scene.model_validate({
            "id": "safe", "narration": "x",
            "elements": [{"id": "t", "type": "text", "anchor": "lt",
                          "text": "Permanent vacuole", "at": [1250, 300]}],
            "actions": [{"verb": "write", "target": "t"}],
        })
        r = SceneRenderer(s)
        x0, y0, x1, y1 = r.bound["t"].box
        assert x1 <= 1256.0 + 1e-6 and x0 >= 24.0 - 1e-6
        assert any(w.startswith("OUT_OF_BOUNDS_TEXT")
                   for w in r._audit_warnings)

    def test_absurdly_long_label_shrinks_or_truncates_but_fits(self):
        s = Scene.model_validate({
            "id": "safe2", "narration": "x",
            "elements": [{"id": "t", "type": "text", "anchor": "lt",
                          "text": "endoplasmic reticulum " * 8, "at": [40, 40]}],
            "actions": [{"verb": "write", "target": "t"}],
        })
        r = SceneRenderer(s)
        x0, _, x1, _ = r.bound["t"].box
        assert x1 - x0 <= 1232.0 + 1e-6 and x1 <= 1256.0 + 1e-6


# ── word-boundary timing + cue offsets ───────────────────────────────────────

class TestWordTiming:
    NARR = "the nucleus controls everything in the cell"
    WORDS = [{"t": 0.2, "w": "the"}, {"t": 0.5, "w": "nucleus"},
             {"t": 1.1, "w": "controls"}, {"t": 1.7, "w": "everything"},
             {"t": 2.2, "w": "in"}, {"t": 2.35, "w": "the"},
             {"t": 2.5, "w": "cell"}]

    def test_word_boundaries_beat_char_midpoint(self):
        cue = Cue(phrase="nucleus controls")
        exact = resolve_cue(cue, self.NARR, 3.0, self.WORDS)
        approx = resolve_cue(cue, self.NARR, 3.0)
        assert exact == 0.5                       # first word's boundary
        assert approx is not None and abs(approx - exact) > 0.2

    def test_offset_shifts_and_clamps(self):
        cue = Cue(phrase="cell", offset=-0.4)
        assert abs(resolve_cue(cue, self.NARR, 3.0, self.WORDS) - 2.1) < 1e-9
        big = Cue(sec=1.0, offset=99.0)           # clamped to +5
        assert resolve_cue(big, self.NARR, 30.0) == 6.0

    def test_missing_phrase_still_falls_back(self):
        assert resolve_cue(Cue(phrase="mitochondria"), self.NARR, 3.0,
                           self.WORDS) is None


# ── action dependencies ──────────────────────────────────────────────────────

class TestDependencies:
    def test_annotation_waits_for_its_introducer(self):
        s = Scene.model_validate({
            "id": "dep", "narration": "look at the label now " * 4,
            "elements": [{"id": "t", "type": "text", "text": "Nucleus",
                          "at": [200, 200]}],
            "actions": [
                {"verb": "circle", "target": "t", "at": {"sec": 0.0}},
                {"verb": "write", "target": "t", "at": {"sec": 4.0}},
            ],
        })
        r = SceneRenderer(s)
        r.compile(20.0)
        circle = next(t for t in r.timeline if t.action.verb == "circle")
        write = next(t for t in r.timeline if t.action.verb == "write")
        assert circle.start >= write.end          # never annotate thin air
        assert any(w.startswith("TIMING_SHIFT") for w in r._audit_warnings)

    def test_arrow_waits_for_anchored_structure(self):
        s = Scene.model_validate({
            "id": "dep2", "narration": "cell first then the arrow " * 3,
            "elements": [
                {"id": "cell", "type": "illustration", "asset": "plant_cell",
                 "at": [640, 360]},
                {"id": "ar", "type": "arrow", "tail": [100, 100],
                 "head": {"el": "cell", "layer": "nucleus", "edge": "center"}},
            ],
            "actions": [
                {"verb": "draw", "target": "ar", "at": {"sec": 0.0}},
                {"verb": "draw", "target": "cell", "at": {"sec": 3.0}},
            ],
        })
        r = SceneRenderer(s, asset_resolver=_resolver(_cell_asset()))
        r.compile(20.0)
        arrow = next(t for t in r.timeline if t.action.target == "ar")
        cell = next(t for t in r.timeline if t.action.target == "cell")
        assert arrow.start >= cell.end


# ── continuity compiler: auto-anchoring, region schedule, moments ────────────

_PROMPT = ("A plant cell in cross-section. Name the layer groups exactly: "
           "wall, nucleus, chloroplast")


def _plan_raw():
    return {"chapters": [{
        "concept": "plant_cell", "transition": "clear_and_redraw",
        "assets": {"plant_cell": _PROMPT},
        "elements": [
            {"id": "cell", "type": "illustration", "asset": "plant_cell",
             "at": [640, 360], "scale": 1.0},
            {"id": "lbl_nucleus", "type": "text", "text": "Nucleus",
             "at": [90, 120], "role": "label"},
            {"id": "arr_nucleus", "type": "arrow", "tail": [180, 130],
             "head": [600, 340]},
        ],
        "steps": [
            {"segment": 1, "decision": "NEW_VISUAL",
             "actions": [{"verb": "draw", "target": "cell"}]},
            {"segment": 2, "decision": "EXTEND",
             "actions": [{"verb": "draw", "target": "cell"},
                         {"verb": "write", "target": "lbl_nucleus"},
                         {"verb": "draw", "target": "arr_nucleus"}]},
            {"segment": 3, "decision": "FOCUS",
             "actions": [{"verb": "zoom", "target": "cell", "scale": 1.4}]},
        ],
    }]}


class TestContinuityQuality:
    def _compiled(self):
        plan = parse_visual_plan(_plan_raw())
        assert plan is not None
        narr = {"s001": "here is the cell", "s002": "this is the nucleus",
                "s003": "look closer"}
        return compile_plan(plan, narr, all_segments=["s001", "s002", "s003"],
                            skip_hold=set())

    def test_part_names_parse(self):
        assert part_names_from_prompt(_PROMPT) == ["wall", "nucleus",
                                                   "chloroplast"]

    def test_arrow_rewired_to_layer_anchor(self):
        scenes, _, report = self._compiled()
        arrow = next(e for e in scenes["s002"]["elements"]
                     if e["id"] == "arr_nucleus")
        assert arrow["head"] == {"el": "cell", "layer": "nucleus",
                                 "edge": "center"}
        assert isinstance(arrow["tail"], dict) and arrow["tail"]["el"] == "lbl_nucleus"
        assert any("ANCHORED arr_nucleus" in ln for ln in report)

    def test_draws_are_region_scheduled(self):
        scenes, _, report = self._compiled()
        d1 = next(a for a in scenes["s001"]["actions"]
                  if a["verb"] == "draw" and a["target"] == "cell")
        d2 = next(a for a in scenes["s002"]["actions"]
                  if a["verb"] == "draw" and a["target"] == "cell")
        assert d1.get("region") is None           # bare draw: uniform slice
        assert d1.get("slice") == (0.0, 0.5)
        assert d2.get("region") == "nucleus"      # nucleus when narrated
        root = next(e for e in scenes["s002"]["elements"] if e["id"] == "cell")
        assert root.get("region_order") == ["nucleus"]

    def test_carried_board_remembers_drawn_regions(self):
        scenes, _, _ = self._compiled()
        cell3 = next(e for e in scenes["s003"]["elements"] if e["id"] == "cell")
        assert set(cell3.get("drawn_regions") or []) == {"nucleus"}
        # the bare s001 draw carries as reach (drawn_frac), never "introduced"
        assert cell3.get("drawn_frac") == 0.5

    def test_compiled_scene_renders_with_annotated_asset(self):
        scenes, _, _ = self._compiled()
        s = Scene.model_validate(scenes["s002"])
        r = SceneRenderer(s, asset_resolver=_resolver(_cell_asset()))
        r.compile(6.0)
        assert r.timeline
        # the region slice really narrowed the draw to the nucleus span
        d_idx = next(i for i, a in enumerate(s.actions)
                     if a.verb == "draw" and a.target == "cell"
                     and a.region == "nucleus")
        lo, w = r._raster_slice(r.bound["cell"], s.actions[d_idx])
        # nucleus is the FIRST scheduled region, so base folds into it:
        # the span starts at 0 and covers base + nucleus points
        assert lo == 0.0 and 0.5 < w <= 1.0


class TestPerPartHandles:
    """The observed Cambridge failure shape: the model declares one
    'illustration' per organelle, all sharing ONE asset — per-part handles,
    not stacked images. The compiler must convert, not amputate."""

    def _raw(self):
        return {"chapters": [{
            "concept": "cell_anatomy", "transition": "clear_and_redraw",
            "assets": {"cell_diagram":
                       "A rectangular plant cell with wall, nucleus and "
                       "chloroplasts."},
            "elements": [
                {"id": "cell_wall", "type": "illustration",
                 "asset": "cell_diagram", "at": [640, 360]},
                {"id": "lbl_cell_wall", "type": "text", "text": "Cell wall",
                 "at": [100, 150], "role": "label"},
                {"id": "nucleus", "type": "illustration",
                 "asset": "cell_diagram", "at": [640, 360]},
                {"id": "lbl_nucleus", "type": "text", "text": "Nucleus",
                 "at": [100, 250], "role": "label"},
                {"id": "chloroplasts", "type": "illustration",
                 "asset": "cell_diagram", "at": [640, 360]},
                {"id": "lbl_chloroplasts", "type": "text",
                 "text": "Chloroplast", "at": [100, 350], "role": "label"},
            ],
            "steps": [
                {"segment": 1, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "cell_wall"},
                             {"verb": "write", "target": "lbl_cell_wall"}]},
                {"segment": 2, "decision": "EXTEND",
                 "actions": [{"verb": "draw", "target": "nucleus"},
                             {"verb": "write", "target": "lbl_nucleus"}]},
                {"segment": 3, "decision": "EXTEND",
                 "actions": [{"verb": "draw", "target": "chloroplasts"},
                             {"verb": "write", "target": "lbl_chloroplasts"}]},
            ],
        }]}

    def _compiled(self):
        plan = parse_visual_plan(self._raw())
        assert plan is not None
        narr = {"s001": "the wall", "s002": "the nucleus",
                "s003": "the chloroplasts"}
        out = compile_plan(plan, narr,
                           all_segments=["s001", "s002", "s003"],
                           skip_hold=set())
        return plan, out

    def test_handles_merge_not_drop(self):
        plan, (scenes, _, report) = self._compiled()
        assert any("MERGED handle 'nucleus'" in ln for ln in report)
        assert not any("DROPPED draw->nucleus" in ln for ln in report)
        d2 = [a for a in scenes["s002"]["actions"]
              if a["verb"] == "draw" and a["target"] == "cell_wall"]
        assert d2 and d2[0].get("region") == "nucleus"

    def test_asset_prompt_learns_layer_groups(self):
        plan, (scenes, assets, _) = self._compiled()
        prompt = assets["s002"]["cell_diagram"]
        assert "name the layer groups exactly:" in prompt.lower()
        assert "nucleus" in prompt and "chloroplasts" in prompt

    def test_arrows_synthesized_for_labels(self):
        plan, (scenes, _, report) = self._compiled()
        assert sum(1 for ln in report if "SYNTHESIZED" in ln) == 3
        s2 = scenes["s002"]
        arrow = next(e for e in s2["elements"]
                     if e["id"] == "arr_auto_lbl_nucleus")
        assert arrow["head"] == {"el": "cell_wall", "layer": "nucleus",
                                 "edge": "center"}
        assert arrow["tail"]["el"] == "lbl_nucleus"
        assert any(a["verb"] == "draw" and a["target"] == "arr_auto_lbl_nucleus"
                   for a in s2["actions"])

    def test_scene_validates_and_renders(self):
        plan, (scenes, _, _) = self._compiled()
        s = Scene.model_validate(scenes["s002"])
        asset = _cell_asset("cell_diagram")
        r = SceneRenderer(s, asset_resolver=_resolver(asset))
        r.compile(6.0)
        assert "arr_auto_lbl_nucleus" in r.audit()["arrow_heads"]


class TestForeignAssetHandles:
    """Round-3 plan shape: one 'illustration' per organelle, each with its
    OWN asset ('nucleus_obj' + asset 'nucleus_img' beside 'lbl_nucleus').
    Labelled part names identify them as handles; the root asset is rebuilt
    under a __merged key so the diagram actually contains the parts."""

    def _raw(self):
        return {"chapters": [{
            "concept": "cell", "transition": "clear_and_redraw",
            "assets": {"wall_img": "The cell wall.",
                       "nucleus_img": "The nucleus.",
                       "scenery": "A meadow."},
            "elements": [
                {"id": "wall", "type": "illustration", "asset": "wall_img",
                 "at": [640, 360]},
                {"id": "nucleus_obj", "type": "illustration",
                 "asset": "nucleus_img", "at": [640, 360]},
                {"id": "meadow", "type": "illustration", "asset": "scenery",
                 "at": [640, 360]},
                {"id": "lbl_nucleus", "type": "text", "text": "Nucleus",
                 "at": [100, 250], "role": "label"},
            ],
            "steps": [
                {"segment": 1, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "wall"},
                             {"verb": "draw", "target": "wall"}]},
                {"segment": 2, "decision": "EXTEND",
                 "actions": [{"verb": "draw", "target": "nucleus_obj"},
                             {"verb": "write", "target": "lbl_nucleus"}]},
            ],
        }]}

    def _compiled(self):
        plan = parse_visual_plan(self._raw())
        return compile_plan(plan, {"s001": "the wall", "s002": "the nucleus"},
                            all_segments=["s001", "s002"], skip_hold=set())

    def test_part_named_foreign_asset_merges(self):
        scenes, assets, report = self._compiled()
        assert any("MERGED handle 'nucleus_obj'" in ln for ln in report)
        assert any("ROOT ASSET rebuilt as 'wall_img__merged'" in ln
                   for ln in report)
        prompt = assets["s002"]["wall_img__merged"]
        assert "name the layer groups exactly:" in prompt.lower()
        assert "nucleus" in prompt
        root = next(e for e in scenes["s002"]["elements"] if e["id"] == "wall")
        assert root["asset"] == "wall_img__merged"
        d = [a for a in scenes["s002"]["actions"]
             if a["verb"] == "draw" and a["target"] == "wall"]
        assert d and d[0].get("region") == "nucleus"

    def test_unrelated_asset_still_drops(self):
        scenes, _, report = self._compiled()
        assert any("DROPPED illustration 'meadow'" in ln for ln in report)


class TestRoundTwoRegressions:
    """Defects the second full render surfaced."""

    def test_underscore_part_names_still_match(self):
        raw = _plan_raw()
        ch = raw["chapters"][0]
        ch["assets"]["plant_cell"] = ("A plant cell. Name the layer groups "
                                      "exactly: cell_wall, nucleus")
        ch["elements"][1]["id"] = "lbl_cell_wall"
        ch["elements"][1]["text"] = "Cell wall"
        ch["elements"][2]["id"] = "arr_cell_wall"
        plan = parse_visual_plan(raw)
        scenes, _, report = compile_plan(
            plan, {"s001": "a", "s002": "b", "s003": "c"},
            all_segments=["s001", "s002", "s003"], skip_hold=set())
        assert any("ANCHORED arr_cell_wall -> cell.cell_wall" in ln
                   for ln in report)

    def test_empty_scene_is_skipped_not_emitted(self):
        raw = {"chapters": [{
            "concept": "c", "transition": "clear_and_redraw",
            "assets": {"a1": "x", "a2": "y"},
            "elements": [
                {"id": "i1", "type": "illustration", "asset": "a1",
                 "at": [600, 300]},
                {"id": "i2", "type": "illustration", "asset": "a2",
                 "at": [600, 300]},
            ],
            "steps": [
                {"segment": 1, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "i2"}]},  # dropped
                {"segment": 2, "decision": "EXTEND",
                 "actions": [{"verb": "draw", "target": "i1"}]},
            ],
        }]}
        plan = parse_visual_plan(raw)
        scenes, _, report = compile_plan(plan, {"s001": "a", "s002": "b"},
                                         all_segments=["s001", "s002"],
                                         skip_hold=set())
        assert "s001" not in scenes            # whiteboard fallback instead
        assert any("SKIPPED empty scene" in ln for ln in report)
        assert "s002" in scenes

    def test_renderer_warnings_deduplicate(self):
        r = SceneRenderer(_anchor_scene("ribosome"),
                          asset_resolver=_resolver(_cell_asset()))
        r.compile(5.0)
        for _ in range(4):
            r._warn("UNRESOLVED_ANCHOR cell.ribosome")
        assert r.audit()["warnings"].count("UNRESOLVED_ANCHOR cell.ribosome") == 1

    def test_scrub_text_erases_only_the_boxes(self):
        from spike.scene_engine.raster_assets import scrub_text
        ink = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        px = ink.load()
        for x in range(100):
            px[x, 10] = (20, 20, 40, 255)      # a stroke to keep
            px[x, 50] = (20, 20, 40, 255)      # "text" to erase
        out = scrub_text(ink, [[0.0, 44.0, 100.0, 56.0]])
        assert out.getpixel((50, 50))[3] == 0      # erased
        assert out.getpixel((50, 10))[3] == 255    # untouched
        assert ink.getpixel((50, 50))[3] == 255    # original not mutated


class TestNarrationInjection:
    """Round-7 degenerate plan: labels + arrows declared, steps only ever
    draw the root. The narration says which part each step teaches — the
    declared label and its anchored arrow must join that step."""

    def _raw(self):
        return {"chapters": [{
            "concept": "cell", "transition": "clear_and_redraw",
            "assets": {"pc": ("A plant cell. Name the layer groups exactly: "
                              "cell wall, nucleus")},
            "elements": [
                {"id": "cell", "type": "illustration", "asset": "pc",
                 "at": [640, 360]},
                {"id": "lbl_cell_wall", "type": "text", "text": "Cell wall",
                 "at": [90, 120], "role": "label"},
                {"id": "arrow_cell_wall", "type": "arrow", "tail": [180, 130],
                 "head": [600, 340]},
                {"id": "lbl_nucleus", "type": "text", "text": "Nucleus",
                 "at": [90, 220], "role": "label"},
                {"id": "arrow_nucleus", "type": "arrow", "tail": [180, 230],
                 "head": [620, 360]},
            ],
            "steps": [
                {"segment": 1, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "cell"}]},
                {"segment": 2, "decision": "EXTEND",
                 "actions": [{"verb": "draw", "target": "cell"}]},
                {"segment": 3, "decision": "EXTEND",
                 "actions": [{"verb": "draw", "target": "cell"}]},
            ],
        }]}

    def _compiled(self):
        plan = parse_visual_plan(self._raw())
        narr = {"s001": "Every plant cell has a strong cell wall around it.",
                "s002": "Inside sits the nucleus, the control centre.",
                "s003": "And that is the whole cell."}
        return compile_plan(plan, narr, all_segments=["s001", "s002", "s003"],
                            skip_hold=set())

    def test_labels_and_arrows_join_their_narrated_steps(self):
        scenes, _, report = self._compiled()
        assert any("INJECTED write->lbl_cell_wall + draw->arrow_cell_wall"
                   in ln for ln in report)
        s1 = scenes["s001"]
        assert any(a["verb"] == "write" and a["target"] == "lbl_cell_wall"
                   for a in s1["actions"])
        s2 = scenes["s002"]
        assert any(a["verb"] == "draw" and a["target"] == "arrow_nucleus"
                   for a in s2["actions"])
        d2 = next(a for a in s2["actions"]
                  if a["verb"] == "draw" and a["target"] == "cell")
        assert d2.get("region") == "nucleus"

    def test_carried_board_never_goes_blank(self):
        scenes, _, _ = self._compiled()
        cell3 = next(e for e in scenes["s003"]["elements"]
                     if e["id"] == "cell")
        # either real region carry (with order stamped) or fully introduced —
        # never drawn_regions without region_order
        if cell3.get("drawn_regions"):
            assert cell3.get("region_order")


class TestNoScheduleMeansNoRegionTags:
    def test_draws_stay_untagged_without_a_real_schedule(self):
        raw = {"chapters": [{
            "concept": "c", "transition": "clear_and_redraw",
            "assets": {"pc": "A cell. Name the layer groups exactly: wall"},
            "elements": [{"id": "cell", "type": "illustration", "asset": "pc",
                          "at": [640, 360]}],
            "steps": [
                {"segment": 1, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "cell"}]},
                {"segment": 2, "decision": "EXTEND",
                 "actions": [{"verb": "draw", "target": "cell"}]},
            ],
        }]}
        plan = parse_visual_plan(raw)
        scenes, _, _ = compile_plan(plan, {"s001": "hello", "s002": "world"},
                                    all_segments=["s001", "s002"],
                                    skip_hold=set())
        for sid in ("s001", "s002"):
            for a in scenes[sid]["actions"]:
                if a["verb"] == "draw" and a["target"] == "cell":
                    assert a.get("region") is None
        cell2 = next(e for e in scenes["s002"]["elements"]
                     if e["id"] == "cell")
        assert not cell2.get("drawn_regions")


class TestPartNamesFromLabels:
    def test_untailed_prompt_learns_parts_from_labels(self):
        raw = _plan_raw()
        ch = raw["chapters"][0]
        ch["assets"]["plant_cell"] = "A rectangular plant cell."  # no tail
        ch["elements"].append({"id": "lbl_wall", "type": "text",
                               "text": "Cell wall", "at": [90, 60],
                               "role": "label"})
        plan = parse_visual_plan(raw)
        scenes, assets, report = compile_plan(
            plan, {"s001": "a", "s002": "b", "s003": "c"},
            all_segments=["s001", "s002", "s003"], skip_hold=set())
        assert any("PART NAMES from labels" in ln for ln in report)
        assert "name the layer groups exactly:" in assets["s002"]["plant_cell"].lower()
        assert any("ANCHORED arr_nucleus" in ln for ln in report)


class TestRegionCarryBeatsLayers:
    def test_draws_with_both_layers_and_labels_carry_regions(self):
        """The model stamps `layers` on its draws; region tracking must win
        or the carried board under-reveals every segment start (round-5's
        flickering wall hatch)."""
        raw = _plan_raw()
        for st in raw["chapters"][0]["steps"]:
            for a in st.actions if hasattr(st, "actions") else st["actions"]:
                if a.get("verb") == "draw" and a.get("target") == "cell":
                    a["layers"] = ["whatever_the_model_said"]
        plan = parse_visual_plan(raw)
        scenes, _, _ = compile_plan(
            plan, {"s001": "a", "s002": "b", "s003": "c"},
            all_segments=["s001", "s002", "s003"], skip_hold=set())
        cell3 = next(e for e in scenes["s003"]["elements"] if e["id"] == "cell")
        assert set(cell3.get("drawn_regions") or []) == {"nucleus"}
        assert cell3.get("drawn_frac") == 0.5     # bare-draw reach, not layers
        assert cell3.get("drawn_layers") is None


class TestBoundaryRenameAnchors:
    def test_prev_board_arrow_anchors_rename_with_their_targets(self):
        raw = {"chapters": [
            {"concept": "one", "transition": "clear_and_redraw",
             "assets": {"a1": "x. Name the layer groups exactly: leaf, stem"},
             "elements": [
                 {"id": "plant", "type": "illustration", "asset": "a1",
                  "at": [640, 360]},
                 {"id": "lbl_leaf", "type": "text", "text": "Leaf",
                  "at": [100, 100], "role": "label"},
             ],
             "steps": [{"segment": 1, "decision": "NEW_VISUAL",
                        "actions": [{"verb": "draw", "target": "plant"},
                                    {"verb": "draw", "target": "plant"},
                                    {"verb": "write", "target": "lbl_leaf"}]}]},
            {"concept": "two", "transition": "clear_and_redraw",
             "assets": {"a2": "y"},
             "elements": [{"id": "next", "type": "illustration", "asset": "a2",
                           "at": [640, 360]}],
             "steps": [{"segment": 2, "decision": "CLEAR_AND_REDRAW",
                        "actions": [{"verb": "draw", "target": "next"}]}]},
        ]}
        plan = parse_visual_plan(raw)
        scenes, _, report = compile_plan(plan, {"s001": "a", "s002": "b"},
                                         all_segments=["s001", "s002"],
                                         skip_hold=set())
        assert any("SYNTHESIZED arr_auto_lbl_leaf" in ln for ln in report)
        s2 = scenes["s002"]
        arrow = next(e for e in s2["elements"]
                     if e["id"].endswith("arr_auto_lbl_leaf"))
        assert arrow["id"].startswith("prev__")
        assert arrow["tail"]["el"].startswith("prev__")
        assert arrow["head"]["el"].startswith("prev__")
        Scene.model_validate(s2)      # the boundary scene must stay valid


class TestLabelSynthesis:
    """Round-12 variance: a full cell declared with NO labels or arrows at
    all. The narration still teaches the parts — so the compiler labels
    them, and the arrow synthesis arms each label."""

    def test_unlabelled_parts_get_labels_and_arrows(self):
        raw = {"chapters": [{
            "concept": "cell", "transition": "clear_and_redraw",
            "assets": {"pc": ("A plant cell. Name the layer groups exactly: "
                              "cell wall, nucleus")},
            "elements": [{"id": "cell", "type": "illustration", "asset": "pc",
                          "at": [640, 360]}],
            "steps": [
                {"segment": 1, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "cell"}]},
                {"segment": 2, "decision": "EXTEND",
                 "actions": [{"verb": "draw", "target": "cell"}]},
            ],
        }]}
        plan = parse_visual_plan(raw)
        narr = {"s001": "The cell wall keeps the cell strong.",
                "s002": "Inside, the nucleus controls everything."}
        scenes, _, report = compile_plan(plan, narr,
                                         all_segments=["s001", "s002"],
                                         skip_hold=set())
        assert any("SYNTHESIZED lbl_auto_cell_wall" in ln for ln in report)
        assert any("SYNTHESIZED lbl_auto_nucleus" in ln for ln in report)
        assert any("SYNTHESIZED arr_auto_lbl_auto_nucleus" in ln
                   for ln in report)
        s2 = scenes["s002"]
        lbl = next(e for e in s2["elements"] if e["id"] == "lbl_auto_nucleus")
        assert lbl["text"] == "Nucleus"
        assert any(a["verb"] == "write" and a["target"] == "lbl_auto_nucleus"
                   for a in s2["actions"])
        arrow = next(e for e in s2["elements"]
                     if e["id"] == "arr_auto_lbl_auto_nucleus")
        assert arrow["head"]["layer"] == "nucleus"
        d2 = next(a for a in s2["actions"]
                  if a["verb"] == "draw" and a["target"] == "cell")
        assert d2.get("region") == "nucleus"

    def test_the_overlap_audit_measures_final_boxes_not_starting_ones(self):
        """Ordering matters more than the check itself. An earlier version
        ran at bind time and reported seven overlaps on a lesson whose
        rendered frames were correct — _relayout_part_labels had already
        flowed those labels around the diagram. A metric that cries wolf
        gets ignored, which is worse than not having it."""
        import inspect
        from spike.scene_engine.render import SceneRenderer
        src = inspect.getsource(SceneRenderer._bind)
        assert src.index("_relayout_part_labels") < \
            src.index("_audit_text_overlaps"), \
            "the overlap audit must run AFTER the label relayout"

    def test_the_overlap_audit_catches_a_real_collision(self):
        from spike.scene_engine.render import SceneRenderer
        from spike.scene_engine.schema import Scene

        scene = {"id": "t", "narration": "Two labels, one spot.",
                 "elements": [
                     {"id": "a", "type": "text", "text": "Sap Vacuole",
                      "at": [95, 200], "anchor": "lt", "role": "label"},
                     {"id": "b", "type": "text", "text": "Chloroplasts",
                      "at": [98, 204], "anchor": "lt", "role": "label"}],
                 "actions": [{"verb": "write", "target": "a"},
                             {"verb": "write", "target": "b"}]}
        r = SceneRenderer(Scene.model_validate(scene))
        hits = [w for w in r.audit()["warnings"] if "TEXT_OVERLAP" in w]
        assert hits, "two labels on the same spot must be reported"

    def test_synthesized_labels_do_not_land_on_declared_ones(self):
        """A rendered frame showed "Plant Cell" and "Cytoplasm" printed into
        each other: synthesis started the left column at the top, and the
        semantic adapter had already filled that same column on the same
        pitch. Every label must get its own row."""
        raw = {"chapters": [{
            "concept": "cell", "transition": "clear_and_redraw",
            "assets": {"pc": ("A plant cell. Name the layer groups exactly: "
                              "cell wall, nucleus")},
            "elements": [
                {"id": "cell", "type": "illustration", "asset": "pc",
                 "at": [640, 360]},
                # what the adapter lays down for a model-declared label
                {"id": "lbl_declared", "type": "text", "text": "Plant Cell",
                 "role": "label", "at": [95.0, 140.0]},
            ],
            "steps": [
                {"segment": 1, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "cell"},
                             {"verb": "write", "target": "lbl_declared"}]},
                {"segment": 2, "decision": "EXTEND",
                 "actions": [{"verb": "draw", "target": "cell"}]},
            ],
        }]}
        plan = parse_visual_plan(raw)
        narr = {"s001": "This is a plant cell with a cell wall.",
                "s002": "Inside, the nucleus controls everything."}
        scenes, _, _ = compile_plan(plan, narr,
                                    all_segments=["s001", "s002"],
                                    skip_hold=set())
        rows = [tuple(e["at"]) for e in scenes["s002"]["elements"]
                if e.get("role") == "label" and e.get("at")]
        assert len(rows) == len(set(rows)), f"labels share a position: {rows}"
        ys = sorted(y for _, y in rows)
        assert all(b - a >= 60.0 for a, b in zip(ys, ys[1:])), \
            f"labels are stacked too tightly to read: {ys}"

    def test_declared_labels_suppress_synthesis(self):
        plan = parse_visual_plan(_plan_raw())     # lbl_nucleus declared
        narr = {"s001": "a", "s002": "the nucleus", "s003": "c"}
        _, _, report = compile_plan(plan, narr,
                                    all_segments=["s001", "s002", "s003"],
                                    skip_hold=set())
        assert not any("SYNTHESIZED lbl_auto_nucleus" in ln for ln in report)


class TestColorAvatars:
    def test_pale_interiors_stay_opaque_paper_is_cut(self):
        """A brightness threshold made light hair/skin/shirts transparent —
        the avatar read as a ghost. Only paper CONNECTED TO THE BORDER may
        be cut."""
        from spike.scene_engine.raster_assets import to_color_art
        import numpy as np
        im = Image.new("RGB", (120, 120), (255, 255, 255))
        px = im.load()
        for y in range(30, 100):
            for x in range(35, 85):
                px[x, y] = (250, 244, 238)      # pale, enclosed = artwork
        for y in range(28, 102):
            for x in (34, 85):
                px[x, y] = (20, 20, 20)
        for x in range(34, 86):
            for y in (28, 101):
                px[x, y] = (20, 20, 20)
        out = to_color_art(im)
        a = np.asarray(out.getchannel("A"))
        assert a[a.shape[0] // 2, a.shape[1] // 2] > 240   # kept
        assert a[2, 2] < 15                                # paper gone
        # colour survives the round trip
        rgb = out.convert("RGB").getpixel((out.width // 2, out.height // 2))
        assert max(rgb) - min(rgb) >= 0 and rgb[0] > 200

    def test_rate_limit_is_retried_not_swallowed(self):
        from spike.scene_engine.raster_assets import _with_backoff
        import requests as rq
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                r = rq.Response()
                r.status_code = 429
                raise rq.HTTPError(response=r)
            return {"ok": True}

        import spike.scene_engine.raster_assets as ra
        real_sleep = __import__("time").sleep
        try:
            __import__("time").sleep = lambda s: None
            assert _with_backoff(flaky, "test") == {"ok": True}
        finally:
            __import__("time").sleep = real_sleep
        assert calls["n"] == 2


class TestHandwritingFont:
    def test_smart_punctuation_never_breaks_the_hand_font(self):
        from spike.scene_engine.render import _hand_font, ascii_punct
        line = "Now, here's the really cool part: all things …"
        assert _hand_font(False, 24, line) is None      # the old failure
        assert _hand_font(False, 24, ascii_punct(line)) is not None
        s = Scene.model_validate({
            "id": "f", "narration": "x", "compiled": True,
            "elements": [{"id": "t", "type": "text",
                          "text": "here's a tree — and an ant…",
                          "at": [400, 300]}],
            "actions": [{"verb": "write", "target": "t"}],
        })
        r = SceneRenderer(s)
        shown = r.bound["t"].text.display
        assert "'" in shown and "..." in shown
        assert not any(ord(c) > 0x2014 for c in shown)


class TestAutoSketch:
    def test_named_objects_are_sketched_when_the_board_is_empty(self):
        from spike.scene_engine.whiteboard import build_whiteboard_scene
        # no heading: the board really IS empty, which is what this test
        # claims to cover. It used to pass a heading, i.e. it encoded the bug
        # where a sketch was drawn over the card's own writing.
        card = build_whiteboard_scene({
            "segment_id": "s001", "type": "hook",
            "text": ("What makes a towering tree so different from a tiny "
                     "ant? Both are alive.")})
        sk = [e for e in card["elements"]
              if str(e.get("id", "")).startswith("sk_")]
        assert {e["asset"] for e in sk} == {"sk_tree", "sk_ant"}
        assert set(card["scene_assets"]) == {"sk_tree", "sk_ant"}
        # each is DRAWN, cued to its own words in the narration
        for e in sk:
            a = next(a for a in card["actions"]
                     if a.get("target") == e["id"])
            assert a["verb"] == "draw"
            assert a["at"]["phrase"].lower() in card["narration"].lower()

    def test_a_planned_diagram_is_never_overdrawn(self):
        plan = parse_visual_plan(_plan_raw())
        narr = {"s001": "A tree and an ant are both alive.",
                "s002": "The nucleus controls the cell.", "s003": "Look."}
        scenes, assets, report = compile_plan(
            plan, narr, all_segments=["s001", "s002", "s003"],
            skip_hold=set())
        # every scene here carries the planned cell — nothing is overdrawn
        assert not any("SKETCHED" in ln for ln in report)
        for sc in scenes.values():
            assert not any(str(e.get("id", "")).startswith("sk_")
                           for e in sc["elements"])

    def test_a_text_only_chapter_gets_sketches(self):
        raw = {"chapters": [{
            "concept": "intro", "transition": "clear_and_redraw",
            "assets": {},
            "elements": [{"id": "t1", "type": "text", "text": "Living things",
                          "at": [400, 120], "role": "title"}],
            "steps": [{"segment": 1, "decision": "NEW_VISUAL",
                       "actions": [{"verb": "write", "target": "t1"}]}],
        }]}
        plan = parse_visual_plan(raw)
        narr = {"s001": "A towering tree and a tiny ant are both alive."}
        scenes, assets, report = compile_plan(plan, narr,
                                              all_segments=["s001"],
                                              skip_hold=set())
        assert any("SKETCHED" in ln and "s001" in ln for ln in report)
        assert "sk_tree" in assets["s001"] and "sk_ant" in assets["s001"]
        Scene.model_validate(scenes["s001"])

    def test_negations_and_unknown_words_draw_nothing(self):
        from spike.scene_engine.sketchables import find_sketchables
        assert find_sketchables("There is no tree in this diagram.") == []
        assert find_sketchables("Mitosis produces two daughter nuclei.") == []
        # longest phrase wins, and each concept appears once
        got = find_sketchables("A blade of grass, more grass, and a whale.")
        assert [g["key"] for g in got] == ["sk_grass", "sk_whale"]


class TestLabelLayout:
    def _scene(self, n_labels=4):
        els = [{"id": "cell", "type": "illustration", "asset": "plant_cell",
                "at": [640, 360], "scale": 1.0},
               {"id": "__teach_av", "type": "illustration",
                "asset": "avatar_teacher", "at": [1150, 556], "scale": 0.30}]
        acts = [{"verb": "draw", "target": "cell"}]
        # model scatters labels badly on purpose (all far right, overlapping)
        parts = ["nucleus", "chloroplast", "chloroplast", "nucleus"][:n_labels]
        for i in range(n_labels):
            els.append({"id": f"lbl{i}", "type": "text",
                        "text": f"Label number {i}", "at": [1150, 80],
                        "anchor": "lt"})
            els.append({"id": f"ar{i}", "type": "arrow",
                        "tail": {"el": f"lbl{i}", "edge": "right"},
                        "head": {"el": "cell", "layer": parts[i],
                                 "edge": "center",
                                 "instance": "first" if i < 2 else "largest"}})
            acts += [{"verb": "write", "target": f"lbl{i}"},
                     {"verb": "draw", "target": f"ar{i}"}]
        return Scene.model_validate({"id": "lay", "narration": "x",
                                     "compiled": True, "elements": els,
                                     "actions": acts})

    def test_labels_flow_in_columns_without_overlap(self):
        r = SceneRenderer(self._scene(), asset_resolver=_resolver(_cell_asset()))
        boxes = [r.bound[f"lbl{i}"].box for i in range(4)]
        for i in range(4):
            for j in range(i + 1, 4):
                a, c = boxes[i], boxes[j]
                assert not (a[0] < c[2] and a[2] > c[0] and
                            a[1] < c[3] and a[3] > c[1]), (i, j, a, c)
        # asset at (640,360) scale 1 -> box 540..740; nucleus box is on the
        # RIGHT half, chloroplast instances on the LEFT half of the art
        # (parts: nucleus, chloroplast, chloroplast, nucleus)
        for i, side in enumerate(["right", "left", "left", "right"]):
            x0 = boxes[i][0]
            if side == "right":
                assert 740 < x0 < 900, (i, boxes[i])   # hugging, not far edge
            else:
                assert boxes[i][2] < 540, (i, boxes[i])

    def test_arrow_leaves_the_side_facing_its_target(self):
        r = SceneRenderer(self._scene(), asset_resolver=_resolver(_cell_asset()))
        # label 2 targets a LEFT-half chloroplast; its relayouted position is
        # the left column, so the arrow must leave its RIGHT edge (toward the
        # art), starting right of the label box
        lb = r.bound["lbl2"].box
        tail_x = r.bound["ar2"].head_pt  # ensure bound; then check strokes
        strokes = r.bound["ar2"].layers[0].strokes
        first_pt = strokes[0].pts[0]
        assert first_pt[0] >= lb[2] - 1.0


class TestAvatarKeepOut:
    def test_labels_never_write_over_the_avatars(self):
        s = Scene.model_validate({
            "id": "koz", "narration": "labels and avatars",
            "compiled": True,
            "elements": [
                {"id": "__teach_av", "type": "illustration",
                 "asset": "avatar_teacher", "at": [1150, 556], "scale": 0.30},
                {"id": "lbl_nucleus", "type": "text", "text": "Nucleus",
                 "at": [1120, 540], "anchor": "lt"},
                {"id": "lbl_vacuole", "type": "text", "text": "Sap Vacuole",
                 "at": [1110, 600], "anchor": "lt"},
                {"id": "lbl_top", "type": "text", "text": "Cell Wall",
                 "at": [1100, 100], "anchor": "lt"},
            ],
            "actions": [{"verb": "write", "target": "lbl_nucleus"}],
        })
        r = SceneRenderer(s)
        zone = (1150 - 118, 556 - 132, 1150 + 118, 556 + 155)
        for lid in ("lbl_nucleus", "lbl_vacuole"):
            x0, y0, x1, y1 = r.bound[lid].box
            assert not (x0 < zone[2] and x1 > zone[0] and
                        y0 < zone[3] and y1 > zone[1]), (lid, r.bound[lid].box)
        # the two relocated labels do not land on each other
        b1, b2 = r.bound["lbl_nucleus"].box, r.bound["lbl_vacuole"].box
        assert not (b1[0] < b2[2] and b1[2] > b2[0] and
                    b1[1] < b2[3] and b1[3] > b2[1])
        # a label far from the zone stays where the model put it
        assert abs(r.bound["lbl_top"].box[1] - 100) < 30
        assert any(w.startswith("LABEL_MOVED_OFF_AVATAR")
                   for w in r._audit_warnings)

    def test_no_avatars_no_keepout(self):
        s = Scene.model_validate({
            "id": "koz2", "narration": "x", "compiled": True,
            "elements": [{"id": "lbl", "type": "text", "text": "Nucleus",
                          "at": [1120, 560], "anchor": "lt"}],
            "actions": [{"verb": "write", "target": "lbl"}],
        })
        r = SceneRenderer(s)
        assert abs(r.bound["lbl"].box[1] - 560) < 30


class TestMassHandles:
    def test_many_part_illustrations_with_no_labels_all_merge(self):
        """Observed: 8 per-part illustrations, per-part assets, ZERO labels.
        All must merge as handles; label synthesis then rebuilds labels from
        the parts the narration names."""
        raw = {"chapters": [{
            "concept": "cell", "transition": "clear_and_redraw",
            "assets": {"base": "A plant cell outline.",
                       "w": "wall", "n": "nucleus", "v": "vacuole",
                       "c": "chloroplasts"},
            "elements": [
                {"id": "cell_base", "type": "illustration", "asset": "base",
                 "at": [640, 360]},
                {"id": "cell_wall", "type": "illustration", "asset": "w",
                 "at": [640, 360]},
                {"id": "cell_nucleus", "type": "illustration", "asset": "n",
                 "at": [640, 360]},
                {"id": "cell_vacuole", "type": "illustration", "asset": "v",
                 "at": [640, 360]},
                {"id": "cell_chloroplasts", "type": "illustration",
                 "asset": "c", "at": [640, 360]},
            ],
            "steps": [
                {"segment": 1, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "cell_base"},
                             {"verb": "draw", "target": "cell_wall"}]},
                {"segment": 2, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "cell_nucleus"}]},
                {"segment": 3, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "cell_vacuole"},
                             {"verb": "draw", "target": "cell_chloroplasts"}]},
            ],
        }]}
        plan = parse_visual_plan(raw)
        narr = {"s001": "The cell wall keeps it strong.",
                "s002": "The cell nucleus controls everything.",
                "s003": "The cell vacuole stores sap and the cell "
                        "chloroplasts make food."}
        scenes, assets, report = compile_plan(
            plan, narr, all_segments=["s001", "s002", "s003"],
            skip_hold=set())
        assert not any("DROPPED illustration" in ln for ln in report)
        assert sum(1 for ln in report if "MERGED handle" in ln) == 4
        assert any("ROOT ASSET rebuilt" in ln for ln in report)
        # every segment kept a scene, draws retargeted to the root
        for sid in ("s001", "s002", "s003"):
            assert sid in scenes
            assert any(a.get("verb") == "draw" and
                       a.get("target") == "cell_base"
                       for a in scenes[sid]["actions"])
        # labels synthesized from the parts the narration names
        assert any("SYNTHESIZED lbl_auto_cell_nucleus" in ln for ln in report)


class TestSpeechBubbles:
    """Founder rules: bubble text is VERBATIM narration (a paraphrase is a
    disconnect), selection is by IMPORTANCE across the whole lesson, and
    speech APPEARS/fades — the hand never draws or letters it."""

    NARR = ("Look at this closely. The cell wall provides support and "
            "protection for the plant cell. Pretty neat, right?")

    def test_snap_replaces_a_paraphrase_with_the_real_sentence(self):
        from spike.scene_engine.whiteboard import snap_to_narration
        snapped = snap_to_narration("Cell wall provides support & protection",
                                    self.NARR)
        assert snapped == ("The cell wall provides support and protection "
                           "for the plant cell.")

    def test_snap_refuses_an_unrelated_claim(self):
        from spike.scene_engine.whiteboard import snap_to_narration
        assert snap_to_narration("Mitochondria release energy",
                                 self.NARR) is None

    def test_importance_selection_skips_filler_and_questions(self):
        from spike.scene_engine.whiteboard import select_key_sentence
        assert select_key_sentence(self.NARR) == (
            "The cell wall provides support and protection for the plant "
            "cell.")
        assert select_key_sentence(
            "Wow. Amazing stuff here. What could it be?") is None

    def test_bubble_appears_and_fades_never_drawn(self):
        from spike.scene_engine.whiteboard import key_point_choreo
        els, acts = key_point_choreo(
            "The cell wall provides support and protection for the plant "
            "cell.", uid="s005")
        bubble_ids = {e["id"] for e in els}
        for a in acts:
            if a.get("target") in bubble_ids:
                assert a["verb"] in ("reveal", "fade"), a
        reveal = next(a for a in acts if a["verb"] == "reveal")
        fade = next(a for a in acts if a["verb"] == "fade")
        # cued to the spoken words, gone shortly after they end
        assert reveal["at"]["phrase"] == "The cell wall provides support"
        assert fade["at"]["phrase"] == reveal["at"]["phrase"]
        assert 1.4 < fade["at"]["offset"] <= 5.0

    def test_long_sentence_wraps_to_two_lines(self):
        from spike.scene_engine.whiteboard import bubble_elements
        els = bubble_elements("b", "The cell wall provides support and "
                              "protection for the plant cell.", (600, 300),
                              (700, 400))
        texts = [e for e in els if e["type"] == "text"]
        assert len(texts) == 2

    def test_planned_key_point_snaps_to_narration(self):
        raw = _plan_raw()
        raw["chapters"][0]["steps"][1]["key_point"] = \
            "nucleus is the control centre"          # model paraphrase
        plan = parse_visual_plan(raw)
        narr = {"s001": "Here is the cell.",
                "s002": "The nucleus is the control centre of the whole "
                        "cell. It stores the instructions.",
                "s003": "Look closer."}
        scenes, _, report = compile_plan(
            plan, narr, all_segments=["s001", "s002", "s003"],
            skip_hold=set())
        bolded = [e for e in scenes["s002"]["elements"]
                  if str(e.get("id", "")).startswith("__nb_") and
                  e.get("type") == "text" and e.get("role") == "term"]
        assert bolded and "".join(e["text"] for e in bolded).startswith(
            "The nucleus is the")
        assert any("STREAM" in ln and "The nucleus is the control centre"
                   in ln for ln in report if "s002" in ln)

    def test_auto_bubbles_cover_unplanned_important_segments(self):
        plan = parse_visual_plan(_plan_raw())    # no key_points planned
        narr = {"s001": "Every living thing is made of cells.",
                "s002": "The nucleus is the control centre of the cell.",
                "s003": "Now just look at it for a moment."}
        scenes, _, report = compile_plan(
            plan, narr, all_segments=["s001", "s002", "s003"],
            skip_hold=set())
        streams = [ln for ln in report if "| STREAM" in ln]
        # EVERY segment streams its narration now; importance decides BOLD
        assert any("s001" in ln and "bold: [" in ln for ln in streams)
        assert any("s002" in ln and "bold: [" in ln for ln in streams)
        s3 = next(ln for ln in streams if "s003" in ln)
        assert "bold: [" not in s3                      # filler: nothing bold

    def test_whiteboard_cards_get_bubbles_but_quizzes_do_not(self):
        from spike.scene_engine.whiteboard import build_whiteboard_scene
        card = build_whiteboard_scene({
            "segment_id": "s004", "type": "explore",
            "slide_heading": "Cells",
            "text": "A microscope makes tiny cells visible to us."})
        assert any(str(e["id"]).startswith("__nb_") for e in card["elements"])
        quiz = build_whiteboard_scene({
            "segment_id": "s005", "type": "explore",
            "slide_heading": "Check",
            "slide_visual": {"kind": "quiz", "caption": "Which part?",
                             "options": ["Wall", "Nucleus"]},
            "text": "The wall is the answer to this question."})
        assert not any(str(e["id"]).startswith("__nb_")
                       for e in quiz["elements"])

    def test_student_moment_bubble_is_not_drawn_either(self):
        from spike.scene_engine.whiteboard import human_moment
        els, acts, _ = human_moment("student", "Why does it wilt?", uid="m1")
        bubble_ids = {e["id"] for e in els if "bub" in e["id"]}
        for a in acts:
            if a.get("target") in bubble_ids:
                assert a["verb"] in ("reveal", "fade"), a


class TestSeedMoment:
    def test_seeds_one_student_question_from_narration(self):
        plan = parse_visual_plan(_plan_raw())
        narr = {"s001": "Here is the cell.",
                "s002": "But why does the nucleus matter? It runs the show.",
                "s003": "Look closer."}
        sid = seed_moment(plan, narr)
        assert sid == "s002"
        st = [st for ch in plan.chapters for st in ch.steps
              if st.segment_id == "s002"][0]
        assert st.moment["role"] == "student"
        assert st.moment["text"].endswith("?") and len(st.moment["text"]) <= 60

    def test_never_stacks_a_second_moment(self):
        plan = parse_visual_plan(_plan_raw())
        plan.chapters[0].steps[1].moment = {"role": "teacher", "text": "Key idea"}
        assert seed_moment(plan, {"s002": "Why though?"}) is None

    def test_moment_expands_into_avatar_on_the_scene(self):
        plan = parse_visual_plan(_plan_raw())
        narr = {"s001": "Here is the cell.",
                "s002": "Why does the nucleus matter? It runs the show.",
                "s003": "Look closer."}
        assert seed_moment(plan, narr) == "s002"
        scenes, assets, _ = compile_plan(
            plan, narr, all_segments=["s001", "s002", "s003"], skip_hold=set())
        els = scenes["s002"]["elements"]
        assert any(e.get("type") == "illustration"
                   and str(e.get("asset", "")).startswith("avatar_")
                   for e in els)
        assert any(k.startswith("avatar_") for k in assets["s002"])


class TestVisionBoxShapes:
    """The vision model answers with either [ymin,xmin,ymax,xmax] or
    {"ymin":..,"xmin":..}. Slicing a dict raises KeyError, which was not in
    the caught tuple — so one dict-shaped reply killed the asset outright and
    that segment fell back to the LEGACY renderer mid-lesson."""

    def _scan(self, monkeypatch, payload):
        from PIL import Image
        import spike.scene_engine.raster_assets as ra
        monkeypatch.setattr(ra, "_vision_json", lambda *a, **k: payload)
        return ra.scan_text(Image.new("RGBA", (1000, 1000), (0, 0, 0, 0)))

    def test_list_shaped_boxes(self, monkeypatch):
        got = self._scan(monkeypatch,
                         {"text_boxes": [[100, 200, 300, 400]]})
        assert got == [[200.0, 100.0, 400.0, 300.0]]

    def test_dict_shaped_boxes_are_understood_not_dropped(self, monkeypatch):
        got = self._scan(monkeypatch, {"text_boxes": [
            {"ymin": 100, "xmin": 200, "ymax": 300, "xmax": 400}]})
        assert got == [[200.0, 100.0, 400.0, 300.0]], \
            "a dict-shaped box must be read, not skipped — otherwise the "
        "baked text stays in the artwork"

    def test_underscored_keys_also_work(self, monkeypatch):
        got = self._scan(monkeypatch, {"text_boxes": [
            {"y_min": 100, "x_min": 200, "y_max": 300, "x_max": 400}]})
        assert got == [[200.0, 100.0, 400.0, 300.0]]

    def test_junk_boxes_are_skipped_without_raising(self, monkeypatch):
        got = self._scan(monkeypatch, {"text_boxes": [
            {"nope": 1}, "banana", [1, 2], None, [10, 20, 30, 40]]})
        assert got == [[20.0, 10.0, 40.0, 30.0]]


class TestSketchNeverCoversTheCardsOwnWriting:
    """At 1:14 of a rendered lesson a potted plant was drawn straight over the
    bullets of the "Specialised Cells" card. An auto-sketch is sized by WIDTH
    alone, so its height follows the asset's aspect ratio: sk_plant binds to
    434x648 on a 720-tall canvas. The old code nudged the centre down 70px,
    which cannot clear text starting at y=240."""

    def test_a_card_that_wrote_text_is_never_sketched_over(self):
        from spike.scene_engine.whiteboard import build_whiteboard_scene
        card = build_whiteboard_scene({
            "segment_id": "s003", "type": "explore",
            "slide_heading": "Specialised Cells",
            "slide_points": ["Cells have specific roles",
                             "Analogy: team roles"],
            "text": "Many cells in your body are like a plant in a pot."})
        assert not any(str(e.get("id", "")).startswith("sk_")
                       for e in card["elements"])
        assert not (card.get("scene_assets") or {})

    def test_an_empty_board_still_gets_its_sketches(self):
        from spike.scene_engine.whiteboard import build_whiteboard_scene
        card = build_whiteboard_scene({
            "segment_id": "s001", "type": "hook",
            "text": "A towering tree and a tiny ant are both alive."})
        assert any(str(e.get("id", "")).startswith("sk_")
                   for e in card["elements"]),             "the feature must still work where there is room for it"


class TestP1LayoutConstraints:
    """P1. Two constraints the layout engine could not previously express."""

    def test_the_caption_panel_is_a_keep_out_region(self):
        """It is on screen for essentially every segment and no placement
        code could see it, so ~a third of every diagram was laid out into
        space that was already occupied."""
        from spike.scene_engine.render import (SceneRenderer, _CAPTION_HALF_H,
                                               _CAPTION_HALF_W)
        from spike.scene_engine.schema import Scene
        scene = Scene.model_validate({
            "id": "cap", "narration": "The nucleus controls the cell.",
            "elements": [
                {"id": "__nb_0", "type": "text", "text": "The nucleus",
                 "at": [970, 360], "role": "caption"},
                {"id": "lbl", "type": "text", "text": "Nucleus",
                 "at": [95, 140], "role": "label"}],
            "actions": [{"verb": "write", "target": "lbl"}]})
        r = SceneRenderer(scene)
        zones = r._avatar_zones
        assert any(abs(z[0] - (970 - _CAPTION_HALF_W)) < 1
                   and abs(z[2] - (970 + _CAPTION_HALF_W)) < 1
                   for z in zones), \
            f"the caption panel is not a keep-out region: {zones}"

    def test_label_relayout_no_longer_requires_arrows(self):
        """The prompt tells the director to prefer pointing over arrows; the
        de-collision layout only ran WHEN arrows existed. We asked for no
        arrows and then skipped the fix for the problem that causes."""
        import inspect
        from spike.scene_engine.render import SceneRenderer
        src = inspect.getsource(SceneRenderer._relayout_part_labels)
        head, _, tail = src.partition("if len(entries) < 2")
        assert 'role", "") or "") != "label"' in head, \
            "arrowless labels must be entered into the layout before the gate"


class TestP2RasterCropIsEquivalent:
    """P2. The raster transform rendered into the FULL 2560x1440 canvas for
    every raster on every frame, including two static avatar sprites that
    occupy a corner — the single largest item in the render phase. It now
    transforms only the destination rectangle."""

    def test_cropped_transform_matches_the_full_frame_one(self):
        from PIL import Image
        from spike.scene_engine.render import SS
        from spike.scene_engine.schema import WORLD_H, WORLD_W

        fw, fh = int(WORLD_W * SS), int(WORLD_H * SS)
        ink = Image.new("RGBA", (120, 180), (0, 0, 0, 0))
        for x in range(20, 100):
            for y in range(30, 150):
                ink.putpixel((x, y), (0, 0, 0, 255))
        k_ws, offx, offy = 1.7, 900.0, 400.0

        full = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
        inv = (1 / k_ws, 0.0, -offx / k_ws, 0.0, 1 / k_ws, -offy / k_ws)
        o = ink.transform((fw, fh), Image.AFFINE, inv, resample=Image.BILINEAR)
        full.paste(o, (0, 0), o)

        crop = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
        bx0, by0 = max(0, int(offx)), max(0, int(offy))
        bx1 = min(fw, int(offx + ink.width * k_ws) + 1)
        by1 = min(fh, int(offy + ink.height * k_ws) + 1)
        inv2 = (1 / k_ws, 0.0, (bx0 - offx) / k_ws,
                0.0, 1 / k_ws, (by0 - offy) / k_ws)
        o2 = ink.transform((bx1 - bx0, by1 - by0), Image.AFFINE, inv2,
                           resample=Image.BILINEAR)
        crop.paste(o2, (bx0, by0), o2)

        import numpy as np
        d = np.abs(np.asarray(full).astype(int) - np.asarray(crop).astype(int))
        # bilinear rounding at the crop boundary only: a handful of pixels at
        # +-2/255. A SHIFT or a dropped region would blow both of these.
        assert d.max() <= 3, f"max channel difference {d.max()} — not rounding"
        differing = int((d.sum(axis=2) > 0).sum())
        assert differing < 0.001 * fw * fh, f"{differing} pixels differ"

    def test_an_offscreen_raster_is_skipped(self):
        import inspect
        from spike.scene_engine.render import SceneRenderer
        src = inspect.getsource(SceneRenderer._draw_raster)
        assert "entirely off-camera" in src
        assert "if bx1 <= bx0 or by1 <= by0" in src


class TestP6AssetsFitTheBoard:
    """P6. world_scale was NOMINAL_WORLD_W / ink.width — width alone — so a
    portrait asset's height was whatever its aspect ratio made it. Measured
    on the real cache: human_outline (267x813) rendered 700x2131 world px on
    a 720-tall canvas, so most of the drawing was off-screen."""

    def test_a_portrait_asset_fits_the_canvas(self):
        from spike.scene_engine.raster_assets import fit_scale
        from spike.scene_engine.schema import WORLD_H
        w, h = 267, 813                       # the real human_outline
        assert h * fit_scale(w, h) <= WORLD_H, "still taller than the canvas"

    def test_landscape_art_is_unchanged(self):
        """It was already width-bound and composed correctly; this fix must
        not quietly restyle every existing lesson."""
        from spike.scene_engine.raster_assets import (NOMINAL_WORLD_W,
                                                      fit_scale)
        for w, h in ((1024, 318), (957, 205), (868, 140), (460, 300)):
            assert abs(fit_scale(w, h) - NOMINAL_WORLD_W / w) < 1e-9

    def test_the_aspect_ratio_is_preserved(self):
        from spike.scene_engine.raster_assets import fit_scale
        for w, h in ((285, 746), (488, 729), (1024, 318)):
            s = fit_scale(w, h)
            assert abs((w * s) / (h * s) - w / h) < 1e-9

    def test_degenerate_sizes_do_not_explode(self):
        from spike.scene_engine.raster_assets import fit_scale
        assert fit_scale(0, 10) == 1.0 and fit_scale(10, 0) == 1.0


class TestP8ImageSpendGuard:
    """P8. TTS has had a spend cap since it became metered; image generation
    — the expensive call — had none. allow_generate defaults True at every
    call site and no production caller passes False, so a plan naming 40
    assets made 40 image calls plus 40+ vision calls, unbounded. One chapter
    produced 71 paid images in a night."""

    def test_the_budget_refuses_past_the_cap(self):
        import spike.scene_engine.raster_assets as ra
        ra.reset_image_budget()
        allowed = sum(1 for _ in range(ra._IMAGE_BUDGET + 5)
                      if ra._image_budget_ok())
        assert allowed == ra._IMAGE_BUDGET
        assert ra.image_budget_state()["blocked"] == 5

    def test_it_resets_per_lesson(self):
        """A global counter would refuse the hundredth honest generation."""
        import spike.scene_engine.raster_assets as ra
        ra.reset_image_budget()
        for _ in range(ra._IMAGE_BUDGET):
            ra._image_budget_ok()
        assert ra._image_budget_ok() is False
        ra.reset_image_budget()
        assert ra._image_budget_ok() is True

    def test_the_worker_resets_it_for_each_generation(self):
        import inspect
        from worker.process import process_generation
        assert "reset_image_budget" in inspect.getsource(process_generation)

    def test_both_transports_are_gated(self):
        import inspect
        import spike.scene_engine.raster_assets as ra
        for fn in (ra._vertex_call, ra._aistudio_call):
            assert "_image_budget_ok" in inspect.getsource(fn), fn.__name__

    def test_exceeding_it_degrades_rather_than_raising(self):
        """A lesson never dies because an asset did — the caller falls back
        to the authored vector tier, as it does for any image failure."""
        import spike.scene_engine.raster_assets as ra
        ra.reset_image_budget()
        for _ in range(ra._IMAGE_BUDGET):
            ra._image_budget_ok()
        assert ra._vertex_call("anything") is None
        assert ra._aistudio_call("anything") is None
        ra.reset_image_budget()
