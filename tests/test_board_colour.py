"""Board colour, phase 1 (spike/scene_engine/colour.py): a second accent the
engine assigns from what it knows — leader arrows, relation arrows, equation
rows — behind FEATURE_BOARD_COLOUR. The flag OFF is the benchmark: the
compiled plan must be byte-identical to today's, so a rollback is unsetting
one variable.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spike.scene_engine import colour
from spike.scene_engine.continuity import compile_plan, parse_visual_plan
from spike.scene_engine.paper import PALETTE, role_color
from spike.scene_engine.schema import ArrowElement, TextElement
from spike.scene_engine.semantic import adapt_semantic_plan

NARR = {
    "s001": "Here is a right-angled triangle. The hypotenuse is the longest side.",
    "s002": "Water flows from the outer bank to the inner bank.",
}


def _plan():
    return {"chapters": [{
        "id": "chapter_1", "concept": "triangle_sides", "transition": "continue",
        "assets": {"triangle": "A right-angled triangle, the hypotenuse on the right, the base at the bottom"},
        "semantic_regions": ["hypotenuse", "base"],
        "elements": [
            {"id": "tri", "type": "illustration", "asset": "triangle", "role": "root_visual"},
            {"id": "lbl_hyp", "type": "text", "text": "Hypotenuse", "role": "label"},
        ],
        "steps": [
            {"segment": 1, "decision": "EXTEND", "reason": "introduce the shape",
             "actions": [
                 {"verb": "DRAW", "target": {"element": "tri"}, "cue": "a right-angled triangle"},
                 {"verb": "WRITE", "target": {"element": "lbl_hyp"}, "cue": "The hypotenuse"},
                 # a leader: the label for this region exists
                 {"verb": "ARROW", "target": {"asset": "triangle", "region": "hypotenuse"}, "cue": "the longest side"}]},
            {"segment": 2, "decision": "EXTEND", "reason": "a relation",
             "actions": [
                 # a relation: no label names this region
                 {"verb": "ARROW", "target": {"asset": "triangle", "region": "base"}, "cue": "outer bank"}]},
        ],
    }]}


def _equation_plan():
    return {"chapters": [{
        "id": "chapter_1", "concept": "respiration_equation", "transition": "clear_and_redraw",
        "assets": {}, "semantic_regions": [],
        "elements": [
            {"id": "lhs", "type": "text", "text": "Glucose + Oxygen", "role": "label"},
            {"id": "arrow", "type": "text", "text": "→", "role": "label"},
            {"id": "rhs", "type": "text", "text": "Carbon dioxide + Water + Energy", "role": "label"},
        ],
        "steps": [{"segment": 1, "decision": "EXTEND", "reason": "the word equation",
                   "actions": [
                       {"verb": "WRITE", "target": {"element": "lhs"}, "cue": "right-angled"},
                       {"verb": "WRITE", "target": {"element": "arrow"}, "cue": "right-angled"},
                       {"verb": "WRITE", "target": {"element": "rhs"}, "cue": "right-angled"}]}],
    }]}


def _arrows(plan):
    return {e["id"]: e for c in plan["chapters"] for e in c["elements"] if e.get("type") == "arrow"}


def _texts(plan):
    return {e["id"]: e for c in plan["chapters"] for e in c["elements"] if e.get("type") == "text"}


@pytest.fixture
def off(monkeypatch):
    monkeypatch.delenv(colour.FLAG, raising=False)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(colour.FLAG, "1")


class TestTheBenchmarkIsUntouched:
    def test_the_flag_reads_off_by_default(self, off):
        assert colour.enabled() is False
        assert colour.arrow_colour(leader=True) is None
        assert colour.arrow_colour(leader=False) is None

    def test_no_colour_key_is_written_with_the_flag_off(self, off):
        plan, issues = adapt_semantic_plan(_plan(), NARR, strict=True)
        assert issues == []
        arrows = _arrows(plan)
        assert set(arrows) == {"arr_hypotenuse", "arr_base"}
        assert all("color" not in a for a in arrows.values()), "the benchmark carries no colour key"
        eq, _ = adapt_semantic_plan(_equation_plan(), NARR, strict=False)
        assert all("color" not in t for t in _texts(eq).values())

    def test_the_adapter_output_is_identical_with_the_flag_off_or_unset(self, monkeypatch):
        monkeypatch.delenv(colour.FLAG, raising=False)
        a, _ = adapt_semantic_plan(_plan(), NARR, strict=True)
        monkeypatch.setenv(colour.FLAG, "0")
        b, _ = adapt_semantic_plan(_plan(), NARR, strict=True)
        assert a == b

    def test_the_schema_default_is_still_ink(self, off):
        a = ArrowElement(id="a", tail=[0, 0], head=[10, 10])
        assert a.color == "ink"
        assert TextElement(id="t", text="x", at=[0, 0]).color == "ink"


class TestTheRoles:
    def test_the_palette_carries_the_second_accent(self):
        for role in ("accent2", "accent2_bright", "accent2_mist"):
            assert role in PALETTE, role
        assert role_color("accent2") == PALETTE["accent2"] != PALETTE["accent"]
        # a scene style's accent override never touches the second accent
        assert role_color("accent2", style_accent=(1, 2, 3)) == PALETTE["accent2"]
        assert role_color("accent", style_accent=(1, 2, 3)) == (1, 2, 3)

    def test_the_schema_accepts_the_role_everywhere_a_role_goes(self):
        assert ArrowElement(id="a", tail=[0, 0], head=[10, 10], color="accent2").color == "accent2"
        assert TextElement(id="t", text="x", at=[0, 0], color="accent2").color == "accent2"

    def test_a_leader_is_accent_and_a_relation_is_accent2(self, on):
        plan, issues = adapt_semantic_plan(_plan(), NARR, strict=True)
        assert issues == []
        arrows = _arrows(plan)
        assert arrows["arr_hypotenuse"]["color"] == "accent", "tail is the label: a leader"
        assert arrows["arr_base"]["color"] == "accent2", "tail is the margin: a relation"

    def test_an_equation_row_reads_accent_ink_accent2(self, on):
        plan, _ = adapt_semantic_plan(_equation_plan(), NARR, strict=False)
        t = _texts(plan)
        assert (t["lhs"]["color"], t["arrow"]["color"], t["rhs"]["color"]) == ("accent", "ink", "accent2")

    def test_a_row_without_a_sign_is_left_alone(self, on):
        texts = {"a": {"text": "Nucleus"}, "b": {"text": "Cell wall"}, "c": {"text": "Vacuole"}}
        assert colour.colour_equation_row(["a", "b", "c"], texts) is False
        assert all("color" not in v for v in texts.values())
        # a sign at an end is not an equation either
        texts = {"a": {"text": "="}, "b": {"text": "x"}, "c": {"text": "y"}}
        assert colour.colour_equation_row(["a", "b", "c"], texts) is False

    def test_the_roles_survive_parsing_and_compiling(self, on):
        plan, _ = adapt_semantic_plan(_plan(), NARR, strict=True)
        vp = parse_visual_plan(copy.deepcopy(plan))
        assert vp is not None
        cols = {e["id"]: e.get("color") for c in vp.chapters for e in c.elements if e.get("type") == "arrow"}
        assert cols == {"arr_hypotenuse": "accent", "arr_base": "accent2"}
        scenes, _, report = compile_plan(vp, NARR)
        seen = {e["id"]: e.get("color") for sc in scenes.values() for e in sc["elements"] if e.get("type") == "arrow"}
        assert seen.get("arr_base") == "accent2" and seen.get("arr_hypotenuse") == "accent"

    def test_a_synthesised_leader_takes_the_leader_colour(self, on):
        """continuity draws a leader for a label the director left bare —
        it annotates, so it is a leader."""
        raw = _plan()
        raw["chapters"][0]["steps"][0]["actions"] = [
            a for a in raw["chapters"][0]["steps"][0]["actions"] if a["verb"] != "ARROW"]
        raw["chapters"][0]["steps"] = raw["chapters"][0]["steps"][:1]
        plan, _ = adapt_semantic_plan(raw, NARR, strict=True)
        vp = parse_visual_plan(plan)
        scenes, _, report = compile_plan(vp, NARR)
        synth = [e for sc in scenes.values() for e in sc["elements"] if e["id"].startswith("arr_auto_")]
        assert synth, "\n".join(report)
        assert {e.get("color") for e in synth} == {"accent"}


class TestThePin:
    """params.board_colour decides for ONE generation, flag or no flag —
    the demo and rollback lever, and how a demo video is drawn in colour
    on a flag-off production worker."""

    def test_a_pin_wins_over_the_flag_both_ways(self, monkeypatch):
        monkeypatch.delenv(colour.FLAG, raising=False)
        t = colour.set_pin(True)
        try:
            assert colour.enabled() is True
            plan, _ = adapt_semantic_plan(_plan(), NARR, strict=True)
            assert _arrows(plan)["arr_base"]["color"] == "accent2"
        finally:
            colour.reset_pin(t)
        assert colour.enabled() is False
        monkeypatch.setenv(colour.FLAG, "1")
        t = colour.set_pin(False)
        try:
            assert colour.enabled() is False
            plan, _ = adapt_semantic_plan(_plan(), NARR, strict=True)
            assert all("color" not in a for a in _arrows(plan).values())
        finally:
            colour.reset_pin(t)
        assert colour.enabled() is True

    def test_the_worker_pins_from_the_params(self, monkeypatch):
        monkeypatch.delenv(colour.FLAG, raising=False)
        for raw, want in (("true", True), ("1", True), (True, True), ("0", False), (False, False), ("no", False)):
            t = colour.pin_from_params({colour.PARAM_KEY: raw})
            try:
                assert colour.enabled() is want, raw
            finally:
                colour.reset_pin(t)
        # absent, blank or a non-dict: unpinned, the flag decides
        for params in ({}, {colour.PARAM_KEY: ""}, {colour.PARAM_KEY: None}, None, "x"):
            t = colour.pin_from_params(params)
            try:
                assert colour.enabled() is False
                monkeypatch.setenv(colour.FLAG, "1")
                assert colour.enabled() is True
                monkeypatch.delenv(colour.FLAG)
            finally:
                colour.reset_pin(t)

    def test_the_worker_sets_the_pin_beside_the_subject_profile(self):
        from pathlib import Path
        import worker.process as P
        src = Path(P.__file__).read_text(encoding="utf-8")
        i = src.index("_colour.pin_from_params(params)")
        assert "_sp.resolve(book.get(\"subject\"), params=params)" in src[i - 600:i]

    def test_a_pinned_generation_is_stamped_format_3(self, monkeypatch):
        from shared import video_format as VF
        monkeypatch.delenv(colour.FLAG, raising=False)
        assert VF.current() == 2
        t = colour.set_pin(True)
        try:
            assert VF.current() == 3
        finally:
            colour.reset_pin(t)


class TestThePictures:
    """Phase 2: restrained colour in the GENERATED PICTURES, behind its own
    switch (FEATURE_BOARD_COLOUR_PICTURES / params.board_colour_pictures)
    on top of phase 1. A coloured picture is a different asset from the ink
    one — its own key, its own cache entry, never a library candidate — so
    the ink benchmark cannot receive one and a colour lesson cannot receive
    an ink one."""

    @staticmethod
    def _stub_generation(monkeypatch, art):
        """One generated image, no vision call, no working-size resample."""
        import io
        from spike.scene_engine import raster_assets as ra

        seen: dict = {}

        def vertex(prompt, *a, **k):
            seen["prompt"] = prompt
            buf = io.BytesIO()
            art.save(buf, "PNG")
            return buf.getvalue()

        monkeypatch.setattr(ra, "current_image_model", lambda: type("M", (), {"id": "m", "size": None})())
        monkeypatch.setattr(ra, "_clear_to_generate", lambda *a, **k: True)
        monkeypatch.setattr(ra, "_take_rate_limited", lambda: (False, 0))
        monkeypatch.setattr(ra, "_vertex_call", vertex)
        monkeypatch.setattr(ra, "_aistudio_call", lambda *a, **k: None)
        monkeypatch.setattr(ra, "to_working_size", lambda im: im)
        monkeypatch.setattr(ra, "annotate_regions", lambda ink, names, desc=None: {"regions": {}, "has_text": False, "text_boxes": []})
        return seen

    @staticmethod
    def _filled_art(fill=(220, 40, 40)):
        """A board picture as the image model would draw it: a black outline
        on white, with a red fill when asked in colour (fill=None: the ink
        drawing the ink suffix asks for)."""
        from PIL import Image, ImageDraw
        art = Image.new("RGB", (240, 180), (255, 255, 255))
        d = ImageDraw.Draw(art)
        d.ellipse([40, 30, 200, 150], fill=fill, outline=(0, 0, 0), width=6)
        return art

    @staticmethod
    def _red_pixels(img) -> int:
        import numpy as np
        a = np.asarray(img.convert("RGBA")).astype(int)
        return int(((a[..., 3] > 128) & (a[..., 0] > 180) & (a[..., 1] < 90) & (a[..., 2] < 90)).sum())

    def test_the_switch_is_its_own_and_never_implied_by_phase_1(self, monkeypatch):
        monkeypatch.delenv(colour.FLAG, raising=False)
        monkeypatch.delenv(colour.PICTURES_FLAG, raising=False)
        assert colour.pictures_enabled() is False
        t = colour.set_pin(True)                       # phase 1 alone
        try:
            assert colour.enabled() is True and colour.pictures_enabled() is False
        finally:
            colour.reset_pin(t)
        t = colour.pin_from_params({colour.PARAM_KEY: True, colour.PICTURES_PARAM: True})
        try:
            assert colour.enabled() is True and colour.pictures_enabled() is True
        finally:
            colour.reset_pin(t)
        assert colour.pictures_enabled() is False
        monkeypatch.setenv(colour.PICTURES_FLAG, "1")
        assert colour.pictures_enabled() is True
        t = colour.pin_from_params({colour.PICTURES_PARAM: "0"})
        try:
            assert colour.pictures_enabled() is False, "a pin says no on a flag-on worker"
        finally:
            colour.reset_pin(t)

    def test_a_board_key_becomes_its_colour_key_only_under_the_switch(self, monkeypatch):
        from spike.scene_engine import raster_assets as ra
        monkeypatch.delenv(colour.PICTURES_FLAG, raising=False)
        assert ra.colour_key("plant_cell") == "plant_cell"
        t = colour.set_pin(True, True)
        try:
            assert ra.colour_key("plant_cell") == "plant_cell__colour"
            assert ra.colour_key("plant_cell__colour") == "plant_cell__colour", "idempotent"
            assert ra.colour_key("avatar_teacher_female") == "avatar_teacher_female", "avatars have their own tier"
            assert ra.colour_key("hand_pen") == "hand_pen", "the pen's sprite is not board art"
            assert ra.is_colour_key("plant_cell__colour") and not ra.is_colour_key("plant_cell")
        finally:
            colour.reset_pin(t)

    def test_the_benchmark_picture_is_untouched_with_the_switch_off(self, tmp_path, monkeypatch):
        from spike.scene_engine import raster_assets as ra
        monkeypatch.delenv(colour.PICTURES_FLAG, raising=False)
        seen = self._stub_generation(monkeypatch, self._filled_art(fill=None))
        asset = ra.get_raster_asset("plant_cell", "A plant cell", tmp_path)
        assert asset is not None and asset.key == "plant_cell"
        assert (ra.cache_dir_for("plant_cell", tmp_path) / "asset.png").exists()
        assert "no color fill" in seen["prompt"] and "restrained palette" not in seen["prompt"]
        assert self._red_pixels(asset.ink) == 0, "the ink cut keeps only dark strokes"

    def test_under_the_switch_the_picture_is_asked_in_colour_and_keeps_its_fills(self, tmp_path, monkeypatch):
        from spike.scene_engine import raster_assets as ra
        monkeypatch.delenv(colour.PICTURES_FLAG, raising=False)
        seen = self._stub_generation(monkeypatch, self._filled_art())
        t = colour.set_pin(True, True)
        try:
            asset = ra.get_raster_asset("plant_cell", "A plant cell", tmp_path)
        finally:
            colour.reset_pin(t)
        assert asset is not None and asset.key == "plant_cell__colour"
        assert (ra.cache_dir_for("plant_cell__colour", tmp_path) / "asset.png").exists()
        assert not (ra.cache_dir_for("plant_cell", tmp_path) / "asset.png").exists(), "the ink entry is a different asset"
        assert ra.cache_dir_for("plant_cell__colour", tmp_path) != ra.cache_dir_for("plant_cell", tmp_path)
        assert "bold, flat, clearly visible" in seen["prompt"] and "no color fill" not in seen["prompt"]
        assert "ABSOLUTELY NO TEXT OF ANY KIND" in seen["prompt"], "the no-text clause travels with every style"
        # phase 3: the fill is kept in the WASH; the lines the pen draws stay dark
        assert asset.wash is not None and self._red_pixels(asset.wash) > 1000, "the cutout keeps the fill"
        assert self._red_pixels(asset.ink) == 0, "the pen draws the outlines, not the fill"
        assert asset.trace, "a filled picture still has a drawing order"

    def test_the_colour_entry_and_the_ink_entry_live_side_by_side(self, tmp_path, monkeypatch):
        """The same lesson key drawn both ways in one cache: the switch picks
        which entry is read, and neither overwrites the other."""
        from spike.scene_engine import raster_assets as ra
        monkeypatch.delenv(colour.PICTURES_FLAG, raising=False)
        self._stub_generation(monkeypatch, self._filled_art(fill=None))
        ink = ra.get_raster_asset("cell", "A cell", tmp_path)
        self._stub_generation(monkeypatch, self._filled_art())
        t = colour.set_pin(True, True)
        try:
            col = ra.get_raster_asset("cell", "A cell", tmp_path)
        finally:
            colour.reset_pin(t)
        again = ra.get_raster_asset("cell", "A cell", tmp_path)
        assert ink.key == again.key == "cell" and col.key == "cell__colour"
        assert self._red_pixels(again.ink) == 0 and again.wash is None
        assert col.wash is not None and self._red_pixels(col.wash) > 1000

    def test_the_pins_travel_into_a_render_thread(self, monkeypatch):
        """contextvars are per thread; bind_generation carries the pins the
        way it carries the generation id and the image role."""
        import threading
        from spike.scene_engine import raster_assets as ra
        monkeypatch.delenv(colour.FLAG, raising=False)
        monkeypatch.delenv(colour.PICTURES_FLAG, raising=False)
        seen: dict = {}

        def probe():
            seen["colour"] = colour.enabled()
            seen["pictures"] = colour.pictures_enabled()

        t = colour.set_pin(True, True)
        try:
            th = threading.Thread(target=ra.bind_generation(probe, "gen-1"))
            th.start()
            th.join()
        finally:
            colour.reset_pin(t)
        assert seen == {"colour": True, "pictures": True}
        # and an unpinned job thread hands over nothing
        th = threading.Thread(target=ra.bind_generation(probe, "gen-2"))
        th.start()
        th.join()
        assert seen == {"colour": False, "pictures": False}

    def test_the_library_never_sees_a_coloured_key(self):
        """Source-level: the wrapper renames BEFORE the lock, and a colour key
        goes straight to the generator — no scoring, no hydrate, no publish,
        no decision line."""
        from pathlib import Path
        import shared.visual_library_integration as vli
        src = Path(vli.__file__).read_text(encoding="utf-8")
        rename = src.index("key = ra.colour_key(key)")
        lock = src.index("with ra.asset_lock(key):", rename)
        assert rename < lock
        bypass = src.index("if ra.is_colour_key(key):\n            return original(key, prompt, cache, allow_generate)")
        for later in ("best_match(", "hydrate(key", "_hydrate_local_library(", "publish_generated(", "log_decision("):
            assert src.index(later, bypass) > bypass, later
        assert src.index("_refresh_from_library(key", bypass) > bypass

    def test_the_format_changelog_names_the_pictures(self):
        from shared import video_format as VF
        assert "pictures switch" in VF.FORMAT_CHANGES[3]


class TestTheLeaksTheFirstPhase2DemoFound:
    """Measured 2026-09-28 on the first phase-2 demo (gen dfd97734): the
    parent generated `sieve_and_settling__colour`, but the render CHILD
    PROCESS asked for `sieve_and_settling` (it inherits no pin), and the
    local library filled the plain key from the colour directory at score
    1.40 — a coloured picture in the ink cache for every later lesson on
    that container. Two fixes: the child answers as the parent did, and the
    library never indexes, matches or hydrates a colour key."""

    def test_the_composer_hands_the_child_the_answers(self):
        from pathlib import Path
        import agent6_animation.video_composer as vc
        src = Path(vc.__file__).read_text(encoding="utf-8")
        i = src.index('"board_colour": list(_colour.answers())')
        assert src.index("render_segment_in_child", i) > i, "in the payload the child is sent"

    def test_the_child_renders_under_the_parents_answers_and_releases_them(self, tmp_path, monkeypatch):
        from spike.scene_engine import segment_worker as sw
        from tests.test_render_pool import _payload
        import spike.scene_engine.encode as enc
        monkeypatch.delenv(colour.FLAG, raising=False)
        monkeypatch.delenv(colour.PICTURES_FLAG, raising=False)
        seen: dict = {}

        def fake_encode(frames, total, audio, out, fps):
            seen["during"] = colour.answers()      # read while the frames are drawn
            for _ in frames:
                pass
            return True
        monkeypatch.setattr(enc, "encode_scene", fake_encode)
        ok, _ = sw.render_segment_in_child(_payload(tmp_path, board_colour=[True, True]))
        assert ok is True
        assert seen["during"] == (True, True)
        assert colour.answers() == (False, False), "released after the call"
        # a parent that answered no pins the child to no, whatever its flags say
        monkeypatch.setenv(colour.PICTURES_FLAG, "1")
        sw.render_segment_in_child(_payload(tmp_path, board_colour=[False, False]))
        assert seen["during"] == (False, False)
        # a payload from before phase 2 leaves the child's own flags to decide
        sw.render_segment_in_child(_payload(tmp_path))
        assert seen["during"] == (False, True)
        assert sw.pins_for_child({"board_colour": "yes"}) == (None, None)

    def test_the_library_index_refuses_a_colour_key(self, tmp_path, monkeypatch):
        import shared.visual_library as vl
        written: list = []
        monkeypatch.setattr(vl, "_local_candidates", lambda: [])
        monkeypatch.setattr(vl, "_write_local_index", lambda rows: written.append(rows))
        vl.register_local({"asset_key": "plant_cell__colour", "canonical_key": "cell_colour_plant"})
        assert written == [], "a coloured picture is never a library row"
        vl.register_local({"asset_key": "plant_cell", "canonical_key": "cell_plant"})
        assert written and written[0][0]["asset_key"] == "plant_cell"

    def test_the_bootstrap_skips_a_colour_directory(self, tmp_path, monkeypatch):
        import json
        import shared.visual_library as vl
        import shared.visual_library_integration as vli
        from spike.scene_engine import raster_assets as ra
        for key in ("plant_cell", "plant_cell__colour"):
            d = tmp_path / ra.canonical_key(key)
            d.mkdir()
            (d / "asset.png").write_bytes(b"png")
            (d / "meta.json").write_text(json.dumps({"key": key, "provenance": "generated", "prompt": "a cell"}))
        rows: list = []
        monkeypatch.setattr(ra, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(vl, "register_local", lambda row: rows.append(row["asset_key"]))
        vli._bootstrap_existing_cache(ra)
        assert rows == ["plant_cell"]

    def test_a_local_hit_on_a_colour_key_is_refused(self, tmp_path, monkeypatch):
        import shared.visual_library as vl
        import shared.visual_library_integration as vli
        src = tmp_path / "src.png"
        src.write_bytes(b"png")
        monkeypatch.setattr(vl, "find", lambda *a, **k: {"asset_key": "sk_cone__colour", "local_cache_path": str(src), "match_score": 1.4})
        assert vli._hydrate_local_library("sk_cone", "A simple cone", tmp_path / "cache") is False
        assert not list((tmp_path / "cache").glob("**/asset.png")) if (tmp_path / "cache").exists() else True


class TestTheWash:
    """Phase 3: a coloured picture is drawn as its OUTLINES first, by the
    pen, and its colour WASHES IN under them over render.WASH_SECS once a
    part's outline is complete — the whole picture when drawn in one, each
    narrated part in turn when the drawing is narration-ordered. An ink
    asset has no wash and renders exactly as before."""

    @staticmethod
    def _cutout(fill=(220, 40, 40)):
        from spike.scene_engine import raster_assets as ra
        return ra.to_color_art(TestThePictures._filled_art(fill))

    @staticmethod
    def _red_in(frame) -> int:
        return TestThePictures._red_pixels(frame)

    @staticmethod
    def _scene(actions, **el):
        from spike.scene_engine.schema import Scene
        pic = {"id": "pic", "type": "illustration", "asset": "cell", "at": [640, 360], "scale": 2.0}
        pic.update(el)
        return Scene.model_validate({
            "id": "w", "narration": "the cell has a nucleus and a wall around it",
            "elements": [pic], "actions": actions})

    @staticmethod
    def _asset(key="cell__colour", regions=None):
        from spike.scene_engine import raster_assets as ra
        cut = TestTheWash._cutout()
        return ra._finish(key, cut, regions or {})

    def test_an_ink_asset_has_no_wash(self):
        from spike.scene_engine import raster_assets as ra
        art = TestThePictures._filled_art(fill=None)
        asset = ra._finish("cell", ra.to_ink(art), {})
        assert asset.wash is None
        lines, wash = ra.split_colour_layers(ra.to_ink(art))
        assert wash is not None, "an outline drawing splits into lines and an (all-line) wash"

    def test_a_colour_asset_splits_into_lines_and_wash(self):
        import numpy as np
        asset = self._asset()
        assert asset.wash is not None and asset.wash.size == asset.ink.size
        assert self._red_in(asset.wash) > 1000
        assert self._red_in(asset.ink) == 0
        assert (np.asarray(asset.ink.getchannel("A")) > 128).sum() > 200, "the outline survives as lines"
        assert asset.trace, "the pen has an outline to follow"

    def test_a_picture_with_no_outline_keeps_the_phase_2_behaviour(self):
        from PIL import Image, ImageDraw
        from spike.scene_engine import raster_assets as ra
        art = Image.new("RGB", (240, 180), (255, 255, 255))
        ImageDraw.Draw(art).ellipse([40, 30, 200, 150], fill=(220, 40, 40))
        asset = ra._finish("blob__colour", ra.to_color_art(art), {})
        assert asset.wash is None and self._red_in(asset.ink) > 1000

    def _render(self, scene, asset, secs=6.0):
        from spike.scene_engine.render import SceneRenderer
        r = SceneRenderer(scene, asset_resolver=lambda k: ("raster", asset))
        r.compile(secs)
        return r

    def test_the_wash_follows_the_pen(self):
        from spike.scene_engine.render import WASH_SECS
        asset = self._asset()
        r = self._render(self._scene([{"verb": "draw", "target": "pic", "duration": 1.0}]), asset)
        draw = next(ta for ta in r.timeline if ta.action.verb == "draw")
        ready = r._wash_ready["pic"]
        assert ready == [pytest.approx(draw.end)], "one unit, complete when the draw ends"
        from spike.scene_engine.paper import make_background
        from spike.scene_engine.render import SS, WORLD_H, WORLD_W
        w, h = WORLD_W * SS, WORLD_H * SS
        base = make_background(w, h, r.scene.style.background)
        mid = r._frame(draw.start + 0.5 * draw.duration, base, w, h)
        assert self._red_in(mid) == 0, "while the pen draws there is no colour yet"
        st = r._state_at(draw.start + 0.5 * draw.duration)["pic"]
        assert 0.0 < st.raster_frac < 1.0 and st.wash == (0.0,)
        half = r._state_at(draw.end + 0.5 * WASH_SECS)["pic"]
        assert half.wash == (pytest.approx(0.5),)
        done = r._frame(draw.end + WASH_SECS + 0.1, base, w, h)
        assert self._red_in(done) > 500, "after the wash the picture is in colour"
        assert r._state_at(draw.end + WASH_SECS + 0.1)["pic"].wash == (1.0,)

    def test_the_frame_key_animates_while_the_wash_fades(self):
        from spike.scene_engine.render import WASH_SECS
        asset = self._asset()
        r = self._render(self._scene([{"verb": "draw", "target": "pic", "duration": 1.0}]), asset)
        draw = next(ta for ta in r.timeline if ta.action.verb == "draw")
        assert r._frame_key(draw.end + 0.3) is None
        assert r._frame_key(draw.end + WASH_SECS + 0.2) is not None

    def test_a_carried_over_picture_arrives_in_colour(self):
        asset = self._asset()
        r = self._render(self._scene([], drawn_frac=1.0), asset)
        assert r._wash_ready["pic"] == [pytest.approx(-0.8)]
        assert r._state_at(0.0)["pic"].wash == (1.0,)

    @staticmethod
    def _cell_art():
        """A cell as the colour prompt asks for it: a green wall enclosing a
        purple nucleus and a blue vacuole, each with its own dark outline."""
        from PIL import Image, ImageDraw
        art = Image.new("RGB", (400, 300), (255, 255, 255))
        d = ImageDraw.Draw(art)
        d.rounded_rectangle([20, 20, 380, 280], radius=40, fill=(170, 220, 150), outline=(20, 20, 20), width=6)
        d.ellipse([60, 90, 170, 200], fill=(150, 120, 200), outline=(20, 20, 20), width=5)
        d.ellipse([220, 70, 350, 230], fill=(150, 200, 235), outline=(20, 20, 20), width=5)
        return art

    def test_each_narrated_part_washes_when_its_outline_is_complete(self):
        from spike.scene_engine import raster_assets as ra
        from spike.scene_engine.render import WASH_SECS
        cut = ra.to_color_art(self._cell_art())          # crops 8 px off each edge (pad 12 around content at 20)
        ox, oy = 20 - 12, 20 - 12
        regions = {"nucleus": [[55 - ox, 85 - oy, 175 - ox, 205 - oy]],
                   "vacuole": [[215 - ox, 65 - oy, 355 - ox, 235 - oy]]}
        asset = ra._finish("cell__colour", cut, regions)
        r = self._render(self._scene(
            [{"verb": "draw", "target": "pic", "region": "nucleus", "duration": 1.0},
             {"verb": "draw", "target": "pic", "region": "vacuole", "duration": 1.0, "at": {"phrase": "wall"}}],
            region_order=["nucleus", "vacuole"]), asset)
        names = [n for n, _hi in r.bound["pic"].raster.wash_units]
        assert names == ["__base", "nucleus", "vacuole"]
        draws = [ta for ta in r.timeline if ta.action.verb == "draw"]
        assert len(draws) == 2 and draws[1].start >= draws[0].end
        base_r, nuc_r, vac_r = r._wash_ready["pic"]
        assert base_r == pytest.approx(draws[0].end) and nuc_r == pytest.approx(draws[0].end)
        assert vac_r == pytest.approx(draws[1].end)
        between = r._state_at(draws[0].end + WASH_SECS + 0.05)["pic"].wash
        assert between[0] == 1.0 and between[1] == 1.0 and between[2] == 0.0
        owner = r.bound["pic"].raster.wash_owner
        assert owner.getpixel((115 - ox, 145 - oy)) == 1, "the nucleus owns what its outline encloses"
        assert owner.getpixel((285 - ox, 150 - oy)) == 2, "the vacuole owns what its outline encloses"
        assert owner.getpixel((60 - ox, 90 - oy)) == 0, "the wall's fill inside the nucleus BOX stays the base's"
        assert owner.getpixel((30 - ox, 150 - oy)) == 0
        # on the board: after the first wash the wall and nucleus are coloured, the vacuole is still white
        from spike.scene_engine.paper import make_background
        from spike.scene_engine.render import SS, WORLD_H, WORLD_W
        import numpy as np
        w, h = WORLD_W * SS, WORLD_H * SS
        frame = r._frame(draws[0].end + WASH_SECS + 0.05, make_background(w, h, r.scene.style.background), w, h)
        a = np.asarray(frame.convert("RGB")).astype(int)
        blue = ((a[..., 2] > 200) & (a[..., 0] < 180) & (a[..., 1] > 170) & (a[..., 1] < 225)).sum()
        purple = ((a[..., 0] > 120) & (a[..., 0] < 175) & (a[..., 1] < 140) & (a[..., 2] > 170)).sum()
        assert purple > 500 and blue < 50, (purple, blue)

    def test_the_benchmark_frame_is_untouched(self):
        """An ink asset: no wash, no schedule, the same state as before."""
        from spike.scene_engine import raster_assets as ra
        art = TestThePictures._filled_art(fill=None)
        asset = ra._finish("cell", ra.to_ink(art), {})
        r = self._render(self._scene([{"verb": "draw", "target": "pic", "duration": 1.0}]), asset)
        assert r._wash_ready == {} and r.bound["pic"].raster.wash is None
        assert r._state_at(3.0)["pic"].wash == ()
