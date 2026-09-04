"""The four render defects the founder saw on the first Google-voiced lesson
(generation 669e84f0, 2026-09-04): the card slides lagging their narration,
one lesson drawn with two different pens, a corner sketch cut off by the
frame, and a teacher avatar regenerated on every deploy instead of taken from
the library. Each class pins the mechanism the investigation found."""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw


# ── 1. card slides: the underline must not drag the bullets to 90% ──────────

class TestCardTiming:
    def _card(self, n=3):
        from spike.scene_engine.director import parse_scene_response
        from spike.scene_engine.whiteboard import build_whiteboard_scene
        seg = {"segment_id": "s001", "type": "explore", "slide_heading": "Beyond Plant Cells",
               "slide_points": [f"Point number {i} about cells" for i in range(n)],
               "text": "Cells are the building blocks of life. What about animal cells? "
                       "They have no wall. They have no chloroplasts."}
        card = build_whiteboard_scene(seg)
        scene = parse_scene_response(card, seg["text"])
        assert scene is not None
        return scene

    def test_bullets_start_early_and_the_card_ends_inside_the_audio(self):
        from spike.scene_engine import timing as T
        scene = self._card(3)
        audio = 20.0
        tl = [t for t in T.compile_timeline(scene, audio) if not T._is_caption(t.action)]
        by_target = {}
        for t in tl:
            by_target.setdefault((t.action.verb, t.action.target), t)
        first_dash = by_target[("draw", "wb_d0")]
        assert first_dash.start < 0.3 * audio, \
            f"the first bullet dash waited until {first_dash.start:.1f}s of {audio}s — the card sat idle"
        writes = [by_target[("write", f"wb_p{i}")].start for i in range(3)]
        assert writes == sorted(writes), "bullets write in order"
        assert writes[0] < 0.4 * audio and writes[2] < 0.9 * audio
        assert T.animation_end(tl) <= audio + 1e-6, \
            "the board must finish inside the audio, not pile up after it"

    def test_a_sketch_on_a_contentless_card_is_cued_to_its_word(self):
        """A card with a heading never carries sketches (heading_taken), so the
        sketches live on contentless cards — where nothing late may hold them."""
        from spike.scene_engine import timing as T
        from spike.scene_engine.director import parse_scene_response
        from spike.scene_engine.whiteboard import build_whiteboard_scene
        seg = {"segment_id": "s002", "type": "explore",
               "text": "Think of a hammer driving a nail. Now think of a tree adding a ring each year. "
                       "Both take time. Both leave a record."}
        card = build_whiteboard_scene(seg)
        sketches = [a for a in card["actions"] if str(a.get("target", "")).startswith("sk_")]
        if not sketches:
            pytest.skip("the sketchables lexicon named nothing in this narration")
        scene = parse_scene_response(card, seg["text"])
        audio = 20.0
        tl = [t for t in T.compile_timeline(scene, audio) if not T._is_caption(t.action)]
        sk = [t for t in tl if str(t.action.target).startswith("sk_")]
        assert sk and min(t.start for t in sk) < 0.7 * audio

    def test_the_underline_follows_the_last_bullet_and_a_short_card_does_not_overshoot(self):
        from spike.scene_engine import timing as T
        scene = self._card(3)
        for audio in (20.0, 6.0):
            tl = [t for t in T.compile_timeline(scene, audio) if not T._is_caption(t.action)]
            board = [t for t in tl if str(t.action.target).startswith("wb_")]
            last = max(board, key=lambda t: t.start)
            assert (last.action.verb, last.action.target) == ("underline", "wb_h")
            bullets = [t for t in board if t.action.verb == "write" and t.action.target != "wb_h"]
            assert last.start >= max(t.start for t in bullets), "the underline comes after the last bullet"
            end = T.animation_end(tl)
            # the underline adds at most a breath after the last bullet — it no
            # longer anchors a tail of its own
            assert end - max(t.end for t in bullets) <= 1.0
            if audio >= 20:
                assert end <= audio, f"{audio}s card ends at {end:.1f}s"
            else:
                # KNOWN LIMIT: the bullet dashes are fraction-cued anchors and the
                # compressor cannot pull anchors, so a 6 s three-bullet card still
                # overruns (7.9 s here; 8.4 s before this change). Pinned so the
                # number cannot silently grow.
                assert end < 8.0, f"{audio}s card ends at {end:.1f}s"

    def test_a_big_card_keeps_its_early_underline(self):
        from spike.scene_engine import timing as T
        from spike.scene_engine.director import parse_scene_response
        from spike.scene_engine.whiteboard import build_whiteboard_scene
        seg = {"segment_id": "s003", "type": "hook", "slide_heading": "Beyond Plant Cells",
               "text": "Have you ever wondered what your own cells look like? Let us find out."}
        scene = parse_scene_response(build_whiteboard_scene(seg), seg["text"])
        tl = [t for t in T.compile_timeline(scene, 10.0) if not T._is_caption(t.action)]
        ul = next(t for t in tl if t.action.verb == "underline")
        assert 1.5 <= ul.start <= 4.5, "cued at 28% of a 10 s hook"


# ── 2. one pen per lesson ────────────────────────────────────────────────────

class TestOnePenPerLesson:
    def test_a_segment_loader_never_generates(self, tmp_path, monkeypatch):
        from spike.scene_engine import raster_assets as ra
        monkeypatch.setattr(ra, "_vertex_call", lambda p: (_ for _ in ()).throw(AssertionError("generated")))
        monkeypatch.setattr(ra, "_aistudio_call", lambda p: (_ for _ in ()).throw(AssertionError("generated")))
        assert ra.load_hand("hand_pen", tmp_path, allow_generate=False) is None

    def test_the_lesson_warms_the_hand_once_before_the_pool(self, tmp_path, monkeypatch):
        import agent6_animation.video_composer as vc
        from spike.scene_engine import raster_assets as ra
        calls = []
        monkeypatch.setattr(ra, "load_hand", lambda *a, **k: calls.append((a, k)) or None)
        monkeypatch.setenv("VIDEO_ENGINE", "scene")
        monkeypatch.setattr(vc, "VIDEO_DIR", tmp_path)
        monkeypatch.setattr(vc, "_MAX_RENDER_WORKERS", 2)
        monkeypatch.setattr(vc, "_ffmpeg_exe", lambda: "ffmpeg")
        monkeypatch.setattr(vc, "_audio_duration", lambda p, f: 2.0)
        monkeypatch.setattr(vc, "concepts_for_slides", lambda hs: ["c"] * len(hs))
        monkeypatch.setattr(vc, "_render_scene_segment",
                            lambda seg, narration, audio, secs, out, direction, scene_dict=None, avatars=None:
                            (Path(out).write_bytes(b"mp4"), True)[1])
        monkeypatch.setattr(vc, "render_native_segment", lambda *a, **k: True)
        monkeypatch.setattr("spike.scene_engine.whiteboard.build_whiteboard_scene",
                            lambda seg, avatars=None: {"stub": True})

        def fake_synth(text, out, *, report=None, **kw):
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"mp3")
            if report is not None:
                report.update({"used": "edge-aria", "provider": "edge", "downgraded": False,
                               "reason": None, "chars": 1, "stats": {}})
            return Path(out)

        monkeypatch.setattr(vc, "synthesize", fake_synth)
        segs = [{"segment_id": f"s{i}", "text": f"Narration {i}.", "slide_heading": "H",
                 "slide_points": ["p"], "estimated_duration_seconds": 3} for i in range(3)]
        script = {"episodes": [{"book_id": "bk", "chapter_num": 1, "episode_num": 1,
                                "episode_title": "Ep", "segments": segs}]}
        vc.compose_episode_videos(script, {"segments": [{"segment_id": s["segment_id"]} for s in segs]},
                                  tts_voice="edge-aria", allow_premium=False, lang="en")
        assert len(calls) == 1 and calls[0] == ((), {}), \
            "exactly one warm-up call, with generation allowed, before any segment"


# ── 3. corner sketches are screen-fixed ─────────────────────────────────────

def _disc_asset():
    from spike.scene_engine.raster_assets import RasterAsset
    from spike.scene_engine.trace import drawing_order
    ink = Image.new("RGBA", (200, 150), (0, 0, 0, 0))
    d = ImageDraw.Draw(ink)
    d.ellipse([30, 30, 170, 120], outline=(20, 20, 20, 255), width=6)
    trace = drawing_order(np.asarray(ink.getchannel("A")))
    return RasterAsset("disc", ink, trace, 4.0, 2.0)


def _corner_ink(frame: Image.Image, box=(20, 50, 260, 235)) -> int:
    g = np.asarray(frame.convert("L"))
    x0, y0, x1, y1 = box
    return int((g[y0:y1, x0:x1] < 110).sum())


class TestCornerSketchSurvivesZoom:
    def _render_last_frame(self, hud: bool):
        from spike.scene_engine.render import SceneRenderer
        from spike.scene_engine.schema import Scene
        sk = {"id": "sk_s001_0", "type": "illustration", "asset": "disc",
              "at": [136, 142], "scale": 0.32}
        if hud:
            sk["hud"] = True
        scene = Scene.model_validate({
            "id": "z", "narration": "look at the cell now",
            "elements": [
                {"id": "root", "type": "text", "text": "Cells", "size": 40, "at": [600, 380]},
                sk,
            ],
            "actions": [
                {"verb": "write", "target": "root", "duration": 0.8},
                {"verb": "zoom", "target": "root", "scale": 1.6, "duration": 0.8},
                {"verb": "draw", "target": "sk_s001_0", "duration": 1.0},
            ],
        })
        asset = _disc_asset()
        r = SceneRenderer(scene, asset_resolver=lambda k: ("raster", asset))
        r.compile(6.0)
        last = None
        for last in r.frames(6.0):
            pass
        return last

    def test_world_fixed_placement_is_what_the_camera_cut(self):
        from spike.scene_engine.camera import CameraState
        # the sketch's world box is inside the canvas; a 1.6x zoom on the
        # centre sends its top-left corner to negative screen coordinates
        assert CameraState(600, 380, 1.6).to_screen((80, 59)) == pytest.approx((-192, -153.6), abs=1)
        assert _corner_ink(self._render_last_frame(hud=False)) == 0, \
            "without the HUD flag the zoom flings the corner sketch off-canvas"

    def test_a_hud_sketch_stays_in_its_corner_through_the_zoom(self):
        frame = self._render_last_frame(hud=True)
        assert _corner_ink(frame) > 200, "the sketch is drawn in the top-left slot"
        g = np.asarray(frame.convert("L"))
        assert (g[:, :18] < 110).sum() == 0 and (g[:48, :] < 110).sum() == 0, \
            "nothing is cut by the left or top edge"

    def test_a_decoration_on_a_hud_sketch_stays_with_it_through_the_zoom(self):
        """Review pass: circle/underline/highlight strokes were still projected
        through the world camera, so a circle around a screen-fixed sketch
        flew off-canvas while the sketch stayed."""
        from spike.scene_engine.render import SceneRenderer
        from spike.scene_engine.schema import Scene
        asset = _disc_asset()

        def render(with_circle: bool):
            actions = [
                {"verb": "write", "target": "root", "duration": 0.8},
                {"verb": "zoom", "target": "root", "scale": 1.6, "duration": 0.8},
                {"verb": "draw", "target": "sk_s001_0", "duration": 1.0},
            ]
            if with_circle:
                actions.append({"verb": "circle", "target": "sk_s001_0", "duration": 0.8})
            scene = Scene.model_validate({
                "id": "c", "narration": "look at the cell now",
                "elements": [
                    {"id": "root", "type": "text", "text": "Cells", "size": 40, "at": [600, 380]},
                    {"id": "sk_s001_0", "type": "illustration", "asset": "disc",
                     "at": [136, 142], "scale": 0.32, "hud": True},
                ],
                "actions": actions,
            })
            r = SceneRenderer(scene, asset_resolver=lambda k: ("raster", asset))
            r.compile(6.0)
            last = None
            for last in r.frames(6.0):
                pass
            return last

        plain, circled = render(False), render(True)
        region = (0, 0, 320, 300)
        assert _corner_ink(circled, region) > _corner_ink(plain, region) + 50, \
            "the circle is drawn around the sketch on screen, not off-canvas"

    def test_a_follow_zoom_never_aims_at_a_hud_sketch(self):
        """Review pass: a zoom with no centre follows the next draw's WORLD
        position; a screen-fixed sketch's world slot is an empty corner."""
        from spike.scene_engine.render import SceneRenderer
        from spike.scene_engine.schema import Scene
        asset = _disc_asset()
        scene = Scene.model_validate({
            "id": "f", "narration": "look at the cell now",
            "elements": [
                {"id": "root", "type": "text", "text": "Cells", "size": 40, "at": [600, 380]},
                {"id": "sk_s001_0", "type": "illustration", "asset": "disc",
                 "at": [1114, 142], "scale": 0.40, "hud": True},
            ],
            "actions": [
                {"verb": "write", "target": "root", "duration": 0.8},
                {"verb": "zoom", "scale": 1.6, "duration": 0.8},
                {"verb": "draw", "target": "sk_s001_0", "duration": 1.0},
            ],
        })
        r = SceneRenderer(scene, asset_resolver=lambda k: ("raster", asset))
        r.compile(6.0)
        cam = r.cam.state_at(5.9)
        assert abs(cam.cx - 1114) > 150 or abs(cam.cy - 142) > 100, \
            f"the camera followed the sketch's world slot: {cam}"

    def test_an_explicit_zoom_onto_a_hud_sketch_does_not_frame_its_empty_slot(self):
        """Second review pass: `zoom target=<hud sketch>` framed the sketch's
        WORLD slot — an empty corner — while the sketch stayed on screen."""
        from spike.scene_engine.render import SceneRenderer
        from spike.scene_engine.schema import Scene
        asset = _disc_asset()
        scene = Scene.model_validate({
            "id": "t", "narration": "look at the cell now",
            "elements": [
                {"id": "root", "type": "text", "text": "Cells", "size": 40, "at": [600, 380]},
                {"id": "sk_s001_0", "type": "illustration", "asset": "disc",
                 "at": [1114, 142], "scale": 0.40, "hud": True},
            ],
            "actions": [
                {"verb": "draw", "target": "sk_s001_0", "duration": 1.0},
                {"verb": "zoom", "target": "sk_s001_0", "scale": 1.6, "duration": 0.8},
                {"verb": "write", "target": "root", "duration": 0.8},
            ],
        })
        r = SceneRenderer(scene, asset_resolver=lambda k: ("raster", asset))
        r.compile(6.0)
        cam = r.cam.state_at(5.9)
        assert abs(cam.cx - 1114) > 150 or abs(cam.cy - 142) > 100, \
            f"the camera framed the sketch's empty world slot: {cam}"

    def test_the_whiteboard_and_the_recap_declare_hud(self):
        from spike.scene_engine.whiteboard import sketch_elements
        els, acts, assets = sketch_elements("Look at the potted plant on the desk.", uid="s1",
                                            slots=[(136.0, 142.0, 0.32)])
        if els:   # the sketchables lexicon decides whether a plant is drawable
            assert all(e.get("hud") is True for e in els)
        src = Path("spike/scene_engine/continuity.py").read_text(encoding="utf-8")
        assert 'clean["hud"] = True' in src


# ── 4. the avatar comes from the roster ─────────────────────────────────────

def _png_bytes() -> bytes:
    img = Image.new("RGBA", (64, 64), (255, 255, 255, 0))
    ImageDraw.Draw(img).ellipse([8, 8, 56, 56], outline=(0, 0, 0, 255), width=4)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _fake_sb(rows: list[dict], calls: dict):
    class Q:
        def __init__(self):
            self.f = {}

        def select(self, *_a, **_k):
            return self

        def eq(self, col, val):
            self.f[f"eq:{col}"] = val
            return self

        def neq(self, col, val):
            self.f[f"neq:{col}"] = val
            return self

        def order(self, col, **_k):
            self.f["order"] = col
            return self

        def limit(self, _n):
            return self

        def insert(self, row):
            calls.setdefault("insert", []).append(row)
            return self

        def execute(self):
            calls.setdefault("queries", []).append(dict(self.f))
            if "eq:content_hash" in self.f:
                return type("R", (), {"data": []})()
            if "eq:canonical_key" in self.f:      # the roster lookup: by key, any type
                return type("R", (), {"data": [r for r in rows if r.get("canonical_key") == self.f["eq:canonical_key"]]})()
            if self.f.get("neq:asset_type") == "avatar":   # educational retrieval
                return type("R", (), {"data": [r for r in rows if r.get("asset_type") != "avatar"]})()
            return type("R", (), {"data": rows})()

    class Storage:
        def from_(self, _b):
            return self

        def download(self, path):
            calls.setdefault("download", []).append(path)
            return _png_bytes()

        def upload(self, path, fh, opts):
            calls.setdefault("upload", []).append(path)

    class SB:
        storage = Storage()

        def table(self, _n):
            return Q()

    return SB()


AVATAR_ROW = {"id": "row-1", "asset_key": "avatar_teacher_female", "canonical_key": "avatar_female_teacher",
              "description": "A friendly female teacher", "status": "approved", "asset_type": "avatar",
              "role": "teacher", "age_band": None, "storage_path": "generated/avatar_female_teacher/aa.png",
              "created_at": "2026-09-02T00:00:00Z"}


class TestAvatarRoster:
    def test_find_avatar_looks_up_by_exact_key_oldest_first(self, tmp_path, monkeypatch):
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "index")
        calls: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb([AVATAR_ROW], calls))
        hit = vl.find_avatar("avatar_teacher_female")
        assert hit and hit["id"] == "row-1"
        q = calls["queries"][0]
        assert q["eq:status"] == "approved"
        assert q["eq:canonical_key"] == "avatar_female_teacher" and q["order"] == "created_at"
        assert "eq:asset_type" not in q, "typed in Python (is_avatar_row), not in SQL"

    def test_a_row_typed_before_asset_type_existed_is_still_the_face(self, tmp_path, monkeypatch):
        """Rows published before the asset_type column carry the default
        'visual'; the key says avatar, and the key is what the lookup trusts."""
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "index")
        legacy = {**AVATAR_ROW, "id": "legacy", "asset_type": "visual", "created_at": "2026-08-30T00:00:00Z"}
        newer = {**AVATAR_ROW, "id": "newer", "created_at": "2026-09-04T00:00:00Z"}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb([legacy, newer], {}))
        assert vl.find_avatar("avatar_teacher_female")["id"] == "legacy", "oldest face wins, whatever its typing"
        # a same-key row that is NOT an avatar by type or key is never served as one
        stray = {**AVATAR_ROW, "id": "stray", "asset_key": "cell_diagram", "canonical_key": "avatar_female_teacher",
                 "asset_type": "visual", "created_at": "2026-08-01T00:00:00Z"}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb([stray, newer], {}))
        assert vl.find_avatar("avatar_teacher_female")["id"] == "newer"

    def test_hydrate_avatar_puts_the_file_where_the_renderer_looks(self, tmp_path, monkeypatch):
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "index")
        calls: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb([AVATAR_ROW], calls))
        cache = tmp_path / "cache"
        assert vl.hydrate_avatar("avatar_teacher_female", cache)
        png = cache / "avatar_female_teacher" / "asset.png"
        assert png.exists() and calls["download"] == [AVATAR_ROW["storage_path"]]
        meta = json.loads((cache / "avatar_female_teacher" / "meta.json").read_text(encoding="utf-8"))
        assert meta["provenance"] == "visual_library" and meta["library_asset_id"] == "row-1"
        # a second call is a cache hit: no second download
        assert vl.hydrate_avatar("avatar_teacher_female", cache)
        assert len(calls["download"]) == 1

    def test_no_roster_row_means_generate_as_before(self, tmp_path, monkeypatch):
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "index")
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb([], {}))
        assert vl.hydrate_avatar("avatar_teacher_female", tmp_path / "cache") is None

    def test_educational_retrieval_stays_avatar_blind(self, tmp_path, monkeypatch):
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "index")
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb([AVATAR_ROW], {}))
        assert vl.find("avatar_teacher_female", "A friendly female teacher character, waist-up") is None

    def test_publishing_a_second_face_for_a_known_avatar_is_refused(self, tmp_path, monkeypatch):
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "index")
        calls: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb([AVATAR_ROW], calls))
        png = tmp_path / "new_face.png"
        png.write_bytes(_png_bytes())
        assert vl.publish_generated("avatar_teacher_female", "A friendly female teacher", png) is True
        assert "upload" not in calls and "insert" not in calls, "no new object, no new row"

    def test_a_new_avatar_key_still_publishes(self, tmp_path, monkeypatch):
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "index")
        calls: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb([], calls))
        png = tmp_path / "student.png"
        png.write_bytes(_png_bytes())
        assert vl.publish_generated("avatar_student_8_10_m", "A friendly student", png) is True
        assert len(calls.get("upload", [])) == 1 and len(calls.get("insert", [])) == 1
        assert calls["insert"][0]["asset_type"] == "avatar"

    def test_the_renderer_wrapper_routes_avatars_to_the_roster_first(self):
        src = Path("shared/visual_library_integration.py").read_text(encoding="utf-8")
        assert "hydrate_avatar(key, cache)" in src
        assert src.index("hydrate_avatar(key, cache)") < src.index("hydrate(key, prompt, cache, context())")
        assert "if not existed_before and not avatar:" in src, "avatars are not scored against diagrams"
