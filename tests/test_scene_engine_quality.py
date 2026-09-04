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

import pytest
from PIL import Image

from spike.scene_engine.continuity import (compile_plan, parse_visual_plan,
                                           seed_moment)
from spike.scene_engine.raster_assets import RasterAsset, part_names_from_prompt
from spike.scene_engine.render import SceneRenderer, _region_ordered_trace
from spike.scene_engine.schema import Scene, WORLD_H, WORLD_W
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

    def test_unresolved_layer_gets_an_edge_leader(self):
        """The part cannot be located in the art. Suppressing the arrow
        outright (the old behaviour) left the label floating in a margin
        column beside a picture, saying nothing about WHICH picture it names
        — the founder saw exactly that. A leader keeps the honest half: it
        stops OUTSIDE the picture on the ray toward its own label, and it
        carries no arrowhead, so it asserts association without asserting a
        structure that may not be there."""
        r = SceneRenderer(_anchor_scene("ribosome"),
                          asset_resolver=_resolver(_cell_asset()))
        warns = r.audit()["warnings"]
        assert any(w.startswith("ANCHOR_EDGE_FALLBACK") for w in warns)
        assert any(w.startswith("UNRESOLVED_ANCHOR") for w in warns)
        head = r.audit()["arrow_heads"]["ar"]
        art = r.bound["cell"].box
        # outside the art, not stabbing its middle
        assert not (art[0] <= head[0] <= art[2] and art[1] <= head[1] <= art[3]),             (head, art)
        cx, cy = (art[0] + art[2]) / 2, (art[1] + art[3]) / 2
        assert ((head[0] - cx) ** 2 + (head[1] - cy) ** 2) ** 0.5 > 40.0
        # a leader tick: one polyline, no barbs
        assert len(r.bound["ar"].layers[0].strokes) == 1

    def test_two_unresolved_labels_get_distinct_edge_points(self):
        s = Scene.model_validate({
            "id": "two_missing", "narration": "ribosomes and golgi",
            "compiled": True,
            "elements": [
                {"id": "cell", "type": "illustration", "asset": "plant_cell",
                 "at": [640, 360], "scale": 1.0},
                {"id": "l1", "type": "text", "text": "Ribosome",
                 "at": [80, 80], "role": "label"},
                {"id": "l2", "type": "text", "text": "Golgi",
                 "at": [80, 600], "role": "label"},
                {"id": "a1", "type": "arrow",
                 "tail": {"el": "l1", "edge": "right"},
                 "head": {"el": "cell", "layer": "ribosome", "edge": "center"}},
                {"id": "a2", "type": "arrow",
                 "tail": {"el": "l2", "edge": "right"},
                 "head": {"el": "cell", "layer": "golgi", "edge": "center"}},
            ],
            "actions": [{"verb": "draw", "target": "cell"},
                        {"verb": "write", "target": "l1"},
                        {"verb": "write", "target": "l2"},
                        {"verb": "draw", "target": "a1"},
                        {"verb": "draw", "target": "a2"}]})
        r = SceneRenderer(s, asset_resolver=_resolver(_cell_asset()))
        audit = r.audit()
        h1, h2 = audit["arrow_heads"]["a1"], audit["arrow_heads"]["a2"]
        # each leader leaves the picture on the ray toward ITS OWN label, so
        # the two ends are separated by the labels' own column pitch — they
        # used to be the identical element-box centre. 30px is the bar the
        # ARROWS_CONVERGE audit itself uses.
        assert ((h1[0] - h2[0]) ** 2 + (h1[1] - h2[1]) ** 2) ** 0.5 > 30.0
        assert not any(w.startswith("ARROWS_CONVERGE")
                       for w in audit["warnings"])

    def test_relayout_orders_unresolved_labels_by_their_own_height(self):
        """The relayout pre-pass resolved the head with no `toward`, so every
        unresolved label got the element CENTRE as its target height and they
        all sorted equal — a pile in one row."""
        s = Scene.model_validate({
            "id": "order", "narration": "x", "compiled": True,
            "elements": [
                {"id": "cell", "type": "illustration", "asset": "plant_cell",
                 "at": [640, 360], "scale": 1.0},
                {"id": "l1", "type": "text", "text": "Alpha", "at": [80, 60],
                 "role": "label"},
                {"id": "l2", "type": "text", "text": "Beta", "at": [80, 300],
                 "role": "label"},
                {"id": "l3", "type": "text", "text": "Gamma", "at": [80, 560],
                 "role": "label"},
            ] + [{"id": f"a{i}", "type": "arrow",
                  "tail": {"el": f"l{i}", "edge": "right"},
                  "head": {"el": "cell", "layer": "ribosome",
                           "edge": "center"}} for i in (1, 2, 3)],
            "actions": [{"verb": "draw", "target": "cell"}]
            + [{"verb": "write", "target": f"l{i}"} for i in (1, 2, 3)]
            + [{"verb": "draw", "target": f"a{i}"} for i in (1, 2, 3)]})
        r = SceneRenderer(s, asset_resolver=_resolver(_cell_asset()))
        ys = [r.bound[f"l{i}"].box[1] for i in (1, 2, 3)]
        assert ys[0] < ys[1] < ys[2], ys

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


class TestPartNamesFromDescription:
    """The founder's Cells Part 2: the root prompt NAMED every part in prose
    and carried no 'Name the layer groups exactly' tail, so part_names was
    empty and the entire labelling pass was skipped."""

    def test_the_prompt_enumeration_becomes_part_names(self):
        from spike.scene_engine.raster_assets import part_names_from_description
        got = part_names_from_description(
            "A plant cell in cross-section with a cell wall, cell membrane, "
            "nucleus, chloroplasts, and cytoplasm. Do not show a vacuole or "
            "label parts.")
        assert got == ["cell wall", "cell membrane", "nucleus",
                       "chloroplasts", "cytoplasm"]
        assert "vacuole" not in got          # a negated clause names an ABSENCE

    def test_a_short_enumeration_parses(self):
        from spike.scene_engine.raster_assets import part_names_from_description
        assert part_names_from_description(
            "A river meander showing an outer bank and an inner bank") == \
            ["outer bank", "inner bank"]

    def test_a_prompt_with_no_list_yields_nothing(self):
        from spike.scene_engine.raster_assets import part_names_from_description
        assert part_names_from_description("A rectangular plant cell.") == []
        assert part_names_from_description("") == []

    def test_an_explicit_tail_wins_outright(self):
        from spike.scene_engine.raster_assets import part_names_from_description
        assert part_names_from_description(
            "A cell with a wall and a nucleus. Name the layer groups "
            "exactly: wall, nucleus.") == []

    def test_the_enumeration_comes_from_the_last_trigger_not_the_first(self):
        """Taking the FIRST trigger made the opening chunk 'the human heart
        with the left atrium' — six words, discarded — so three chambers got
        labels and the fourth did not, which reads as a mistake on screen."""
        from spike.scene_engine.raster_assets import part_names_from_description
        assert part_names_from_description(
            "A diagram showing the human heart with the left atrium, right "
            "atrium, left ventricle and right ventricle.") == \
            ["left atrium", "right atrium", "left ventricle",
             "right ventricle"]

    def test_a_trailing_clause_does_not_swallow_the_last_part(self):
        """The last item wears the sentence's tail: 'carpel on a white
        background' is four words, so the carpel was thrown away whole."""
        from spike.scene_engine.raster_assets import part_names_from_description
        assert part_names_from_description(
            "A simple line drawing of a flower with petals, sepals, stamen "
            "and carpel on a white background.") == \
            ["petals", "sepals", "stamen", "carpel"]

    @pytest.mark.parametrize("prompt,expect", [
        ("A plant cell with a nucleus, cytoplasm on white.",
         ["nucleus", "cytoplasm"]),
        ("A flower with petals, sepals and a stamen in profile.",
         ["petals", "sepals", "stamen"]),
        ("A leaf with a midrib, veins and a blade in section.",
         ["midrib", "veins", "blade"]),
    ])
    def test_a_short_name_plus_clause_is_trimmed_too(self, prompt, expect):
        # each of these chunks is THREE words or fewer, so the old length
        # gate never let the trim look at it
        """The trim ran only ABOVE three words — the length at which a clause
        makes a chunk fail the 1..3 length check anyway. So a chunk that was
        name-plus-clause and still short passed straight through: 'cytoplasm
        on white' shipped as a part name, went into the image prompt's layer
        tail, and the vision annotator was paid to find a region by that
        name."""
        from spike.scene_engine.raster_assets import part_names_from_description
        assert part_names_from_description(prompt) == expect

    def test_a_chunk_that_is_only_a_clause_names_nothing_and_is_not_reported(self):
        from spike.scene_engine.raster_assets import part_names_from_description
        skipped = []
        assert part_names_from_description(
            "A diagram with a nucleus, cytoplasm and cell wall, drawn in "
            "black ink on white paper.", skipped) == \
            ["nucleus", "cytoplasm", "cell wall"]
        assert skipped == []

    def test_a_discarded_chunk_is_handed_back_to_the_caller(self):
        from spike.scene_engine.raster_assets import part_names_from_description
        skipped = []
        got = part_names_from_description(
            "A cell with a nucleus, cytoplasm and the region where the "
            "spindle fibres attach.", skipped)
        assert got == ["nucleus", "cytoplasm"]
        assert skipped == ["region where the spindle fibres attach"]

    def _chapter(self, narrations, prompt=None):
        raw = _plan_raw()
        ch = raw["chapters"][0]
        ch["assets"]["plant_cell"] = prompt or (
            "A plant cell in cross-section with a cell wall, cell membrane, "
            "nucleus, chloroplasts, and cytoplasm. Do not show a vacuole or "
            "label parts.")
        # no label/arrow ids that strip to a part name, so only the
        # description tier can supply names
        ch["elements"] = [e for e in ch["elements"]
                          if e["id"] not in ("lbl_nucleus", "arr_nucleus")]
        for st in ch["steps"]:
            st["actions"] = [a for a in st["actions"]
                             if a.get("target") not in ("lbl_nucleus",
                                                        "arr_nucleus")]
        plan = parse_visual_plan(raw)
        return compile_plan(plan, narrations,
                            all_segments=sorted(narrations), skip_hold=set())

    def test_the_description_tier_names_the_diagram(self):
        narr = {"s001": "The cell wall is rigid.",
                "s002": "Inside, the nucleus directs everything.",
                "s003": "The chloroplasts catch the light."}
        _, assets, report = self._chapter(narr)
        line = next(ln for ln in report if "PART NAMES from description" in ln)
        assert "cell wall" in line and "nucleus" in line \
            and "chloroplasts" in line
        assert "vacuole" not in line
        # 'cell membrane' and 'cytoplasm' are never spoken in this chapter —
        # asking vision for them buys a wrong box and a paid call
        assert "membrane" not in line and "cytoplasm" not in line
        assert "name the layer groups exactly:" in \
            assets["s002"]["plant_cell"].lower()

    def test_names_the_narration_never_says_are_not_requested(self):
        _, assets, report = self._chapter(
            {"s001": "a", "s002": "b", "s003": "c"})
        assert not any("PART NAMES" in ln for ln in report)
        assert "name the layer groups exactly:" not in \
            assets["s002"]["plant_cell"].lower()

    def test_an_inflected_narration_still_names_the_part(self):
        """The narration filter was a raw substring test, so 'Each
        chloroplast traps light.' did not count as saying 'chloroplasts' and
        'Inside the nuclei...' did not count as saying 'nucleus' — the two
        organelles the chapter is about were the two it refused to name."""
        narr = {"s001": "The cell wall is rigid.",
                "s002": "Inside the nuclei sit the chromosomes.",
                "s003": "Each chloroplast traps light."}
        _, _, report = self._chapter(narr)
        line = next(ln for ln in report if "PART NAMES from description" in ln)
        assert "nucleus" in line and "chloroplasts" in line, line

    def test_the_report_says_which_chunk_it_could_not_name(self):
        narr = {"s001": "The cell wall is rigid.",
                "s002": "Inside, the nucleus directs everything.",
                "s003": "The spindle fibres attach here."}
        _, _, report = self._chapter(
            narr, prompt="A cell with a cell wall, nucleus and the region "
                         "where the spindle fibres attach.")
        assert any("PART NAME SKIPPED" in ln and "spindle fibres" in ln
                   for ln in report), report


class TestPartNamesFromLabelText:
    """An id says what a programmer called the element; the TEXT says what the
    picture must contain. lbl1/'Nucleus' used to yield nothing at all."""

    def test_label_text_supplies_the_tail(self):
        raw = _plan_raw()
        ch = raw["chapters"][0]
        ch["assets"]["plant_cell"] = "A rectangular plant cell."   # no tail
        ch["elements"] = [e for e in ch["elements"] if e["id"] == "cell"]
        ch["elements"] += [
            {"id": "lbl1", "type": "text", "text": "Nucleus",
             "at": [90, 120], "role": "label"},
            {"id": "lbl2", "type": "text", "text": "Cell wall",
             "at": [90, 200], "role": "label"}]
        for st in ch["steps"]:
            st["actions"] = [a for a in st["actions"]
                             if a.get("target") == "cell"]
        plan = parse_visual_plan(raw)
        narr = {"s001": "The cell wall is rigid.",
                "s002": "The nucleus is the control centre.",
                "s003": "look closer"}
        _, assets, report = compile_plan(plan, narr,
                                         all_segments=["s001", "s002", "s003"],
                                         skip_hold=set())
        line = next(ln for ln in report if "PART NAMES from label text" in ln)
        assert "nucleus" in line and "cell wall" in line
        assert "name the layer groups exactly:" in \
            assets["s002"]["plant_cell"].lower()


class TestAQuestionIsNotAPartName:
    """`_classify_text` keeps a short question as a LABEL on purpose — 'Why?'
    and 'What is a cell?' are exactly the board text a socratic lesson wants —
    but a label is not therefore a NAME for a part of the picture, and nothing
    stopped one entering the label-text candidate pool.

    That pool is written verbatim into the asset prompt as 'Name the layer
    groups exactly: ...', so the image model was asked for a layer group
    called 'why?' and the vision annotator was then paid to go looking for it.
    """

    @staticmethod
    def _report(labels, narr):
        raw = _plan_raw()
        ch = raw["chapters"][0]
        ch["assets"]["plant_cell"] = "A rectangular plant cell."   # no tail
        ch["elements"] = [e for e in ch["elements"] if e["id"] == "cell"]
        ch["elements"] += [
            {"id": f"lbl{i}", "type": "text", "text": t, "at": [90, 120 + 60 * i],
             "role": "label"} for i, t in enumerate(labels)]
        for st in ch["steps"]:
            st["actions"] = [a for a in st["actions"]
                             if a.get("target") == "cell"]
        plan = parse_visual_plan(raw)
        _, assets, report = compile_plan(plan, narr,
                                         all_segments=["s001", "s002", "s003"],
                                         skip_hold=set())
        line = next(ln for ln in report if "PART NAMES from label text" in ln)
        return line, assets

    @pytest.mark.parametrize("question", ["Why?", "What is a cell?",
                                          "How does it work?"])
    def test_a_question_never_becomes_a_layer_group(self, question):
        narr = {"s001": "The cell wall is rigid.",
                "s002": f"{question} The nucleus is the control centre.",
                "s003": "look closer"}
        line, assets = self._report([question, "Nucleus"], narr)
        assert "nucleus" in line, line
        first = question.split()[0].lower()
        assert first not in line.lower(), line
        tail = assets["s002"]["plant_cell"].lower()
        assert "name the layer groups exactly: nucleus." in tail, tail

    def test_a_real_name_still_reaches_the_pool(self):
        """The exclusion must not cost the tier the names it exists for."""
        narr = {"s001": "The cell wall is rigid.",
                "s002": "The nucleus is the control centre.",
                "s003": "look closer"}
        line, _ = self._report(["Nucleus", "Cell wall"], narr)
        assert "nucleus" in line and "cell wall" in line, line


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
        """A board carrying a planned diagram MAY still sketch what the
        narration names — the founder asked for exactly that — but the sketch
        must not land on the diagram.

        This test used to assert no sketch appeared at all, which enforced the
        letter of its name and not the meaning: it passed trivially because
        auto-sketch was skipped on every board with a picture, which on the
        semantic path is every board. The invariant is NON-OVERLAP.
        """
        plan = parse_visual_plan(_plan_raw())
        narr = {"s001": "A tree and an ant are both alive.",
                "s002": "The nucleus controls the cell.", "s003": "Look."}
        scenes, assets, report = compile_plan(
            plan, narr, all_segments=["s001", "s002", "s003"],
            skip_hold=set())

        def box(at, scale):
            # raster_assets.fit_scale bounds an illustration to 700x520 world
            # px, so this is the worst case regardless of the actual art
            return (at[0] - 350 * scale, at[1] - 260 * scale,
                    at[0] + 350 * scale, at[1] + 260 * scale)

        found = 0
        for sc in scenes.values():
            planned = [box(e["at"], e.get("scale", 1.0))
                       for e in sc["elements"]
                       if e.get("type") == "illustration"
                       and not str(e.get("id", "")).startswith("sk_")]
            for e in sc["elements"]:
                if not str(e.get("id", "")).startswith("sk_"):
                    continue
                found += 1
                s = box(e["at"], e.get("scale", 1.0))
                for p in planned:
                    assert (s[2] <= p[0] or s[0] >= p[2]
                            or s[3] <= p[1] or s[1] >= p[3]), (
                        f"sketch {e['id']} at {s} overlaps planned {p}")
        assert found, ("the narration names a tree and an ant and the board "
                       "drew neither — the sketch pass is dead again")

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


class TestP17ModelCallBurst:
    """RENDER_WORKERS sized the frame renderer AND, by accident, how many paid
    model calls went out at once. Four measured renders: two clean, one with
    8 x 429 across BOTH transports — bursts, not a ceiling (0 project quotas
    above 90% usage). The two knobs are now separate."""

    def test_concurrency_is_its_own_knob(self, monkeypatch):
        import spike.scene_engine.raster_assets as ra
        monkeypatch.setenv("MODEL_CALL_CONCURRENCY", "2")
        monkeypatch.setenv("RENDER_WORKERS", "9")
        assert ra.model_call_concurrency() == 2, "render width != call width"

    def test_never_more_than_n_calls_in_flight(self, monkeypatch):
        """The property that matters: whatever the caller's thread count."""
        import threading
        import spike.scene_engine.raster_assets as ra
        monkeypatch.setenv("MODEL_CALL_CONCURRENCY", "3")
        monkeypatch.setattr(ra, "_MODEL_GATE", None)   # rebuild at this bound

        live, peak, lock = 0, 0, threading.Lock()
        start = threading.Barrier(12)

        def call():
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            threading.Event().wait(0.02)   # hold the slot long enough to overlap
            with lock:
                live -= 1
            return "ok"

        def worker():
            start.wait()
            ra._with_backoff(call, "test")

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert peak <= 3, f"{peak} concurrent model calls, cap is 3"
        assert peak > 1, "a cap of 3 that never reaches 2 is not being exercised"

    def test_backoff_sleep_does_not_hold_a_slot(self):
        """A rate-limited caller waiting 6-35s must not block the callers that
        would have succeeded — that turns one 429 into a stalled render.
        The per-minute pacing window (`_limiter(kind).acquire()`) follows the
        same rule: it is taken BEFORE `with gate:`, so a paced caller sleeps
        holding no slot."""
        import inspect
        import spike.scene_engine.raster_assets as ra
        src = inspect.getsource(ra._with_backoff)
        gate_line = next(i for i, l in enumerate(src.splitlines())
                         if "with gate:" in l)
        sleep_line = next(i for i, l in enumerate(src.splitlines())
                          if "_t.sleep(" in l)
        pace_line = next(i for i, l in enumerate(src.splitlines())
                         if ".acquire()" in l)
        assert pace_line < gate_line, "the pacing wait must happen before the gate"
        # the sleep lives in the except handler, outside the with-block
        assert sleep_line > gate_line
        assert "with gate" not in src.splitlines()[sleep_line]
        indent = lambda i: len(src.splitlines()[i]) - len(src.splitlines()[i].lstrip())
        assert indent(sleep_line) <= indent(gate_line), \
            "sleeping inside the gate would serialise every waiter"

    def test_every_paid_transport_goes_through_the_gate(self):
        """Image (both transports) and vision (both transports) all funnel
        through _with_backoff — so gating it there covers all four."""
        import inspect
        import spike.scene_engine.raster_assets as ra
        for fn in (ra._vertex_call, ra._aistudio_call, ra._vision_json):
            assert "_with_backoff(" in inspect.getsource(fn), fn.__name__


class TestP17RescanConverges:
    """Vision dominated the traffic: up to 45 vision calls against 9 images in
    one lesson, because every scrub round paid for a fresh full-image scan."""

    def test_a_round_that_finds_no_fewer_boxes_stops(self, monkeypatch):
        from PIL import Image
        import spike.scene_engine.raster_assets as ra
        calls = []

        def stuck_scan(ink):
            calls.append(1)
            return [[0.0, 0.0, 10.0, 10.0]]      # never shrinks

        monkeypatch.setattr(ra, "scan_text", stuck_scan)
        monkeypatch.setattr(ra, "scrub_text", lambda ink, boxes: ink)
        ink = Image.new("RGBA", (40, 40))
        _, boxes = ra.scrub_all_text(ink, [[0.0, 0.0, 10.0, 10.0]])
        assert len(calls) == 1, (
            f"{len(calls)} paid scans chasing text that is not going away")
        assert boxes, "unscrubbed text is still reported to the caller"

    def test_it_keeps_going_while_it_is_working(self, monkeypatch):
        """Converging is the point — stop early ONLY when progress stops."""
        from PIL import Image
        import spike.scene_engine.raster_assets as ra
        shrinking = [[[0.0, 0.0, 1.0, 1.0]] * 2, [[0.0, 0.0, 1.0, 1.0]], []]

        monkeypatch.setattr(ra, "scan_text", lambda ink: shrinking.pop(0))
        monkeypatch.setattr(ra, "scrub_text", lambda ink, boxes: ink)
        ink = Image.new("RGBA", (40, 40))
        _, boxes = ra.scrub_all_text(ink, [[0.0, 0.0, 1.0, 1.0]] * 3)
        assert boxes == [], "a converging scrub must be allowed to finish"

    def test_the_cap_did_not_move(self):
        """Lowering it would have cut the converging scrubs too — the ones
        worth paying for. Convergence, not the cap, is what saves the calls."""
        import spike.scene_engine.raster_assets as ra
        assert ra.scrub_all_text.__defaults__[0] == 3


class TestBoardSurvivesAChapterCarry:
    """Measured on a live lesson: the model emitted 3 CLEAR_AND_REDRAW across
    15 steps, and the board was wiped 14 times. The extra wipes were
    manufactured by the adapter, which stamped a constant clear_and_redraw on
    every part it split out of an overloaded chapter, discarding the
    director's own EXTEND."""

    def _split_raw(self, decision):
        """One chapter declaring two successive pictures — the shape that
        forces _split_on_new_root to act."""
        return {"chapters": [{
            "id": "c1", "concept": "levels", "transition": "clear_and_redraw",
            "assets": {"cells": "Cells side by side",
                       "tissue": "Cells forming a tissue"},
            "elements": [
                {"id": "cells_pic", "type": "illustration", "asset": "cells",
                 "role": "root_visual"},
                {"id": "tissue_pic", "type": "illustration", "asset": "tissue",
                 "role": "root_visual"},
            ],
            "steps": [
                {"segment": 1, "decision": "NEW_VISUAL", "reason": "start",
                 "actions": [{"verb": "DRAW",
                              "target": {"element": "cells_pic"},
                              "cue": "cells"}]},
                {"segment": 2, "decision": decision, "reason": "build on it",
                 "actions": [{"verb": "DRAW",
                              "target": {"element": "tissue_pic"},
                              "cue": "a tissue"}]},
            ],
        }]}

    def _adapt(self, decision):
        from spike.scene_engine.semantic import adapt_semantic_plan
        plan, _ = adapt_semantic_plan(self._split_raw(decision))
        return plan

    def test_extend_carries_instead_of_wiping(self):
        parts = self._adapt("EXTEND")["chapters"]
        assert len(parts) == 2, "the split itself must still happen"
        # the adapter normalises "continue" to the compiler's word, "carry"
        assert parts[1]["transition"] == "carry", (
            "the director said EXTEND; a wipe is not what it asked for")

    def test_an_explicit_redraw_still_wipes(self):
        """The escape hatch must survive — otherwise a lesson that genuinely
        changes subject piles unrelated pictures onto one board."""
        parts = self._adapt("CLEAR_AND_REDRAW")["chapters"]
        assert parts[1]["transition"] == "clear_and_redraw"

    def test_the_previous_picture_is_demoted_not_stacked(self):
        """Carrying without moving the old picture would put two 700x520
        rasters on one point — every illustration is placed at the same world
        position. That would be a worse regression than the churn."""
        from spike.scene_engine.continuity import (_RECAP_SCALE, compile_plan,
                                                   parse_visual_plan)
        from spike.scene_engine.whiteboard import illustration_box
        plan = parse_visual_plan(self._adapt("EXTEND"))
        scenes, _, report = compile_plan(
            plan, {"s001": "cells", "s002": "a tissue"},
            all_segments=["s001", "s002"], skip_hold=set())
        sc = scenes["s002"]
        ills = [e for e in sc["elements"] if e.get("type") == "illustration"
                and not str(e.get("id", "")).startswith("__")]
        assert len(ills) == 2, "the earlier picture should still be on screen"
        boxes = [illustration_box(e["at"], float(e.get("scale", 1.0)))
                 for e in ills]
        a, b = boxes
        assert (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3]), \
            f"the two pictures overlap: {a} vs {b}"
        assert any(abs(float(e.get("scale", 1.0)) - _RECAP_SCALE) < 1e-6
                   for e in ills), "one of them must be the small recap"
        assert any("RECAP" in ln for ln in report)

    def test_old_labels_do_not_ride_along_onto_the_new_picture(self):
        """A label is positioned FOR the picture it annotates. Carried at full
        size onto a different diagram it names parts no longer on screen."""
        from spike.scene_engine.continuity import compile_plan, parse_visual_plan
        from spike.scene_engine.semantic import adapt_semantic_plan
        raw = self._split_raw("EXTEND")
        raw["chapters"][0]["elements"].append(
            {"id": "lbl_old", "type": "text", "text": "Cell wall",
             "role": "label"})
        raw["chapters"][0]["steps"][0]["actions"].append(
            {"verb": "WRITE", "target": {"element": "lbl_old"},
             "cue": "cells"})
        plan, _ = adapt_semantic_plan(raw)
        scenes, _, _ = compile_plan(
            parse_visual_plan(plan), {"s001": "cells", "s002": "a tissue"},
            all_segments=["s001", "s002"], skip_hold=set())
        assert not any(e.get("id") == "lbl_old"
                       for e in scenes["s002"]["elements"]), \
            "the old picture's label followed it onto the new board"


class TestLabelsGetLeaderLines:
    """arrow_count was 0 on a live lesson. Two independent causes."""

    def test_a_suffix_named_label_resolves_to_its_part(self):
        """The prompt's example names labels by PREFIX (lbl_brain), so only
        prefixes were stripped. A director that used a SUFFIX instead —
        measured: brain_label, heart_label, lungs_label — produced
        'brain label', which matches no part, and the leader line was dropped
        silently."""
        from spike.scene_engine.continuity import _guess_part_name
        assert _guess_part_name("brain_label") == "brain"
        assert _guess_part_name("lungs_label") == "lungs"
        assert _guess_part_name("lbl_nucleus") == "nucleus"      # unchanged
        assert _guess_part_name("cell_membrane") == "cell membrane"

    def test_the_guesser_never_strips_a_name_to_nothing(self):
        from spike.scene_engine.continuity import _guess_part_name
        for eid in ("label", "text", "part", "obj"):
            assert _guess_part_name(eid), f"{eid} collapsed to empty"

    def test_the_prompt_no_longer_argues_against_arrows(self):
        """The model emitted highlight 8 times and arrow 0 times. It was
        obeying: the prose told it to prefer highlighting OVER arrows, and
        listed arrows among the things not to maximise."""
        from agent3_scripts.semantic_prompt import _CAPS, _LABELS_CAMERA
        assert ("Prefer pointing, circling, highlighting or zooming over "
                "arrows") not in _LABELS_CAMERA
        assert "maximum animation, assets, arrows" not in _CAPS
        # and the replacement actually asks for the leader line
        assert "ARROW" in _LABELS_CAMERA

    def test_the_example_pairs_a_label_with_an_arrow(self):
        """This repo has confirmed 6+ times that the model imitates the OUTPUT
        FORMAT EXAMPLE over the instructions. A rule about leader lines the
        example never demonstrates is not enforcement."""
        from agent3_scripts.semantic_prompt import _EXAMPLE
        assert '"verb": "ARROW"' in _EXAMPLE, "the example must SHOW an arrow"
        # and paired with its label in the SAME step, which is the actual rule
        step = _EXAMPLE[_EXAMPLE.index('"lbl_cutoff"'):]
        step = step[:step.index("]", step.index('"actions"'))]
        assert '"verb": "WRITE"' in step and '"verb": "ARROW"' in step, \
            "no step demonstrates a label and its leader line together"


class TestTextClassification:
    """The founder's Cells Part 2 wrote 'Compare your model cell with the
    models made by other groups.' — a verbatim textbook activity line — across
    the plant cell for nine segments. Nothing between the model and the pixels
    refused a sentence as a label."""

    @staticmethod
    def _chapter(text, narr, role=None, eid="compare_table"):
        el = {"id": eid, "type": "text", "text": text, "at": [700, 230]}
        if role:
            el["role"] = role
        raw = {"chapters": [{
            "concept": "cells", "transition": "clear_and_redraw",
            "assets": {"pc": "A plant cell."},
            "elements": [
                {"id": "cell", "type": "illustration", "asset": "pc",
                 "at": [600, 380], "scale": 1.0},
                el,
            ],
            "steps": [
                {"segment": 1, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "cell"}]},
                {"segment": 2, "decision": "EXTEND",
                 "actions": [{"verb": "write", "target": eid,
                              "at": {"phrase": text[:20]}}]},
            ],
        }]}
        plan = parse_visual_plan(raw)
        scenes, _, report = compile_plan(plan, narr,
                                         all_segments=["s001", "s002"],
                                         skip_hold=set())
        return plan, scenes, report

    def test_short_labels_are_untouched(self):
        from spike.scene_engine.continuity import _classify_text
        for t in ("Cell wall", "Outer bank", "Cut off loop", "Mitochondria",
                  "Nucleus"):
            assert _classify_text({"text": t}) == "label", t
        assert _classify_text({"text": "Photosynthesis",
                               "role": "title"}) == "title"

    def test_a_sentence_is_a_sentence(self):
        from spike.scene_engine.continuity import _classify_text
        assert _classify_text({
            "text": "Compare your model cell with the models made by other "
                    "groups."}) == "sentence"
        # an instruction verb gives it away even when it is short
        assert _classify_text({"text": "Compare the two"}) == "sentence"
        # ...and so does terminal punctuation
        assert _classify_text({"text": "It is small."}) == "sentence"

    @pytest.mark.parametrize("text", [
        "Nucleus.",                 # a one-word name that ends in a full stop
        "1.",                       # a numbered tag
        "Why?",                     # a socratic prompt
        "What is a cell?",
        "Concentration gradient!",
        "Record",                   # a verb homograph with no object
        "List of organs",           # ...and one followed by 'of'
    ])
    def test_short_board_text_is_a_label_not_a_deletion(self, text):
        """Each of these classified as 'sentence' and was DELETED from the
        roster — not demoted, deleted — so a one-word label the model happened
        to punctuate, or a three-word question, vanished from the lesson."""
        from spike.scene_engine.continuity import _classify_text
        assert _classify_text({"text": text}) == "label", text

    def test_a_short_statement_is_captioned_not_deleted(self):
        text = "Osmosis: water moves across a membrane"
        assert len(text) <= 40
        _, scenes, report = self._chapter(
            text, {"s001": "a plant cell", "s002": "water moves"},
            role="term")
        assert any("CAPTIONED text->compare_table" in ln for ln in report), \
            report
        el = next(e for e in scenes["s002"]["elements"]
                  if e["id"] == "compare_table")
        assert el["role"] == "caption"

    def test_an_instruction_is_dropped_with_a_report(self):
        text = ("Compare your model cell with the models made by other "
                "groups.")
        _, scenes, report = self._chapter(
            text, {"s001": "Here is a plant cell.",
                   "s002": "The nucleus is the control centre."})
        assert any("DROPPED text->compare_table" in ln and "sentence, not a "
                   "label" in ln for ln in report)
        for sc in scenes.values():
            assert not any(e["id"] == "compare_table"
                           for e in sc["elements"])
        # its write went with it, so there is no lost cue either
        for sc in scenes.values():
            assert not any(a.get("target") == "compare_table"
                           for a in sc["actions"])

    def test_a_short_instruction_is_dropped_not_captioned(self):
        """The demote-instead-of-delete branch tested LENGTH and nothing else.
        'Compare your models.' is 20 characters, so the branch lettered it
        under the picture as a caption — readmitting through the back door the
        exact class of text this pass exists to keep off the board."""
        text = "Compare your models."
        assert len(text) <= 40, "the length branch must be the one under test"
        _, scenes, report = self._chapter(
            text, {"s001": "Here is a plant cell.",
                   "s002": "The nucleus is the control centre."})
        assert any("DROPPED text->compare_table" in ln for ln in report),             report
        assert not any("CAPTIONED text->compare_table" in ln
                       for ln in report), report
        for sc in scenes.values():
            assert not any(e["id"] == "compare_table" for e in sc["elements"])

    def test_a_verb_with_no_object_is_still_captioned(self):
        """The carve-out has to reach this branch too: 'List of organs...'
        opens with a verb from the instruction list but is a NOUN phrase, and
        the classifier already refuses to call it an instruction. The demote
        branch must agree, or the fix above deletes real board text."""
        text = "List of organs in the human body"
        assert len(text) <= 40
        _, _, report = self._chapter(
            text, {"s001": "a plant cell", "s002": "water moves"},
            role="term")
        assert any("CAPTIONED text->compare_table" in ln for ln in report),             report

    def test_a_spoken_sentence_becomes_a_key_point(self):
        text = "The nucleus controls everything the cell does."
        plan, scenes, report = self._chapter(
            text, {"s001": "Here is a plant cell.",
                   "s002": "The nucleus controls everything the cell does. "
                           "It is the control centre."})
        step = plan.chapters[0].steps[1]
        assert step.key_point == text
        assert any("KEY POINT from text->compare_table" in ln
                   for ln in report)
        assert not any(e["id"] == "compare_table"
                       for e in scenes["s002"]["elements"])
        # ...and the narration stream bolds it, which is where a statement
        # belongs: spoken by the teacher, not lettered onto the diagram
        assert any("STREAM" in ln and text in ln for ln in report)

    def test_an_arrow_anchored_to_a_dropped_sentence_goes_too(self):
        """An arrow whose tail names a missing element fails scene validation
        outright — the segment would lose its picture, not just its label."""
        text = ("Compare your model cell with the models made by other "
                "groups.")
        raw = {"chapters": [{
            "concept": "cells", "transition": "clear_and_redraw",
            "assets": {"pc": "A plant cell."},
            "elements": [
                {"id": "cell", "type": "illustration", "asset": "pc",
                 "at": [600, 380], "scale": 1.0},
                {"id": "sent", "type": "text", "text": text, "at": [700, 230]},
                {"id": "arr", "type": "arrow",
                 "tail": {"el": "sent", "edge": "right"},
                 "head": [600, 380]},
            ],
            "steps": [{"segment": 1, "decision": "NEW_VISUAL",
                       "actions": [{"verb": "draw", "target": "cell"},
                                   {"verb": "write", "target": "sent"},
                                   {"verb": "draw", "target": "arr"}]}],
        }]}
        plan = parse_visual_plan(raw)
        scenes, _, report = compile_plan(plan, {"s001": "a plant cell"},
                                         all_segments=["s001"],
                                         skip_hold=set())
        assert any("DROPPED arrow 'arr'" in ln for ln in report)
        Scene.model_validate(scenes["s001"])       # would have raised before

    def test_the_prompts_say_a_long_text_is_discarded(self):
        from spike.scene_engine.director import SCENE_DIRECTION_SPEC
        from agent3_scripts.semantic_prompt import _LABELS_CAMERA
        for src in (SCENE_DIRECTION_SPEC, _LABELS_CAMERA):
            assert "longer than 5 words is DISCARDED" in src


_PART2_PROMPT = ("A plant cell in cross-section with a cell wall, cell "
                 "membrane, nucleus, chloroplasts, and cytoplasm. Do not show "
                 "a vacuole or label parts.")
_PART2_SENTENCE = ("Compare your model cell with the models made by other "
                   "groups.")


class TestCellsPart2Regression:
    """The founder's shipped Cells Part 2 (generation 18202ff7): a plant cell
    drawn for 6.5 minutes with NO part labels, and a 62-character textbook
    activity line lettered across it from s010 to s018."""

    _NARR = {
        "s004": "The cell wall is a rigid box around the whole cell.",
        "s005": "Just inside it lies the cell membrane, which controls what "
                "gets in.",
        "s006": "The green chloroplasts are where photosynthesis happens.",
        "s007": "The nucleus is the control centre of the cell.",
        "s008": "Everything floats in a jelly called cytoplasm.",
        "s009": "So a plant cell is a busy place.",
    }

    def _plan(self):
        els = [{"id": "cell", "type": "illustration",
                "asset": "plant_cell_simplified", "at": [600, 380],
                "scale": 1.0},
               {"id": "compare_table", "type": "text",
                "text": _PART2_SENTENCE, "at": [700, 230], "role": "label"}]
        steps = [{"segment": int(sid[1:]), "decision": "EXTEND",
                  "actions": [{"verb": "draw", "target": "cell"}]}
                 for sid in ("s004", "s005", "s006", "s007", "s008")]
        steps[0]["decision"] = "NEW_VISUAL"
        steps.append({"segment": 9, "decision": "CONTINUE", "actions": [
            {"verb": "write", "target": "compare_table",
             "at": {"phrase": _PART2_SENTENCE}}]})
        return parse_visual_plan({"chapters": [{
            "concept": "plant_cell", "transition": "clear_and_redraw",
            "assets": {"plant_cell_simplified": _PART2_PROMPT},
            "elements": els, "steps": steps}]})

    _NARR_INFLECTED = {
        "s004": "A rigid cell wall boxes the whole cell in.",
        "s005": "Just inside it lies the cell membrane, which controls what "
                "gets in.",
        "s006": "Each chloroplast traps light for photosynthesis.",
        "s007": "Inside the nuclei sit the chromosomes.",
        "s008": "Everything floats in a jelly called cytoplasm.",
        "s009": "So a plant cell is a busy place.",
    }

    def _compiled(self, narr=None):
        narr = narr or self._NARR
        return compile_plan(self._plan(), narr,
                            all_segments=sorted(narr), skip_hold=set())

    def test_singular_narration_still_labels_every_part(self):
        """The narration filter and the synthesis gate were raw substring
        tests. With this narration the report read PART NAMES from
        description: ['cell wall','cell membrane','cytoplasm'] and only three
        labels were synthesized — the nucleus and the chloroplasts shipped
        bare and nothing said why."""
        scenes, _, report = self._compiled(self._NARR_INFLECTED)
        want = {"cell_wall": "s004", "cell_membrane": "s005",
                "chloroplasts": "s006", "nucleus": "s007",
                "cytoplasm": "s008"}
        for part, sid in want.items():
            lid = "lbl_auto_" + part
            assert any("SYNTHESIZED " + lid in ln for ln in report), \
                (lid, [ln for ln in report if "PART NAMES" in ln])
            assert any(a["verb"] == "write" and a["target"] == lid
                       for a in scenes[sid]["actions"]), lid

    def test_untailed_prompt_that_lists_its_parts_gets_labelled(self):
        scenes, assets, report = self._compiled()
        line = next(ln for ln in report if "PART NAMES from description" in ln)
        for p in ("cell wall", "cell membrane", "nucleus", "chloroplasts",
                  "cytoplasm"):
            assert p in line, p
        assert "vacuole" not in line
        prompt = assets["s004"]["plant_cell_simplified"]
        assert prompt.lower().rstrip().endswith(
            "name the layer groups exactly: cell wall, cell membrane, "
            "nucleus, chloroplasts, cytoplasm."), prompt

    def test_every_part_gets_a_label_in_its_own_step(self):
        scenes, _, report = self._compiled()
        want = {"cell_wall": "s004", "cell_membrane": "s005",
                "chloroplasts": "s006", "nucleus": "s007",
                "cytoplasm": "s008"}
        for part, sid in want.items():
            lid = "lbl_auto_" + part
            assert any("SYNTHESIZED " + lid in ln for ln in report), lid
            writes = [a for a in scenes[sid]["actions"]
                      if a["verb"] == "write" and a["target"] == lid]
            assert writes, lid + " is not written in " + sid
            arr = "arr_auto_" + lid
            el = next(e for e in scenes[sid]["elements"] if e["id"] == arr)
            assert el["head"]["el"] == "cell"
            assert el["head"]["layer"] == part.replace("_", " ")

    def test_the_root_is_drawn_region_by_region(self):
        _, _, report = self._compiled()
        assert any("REGION SCHEDULE cell:" in ln for ln in report)

    def test_the_activity_sentence_never_reaches_the_board(self):
        scenes, _, report = self._compiled()
        assert any("DROPPED text->compare_table" in ln for ln in report)
        for sc in scenes.values():
            assert not any(e["id"] == "compare_table" for e in sc["elements"])
            assert not any(a.get("target") == "compare_table"
                           for a in sc["actions"])

    # the renderer half: even if a sentence DID reach a scene
    def _rendered(self, at, extra_labels=0):
        els = [{"id": "cell", "type": "illustration", "asset": "plant_cell",
                "at": [600, 380], "scale": 2.0},
               {"id": "__teach_av", "type": "illustration",
                "asset": "avatar_teacher", "at": [1150, 556], "scale": 0.30},
               {"id": "__nb_s010_0", "type": "text", "role": "caption",
                "text": "What about energy production?", "at": [970, 360]},
               {"id": "compare_table", "type": "text", "text": _PART2_SENTENCE,
                "at": list(at), "size": 27, "anchor": "lt", "role": "label"}]
        acts = [{"verb": "draw", "target": "cell"},
                {"verb": "write", "target": "compare_table"}]
        for i in range(extra_labels):
            els.append({"id": "lbl%d" % i, "type": "text",
                        "text": "Part %d" % i,
                        "at": [95, 140 + 78 * i], "role": "label"})
            acts.append({"verb": "write", "target": "lbl%d" % i})
        scene = Scene.model_validate({"id": "p2", "compiled": True,
                                      "narration": "the plant cell",
                                      "elements": els, "actions": acts})
        return SceneRenderer(scene, asset_resolver=_resolver(_cell_asset()))

    @pytest.mark.parametrize("at,extra", [([700, 230], 0),   # legacy plan
                                          ([95, 296], 0),    # semantic column
                                          ([700, 230], 3)])  # with a column
    def test_the_stray_sentence_never_lands_on_the_diagram(self, at, extra):
        r = self._rendered(at, extra)
        box = r.bound["compare_table"].box
        art = r.bound["cell"].box
        assert not (box[0] < art[2] and box[2] > art[0]
                    and box[1] < art[3] and box[3] > art[1]), (box, art)
        assert 24.0 <= box[0] and box[2] <= WORLD_W - 24.0
        assert 22.0 <= box[1] and box[3] <= WORLD_H - 46.0
        warns = r.audit()["warnings"]
        if not extra:
            # with a full label column the relayout's new per-side width
            # budget already spills it to the top row, so the keep-off-art
            # pass finds nothing left to move
            assert "TEXT_MOVED_OFF_ART compare_table" in warns
        assert not any(w.startswith("TEXT_OVER_ART") for w in warns)

    def test_a_caption_sits_under_the_root(self):
        scene = Scene.model_validate({
            "id": "cap", "compiled": True, "narration": "the plant cell",
            "elements": [
                {"id": "cell", "type": "illustration", "asset": "plant_cell",
                 "at": [600, 300], "scale": 1.0},
                {"id": "cap1", "type": "text", "role": "caption",
                 "text": "A plant cell in cross-section",
                 "at": [600, 300], "anchor": "mt"}],
            "actions": [{"verb": "draw", "target": "cell"},
                        {"verb": "write", "target": "cap1"}]})
        r = SceneRenderer(scene, asset_resolver=_resolver(_cell_asset()))
        box, art = r.bound["cap1"].box, r.bound["cell"].box
        assert box[1] >= art[3], (box, art)      # under the picture
        assert not any(w.startswith("TEXT_OVER_ART")
                       for w in r.audit()["warnings"])

    def test_text_over_art_is_reported_when_nothing_fits(self):
        """A board with no free margin at all: the audit must SAY the text is
        on the picture rather than report a clean lesson."""
        scene = Scene.model_validate({
            "id": "full", "compiled": True, "narration": "x",
            "elements": [
                {"id": "cell", "type": "illustration", "asset": "plant_cell",
                 "at": [640, 360], "scale": 6.0},
                {"id": "t", "type": "text", "text": _PART2_SENTENCE,
                 "at": [400, 340], "size": 27, "anchor": "lt"}],
            "actions": [{"verb": "draw", "target": "cell"},
                        {"verb": "write", "target": "t"}]})
        r = SceneRenderer(scene, asset_resolver=_resolver(_cell_asset()))
        assert "TEXT_OVER_ART t" in r.audit()["warnings"]


class TestTextHeavyLintMeansSomething:
    def test_caption_bubbles_do_not_trip_the_lint(self):
        """The narration stream adds a __nb_ element per sentence, so this
        lint fired on 14 of 17 scenes in a lesson whose BOARD text was one
        label. A lint that always fires carries no signal."""
        from spike.scene_engine.schema import scene_warnings
        els = [{"id": "d", "type": "shape", "shape": "path",
                "points": [[10, 10], [200, 200]]}]
        for i in range(6):
            els.append({"id": "__nb_x_%d" % i, "type": "text",
                        "role": "caption", "text": "A" * 60,
                        "at": [970, 360]})
        scene = Scene.model_validate({"id": "s", "narration": "n",
                                      "elements": els,
                                      "actions": [{"verb": "draw",
                                                   "target": "d"}]})
        assert not any("text-heavy" in w for w in scene_warnings(scene))

    def test_real_board_text_still_trips_it(self):
        from spike.scene_engine.schema import scene_warnings
        els = [{"id": "d", "type": "shape", "shape": "path",
                "points": [[10, 10], [200, 200]]}]
        for i in range(6):
            els.append({"id": "t%d" % i, "type": "text", "text": "A" * 60,
                        "at": [100, 100 + i * 40]})
        scene = Scene.model_validate({"id": "s", "narration": "n",
                                      "elements": els,
                                      "actions": [{"verb": "draw",
                                                   "target": "d"}]})
        assert any("text-heavy" in w for w in scene_warnings(scene))


class TestSeededMomentIsNotSaidTwice:
    """Founder frame at 3:35 of Cells Part 2: the student bubble and the
    teacher's caption bubble both read 'What about energy production?'.
    seed_moment lifts the question VERBATIM from the narration, and in a
    single-narrator style the same narrator speaks it."""

    _NARR = {
        "s001": "Here is a plant cell.",
        "s002": "Animal cells are more flexible. What about energy "
                "production? They rely on mitochondria.",
    }

    def _plan(self):
        return parse_visual_plan({"chapters": [{
            "concept": "cells", "transition": "clear_and_redraw",
            "assets": {"pc": "A plant cell."},
            "elements": [{"id": "cell", "type": "illustration", "asset": "pc",
                          "at": [600, 380], "scale": 1.0}],
            "steps": [
                {"segment": 1, "decision": "NEW_VISUAL",
                 "actions": [{"verb": "draw", "target": "cell"}]},
                {"segment": 2, "decision": "CONTINUE", "actions": []},
            ]}]})

    def test_the_seeded_question_is_flagged_as_seeded(self):
        plan = self._plan()
        sid = seed_moment(plan, self._NARR)
        assert sid == "s002"
        moment = plan.chapters[0].steps[1].moment
        assert moment["text"] == "What about energy production?"
        assert moment["seeded"] is True

    def test_the_caption_stream_does_not_repeat_it(self):
        plan = self._plan()
        seed_moment(plan, self._NARR)
        scenes, _, _ = compile_plan(plan, self._NARR,
                                    all_segments=["s001", "s002"],
                                    skip_hold=set())
        captions = [e["text"] for e in scenes["s002"]["elements"]
                    if str(e["id"]).startswith("__nb_")
                    and isinstance(e.get("text"), str)]
        assert not any("energy production" in c for c in captions), captions
        # the rest of the narration is still captioned
        assert any("mitochondria" in c for c in captions), captions
        # ...and the student bubble does carry it
        bubbles = [e["text"] for e in scenes["s002"]["elements"]
                   if str(e["id"]).startswith("__hm_")
                   and isinstance(e.get("text"), str)]
        assert any("energy production" in b for b in bubbles), bubbles

    def test_a_planned_moment_is_left_alone(self):
        """Only a SEEDED moment quotes the narration verbatim; a moment the
        director wrote is its own line and the caption stream keeps every
        sentence."""
        plan = self._plan()
        plan.chapters[0].steps[1].moment = {
            "role": "student", "text": "What about energy production?"}
        scenes, _, _ = compile_plan(plan, self._NARR,
                                    all_segments=["s001", "s002"],
                                    skip_hold=set())
        captions = [e["text"] for e in scenes["s002"]["elements"]
                    if str(e["id"]).startswith("__nb_")
                    and isinstance(e.get("text"), str)]
        assert any("energy production" in c for c in captions)

    def test_a_single_sentence_segment_keeps_its_caption(self):
        """Skipping the only sentence would leave the segment with no caption
        track at all."""
        from spike.scene_engine.whiteboard import narration_stream
        els, acts = narration_stream("What about energy production?",
                                     uid="s", skip={"What about energy "
                                                    "production?"})
        assert els and acts

class TestOrphansOfAnUnresolvedAsset:
    """Cells Part 3 (generation fa8c0d7d, 2026-09-04): three illustrations
    were lost to image-model 429s on both providers. The engine dropped the
    elements — §20, a missing asset never fails the scene — but laid out their
    LABELS anyway: the two labels for the missing ciliated cell were placed
    over the red blood cell diagram, with their arrows pointing into empty
    space. Naming the wrong picture's parts is worse than saying nothing."""

    def _scene(self):
        return Scene.model_validate({
            "id": "p3", "compiled": True, "narration": "blood and cilia",
            "elements": [
                {"id": "rbc", "type": "illustration", "asset": "plant_cell",
                 "at": [640, 360], "scale": 1.0},
                {"id": "cilia_cell", "type": "illustration",
                 "asset": "ciliated_cell", "at": [640, 360], "scale": 1.0},
                {"id": "lbl_hair", "type": "text", "text": "Hair-like cilia",
                 "at": [95, 140], "role": "label"},
                {"id": "lbl_tail", "type": "text", "text": "Sweeping motion",
                 "at": [95, 220], "role": "label"},
                {"id": "lbl_rbc", "type": "text", "text": "Nucleus",
                 "at": [95, 300], "role": "label"},
                {"id": "arr_hair", "type": "arrow",
                 "tail": {"el": "lbl_hair", "edge": "right"},
                 "head": {"el": "cilia_cell", "layer": "cilia",
                          "edge": "center"}},
                {"id": "arr_tail", "type": "arrow",
                 "tail": {"el": "lbl_tail", "edge": "right"},
                 "head": {"el": "cilia_cell", "layer": "motion",
                          "edge": "center"}},
                {"id": "arr_rbc", "type": "arrow",
                 "tail": {"el": "lbl_rbc", "edge": "right"},
                 "head": {"el": "rbc", "layer": "nucleus", "edge": "center"}},
            ],
            "actions": [{"verb": "draw", "target": "rbc"},
                        {"verb": "draw", "target": "cilia_cell"},
                        {"verb": "write", "target": "lbl_hair"},
                        {"verb": "write", "target": "lbl_tail"},
                        {"verb": "write", "target": "lbl_rbc"},
                        {"verb": "draw", "target": "arr_hair"},
                        {"verb": "draw", "target": "arr_tail"},
                        {"verb": "draw", "target": "arr_rbc"}]})

    def _rendered(self):
        # only the red blood cell resolves; 'ciliated_cell' is the 429
        return SceneRenderer(self._scene(),
                             asset_resolver=_resolver(_cell_asset()))

    def test_the_orphan_labels_and_arrows_are_dropped(self):
        r = self._rendered()
        warns = r.audit()["warnings"]
        for eid in ("lbl_hair", "lbl_tail", "arr_hair", "arr_tail"):
            assert any(w.startswith(f"ORPHANED_BY_UNRESOLVED_ASSET {eid} ")
                       for w in warns), (eid, warns)
            assert not r._flat[eid]
        # ...and the warning names the element that went missing
        assert any("(cilia_cell)" in w for w in warns
                   if w.startswith("ORPHANED_BY_UNRESOLVED_ASSET"))
        assert any(w.startswith("ASSET_UNRESOLVED cilia_cell") for w in warns)

    def test_nothing_of_the_orphans_is_drawn(self):
        r = self._rendered()
        for eid in ("lbl_hair", "lbl_tail"):
            assert r.bound[eid].text is None
        for eid in ("arr_hair", "arr_tail"):
            assert eid not in r.audit()["arrow_heads"]

    def test_they_are_never_laid_out_over_the_other_diagram(self):
        r = self._rendered()
        art = r.bound["rbc"].box
        for eid in ("lbl_hair", "lbl_tail"):
            box = r.bound[eid].box
            assert not (box[0] < art[2] and box[2] > art[0]
                        and box[1] < art[3] and box[3] > art[1]), (eid, box)
        assert not any(w.startswith("TEXT_OVER_ART") for w in r.audit()["warnings"])

    def test_the_surviving_diagram_keeps_its_own_label(self):
        r = self._rendered()
        assert r.bound["lbl_rbc"].text is not None
        assert "arr_rbc" in r.audit()["arrow_heads"]
        assert not any("ORPHANED_BY_UNRESOLVED_ASSET lbl_rbc" in w
                       for w in r.audit()["warnings"])

    def test_the_pen_does_not_mime_the_dropped_write(self):
        r = self._rendered()
        r.compile(20.0)
        for ta in r.timeline:
            if ta.action.target in ("lbl_hair", "arr_hair"):
                assert ta.duration <= 0.05 + 1e-6, ta.action.target

    def test_a_decoration_aimed_at_an_orphan_draws_nothing(self):
        data = self._scene().model_dump()
        data["actions"].append({"verb": "circle", "target": "lbl_hair"})
        r = SceneRenderer(Scene.model_validate(data),
                          asset_resolver=_resolver(_cell_asset()))
        assert not r.deco

    def test_a_scene_whose_assets_all_resolve_is_untouched(self):
        r = SceneRenderer(self._scene(),
                          asset_resolver=lambda k: ("raster", _cell_asset(k)))
        assert not any(w.startswith("ORPHANED_BY_UNRESOLVED_ASSET")
                       for w in r.audit()["warnings"])
        for eid in ("lbl_hair", "lbl_tail", "lbl_rbc"):
            assert r.bound[eid].text is not None


class TestBubbleFootprint:
    """Founder: bubbles 'take up a lot of space on the whiteboard and in some
    instances hide the image being drawn underneath'."""

    LONG = ("A group of similar cells, which all work together to carry out "
            "a particular function, is called a tissue in every living thing.")

    def _area(self, text):
        from spike.scene_engine.whiteboard import bubble_elements
        els = bubble_elements("b", text, (640.0, 360.0), (640.0, 500.0))
        outline = next(e for e in els if e["id"] == "b")
        xs = [p[0] for p in outline["points"]]
        ys = [p[1] for p in outline["points"]]
        return (max(xs) - min(xs)) * (max(ys) - min(ys))

    def test_the_worst_case_bubble_shrank_substantially(self):
        # the old geometry could reach 600 x 132 = 79,200 world px^2
        assert self._area(self.LONG) <= 0.65 * 79200

    def test_text_is_smaller_than_it_was(self):
        from spike.scene_engine.whiteboard import bubble_elements
        els = bubble_elements("b", self.LONG, (640.0, 360.0), (640.0, 500.0))
        sizes = {e["size"] for e in els if e.get("type") == "text"}
        assert sizes and max(sizes) < 24, "the founder asked for smaller text"

    def test_it_is_still_readable(self):
        """The opposite failure. Shrinking with no floor is how a bubble stops
        being speech and becomes decoration."""
        from spike.scene_engine.whiteboard import bubble_elements
        els = bubble_elements("b", self.LONG, (640.0, 360.0), (640.0, 500.0))
        sizes = {e["size"] for e in els if e.get("type") == "text"}
        assert min(sizes) >= 16

    def test_every_line_stays_inside_the_outline(self):
        """A 70-char third line once rendered outside its bubble."""
        from spike.scene_engine.whiteboard import bubble_elements
        for text in (self.LONG, "Yes.", "What makes a tree different?",
                     " ".join(["word"] * 60)):
            els = bubble_elements("b", text, (640.0, 360.0), (640.0, 500.0))
            outline = next(e for e in els if e["id"] == "b")
            ys = [p[1] for p in outline["points"]]
            top, bot = min(ys), max(ys)
            for e in els:
                if e.get("type") != "text":
                    continue
                assert top < e["at"][1] < bot, (
                    f"line {e['text']!r} sits outside its bubble")


class TestNarrationObjectsAreDrawn:
    """Founder: draw the common objects the narration names (hammer, tree).
    Measured: a live lesson produced ZERO sketches, because the pass skipped
    any board already carrying a picture — which on the semantic path is
    every board."""

    def test_the_founders_example_word_is_drawable(self):
        from spike.scene_engine.sketchables import find_sketchables
        got = find_sketchables("You hit the nail with a hammer.", limit=2)
        assert "sk_hammer" in {g["key"] for g in got}

    def test_a_sketch_lands_on_a_board_that_already_has_a_diagram(self):
        from spike.scene_engine.continuity import compile_plan, parse_visual_plan
        plan = parse_visual_plan({"chapters": [{
            "concept": "forces", "transition": "clear_and_redraw",
            "assets": {"lever": "A lever on a fulcrum"},
            "elements": [{"id": "pic", "type": "illustration",
                          "asset": "lever", "at": [600, 380], "scale": 1.0}],
            "steps": [{"segment": 1, "decision": "NEW_VISUAL",
                       "actions": [{"verb": "draw", "target": "pic"}]}],
        }]})
        scenes, assets, report = compile_plan(
            plan, {"s001": "Think of hitting it with a hammer."},
            all_segments=["s001"], skip_hold=set())
        assert "sk_hammer" in assets.get("s001", {}), \
            "the board had a diagram, so the sketch pass skipped it again"
        assert any("SKETCHED" in ln and "margin" in ln for ln in report)

    def test_the_slot_is_measured_against_the_real_board_not_assumed(self):
        """The semantic path always places at (600, 380); a hand-authored plan
        does not. Choosing the corner from the assumed position put a sketch
        16px inside a real diagram."""
        from spike.scene_engine.whiteboard import (free_margin_slots,
                                                   illustration_box)
        wide = illustration_box((640.0, 360.0), 1.0)     # x 290..990
        slots = free_margin_slots([wide])
        assert slots, "a wide diagram still leaves a usable corner"
        for s in slots:
            b = illustration_box((s[0], s[1]), s[2])
            assert (b[2] <= wide[0] or b[0] >= wide[2]
                    or b[3] <= wide[1] or b[1] >= wide[3])

    def test_a_full_board_is_left_alone(self):
        from spike.scene_engine.whiteboard import free_margin_slots
        assert free_margin_slots([(0.0, 0.0, 1280.0, 720.0)]) == []

    def test_one_object_is_not_drawn_over_and_over(self):
        """A narration that says 'tree' in five segments draws it once."""
        from spike.scene_engine.continuity import compile_plan, parse_visual_plan
        plan = parse_visual_plan({"chapters": [{
            "concept": "t", "transition": "clear_and_redraw", "assets": {},
            "elements": [{"id": "t1", "type": "text", "text": "Trees",
                          "at": [400, 120], "role": "title"}],
            "steps": [{"segment": 1, "decision": "NEW_VISUAL",
                       "actions": [{"verb": "write", "target": "t1"}]}],
        }]})
        segs = [f"s{i:03d}" for i in range(1, 6)]
        scenes, assets, _ = compile_plan(
            plan, {s: "Look at the tree." for s in segs},
            all_segments=segs, skip_hold=set())
        drew = sum(1 for s in segs if "sk_tree" in assets.get(s, {}))
        assert drew == 1, f"sk_tree drawn on {drew} boards"


class TestRedrawStatCountsRealWipes:
    """The stat added every chapter boundary to the wipe count, which was
    true only while every boundary wiped. A measured lesson with 8 chapters
    and 1 real wipe scored 7 — the metric would have reported churn that the
    fix had already removed."""

    def _plan(self, transitions):
        # NB the compiler's vocabulary is "carry"; the director writes
        # "continue" and the adapter maps it. parse_visual_plan drops anything
        # it does not recognise and falls back to a wipe.
        from spike.scene_engine.continuity import parse_visual_plan
        return parse_visual_plan({"chapters": [
            {"concept": f"c{i}", "transition": tr, "assets": {},
             "elements": [{"id": f"t{i}", "type": "text", "text": "x",
                           "at": [400, 120], "role": "title"}],
             "steps": [{"segment": i + 1, "decision": "EXTEND",
                        "actions": [{"verb": "write", "target": f"t{i}"}]}]}
            for i, tr in enumerate(transitions)]})

    def test_carrying_chapters_are_not_counted_as_wipes(self):
        from spike.scene_engine.continuity import plan_stats
        p = self._plan(["clear_and_redraw"] + ["carry"] * 7)
        assert plan_stats(p)["visual_chapters"] == 8
        assert plan_stats(p)["full_redraws"] == 0, "no boundary here wipes"

    def test_a_real_wipe_is_still_counted(self):
        from spike.scene_engine.continuity import plan_stats
        p = self._plan(["clear_and_redraw", "carry", "clear_and_redraw"])
        assert plan_stats(p)["full_redraws"] == 1

    def test_the_opening_chapter_is_never_a_wipe(self):
        """There is no board to clear before the first one."""
        from spike.scene_engine.continuity import plan_stats
        assert plan_stats(self._plan(["clear_and_redraw"]))["full_redraws"] == 0


# ── the title's slot, and the picture's transparent margin ───────────────────

def _fitted_asset(key: str = "big_cell", size: int = 1024,
                  inset: int = 0) -> RasterAsset:
    """The production root geometry: a square generated illustration fitted to
    the board (1024px -> 520px world, world_scale 0.5078125). `inset` is the
    transparent margin around the drawing, which every generated image has and
    which `b.box` — the CANVAS — knows nothing about."""
    from PIL import ImageDraw
    ink = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(ink).ellipse(
        [inset, inset, size - 1 - inset, size - 1 - inset],
        outline=(0, 0, 0, 255), width=10)
    trace = [(size / 2.0, float(inset + 20 + i))
             for i in range(0, max(20, size - 2 * inset - 40), 20)]
    return RasterAsset(key=key, ink=ink, trace=trace, stamp_r=8.0,
                       world_scale=0.5078125, regions={})


_TITLE_EL = {"id": "ttl", "type": "text", "text": "Structure of a plant cell",
             "role": "title", "size": 42, "at": [640, 80], "anchor": "mt"}


def _board(extra: list, inset: int = 0) -> SceneRenderer:
    els = [{"id": "cell", "type": "illustration", "asset": "big_cell",
            "at": [600, 380], "scale": 1.0}] + extra
    acts = [{"verb": "draw", "target": "cell"}] + \
           [{"verb": "write", "target": e["id"]} for e in extra]
    scene = Scene.model_validate({"id": "t", "compiled": True,
                                  "narration": "the plant cell",
                                  "elements": els, "actions": acts})
    return SceneRenderer(scene,
                         asset_resolver=_resolver(_fitted_asset(inset=inset)))


class TestTheTitleKeepsItsSlot:
    """`_keep_text_off_art` had no exemption for role='title', and it measured
    against the illustration's CANVAS. On the production root geometry the
    title clipped the canvas's transparent top margin by 40% of its own
    height, so every scene-engine lesson's title was yanked from top-centre to
    the corner at (24, 27)."""

    def test_the_title_is_not_moved_off_the_picture(self):
        r = _board([_TITLE_EL])
        box = r.bound["ttl"].box
        # the geometry that reproduces the defect: the art really does reach
        # up under the title
        assert r._root_art_box()[1] < box[3]
        assert box[0] > 300.0, box            # still centred, not the corner
        assert box[1] == 80.0, box            # ...at its own y
        warns = r.audit()["warnings"]
        assert not any("ttl" in w for w in warns
                       if w.startswith(("TEXT_MOVED_OFF_ART",
                                        "TEXT_OVER_ART"))), warns

    def test_a_labels_slot_is_still_a_collision_to_resolve(self):
        """The exemption is for the title alone: ordinary board text over the
        art must still move."""
        lbl = {"id": "lbl", "type": "text", "text": "Nucleus", "role": "label",
               "size": 27, "at": [560, 350], "anchor": "lt"}
        r = _board([lbl])
        assert "TEXT_MOVED_OFF_ART lbl" in r.audit()["warnings"]

    def test_the_exempt_title_still_occupies_the_space_it_sits_in(self):
        """Exempt from being MOVED is not the same as not being THERE.

        The title was filtered out of the pass's `texts` and so never joined
        `occupied` — a hole in the board exactly where the top row is. This
        geometry (a picture whose ink leaves a wide margin, and a label too
        wide for either column) sends the label to the top row, where every
        slot crosses the title: measured on the previous commit the label was
        placed at (24, 120)-(502, 175), 2167px^2 across the chapter title,
        with the pass reporting TEXT_MOVED_OFF_ART as if it had succeeded.
        One collision traded for another.
        """
        lbl = {"id": "lbl", "type": "text", "size": 34, "role": "label",
               "text": "Chloroplasts trap sunlight energy",
               "at": [560, 350], "anchor": "lt"}
        r = _board([_TITLE_EL, lbl], inset=160)
        warns = r.audit()["warnings"]
        ttl, box = r.bound["ttl"].box, r.bound["lbl"].box
        assert "TEXT_MOVED_OFF_ART lbl" in warns
        assert ttl == (421.0, 80.0, 859.0, 147.0), ttl   # the title is unmoved
        overlap = (max(0.0, min(ttl[2], box[2]) - max(ttl[0], box[0]))
                   * max(0.0, min(ttl[3], box[3]) - max(ttl[1], box[1])))
        assert overlap == 0.0, (box, ttl)
        assert not any(w.startswith("TEXT_OVERLAP") for w in warns), warns


class TestArtIsTheInkNotTheCanvas:
    """A generated illustration arrives on a square canvas with a wide
    transparent margin. Measuring board text against the canvas counts empty
    air as part of the picture."""

    def test_the_art_box_is_the_inks_extent(self):
        r = _board([], inset=160)
        canvas = r.bound["cell"].box
        art = r._root_art_box()
        assert canvas == (340.0, 120.0, 860.0, 640.0)
        assert art[0] > canvas[0] + 60 and art[3] < canvas[3] - 60, art

    def test_an_untextured_canvas_falls_back_to_the_whole_box(self):
        # the vector tier and a fully transparent ink have no bbox to read;
        # the pass must degrade to what it measured before, not to nothing
        r = SceneRenderer(
            Scene.model_validate({
                "id": "v", "compiled": True, "narration": "x",
                "elements": [{"id": "cell", "type": "illustration",
                              "asset": "plant_cell", "at": [640, 360],
                              "scale": 1.0}],
                "actions": [{"verb": "draw", "target": "cell"}]}),
            asset_resolver=_resolver(_cell_asset()))
        assert r._root_art_box() == r.bound["cell"].box

    def test_a_label_in_the_margin_is_left_where_it_was(self):
        lbl = {"id": "lbl", "type": "text", "text": "Nucleus", "role": "label",
               "size": 27, "at": [350, 150], "anchor": "lt"}
        r = _board([lbl], inset=160)
        assert r.bound["lbl"].box[:2] == (350.0, 150.0)
        assert not any(w.startswith("TEXT_MOVED_OFF_ART")
                       for w in r.audit()["warnings"])

    def test_a_caption_still_gets_one_row_under_a_deep_picture(self):
        """A picture whose ink ends within a caption's height of the bottom
        safe edge: `below()` started at ry1+12, which no longer fits, so the
        caption was pushed into the right column beside the picture it is
        about."""
        cap = {"id": "cap", "type": "text", "role": "caption", "size": 24,
               "text": "A plant cell in cross-section", "at": [640, 400],
               "anchor": "mt"}
        r = _board([cap], inset=12)
        art, box = r._root_art_box(), r.bound["cap"].box
        assert 630.0 < art[3] < 636.0, art     # the geometry under test
        assert box[1] >= art[3], (box, art)    # under the picture...
        assert box[3] <= WORLD_H - 46.0
        assert abs((box[0] + box[2]) / 2 - (art[0] + art[2]) / 2) < 40.0


class TestADroppedElementIsNotAZoomTarget:
    """Cells Part 3: a 429'd illustration took its labels with it, and
    `_drop_element` zeroes the orphan's box. A zoom whose target was one of
    them focused (0, 0) — the empty top-left corner of the board."""

    def _rendered(self):
        base = TestOrphansOfAnUnresolvedAsset()._scene().model_dump()
        base["actions"].insert(2, {"verb": "zoom", "target": "lbl_hair",
                                   "scale": 1.6})
        r = SceneRenderer(Scene.model_validate(base),
                          asset_resolver=_resolver(_cell_asset()))
        r.compile(30.0)
        return r

    def test_the_camera_follows_the_next_board_action_instead(self):
        r = self._rendered()
        assert r.bound["lbl_hair"].box == (0.0, 0.0, 0.0, 0.0)
        zt = next(ta for ta in r.timeline if ta.action.verb == "zoom")
        st = r.cam.state_at(zt.end)
        # lbl_rbc — the one label whose picture arrived — is the next board
        # action with ink; its box centre is y=300. The zeroed corner clamps
        # to y=225 at this scale, which is what shipped.
        lo, hi = r.bound["lbl_rbc"].box[1], r.bound["lbl_rbc"].box[3]
        assert st.cy == pytest.approx((lo + hi) / 2, abs=1.0), st.cy

    def test_a_live_element_is_still_framed_directly(self):
        base = TestOrphansOfAnUnresolvedAsset()._scene().model_dump()
        base["actions"].append({"verb": "zoom", "target": "lbl_rbc",
                                "scale": 1.6})
        r = SceneRenderer(Scene.model_validate(base),
                          asset_resolver=_resolver(_cell_asset()))
        r.compile(30.0)
        zt = next(ta for ta in r.timeline if ta.action.verb == "zoom")
        b = r.bound["lbl_rbc"].box
        assert r.cam.state_at(zt.end).cy == \
            pytest.approx((b[1] + b[3]) / 2, abs=1.0)
