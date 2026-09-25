"""A picture asked for N parts that vision boxed NONE of.

States of Matter and the Particle Model, 2026-09-25: the director named
`melting_path`, `freezing_path`, `evaporation_path`, `sublimation_path` on
an unlabelled triangle of three shapes joined by arrows. Vision, shown only
the wordless drawing and those four names, returned no box for any of them.
The generation path then latched all four as ASKED, the library published the
row as `renderer_validated` with group_count 0, `_lift_library_vision` seeded
that empty answer onto every later container, and each of the four labels
drew its leader line to the edge of the picture — for every lesson, forever.

Three things change here, each pinned below:

  * the annotator is told what the drawing IS (its own description), so it
    can tell one unlabelled arrow from another by shape and position;
  * a TOTAL miss does not latch the names as asked (once); the next load asks
    again, and the second miss latches so the question is bounded;
  * the library refuses to publish a picture that was asked for parts and
    delivered none, and refuses to seed from a row that says so.

No test here makes a live call: the vision pass is a stub and the library is
offline.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import shared.visual_library as vl
from spike.scene_engine import raster_assets as ra
from spike.scene_engine.partnames import norm_part
from tests.test_library_regions import _fake_sb

TRIANGLE = ("A triangle of three simple shapes joined by curved arrows: a "
            "block of packed circles at the lower left, a beaker of loose "
            "circles at the lower right, and a scattered cloud of circles at "
            "the top. Name the layer groups exactly: solid_block, "
            "liquid_beaker, gas_cloud, solid_to_liquid_arrow.")
NAMES = ["solid_block", "liquid_beaker", "gas_cloud", "solid_to_liquid_arrow"]


def _png_bytes(width: int = 640, height: int = 480) -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([10, 10, width - 10, height - 10],
                                  outline=(0, 0, 0, 255), width=3)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _ink():
    from PIL import Image
    return Image.open(io.BytesIO(_png_bytes())).convert("RGBA")


def _cache(tmp_path: Path, md: dict) -> Path:
    cache = tmp_path / "cache"
    d = cache / ra.canonical_key("transition_triangle")
    d.mkdir(parents=True)
    (d / "asset.png").write_bytes(_png_bytes())
    (d / "meta.json").write_text(json.dumps(md), encoding="utf-8")
    return cache


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "idx")
    monkeypatch.setattr(vl, "DECISION_LOG", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(vl, "_sb", lambda: None)
    return vl


# ── 1. the annotator is told what the drawing is ─────────────────────────────

class TestTheAnnotatorIsToldWhatTheDrawingIs:
    def _prompts(self, monkeypatch, replies):
        seen: list[str] = []
        it = iter(replies)

        def fake(prompt, _png):
            seen.append(prompt)
            return next(it, {})

        monkeypatch.setattr(ra, "_vision_json", fake)
        monkeypatch.setattr(ra, "scan_text", lambda ink: [])
        return seen

    def test_the_description_goes_into_the_locate_prompt_without_the_tail(
            self, monkeypatch):
        seen = self._prompts(monkeypatch, [{"has_text": False, "regions": {
            "solid_block": [[700, 50, 950, 300]],
            "liquid_beaker": [[700, 700, 950, 950]],
            "gas_cloud": [[50, 400, 300, 600]],
            "solid_to_liquid_arrow": [[900, 300, 950, 700]]}}])
        out = ra.annotate_regions(_ink(), NAMES, TRIANGLE)
        assert set(out["regions"]) == {norm_part(n) for n in NAMES}
        assert len(seen) == 1
        assert "block of packed circles at the lower left" in seen[0]
        assert "Name the layer groups" not in seen[0]
        assert "SHAPE and POSITION" in seen[0]
        # and the names still come after it, as the question
        assert seen[0].index("lower left") < seen[0].index("solid_block")

    def test_an_arrow_named_by_its_ends_is_explained_to_the_annotator(
            self, monkeypatch):
        seen = self._prompts(monkeypatch, [{"has_text": False, "regions": {}}, {}])
        ra.annotate_regions(_ink(), NAMES, TRIANGLE)
        assert "one_to_other" in seen[0] and "box that arrow" in seen[0]

    def test_the_focused_re_ask_carries_the_description_too(self, monkeypatch):
        seen = self._prompts(monkeypatch, [
            {"has_text": False, "regions": {"solid_block": [[700, 50, 950, 300]]}},
            {"regions": {"gas_cloud": [[50, 400, 300, 600]]}}])
        out = ra.annotate_regions(_ink(), NAMES, TRIANGLE)
        assert len(seen) == 2
        assert "scattered cloud of circles at the top" in seen[1]
        assert set(out["regions"]) == {"solid block", "gas cloud"}

    def test_no_description_means_the_old_prompt_exactly(self, monkeypatch):
        seen = self._prompts(monkeypatch, [{"has_text": False, "regions": {}}, {}])
        ra.annotate_regions(_ink(), ["nucleus"])
        assert "generated from this description" not in seen[0]

    def test_asset_description_strips_the_tail_and_squeezes_whitespace(self):
        assert ra.asset_description(TRIANGLE) == (
            "A triangle of three simple shapes joined by curved arrows: a "
            "block of packed circles at the lower left, a beaker of loose "
            "circles at the lower right, and a scattered cloud of circles at "
            "the top.")
        assert ra.asset_description("  two\n\nlines  ") == "two lines"
        assert ra.asset_description(None) == ""


# ── 2. a total miss does not latch (once) ────────────────────────────────────

class TestATotalMissIsNotLatched:
    def test_the_generation_path_leaves_the_names_open(self, tmp_path, monkeypatch):
        """Vision boxed none of the four: annotated_for is written EMPTY, the
        vision doc says "not annotated", and the miss is counted."""
        from PIL import Image
        monkeypatch.setattr(ra, "current_image_model",
                            lambda: type("M", (), {"id": "m", "size": None})())
        monkeypatch.setattr(ra, "_clear_to_generate", lambda *a, **k: True)
        monkeypatch.setattr(ra, "_take_rate_limited", lambda: (False, 0))
        monkeypatch.setattr(ra, "_vertex_call", lambda *a, **k: _png_bytes())
        monkeypatch.setattr(ra, "_aistudio_call", lambda *a, **k: None)
        monkeypatch.setattr(ra, "to_working_size", lambda im: im)
        monkeypatch.setattr(ra, "to_ink", lambda im: im.convert("RGBA"))
        asked: list = []

        def annotate(ink, names, desc=None):
            asked.append((list(names), desc))
            return {"regions": {}, "has_text": False, "text_boxes": []}

        monkeypatch.setattr(ra, "annotate_regions", annotate)
        cache = tmp_path / "cache"
        asset = ra._get_raster_asset("transition_triangle", TRIANGLE, cache)
        assert asset is not None
        assert asked and asked[0][0] == NAMES and asked[0][1] == TRIANGLE
        md = json.loads((cache / "transition_triangle" / "meta.json").read_text())
        assert md["annotated_for"] == []
        assert md["region_misses"] == 1
        assert md["vision"] == {}, "an empty document says 'not annotated'"

    def test_the_cached_path_asks_again_and_the_second_miss_latches(
            self, tmp_path, monkeypatch):
        cache = _cache(tmp_path, {"key": "transition_triangle", "prompt": TRIANGLE,
                                  "provenance": "generated", "regions": {},
                                  "annotated_for": [], "region_misses": 1})
        calls: list = []

        def annotate(ink, names, desc=None):
            calls.append(list(names))
            return {"regions": {}, "has_text": False, "text_boxes": []}

        monkeypatch.setattr(ra, "annotate_regions", annotate)
        ra._get_raster_asset("transition_triangle", TRIANGLE, cache,
                             allow_generate=False)
        md = json.loads((cache / "transition_triangle" / "meta.json").read_text())
        assert calls == [NAMES], "the open question is asked once more"
        assert md["annotated_for"] == NAMES, "the second miss latches"
        assert md["region_misses"] == 2
        # and a third load is silent
        ra._get_raster_asset("transition_triangle", TRIANGLE, cache,
                             allow_generate=False)
        assert calls == [NAMES]

    def test_a_partial_answer_latches_every_name_as_before(self, tmp_path, monkeypatch):
        cache = _cache(tmp_path, {"key": "transition_triangle", "prompt": TRIANGLE,
                                  "provenance": "generated", "regions": {},
                                  "annotated_for": []})
        monkeypatch.setattr(ra, "annotate_regions", lambda ink, names, *_: {
            "regions": {"solid_block": [[1, 2, 3, 4]]}, "has_text": False,
            "text_boxes": []})
        asset = ra._get_raster_asset("transition_triangle", TRIANGLE, cache,
                                     allow_generate=False)
        md = json.loads((cache / "transition_triangle" / "meta.json").read_text())
        assert md["annotated_for"] == NAMES
        assert "region_misses" not in md
        assert asset.regions == {"solid_block": [[1, 2, 3, 4]]}

    def test_a_library_row_that_found_nothing_is_not_seeded_from(self):
        md = {"key": "transition_triangle",
              "vision": {"regions": {}, "annotated_for": NAMES,
                         "baked_text": False, "w": 640, "h": 480}}
        ra._lift_library_vision(md, (640, 480))
        assert "annotated_for" not in md and "regions" not in md

    def test_a_library_row_with_one_box_still_seeds(self):
        md = {"key": "transition_triangle",
              "vision": {"regions": {"gas_cloud": [[1, 2, 3, 4]], "solid_block": []},
                         "annotated_for": NAMES, "baked_text": False,
                         "w": 640, "h": 480}}
        ra._lift_library_vision(md, (640, 480))
        assert md["annotated_for"] == NAMES
        assert md["regions"] == {"gas_cloud": [[1, 2, 3, 4]], "solid_block": []}

    def test_the_repair_pass_carries_the_description_and_does_not_latch_a_miss(
            self, tmp_path, monkeypatch):
        cache = _cache(tmp_path, {"key": "transition_triangle", "prompt": TRIANGLE,
                                  "provenance": "generated", "regions": {},
                                  "annotated_for": []})
        seen: list = []

        def annotate(ink, names, desc=None):
            seen.append((list(names), desc))
            return {"regions": {}, "has_text": False, "text_boxes": []}

        monkeypatch.setattr(ra, "annotate_regions", annotate)
        out = ra.repair_asset_regions("transition_triangle", ["gas_cloud"], cache)
        assert out["asked"] and out["found"] == []
        assert seen == [(["gas_cloud"], TRIANGLE)]
        md = json.loads((cache / "transition_triangle" / "meta.json").read_text())
        assert md["annotated_for"] == [] and md["region_misses"] == 1


# ── 3. the library refuses the miss at the door ──────────────────────────────

class TestTheLibraryRefusesAPictureNoLabelCanPointInto:
    def _publish(self, tmp_path, monkeypatch, md, prompt=TRIANGLE):
        seen: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(seen))
        png = tmp_path / "asset.png"
        png.write_bytes(_png_bytes(640, 480))
        ok = vl.publish_generated("transition_triangle", prompt, png, md)
        return ok, seen

    def test_asked_for_four_found_none_is_refused(self, library, tmp_path, monkeypatch):
        ok, seen = self._publish(tmp_path, monkeypatch, {
            "provenance": "generated", "baked_text": False,
            "regions": {}, "annotated_for": NAMES})
        assert ok is False and "inserted" not in seen and "uploaded" not in seen

    def test_the_unlatched_miss_is_judged_by_the_prompts_own_question(
            self, library, tmp_path, monkeypatch):
        """After the change above a total miss writes annotated_for []: the
        gate reads the tail so it cannot be mistaken for 'nobody asked'."""
        ok, seen = self._publish(tmp_path, monkeypatch, {
            "provenance": "generated", "baked_text": False,
            "regions": {}, "annotated_for": [], "region_misses": 1})
        assert ok is False and "inserted" not in seen

    def test_one_box_of_four_still_publishes(self, library, tmp_path, monkeypatch):
        ok, seen = self._publish(tmp_path, monkeypatch, {
            "provenance": "generated", "baked_text": False,
            "regions": {"gas_cloud": [[1, 2, 3, 4]]}, "annotated_for": NAMES})
        assert ok is True
        row = seen["inserted"][0]
        assert row["group_ids"] == ["gas_cloud"]
        assert row["vision"]["annotated_for"] == NAMES

    def test_a_picture_nobody_asked_a_question_of_still_publishes(
            self, library, tmp_path, monkeypatch):
        ok, seen = self._publish(tmp_path, monkeypatch, {"provenance": "generated"},
                                 prompt="A single right hand holding a marker pen.")
        assert ok is True and "vision" not in seen["inserted"][0]

    def test_prompt_part_names_reads_the_tail_the_way_the_renderer_does(self):
        assert vl.prompt_part_names(TRIANGLE) == NAMES
        assert vl.prompt_part_names(TRIANGLE) == ra.part_names_from_prompt(TRIANGLE)
        assert vl.prompt_part_names("no tail here.") == []

    def test_the_integration_reports_a_refused_publish_honestly(self):
        src = Path("shared/visual_library_integration.py").read_text(encoding="utf-8")
        assert "published = bool(publish_generated(" in src


# ── the director is told the same thing ──────────────────────────────────────

class TestTheDirectorNamesThingsNotProcesses:
    def test_the_semantic_prompt_says_a_region_is_a_visible_part(self):
        from agent3_scripts.semantic_prompt import build_semantic_prompt
        p = build_semantic_prompt("conversational", "States of Matter", "Grade 7",
                                  "6.0", "<sections>")
        assert "never a process, change or relation" in p
        assert "solid_to_liquid_arrow" in p
        assert "WHERE each region sits" in p
        assert "name a visible part, never what happens to it" in p
        # the worked example shows a layout, not just a subject
        assert "side by side from left to right" in p

    def test_the_legacy_prompt_says_it_too(self, monkeypatch):
        monkeypatch.setenv("VIDEO_ENGINE", "scene")
        from agent3_scripts.prompts import build_episode_prompt
        p = build_episode_prompt("conversational", chapter_title="States of Matter",
                                 difficulty_level="Grade 7", target_duration="6.0",
                                 episode_context="<sections>")
        assert "visual_plan" in p
        assert "VISIBLE PARTS of the picture" in p
        assert "solid_to_liquid_arrow" in p

    def test_a_never_asked_cache_file_still_publishes(self, tmp_path, monkeypatch):
        """No annotated_for, no regions, no miss count: the migration over a
        meta.json that predates annotation. Not a miss — never asked."""
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "idx")
        monkeypatch.setattr(vl, "DECISION_LOG", tmp_path / "decisions.jsonl")
        seen: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(seen))
        png = tmp_path / "asset.png"
        png.write_bytes(_png_bytes())
        assert vl.publish_generated("transition_triangle", TRIANGLE, png,
                                    {"provenance": "generated"})
        assert "vision" not in seen["inserted"][0]
