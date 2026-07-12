"""Agent 6 composer: the parallelized segment loop must preserve ORDER (Agent 8
concatenates segments in order) and build every segment. TTS + native render are
stubbed so the test exercises only the loop/aggregation, not ffmpeg or the network.

Run from the repo root: python -m pytest tests/test_video_composer.py -q
"""

from __future__ import annotations

from pathlib import Path

import agent6_animation.video_composer as vc
from agent6_animation.video_composer import compose_episode_videos


def _stub(monkeypatch, tmp_path):
    def fake_synthesize(text, out, *, voice_id=None, allow_premium=False, ssml_text=None, report=None):
        if report is not None:
            report.update({"used": "edge-aria", "downgraded": False})
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"mp3")
        return Path(out)

    def fake_render(spec, audio, out, ffmpeg, **kw):
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"mp4")
        return True

    monkeypatch.setattr(vc, "synthesize", fake_synthesize)
    monkeypatch.setattr(vc, "render_native_segment", fake_render)
    monkeypatch.setattr(vc, "_audio_duration", lambda p, f: 5.0)
    monkeypatch.setattr(vc, "_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(vc, "concepts_for_slides", lambda headings: ["c"] * len(headings))
    monkeypatch.setattr(vc, "VIDEO_DIR", tmp_path)


def _inputs(n: int):
    script = {"episodes": [{
        "book_id": "bk", "chapter_num": 1, "episode_num": 1, "episode_title": "Ep",
        "segments": [
            {"segment_id": f"s{i}", "text": f"narration {i}", "slide_heading": f"H{i}",
             "slide_points": ["p"], "estimated_duration_seconds": 5}
            for i in range(n)
        ],
    }]}
    slides = {"segments": [{"segment_id": f"s{i}"} for i in range(n)]}
    return script, slides


def test_parallel_render_preserves_segment_order(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path)
    n = 8
    script, slides = _inputs(n)
    m = compose_episode_videos(script, slides).model_dump()
    assert [s["segment_id"] for s in m["segments"]] == [f"s{i}" for i in range(n)]
    assert m["video_segments_count"] == n
    assert m["total_segments"] == n
    assert m["total_duration_seconds"] == round(5.0 * n, 2)


def test_render_workers_1_is_sequential_and_equivalent(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path)
    monkeypatch.setattr(vc, "_MAX_RENDER_WORKERS", 1)  # force the sequential path
    script, slides = _inputs(4)
    m = compose_episode_videos(script, slides).model_dump()
    assert [s["segment_id"] for s in m["segments"]] == ["s0", "s1", "s2", "s3"]
    assert m["video_segments_count"] == 4


def test_a_failed_segment_is_skipped_not_fatal(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path)

    def flaky_render(spec, audio, out, ffmpeg, **kw):
        if spec["number"] == 2:  # 1-indexed → the 2nd segment fails to render
            return False
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"mp4")
        return True

    monkeypatch.setattr(vc, "render_native_segment", flaky_render)
    script, slides = _inputs(4)
    m = compose_episode_videos(script, slides).model_dump()
    # 3 built, still in order, the failed one dropped (not a crash).
    assert [s["segment_id"] for s in m["segments"]] == ["s0", "s2", "s3"]
    assert m["video_segments_count"] == 3
    assert m["total_segments"] == 4
