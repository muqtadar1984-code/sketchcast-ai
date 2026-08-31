"""Scene engine integration: real (tiny) MP4 through the real ffmpeg.

Skips itself when no ffmpeg binary is reachable, per the repo's convention
that the fast suite runs everywhere. Uses a short silent scene so the test
adds ~2s of encode, not a full lesson.
"""

from __future__ import annotations

import shutil

import pytest

from spike.scene_engine.encode import concat_segments, encode_scene, ffmpeg_exe
from spike.scene_engine.render import SceneRenderer
from spike.scene_engine.schema import Scene


def _have_ffmpeg() -> bool:
    try:
        return bool(shutil.which("ffmpeg") or ffmpeg_exe())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _have_ffmpeg(), reason="no ffmpeg available")


def _tiny_scene(sid: str) -> Scene:
    return Scene.model_validate({
        "id": sid, "narration": "",
        "min_hold": 0.4,
        "elements": [
            {"id": "box", "type": "shape", "shape": "path",
             "points": [(200, 200), (500, 200), (500, 420), (200, 420)],
             "closed": True},
            {"id": "lbl", "type": "text", "text": "Box", "at": (540, 200)},
        ],
        "actions": [
            {"verb": "draw", "target": "box", "duration": 0.6},
            {"verb": "write", "target": "lbl", "duration": 0.4},
        ],
    })


def test_silent_scene_encodes_and_segments_stream_copy_concat(tmp_path):
    paths = []
    for sid in ("t1", "t2"):
        sc = _tiny_scene(sid)
        r = SceneRenderer(sc)
        r.compile(0.0)
        out = tmp_path / f"{sid}.mp4"
        assert encode_scene(r.frames(0.0), r.total_secs(0.0), None, out)
        assert out.stat().st_size > 5_000
        paths.append(out)

    final = tmp_path / "final.mp4"
    # -c copy hard-fails on non-uniform segments: success IS the contract test
    assert concat_segments(paths, final)
    assert final.stat().st_size > sum(p.stat().st_size for p in paths) * 0.5
