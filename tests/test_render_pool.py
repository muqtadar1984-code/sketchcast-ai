"""Rasterization in child processes (RENDER_PROCESSES).

The cut line is `SceneRenderer.frames() -> encode_scene`: the child receives
a picklable payload, re-binds with a cache-only resolver and never generates.
Off by default (RENDER_PROCESSES=0), so every other test still exercises the
in-process path and its monkeypatch seams. The real spawn-pool test is
marked slow and skips without ffmpeg, like test_scene_engine_render.py.
"""

from __future__ import annotations

import pickle
import shutil
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest

import agent6_animation.video_composer as vc
from spike.scene_engine import segment_worker as sw
from spike.scene_engine.encode import ffmpeg_exe

_SCENE = {
    "id": "s", "narration": "ignored",
    "elements": [
        {"id": "d", "type": "shape", "shape": "path",
         "points": [(10, 10), (200, 200)]},
        {"id": "t", "type": "text", "text": "Photosynthesis", "at": (50, 50)},
    ],
    "actions": [{"verb": "draw", "target": "d"},
                {"verb": "write", "target": "t"}],
}


def _have_ffmpeg() -> bool:
    try:
        return bool(shutil.which("ffmpeg") or ffmpeg_exe())
    except Exception:
        return False


def _payload(tmp_path, **over) -> dict:
    p = {"scene": dict(_SCENE), "narration": "some narration", "prompts": {},
         "words": None, "audio_path": None, "audio_secs": 0.0,
         "out_mp4": str(tmp_path / "o.mp4"), "direction": "ltr"}
    p.update(over)
    return p


class TestChildFunction:
    def test_renders_through_the_encoder_seam(self, tmp_path, monkeypatch):
        """(a) in-process: bind + compile ran, the mp4 stub is written and the
        audit warnings come back. encode_scene is resolved through its module
        at call time, so the composer's seam still applies here."""
        seen = {}

        def fake_encode(frames, total, audio, out, fps):
            seen["total"] = total
            seen["n"] = sum(1 for _ in frames)
            Path(out).write_bytes(b"mp4")
            return True
        import spike.scene_engine.encode as enc
        monkeypatch.setattr(enc, "encode_scene", fake_encode)
        ok, warnings = sw.render_segment_in_child(_payload(tmp_path))
        assert ok is True and isinstance(warnings, list)
        assert seen["total"] > 0 and seen["n"] == round(seen["total"] * enc.FPS)
        assert (tmp_path / "o.mp4").read_bytes() == b"mp4"

    def test_unparseable_scene_is_false_not_an_exception(self, tmp_path):
        ok, warnings = sw.render_segment_in_child(
            _payload(tmp_path, scene={"garbage": True}))
        assert (ok, warnings) == (False, [])

    def test_rtl_and_hand_pen_are_applied_like_the_composer(self, tmp_path, monkeypatch):
        seen = {}
        real = sw.SceneRenderer

        class Spy(real):
            def __init__(self, scene, **kw):
                seen["scene"] = scene
                super().__init__(scene, **kw)
        monkeypatch.setattr(sw, "SceneRenderer", Spy)
        import spike.scene_engine.encode as enc
        monkeypatch.setattr(enc, "encode_scene", lambda *a, **k: True)
        sw.render_segment_in_child(_payload(tmp_path, direction="rtl"))
        s = seen["scene"]
        assert s.direction == "rtl"
        assert s.style.pen_mode == "hand" and s.style.hand_scale == pytest.approx(0.8)

    def test_payload_pickles_with_extra_scene_fields(self, tmp_path):
        """(b) the scene dict may carry extras such as `hud: true`
        (schema extra='allow'); the payload must survive the pickle round
        trip the pool performs."""
        scene = dict(_SCENE)
        scene["elements"] = list(scene["elements"]) + [
            {"id": "sk", "type": "illustration", "asset": "disc", "at": [136, 142],
             "scale": 0.32, "hud": True}]
        p = _payload(tmp_path, scene=scene, words=[{"w": "some", "t": 0.1}],
                     prompts={"disc": "a disc"})
        back = pickle.loads(pickle.dumps(p))
        assert back == p
        assert back["scene"]["elements"][-1]["hud"] is True


class _StubPool:
    def __init__(self, fn):
        self._fn = fn

    def submit(self, f, *a, **k):
        fut = ThreadPoolExecutor(max_workers=1).submit(self._fn, f, *a, **k)
        return fut


class TestComposerDispatch:
    def _stub_encode(self, monkeypatch, calls):
        import spike.scene_engine.encode as enc

        def fake_encode(frames, total, audio, out, fps):
            calls.append(out)
            for _ in frames:
                pass
            return True
        monkeypatch.setattr(enc, "encode_scene", fake_encode)

    def test_pool_on_submits_and_records_the_audit_in_the_caller(self, tmp_path, monkeypatch):
        """(c) RENDER_PROCESSES=2 with a thread-backed stub pool: the segment
        renders through the child function and the caller's dict carries
        the audit."""
        calls = []
        self._stub_encode(monkeypatch, calls)
        monkeypatch.setattr(vc, "_RENDER_PROCESSES", 2)
        submitted = []

        def run(f, *a, **k):
            submitted.append(f.__name__)
            return f(*a, **k)
        monkeypatch.setattr(vc, "_pool", lambda: _StubPool(run))
        monkeypatch.setattr(sw, "render_segment_in_child",
                            lambda payload: (True, ["warned: something"]))
        seg = {"segment_id": "s001", "scene": dict(_SCENE)}
        ok = vc._render_scene_segment(seg, "some narration", None, 0.0,
                                      tmp_path / "o.mp4", "ltr")
        assert ok is True
        assert submitted == ["<lambda>"] or submitted == ["render_segment_in_child"]
        assert seg["scene_audit"] == ["warned: something"]
        assert calls == []                       # nothing rendered in-process

    def test_child_payload_is_the_composed_scene(self, tmp_path, monkeypatch):
        calls = []
        self._stub_encode(monkeypatch, calls)
        monkeypatch.setattr(vc, "_RENDER_PROCESSES", 2)
        monkeypatch.setattr(vc, "_pool", lambda: _StubPool(lambda f, *a, **k: f(*a, **k)))
        got = {}

        def child(payload):
            got.update(payload)
            return True, []
        monkeypatch.setattr(sw, "render_segment_in_child", child)
        seg = {"segment_id": "s001", "scene": dict(_SCENE),
               "scene_assets": {"cell": "a plant cell"}}
        ok = vc._render_scene_segment(seg, "some narration", None, 0.0,
                                      tmp_path / "o.mp4", "rtl")
        assert ok is True
        assert got["direction"] == "rtl" and got["narration"] == "some narration"
        assert got["prompts"]["cell"] == "a plant cell"
        assert "avatar_teacher" in got["prompts"]      # AVATAR_PROMPTS merged
        assert got["out_mp4"] == str(tmp_path / "o.mp4")
        assert pickle.loads(pickle.dumps(got)) == got
        assert "scene_audit" not in seg              # empty warnings: untouched

    def test_broken_pool_falls_back_in_process_and_resets(self, tmp_path, monkeypatch):
        """(d) BrokenProcessPool from result(): the segment finishes on the
        in-process path (encode seam observed) and the pool is dropped."""
        calls = []
        self._stub_encode(monkeypatch, calls)
        monkeypatch.setattr(vc, "_RENDER_PROCESSES", 2)

        class Broken:
            def submit(self, f, *a, **k):
                class F:
                    def result(self_):
                        raise BrokenProcessPool("child died")
                return F()

        class OldPool:
            down = False

            def shutdown(self, wait=True, cancel_futures=False):
                self.down = True
        old = OldPool()
        monkeypatch.setattr(vc, "_POOL", old)
        monkeypatch.setattr(vc, "_pool", lambda: Broken())
        seg = {"segment_id": "s001", "scene": dict(_SCENE)}
        ok = vc._render_scene_segment(seg, "some narration", None, 0.0,
                                      tmp_path / "o.mp4", "ltr")
        assert ok is True
        assert len(calls) == 1                       # rendered in-process
        assert vc._POOL is None and old.down

    def test_a_child_exception_is_false_not_a_crash(self, tmp_path, monkeypatch):
        calls = []
        self._stub_encode(monkeypatch, calls)
        monkeypatch.setattr(vc, "_RENDER_PROCESSES", 2)

        def boom(f, *a, **k):
            raise RuntimeError("PIL blew up in the child")
        monkeypatch.setattr(vc, "_pool", lambda: _StubPool(boom))
        seg = {"segment_id": "s001", "scene": dict(_SCENE)}
        ok = vc._render_scene_segment(seg, "some narration", None, 0.0,
                                      tmp_path / "o.mp4", "ltr")
        assert ok is False and calls == []

    def test_pool_off_by_default_renders_in_process(self, tmp_path, monkeypatch):
        calls = []
        self._stub_encode(monkeypatch, calls)
        assert vc._RENDER_PROCESSES == 0
        monkeypatch.setattr(vc, "_pool", lambda: (_ for _ in ()).throw(AssertionError("pool used")))
        seg = {"segment_id": "s001", "scene": dict(_SCENE)}
        assert vc._render_scene_segment(seg, "n", None, 0.0, tmp_path / "o.mp4", "ltr") is True
        assert len(calls) == 1

    def test_thread_cap_is_not_below_the_process_count(self, monkeypatch):
        monkeypatch.setattr(vc, "_cpus", lambda: 64)
        monkeypatch.setattr(vc, "_MAX_RENDER_WORKERS", 4)
        monkeypatch.setattr(vc, "_RENDER_PROCESSES", 8)
        assert max(1, min(vc._cpus(), max(vc._MAX_RENDER_WORKERS, vc._RENDER_PROCESSES))) == 8
        monkeypatch.setattr(vc, "_RENDER_PROCESSES", 0)
        assert max(1, min(vc._cpus(), max(vc._MAX_RENDER_WORKERS, vc._RENDER_PROCESSES))) == 4


class TestPoolConstruction:
    def test_pool_is_spawn_bounded_and_recycled(self, monkeypatch):
        made = {}

        class FakeExec:
            def __init__(self, max_workers, mp_context, max_tasks_per_child):
                made.update(max_workers=max_workers, ctx=mp_context,
                            recycle=max_tasks_per_child)
        monkeypatch.setattr(vc, "ProcessPoolExecutor", FakeExec)
        monkeypatch.setattr(vc, "_POOL", None)
        monkeypatch.setattr(vc, "_RENDER_PROCESSES", 8)
        monkeypatch.setattr(vc, "_cpus", lambda: 3)
        p1 = vc._pool()
        assert made["max_workers"] == 3                  # min(processes, cpus)
        assert made["ctx"].get_start_method() == "spawn"
        assert made["recycle"] == 32
        assert vc._pool() is p1                          # one pool per process
        vc._reset_pool()
        assert vc._POOL is None


@pytest.mark.slow
@pytest.mark.skipif(not _have_ffmpeg(), reason="no ffmpeg available")
def test_a_real_spawned_child_renders_the_same_bytes(tmp_path):
    """(e) exactness across the process boundary: frame 30 of the scene
    rendered here vs in a real spawn pool of 1 — identical pixels."""
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    payload = _payload(tmp_path, audio_secs=6.0)
    here = sw.render_frame_in_child(payload, 30)
    assert here is not None
    with ProcessPoolExecutor(max_workers=1,
                             mp_context=multiprocessing.get_context("spawn")) as ex:
        there = ex.submit(sw.render_frame_in_child, payload, 30).result(timeout=300)
    assert there is not None
    assert here == there
