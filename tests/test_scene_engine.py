"""Scene engine tests — schema, timing, camera, geometry, fallback, RTL, encode args.

Everything here runs WITHOUT ffmpeg, network, or an API key (the repo's test
seam convention): the encoder is tested at the argv level, assets via the
authored vector tier, and the AI path via its offline fallback behaviour.
The real-render integration test lives in test_scene_engine_render.py and
skips itself when ffmpeg is unavailable.
"""

from __future__ import annotations

import math

import pytest

from spike.scene_engine import geometry as G
from spike.scene_engine.camera import CameraState, CameraTrack
from spike.scene_engine.encode import encode_args
from spike.scene_engine.raster_assets import make_resolver
from spike.scene_engine.render import SceneRenderer
from spike.scene_engine.schema import (Cue, Scene, UnsupportedSchemaVersion,
                                       parse_scene, scene_warnings)
from spike.scene_engine.scenes_demo import demo_scenes
from spike.scene_engine.timing import (TimedAction, animation_end,
                                       compile_timeline, natural_duration,
                                       resolve_cue)
from spike.scene_engine.vector_assets import known_vector_assets, vector_asset


def _mini_scene(**over) -> Scene:
    data = {
        "id": "t", "narration": "the wall blocks larger particles today",
        "elements": [
            {"id": "box", "type": "shape", "shape": "path",
             "points": [(100, 100), (300, 100), (300, 300)]},
            {"id": "lbl", "type": "text", "text": "Wall", "at": (400, 120)},
        ],
        "actions": [
            {"verb": "draw", "target": "box"},
            {"verb": "write", "target": "lbl", "at": {"phrase": "blocks"}},
        ],
    }
    data.update(over)
    return Scene.model_validate(data)


# ── schema ───────────────────────────────────────────────────────────────────

class TestSchema:
    def test_valid_scene_parses(self):
        s = _mini_scene()
        assert s.schema_version.split(".")[0] == "1"
        assert len(s.elements) == 2

    def test_unknown_action_target_rejected(self):
        with pytest.raises(Exception, match="unknown element"):
            _mini_scene(actions=[{"verb": "draw", "target": "ghost"}])

    def test_duplicate_element_ids_rejected(self):
        with pytest.raises(Exception, match="duplicate"):
            _mini_scene(elements=[
                {"id": "a", "type": "text", "text": "x", "at": (0, 0)},
                {"id": "a", "type": "text", "text": "y", "at": (0, 0)},
            ])

    def test_empty_elements_rejected(self):
        with pytest.raises(Exception, match="no elements"):
            _mini_scene(elements=[], actions=[])

    def test_unknown_major_version_rejected(self):
        with pytest.raises(UnsupportedSchemaVersion):
            parse_scene({"schema_version": "2.0", "id": "x", "narration": "n",
                         "elements": [], "actions": []})

    def test_newer_minor_version_accepted(self):
        s = parse_scene(_mini_scene().model_dump() | {"schema_version": "1.7"})
        assert s.id == "t"

    def test_cue_needs_exactly_one_field(self):
        with pytest.raises(Exception):
            Cue()
        with pytest.raises(Exception):
            Cue(phrase="x", sec=1.0)
        assert Cue(frac=2.0).frac == 1.0  # clamped, not rejected

    def test_zoom_scale_clamped_by_schema(self):
        with pytest.raises(Exception):
            _mini_scene(actions=[{"verb": "zoom", "scale": 9.0}])

    def test_label_text_clamped_to_80_chars(self):
        s = _mini_scene(elements=[
            {"id": "lbl", "type": "text", "text": "x" * 500, "at": (0, 0)}],
            actions=[])
        assert len(s.elements[0].text) == 80

    def test_particles_clamped_to_24(self):
        s = _mini_scene(elements=[
            {"id": "p", "type": "particles", "spawn": [(i, i) for i in range(80)]}],
            actions=[])
        assert len(s.elements[0].spawn) == 24

    def test_morph_into_must_exist(self):
        with pytest.raises(Exception, match="morph into unknown"):
            _mini_scene(actions=[
                {"verb": "morph", "target": "box", "into": "nope"}])

    def test_warnings_flag_text_heavy_and_missing_draw(self):
        s = _mini_scene(
            elements=[{"id": f"t{i}", "type": "text", "text": "w" * 60,
                       "at": (0, 30 * i)} for i in range(6)],
            actions=[{"verb": "write", "target": "t0"}])
        w = scene_warnings(s)
        assert any("text-heavy" in x for x in w)
        assert any("no draw action" in x for x in w)

    def test_demo_scenes_valid_and_warning_free(self):
        for s in demo_scenes():
            assert scene_warnings(s) == []


# ── timing ───────────────────────────────────────────────────────────────────

class TestTiming:
    def test_phrase_cue_resolves_at_char_midpoint(self):
        n = "aaaa blocks zzzz"
        t = resolve_cue(Cue(phrase="blocks"), n, 16.0)
        mid = (n.find("blocks") + 3) / len(n)
        assert t == pytest.approx(mid * 16.0)

    def test_unknown_phrase_returns_none(self):
        assert resolve_cue(Cue(phrase="nope"), "abc", 10.0) is None

    def test_frac_and_sec_cues(self):
        assert resolve_cue(Cue(frac=0.5), "x", 20.0) == 10.0
        assert resolve_cue(Cue(sec=3.0), "x", 20.0) == 3.0

    def test_uncued_actions_chain_sequentially(self):
        s = _mini_scene(actions=[
            {"verb": "draw", "target": "box", "duration": 2.0},
            {"verb": "write", "target": "lbl", "duration": 1.0},
        ])
        tl = compile_timeline(s, 60.0)
        assert tl[1].start >= tl[0].end

    def test_timeline_compresses_to_fit_narration_minus_hold(self):
        s = _mini_scene(actions=[
            {"verb": "draw", "target": "box", "duration": 6.0},
            {"verb": "draw", "target": "box", "duration": 6.0},
        ])
        tl = compile_timeline(s, 10.0)
        assert animation_end(tl) <= 10.0 - s.min_hold + 1e-6

    def test_compression_floor_protects_pace_and_encoder_pads(self):
        # grossly overloaded scene: compression stops at the 35% floor and the
        # clip simply runs past the narration (total_secs = anim + 0.2 rule)
        s = _mini_scene(actions=[
            {"verb": "draw", "target": "box", "duration": 20.0},
            {"verb": "draw", "target": "box", "duration": 20.0},
        ])
        tl = compile_timeline(s, 10.0)
        natural = 40.0
        assert animation_end(tl) >= natural * 0.35 - 1e-6
        assert animation_end(tl) > 10.0  # overran audio; -t covers it downstream

    def test_workload_drives_draw_duration(self):
        s = _mini_scene()
        short = natural_duration(s.actions[0], 550.0)
        long = natural_duration(s.actions[0], 2750.0)
        assert long == pytest.approx(short * 5, rel=0.01)
        assert natural_duration(s.actions[0], 1e9) <= 7.0  # clamped

    def test_explicit_duration_wins_over_workload(self):
        s = _mini_scene(actions=[{"verb": "draw", "target": "box", "duration": 3.3}])
        assert natural_duration(s.actions[0], 99999.0) == 3.3

    def test_cued_action_never_starts_before_previous(self):
        # cue phrase near t=0 but listed AFTER a long first action
        s = _mini_scene(
            narration="wall wall wall wall blocks",
            actions=[
                {"verb": "draw", "target": "box", "duration": 5.0,
                 "at": {"sec": 4.0}},
                {"verb": "write", "target": "lbl", "at": {"phrase": "wall"}},
            ])
        tl = compile_timeline(s, 30.0)
        assert tl[1].start >= tl[0].start

    def test_silent_scene_timeline_uncompressed(self):
        s = _mini_scene(actions=[
            {"verb": "draw", "target": "box", "duration": 6.0}])
        tl = compile_timeline(s, 0.0)
        assert animation_end(tl) >= 6.0


# ── camera ───────────────────────────────────────────────────────────────────

class TestCamera:
    def test_identity_transform(self):
        c = CameraState()
        assert c.to_screen((640, 360)) == (640, 360)

    def test_zoom_maps_center_to_screen_center(self):
        c = CameraState(800, 300, 2.0)
        assert c.to_screen((800, 300)) == (640, 360)
        # a point 100px right of focus lands 200px right on screen
        assert c.to_screen((900, 300)) == (840, 360)

    def test_clamp_keeps_viewport_inside_world(self):
        c = CameraState(0, 0, 2.0).clamped()
        assert c.cx == 320 and c.cy == 180

    def test_track_eases_between_states_and_resets(self):
        acts = Scene.model_validate({
            "id": "c", "narration": "n",
            "elements": [{"id": "e", "type": "text", "text": "x", "at": (100, 100)}],
            "actions": [
                {"verb": "zoom", "scale": 2.0, "center": (900, 500),
                 "at": {"sec": 1.0}, "duration": 1.0},
                {"verb": "camera_reset", "at": {"sec": 4.0}, "duration": 1.0},
            ]}).actions
        tl = [TimedAction(acts[0], 1.0, 1.0), TimedAction(acts[1], 4.0, 1.0)]
        tr = CameraTrack(tl)
        assert tr.state_at(0.0).scale == 1.0
        assert tr.state_at(2.5).scale == pytest.approx(2.0)
        mid = tr.state_at(1.5).scale
        assert 1.0 < mid < 2.0
        assert tr.state_at(6.0).scale == 1.0


# ── geometry ─────────────────────────────────────────────────────────────────

class TestGeometry:
    def test_cut_at_fraction_is_arc_length_exact(self):
        pts = [(0, 0), (10, 0), (10, 10)]  # length 20
        cut = G.cut_at_fraction(pts, 0.75)
        assert cut[-1] == pytest.approx((10, 5))

    def test_cut_bounds(self):
        pts = [(0, 0), (10, 0)]
        assert G.cut_at_fraction(pts, 0.0) == [(0, 0)]
        assert G.cut_at_fraction(pts, 1.0) == [(0, 0), (10, 0)]

    def test_roughen_is_deterministic_and_pins_endpoints(self):
        base = G.resample([(0.0, 0.0), (300.0, 0.0)], 5.0)
        a = G.roughen(base, seed=42)
        b = G.roughen(base, seed=42)
        assert a == b
        assert a != G.roughen(base, seed=43)
        assert a[0] == pytest.approx(base[0])
        assert a[-1] == pytest.approx(base[-1])

    def test_resample_spacing(self):
        pts = G.resample([(0.0, 0.0), (100.0, 0.0)], 10.0)
        gaps = [G.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
        assert all(g <= 10.0 + 1e-6 for g in gaps)

    def test_easing_monotonic(self):
        for name in ("linear", "ease_in", "ease_out", "ease_in_out"):
            vals = [G.ease(name, t / 20) for t in range(21)]
            assert vals == sorted(vals)
            assert vals[0] == 0.0 and vals[-1] == pytest.approx(1.0)


# ── assets & fallback ────────────────────────────────────────────────────────

class TestAssets:
    def test_authored_assets_exist_with_layers(self):
        assert set(known_vector_assets()) >= {"plant_cell", "membrane_section"}
        cell = vector_asset("plant_cell")
        assert {"wall", "membrane", "nucleus", "vacuole",
                "chloroplasts"} <= set(cell.layer_ids())
        assert cell.ink_length(["wall"]) > cell.ink_length(["nucleus"]) > 0

    def test_unknown_asset_returns_none(self):
        assert vector_asset("volcano") is None

    def test_resolver_falls_back_to_vector_without_credentials(self, tmp_path,
                                                               monkeypatch):
        """§20: AI failure never fails the lesson.

        The visual library is now a SECOND source of rasters, independent of
        the image credentials — on a developer machine its local index holds
        the whole scene-asset cache, so this test began passing a raster back
        and failing. That is the library working, not a fallback bug. Isolate
        it, so what is under test is still the credential path.
        """
        import shared.visual_library as vl
        for var in ("GOOGLE_AI_API_KEY", "GEMINI_API_KEY", "VERTEX_PROJECT_ID",
                    "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "empty_library")
        resolve = make_resolver({"plant_cell": "a cell"}, prefer_ai=True,
                                cache_dir=tmp_path)
        kind, asset = resolve("plant_cell")
        assert kind == "vector"

    def test_resolver_unknown_key_is_none(self, tmp_path):
        resolve = make_resolver({}, prefer_ai=False, cache_dir=tmp_path)
        assert resolve("volcano") is None


# ── renderer semantics ───────────────────────────────────────────────────────

class TestRenderer:
    def test_introduced_elements_start_hidden(self):
        r = SceneRenderer(_mini_scene())
        r.compile(20.0)
        st0 = r._state_at(0.0)
        assert st0["box"].visible is False    # draw target: hidden until drawn
        assert st0["lbl"].visible is False

    def test_draw_reveals_by_arc_length_over_time(self):
        r = SceneRenderer(_mini_scene())
        tl = r.compile(20.0)
        t_draw = tl[0]
        mid = r._state_at(t_draw.start + t_draw.duration / 2)["box"]
        end = r._state_at(t_draw.end + 0.01)["box"]
        assert 0.0 < max(mid.reveal.values()) < 1.0
        assert all(v == 1.0 for v in end.reveal.values())

    def test_missing_asset_drops_element_not_scene(self):
        # §20: an unresolvable asset drops ITS element; the scene survives
        s = _mini_scene(elements=[
            {"id": "x", "type": "illustration", "asset": "volcano", "at": (0, 0)},
            {"id": "t", "type": "text", "text": "Still here", "at": (400, 300)}],
            actions=[{"verb": "draw", "target": "x"},
                     {"verb": "write", "target": "t"}])
        r = SceneRenderer(s)          # must NOT raise
        tl = r.compile(10.0)
        assert r._state_at(tl[-1].end + 0.1)["t"].text_frac == 1.0

    def test_move_with_stop_frac_blocks_short_of_path_end(self):
        s = _mini_scene(
            elements=[{"id": "p", "type": "particles", "spawn": [(0.0, 0.0)]}],
            actions=[
                {"verb": "reveal", "target": "p"},
                {"verb": "move", "target": "p", "duration": 2.0, "stop_frac": 0.5,
                 "path": [(0, 0), (100, 0)], "easing": "linear"},
            ])
        r = SceneRenderer(s)
        tl = r.compile(30.0)
        end = r._state_at(tl[1].end + 2.0)["p"]
        x = end.particle_off[0][0]
        assert x <= 51.0                      # never passes the block point
        assert x >= 40.0                      # ...but reached it (with recoil)

    def test_rtl_text_reveals_from_the_right(self):
        s = _mini_scene(
            elements=[{"id": "ar", "type": "text", "text": "مرحبا بالعالم",
                       "at": (600, 300), "direction": "rtl"}],
            actions=[{"verb": "write", "target": "ar", "duration": 2.0}])
        r = SceneRenderer(s)
        assert r.bound["ar"].text.rtl is True
        assert r.bound["ar"].text.display != "مرحبا بالعالم"  # shaped for PIL
        tl = r.compile(10.0)
        # frontier moves right -> left as the write progresses
        f_early = r._frontier(0, tl[0], tl[0].start + 0.2 * tl[0].duration)
        f_late = r._frontier(0, tl[0], tl[0].start + 0.9 * tl[0].duration)
        assert f_early[0] > f_late[0]

    def test_frame_renders_and_is_deterministic(self):
        import itertools
        s = _mini_scene()
        imgs = []
        for _ in range(2):
            r = SceneRenderer(s)
            r.compile(6.0)
            img = next(itertools.islice(r.frames(6.0), 30, 31))
            imgs.append(img)
        assert imgs[0].size == (1280, 720)
        assert list(imgs[0].getdata()) == list(imgs[1].getdata())

    def test_total_secs_rules(self):
        r = SceneRenderer(_mini_scene())
        r.compile(30.0)
        assert r.total_secs(30.0) == 30.0                       # audio wins
        anim = animation_end(r.timeline)
        assert r.total_secs(0.0) >= anim + r.scene.min_hold     # silent: anim+hold
        r2 = SceneRenderer(_mini_scene())
        r2.compile(1.0)
        assert r2.total_secs(1.0) >= animation_end(r2.timeline) + 0.2


# ── regression: adversarial-review findings (2026-08-31) ────────────────────

class TestReviewRegressions:
    def test_seeds_are_process_stable_not_hash(self):
        # hash() is salted per process; wobble seeds must be crc32-stable
        import zlib
        from spike.scene_engine.render import _seed
        assert _seed("ar_wall") == zlib.crc32(b"ar_wall") & 0xFFFF
        import inspect
        from spike.scene_engine import render as rmod
        assert "hash(el.id)" not in inspect.getsource(rmod)

    def test_draw_on_group_reveals_children(self):
        s = _mini_scene(
            elements=[
                {"id": "a", "type": "shape", "shape": "path",
                 "points": [(0, 0), (100, 0)]},
                {"id": "b", "type": "text", "text": "Hi", "at": (200, 200)},
                {"id": "g", "type": "group", "children": ["a", "b"]},
            ],
            actions=[{"verb": "draw", "target": "g", "duration": 1.0},
                     {"verb": "write", "target": "g", "duration": 0.5}])
        r = SceneRenderer(s)
        tl = r.compile(20.0)
        end = r._state_at(tl[-1].end + 0.1)
        assert end["a"].visible and end["a"].reveal.get(0) == 1.0
        assert end["b"].visible and end["b"].text_frac == 1.0

    def test_group_box_is_union_of_children(self):
        s = _mini_scene(
            elements=[
                {"id": "a", "type": "shape", "shape": "path",
                 "points": [(10, 10), (50, 50)]},
                {"id": "b", "type": "shape", "shape": "path",
                 "points": [(300, 300), (400, 350)]},
                {"id": "g", "type": "group", "children": ["a", "b"]},
            ], actions=[{"verb": "circle", "target": "g"}])
        r = SceneRenderer(s)
        x0, y0, x1, y1 = r.bound["g"].box
        assert x0 <= 10 and y0 <= 10 and x1 >= 400 and y1 >= 350

    def test_overlapping_camera_cues_never_teleport(self):
        s = _mini_scene(actions=[
            {"verb": "zoom", "scale": 2.0, "center": (900, 500),
             "at": {"sec": 5.0}, "duration": 1.2},
            {"verb": "camera_reset", "at": {"sec": 5.5}, "duration": 1.0},
        ])
        r = SceneRenderer(s)
        r.compile(20.0)
        last = r.cam.state_at(0.0).scale
        for i in range(1, 400):  # 1/24s steps across the overlap window
            cur = r.cam.state_at(i / 24).scale
            assert abs(cur - last) < 0.25, f"camera jump at t={i/24:.2f}"
            last = cur

    def test_silent_scene_ignores_cues_and_sequences(self):
        s = _mini_scene(actions=[
            {"verb": "draw", "target": "box", "duration": 1.0,
             "at": {"sec": 7.0}},
            {"verb": "write", "target": "lbl", "duration": 1.0,
             "at": {"frac": 0.9}},
        ])
        tl = compile_timeline(s, 0.0)
        assert tl[0].start < 1.0            # not parked at sec=7
        assert tl[1].start >= tl[0].end     # plain sequence order

    def test_cue_sec_bounded_by_schema_and_timing(self):
        with pytest.raises(Exception):
            Cue(sec=5000.0)
        assert resolve_cue(Cue(sec=890.0), "x", 30.0) <= 40.0  # audio + 10

    def test_total_secs_is_frame_grid_aligned(self):
        r = SceneRenderer(_mini_scene())
        r.compile(10.37)
        for audio in (10.37, 0.0, 3.111):
            t = r.total_secs(audio)
            assert abs(t * 24 - round(t * 24)) < 1e-6

    def test_particle_stagger_completes_within_action_end(self):
        s = _mini_scene(
            elements=[{"id": "p", "type": "particles",
                       "spawn": [(0.0, 0.0), (30.0, 0.0), (60.0, 0.0)]}],
            actions=[
                {"verb": "reveal", "target": "p"},
                {"verb": "move", "target": "p", "duration": 2.0, "stagger": 0.9,
                 "path": [(0, 0), (100, 0)], "easing": "linear"},
            ])
        r = SceneRenderer(s)
        tl = r.compile(30.0)
        at_end = r._state_at(tl[1].end + 1e-3)["p"]
        for off in at_end.particle_off:
            assert off[0] == pytest.approx(100.0, abs=1.0)

    def test_zero_length_arrow_degrades_not_crashes(self):
        from spike.scene_engine.geometry import arrow_paths
        paths = arrow_paths((100.0, 100.0), (100.0, 100.0))
        assert len(paths) == 3 and all(len(p) >= 2 for p in paths)

    def test_director_rejects_non_object_json(self):
        from spike.scene_engine.director import parse_scene_response
        assert parse_scene_response("[1,2,3]", "narr") is None
        assert parse_scene_response('"hello"', "narr") is None
        assert parse_scene_response("not json at all {", "narr") is None

    def test_latin_text_in_rtl_scene_still_writes_ltr(self):
        s = _mini_scene(
            direction="rtl",
            elements=[{"id": "en", "type": "text", "text": "Photosynthesis",
                       "at": (400, 300)}],
            actions=[{"verb": "write", "target": "en", "duration": 2.0}])
        r = SceneRenderer(s)
        assert r.bound["en"].text.rtl is True       # layout side
        assert r.bound["en"].text.shaped is False   # but glyphs are Latin
        tl = r.compile(10.0)
        f_early = r._frontier(0, tl[0], tl[0].start + 0.2 * tl[0].duration)
        f_late = r._frontier(0, tl[0], tl[0].start + 0.9 * tl[0].duration)
        assert f_late[0] > f_early[0]               # frontier moves rightward

    def test_erase_then_redraw_recovers(self):
        s = _mini_scene(actions=[
            {"verb": "draw", "target": "box", "duration": 1.0},
            {"verb": "erase", "target": "box", "duration": 0.5},
            {"verb": "draw", "target": "box", "duration": 1.0},
        ])
        r = SceneRenderer(s)
        tl = r.compile(30.0)
        mid = r._state_at(tl[1].end + 0.05)["box"]
        assert mid.erase > 0.0 or mid.reveal.get(0, 0) < 1.0  # erased state
        end = r._state_at(tl[2].end + 0.1)["box"]
        assert end.erase == 0.0 and end.reveal.get(0) == 1.0  # redrawn

    def test_to_ink_luminance_does_not_overflow(self):
        # regression: int16 luminance wrapped negative on bright pixels, so a
        # WHITE image scored 100% ink coverage and every asset was rejected
        import numpy as np
        from PIL import Image as PILImage
        from spike.scene_engine.raster_assets import to_ink
        white = PILImage.new("RGB", (64, 64), (255, 255, 255))
        a = np.asarray(to_ink(white).getchannel("A"))
        assert float((a > 128).mean()) < 0.01
        from PIL import ImageDraw as PILDraw
        art = PILImage.new("RGB", (200, 200), (255, 255, 255))
        PILDraw.Draw(art).ellipse([40, 40, 160, 160], outline=(0, 0, 0), width=5)
        a2 = np.asarray(to_ink(art).getchannel("A"))
        assert 0.005 <= float((a2 > 128).mean()) <= 0.45  # sane line-art band

    def test_ink_coverage_sanity_rejects_non_line_art(self, tmp_path,
                                                      monkeypatch):
        import numpy as np
        from PIL import Image as PILImage
        from spike.scene_engine import raster_assets as ra
        black = PILImage.new("RGB", (256, 256), (0, 0, 0))
        import io
        buf = io.BytesIO()
        black.save(buf, "PNG")
        monkeypatch.setattr(ra, "_vertex_call", lambda p: buf.getvalue())
        monkeypatch.setattr(ra, "_aistudio_call", lambda p: None)
        assert ra.get_raster_asset("k", "prompt", tmp_path) is None

    def test_frames_twice_same_renderer_is_deterministic(self, tmp_path):
        import itertools
        import numpy as np
        from PIL import Image as PILImage, ImageDraw as PILDraw
        from spike.scene_engine.raster_assets import RasterAsset
        from spike.scene_engine.trace import drawing_order
        ink = PILImage.new("RGBA", (200, 150), (0, 0, 0, 0))
        d = PILDraw.Draw(ink)
        d.ellipse([30, 30, 170, 120], outline=(20, 20, 20, 255), width=6)
        trace = drawing_order(np.asarray(ink.getchannel("A")))
        asset = RasterAsset("disc", ink, trace, 4.0, 2.0)
        s = _mini_scene(
            elements=[{"id": "im", "type": "illustration", "asset": "disc",
                       "at": (640, 360)}],
            actions=[{"verb": "draw", "target": "im", "duration": 1.0}])
        resolver = lambda k: ("raster", asset)
        datas = []
        r = SceneRenderer(s, asset_resolver=resolver)
        r.compile(4.0)
        for _ in range(2):
            img = next(itertools.islice(r.frames(4.0), 12, 13))
            datas.append(list(img.getdata()))
        assert datas[0] == datas[1]


# ── regression: font-drift class (anchors / chaining / camera-follow) ────────

class TestAnchoredGeometry:
    def _scene(self):
        return Scene.model_validate({
            "id": "a", "narration": "split five x into parts now",
            "elements": [
                {"id": "title", "type": "text", "text": "Factorise:  x2 + 5x + 6",
                 "at": (80, 60), "role": "title", "size": 38, "anchor": "lt"},
                {"id": "f1", "type": "text", "text": "x2 + ", "at": (120, 250),
                 "size": 36, "anchor": "lt"},
                {"id": "f2", "type": "text", "text": "2x", "at": (0, 250),
                 "size": 36, "anchor": "lt", "after": {"el": "f1", "gap": 2}},
                {"id": "ar", "type": "arrow", "curve": 0,
                 "tail": {"el": "title", "sub": "5x", "edge": "bottom", "dy": 6},
                 "head": {"el": "f2", "edge": "top", "dy": -6}},
            ],
            "actions": [{"verb": "write", "target": "title"},
                        {"verb": "draw", "target": "ar"}],
        })

    def test_arrow_tail_sits_under_the_substring_at_any_font(self):
        r = SceneRenderer(self._scene())
        b = r.bound["title"]
        sb = r._sub_box(b, "5x")
        assert sb is not None
        # the substring box lies strictly inside the title box, right of centre
        assert b.box[0] < sb[0] < sb[2] < b.box[2]
        tail = r._resolve_point(r.bound["ar"].element.tail)
        assert sb[0] <= tail[0] <= sb[2]          # under "5x", wherever it is
        assert tail[1] == pytest.approx(sb[3] + 6)

    def test_arrow_head_tracks_chained_fragment(self):
        r = SceneRenderer(self._scene())
        f1, f2 = r.bound["f1"], r.bound["f2"]
        assert f2.box[0] == pytest.approx(f1.box[2] + 2)   # chained, not authored
        head = r._resolve_point(r.bound["ar"].element.head)
        assert f2.box[0] <= head[0] <= f2.box[2]

    def test_rtl_chaining_grows_leftward(self):
        s = Scene.model_validate({
            "id": "r", "narration": "n", "direction": "rtl",
            "elements": [
                {"id": "a", "type": "text", "text": "مرحبا", "at": (900, 200)},
                {"id": "b", "type": "text", "text": "بالعالم", "at": (0, 200),
                 "after": {"el": "a", "gap": 4}},
            ],
            "actions": [{"verb": "write", "target": "a"}]})
        r = SceneRenderer(s)
        assert r.bound["b"].box[2] == pytest.approx(r.bound["a"].box[0] - 4)

    def test_anchor_to_unknown_element_rejected(self):
        with pytest.raises(Exception, match="anchors to unknown"):
            Scene.model_validate({
                "id": "x", "narration": "n",
                "elements": [{"id": "ar", "type": "arrow",
                              "tail": {"el": "ghost"}, "head": (5, 5)}],
                "actions": [{"verb": "draw", "target": "ar"}]})

    def test_after_must_reference_earlier_element(self):
        with pytest.raises(Exception, match="EARLIER"):
            Scene.model_validate({
                "id": "x", "narration": "n",
                "elements": [
                    {"id": "b", "type": "text", "text": "b", "at": (0, 0),
                     "after": {"el": "c", "gap": 2}},
                    {"id": "c", "type": "text", "text": "c", "at": (0, 0)},
                ], "actions": [{"verb": "write", "target": "b"}]})

    def test_zoom_with_no_center_follows_next_draw(self):
        s = Scene.model_validate({
            "id": "z", "narration": "watch this corner closely now",
            "elements": [{"id": "sh", "type": "shape", "shape": "path",
                          "points": [(900, 500), (1100, 640)]}],
            "actions": [
                {"verb": "zoom", "scale": 1.6, "at": {"sec": 1.0}},
                {"verb": "draw", "target": "sh"},
            ]})
        r = SceneRenderer(s)
        r.compile(20.0)
        state = r.cam.state_at(10.0)
        assert state.scale == pytest.approx(1.6)
        assert state.cx > 700           # camera went where the ink goes
        assert state.cy > 400

    def test_demo_scenes_still_valid_after_migration(self):
        from spike.scene_engine.scenes_math import factorise_scene
        from spike.scene_engine.scenes_physics import newton_scene
        for s in [factorise_scene(), newton_scene()] + demo_scenes():
            SceneRenderer(s).compile(40.0)   # binds + compiles without error


# ── encoder contract ─────────────────────────────────────────────────────────

class TestEncodeContract:
    def test_codec_contract_pinned(self, tmp_path):
        args = encode_args(12.34, "voice.mp3", tmp_path / "o.mp4")
        s = " ".join(args)
        for token in ("-c:v libx264", "-pix_fmt yuv420p", "-r 24",
                      "-c:a aac", "-b:a 128k", "-ar 44100", "-ac 2",
                      "-movflags +faststart", "-t 12.34", "1280x720"):
            assert token in s, token
        assert "-shortest" not in s          # audit fact 3

    def test_silent_scene_gets_real_audio_track(self, tmp_path):
        args = encode_args(5.0, None, tmp_path / "o.mp4")
        assert any("anullsrc" in a for a in args)
        assert "-map" in args                # audio still mapped in


# ── render speed: exact caches (2026-09-04) ─────────────────────────────────

class TestHandSpriteCache:
    """The hand is a uniform scale of ONE image; resizing it on every pen
    frame cost 11-16 ms. The per-(w, h) cache must be pixel-identical to the
    resize it replaces, and must resize exactly once per size."""

    def _hand(self):
        from spike.scene_engine.raster_assets import load_hand
        loaded = load_hand("hand_pen", allow_generate=False)
        assert loaded is not None, "bundled hand missing"
        return loaded

    def test_cached_stamp_is_pixel_identical_to_a_fresh_sprite(self):
        from PIL import Image
        from spike.scene_engine.pen import PenSprite
        loaded = self._hand()
        loader = lambda k: loaded  # noqa: E731
        cached = PenSprite(loader)
        stamps = [(300.0, 400.0), (900.0, 650.0)]
        for x, y in stamps:
            fresh_sprite = PenSprite(loader)
            a = Image.new("RGB", (1280, 720), (250, 250, 248))
            b = Image.new("RGB", (1280, 720), (250, 250, 248))
            fresh_sprite.stamp(a, "hand", x, y, 2, scale=0.8)
            cached.stamp(b, "hand", x, y, 2, scale=0.8)
            assert list(a.getdata()) == list(b.getdata())
        assert len(cached._scaled) == 1

    def test_the_resize_runs_once_per_size(self, monkeypatch):
        from PIL import Image
        from spike.scene_engine.pen import PenSprite
        loaded = self._hand()
        calls = []
        real = Image.Image.resize
        depth = [0]

        def counting(self, *a, **k):
            # Pillow's resize re-enters itself (a second call carrying a
            # `box`); count only the outermost call, i.e. OUR resize
            if depth[0] == 0:
                calls.append(a[0] if a else k.get("size"))
            depth[0] += 1
            try:
                return real(self, *a, **k)
            finally:
                depth[0] -= 1
        monkeypatch.setattr(Image.Image, "resize", counting)
        sp = PenSprite(lambda k: loaded)
        frame = Image.new("RGB", (1280, 720), (250, 250, 248))
        for i in range(5):
            sp.stamp(frame, "hand", 200.0 + 40 * i, 300.0, 2, scale=0.8)
        assert len(calls) == 1
        sp.stamp(frame, "hand", 200.0, 300.0, 2, scale=1.0)   # a new size
        assert len(calls) == 2
        sp.stamp(frame, "hand", 250.0, 300.0, 2, scale=1.0)
        assert len(calls) == 2
