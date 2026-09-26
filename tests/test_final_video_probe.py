"""The final video is read back before it is shipped (agent8_render.probe).

States of Matter, 2026-09-26: a kit read `done`, its mp4 was in storage at
the right size and mime type, and the reviewer's phone showed a broken-media
icon. Nothing in the pipeline had ever opened the file it produced. The
probe decodes it and checks its length against the manifest; the renderer
re-encodes once on a failed probe and refuses to ship on a second.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent8_render import probe as P


def _mp4(path: Path, secs: float = 3.0, audio: bool = True, size: str = "320x240") -> Path:
    cmd = [P.ffmpeg_exe(), "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", f"testsrc=size={size}:rate=24:duration={secs}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo", "-t", str(secs),
                "-c:a", "aac", "-b:a", "64k"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
            "-movflags", "+faststart", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


@pytest.fixture(scope="module")
def good(tmp_path_factory) -> Path:
    return _mp4(tmp_path_factory.mktemp("v") / "good.mp4")


class TestAGoodFile:
    def test_is_read_with_its_streams_and_length(self, good):
        pr = P.probe_video(good)
        assert pr.ok, pr.errors
        assert pr.video_codec == "h264" and pr.audio_codec == "aac"
        assert (pr.width, pr.height) == (320, 240)
        assert pr.duration == pytest.approx(3.0, abs=0.3)
        assert "h264/aac 320x240" in pr.summary()

    def test_passes_the_gate_against_its_manifest_length(self, good):
        assert P.check_final_video(good, 3.0).ok

    def test_the_ffmpeg_fallback_reads_the_same_header(self, good, monkeypatch):
        monkeypatch.setattr(P, "ffprobe_exe", lambda: None)
        pr = P.probe_video(good, decode=False)
        assert pr.tool == "ffmpeg" and pr.ok, pr.errors
        assert pr.video_codec == "h264" and pr.audio_codec == "aac"
        assert pr.duration == pytest.approx(3.0, abs=0.3)


class TestABadFileIsRefused:
    def test_a_truncated_file_fails_the_decode(self, good, tmp_path):
        data = good.read_bytes()
        cut = tmp_path / "cut.mp4"
        cut.write_bytes(data[: len(data) // 3])
        pr = P.probe_video(cut)
        assert not pr.ok and pr.errors, pr
        with pytest.raises(RuntimeError, match="unreadable"):
            P.check_final_video(cut, 3.0)

    def test_garbage_is_refused(self, tmp_path):
        junk = tmp_path / "junk.mp4"
        junk.write_bytes(b"mp4" * 1000)
        with pytest.raises(RuntimeError, match="unreadable"):
            P.check_final_video(junk, 1.0)

    def test_a_missing_or_empty_file_is_refused(self, tmp_path):
        with pytest.raises(RuntimeError, match="missing or empty"):
            P.check_final_video(tmp_path / "nope.mp4", 1.0)

    def test_a_silent_file_has_no_audio_stream(self, tmp_path):
        mute = _mp4(tmp_path / "mute.mp4", audio=False)
        with pytest.raises(RuntimeError, match="no audio stream"):
            P.check_final_video(mute, 3.0)

    def test_a_length_that_is_not_the_manifests_is_refused(self, good):
        with pytest.raises(RuntimeError, match="manifest expects 180.0s"):
            P.check_final_video(good, 180.0)
        # within tolerance: the concat of N segments is never exact
        assert P.check_final_video(good, 3.0 + P.DURATION_TOLERANCE_SECS * 0.9).ok


class TestTheRendererRunsIt:
    def _manifest(self, segs, tmp_path):
        return {"book_id": "bk", "chapter_num": 1, "episode_num": 1, "run_id": "r1",
                "segments": [{"segment_id": f"s{i}", "video_path": str(p),
                              "clip_duration_seconds": d} for i, (p, d) in enumerate(segs)]}

    def test_two_real_segments_concat_and_pass(self, tmp_path, monkeypatch, caplog):
        import agent8_render.renderer as rr
        monkeypatch.setattr(rr, "FINAL_DIR", tmp_path / "final")
        a = _mp4(tmp_path / "a.mp4", 2.0)
        b = _mp4(tmp_path / "b.mp4", 3.0)
        with caplog.at_level("INFO", logger="agent8_render.renderer"):
            out = rr.render_final_video(self._manifest([(a, 2.0), (b, 3.0)], tmp_path))
        assert Path(out.final_video_path).exists()
        assert P.probe_video(out.final_video_path).duration == pytest.approx(5.0, abs=0.5)
        assert any("Final video probed" in r.message for r in caplog.records)

    def test_a_segment_that_cannot_be_decoded_never_ships(self, tmp_path, monkeypatch):
        import agent8_render.renderer as rr
        monkeypatch.setattr(rr, "FINAL_DIR", tmp_path / "final")
        a = _mp4(tmp_path / "a.mp4", 2.0)
        junk = tmp_path / "junk.mp4"
        junk.write_bytes(b"mp4" * 4000)
        with pytest.raises(RuntimeError) as e:
            rr.render_final_video(self._manifest([(a, 2.0), (junk, 3.0)], tmp_path))
        # stream-copy concat of a junk entry mistimed the container (7.2s),
        # the one re-encode dropped the entry (2.0s), and the length check
        # refused what was left against the 5.0s manifest
        assert "manifest expects 5.0s" in str(e.value) or "unreadable" in str(e.value)

    def test_the_probe_runs_before_the_manifest_is_written(self):
        import inspect
        import agent8_render.renderer as rr
        src = inspect.getsource(rr.render_final_video)
        assert src.index("check_final_video(output_path") < src.index("manifest_path.write_text")
