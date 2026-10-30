"""New YouTube uploads go up at 1440p (catalogue.youtube_upscale).

Founder decision 2026-10-30: the lessons render at 1280x720 and YouTube gives
a 720p upload its basic low-bitrate encode, so the upload is a temporary
2560x1440 re-encode. What these tests hold in place:
  * the ffmpeg command (lanczos scale, x264 crf 18 / tune animation, the
    source's frame rate, audio copied, faststart);
  * a source already at the target height, the kill switch and a file that
    is not an MP4 upload the ORIGINAL with no encode;
  * a failed or timed-out encode uploads the original and logs a warning;
  * the temporary copy is removed after the upload, on success and on
    failure, and the stored artifact is never changed.

Nothing here reaches the network: the transport is ``FakeYouTube`` and the
database ``FakeSB``. ffmpeg runs only on a one-second synthetic clip, raised
to 360p rather than 1440p so the suite stays fast.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from agent8_render import probe as VP
from catalogue import publish as P
from catalogue import youtube_upscale as U
from tests.catalogue_fakes import FakeYouTube
from tests.test_catalogue_publish import KIT, _job, _sb

MP4_HEAD = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2avc1mp41"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(P.FEATURE_FLAG, "1")
    monkeypatch.delenv(P.AUDIT_FLAG, raising=False)
    monkeypatch.delenv(P.MAX_PARTS_ENV, raising=False)
    monkeypatch.delenv("YOUTUBE_PLAYLISTS_EN", raising=False)
    for name in (P.CLIENT_ID_ENV, P.CLIENT_SECRET_ENV, "YOUTUBE_REFRESH_TOKEN_EN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(U.HEIGHT_ENV, raising=False)
    monkeypatch.delenv(U.THREADS_ENV, raising=False)
    monkeypatch.setattr(P, "builders_are_queued", lambda sb: False)


def _clip(path: Path, secs: float = 1.0, size: str = "320x180") -> Path:
    cmd = [VP.ffmpeg_exe(), "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", f"testsrc=size={size}:rate=24:duration={secs}",
           "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", str(secs),
           "-c:a", "aac", "-b:a", "64k",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
           "-movflags", "+faststart", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> Path:
    return _clip(tmp_path_factory.mktemp("clip") / "lesson.mp4")


def _fake_mp4(path: Path) -> Path:
    path.write_bytes(MP4_HEAD + b"\x00" * 64)
    return path


def _probe(height=720, width=1280, duration=300.0, rate="24/1", audio="aac", errors=()):
    def fake(path, *, decode=True):
        return VP.VideoProbe(path=Path(path), duration=duration, video_codec="h264", audio_codec=audio,
                             width=width, height=height, frame_rate=rate, errors=list(errors))
    return fake


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── the command ─────────────────────────────────────────────────────────


def test_the_command_scales_to_1440p_with_lanczos_crf18_and_tune_animation():
    cmd = U.build_command("ffmpeg", Path("in.mp4"), Path("out.mp4"), width=2560, height=1440,
                          frame_rate="24/1", audio_codec="aac", threads=2)
    joined = " ".join(cmd)
    assert cmd[0] == "ffmpeg" and cmd[-1] == "out.mp4"
    assert cmd[cmd.index("-i") + 1] == "in.mp4"
    assert cmd[cmd.index("-vf") + 1] == "scale=2560:1440:flags=lanczos"
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-preset") + 1] == "medium"
    assert cmd[cmd.index("-crf") + 1] == "18"
    assert cmd[cmd.index("-tune") + 1] == "animation"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert cmd[cmd.index("-profile:v") + 1] == "high"
    assert cmd[cmd.index("-r") + 1] == "24/1"
    assert cmd[cmd.index("-c:a") + 1] == "copy"
    assert cmd[cmd.index("-movflags") + 1] == "+faststart"
    assert cmd[cmd.index("-threads") + 1] == "2"
    assert "-nostdin" in cmd and "-y" in joined


def test_audio_the_mp4_cannot_carry_is_reencoded_to_aac_and_an_unknown_rate_is_left_to_ffmpeg():
    cmd = U.build_command("ffmpeg", Path("in.mp4"), Path("out.mp4"), width=2560, height=1440,
                          frame_rate=None, audio_codec="pcm_s16le", threads=1)
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert "-r" not in cmd


def test_the_width_keeps_the_aspect_ratio_and_is_even():
    assert U.target_width(1440, 1280, 720) == 2560
    assert U.target_width(1440, 1920, 1080) == 2560
    assert U.target_width(1440, 1080, 1920) % 2 == 0


def test_the_timeout_is_three_times_real_time_with_a_two_minute_floor():
    assert U.encode_timeout(10) == 120
    assert U.encode_timeout(300) == 900


@pytest.mark.parametrize("raw,height", [(None, 1440), ("", 1440), ("1440", 1440), ("2160", 2160),
                                        ("0", 0), ("off", 0), ("OFF", 0), ("false", 0), ("tall", 1440)])
def test_the_kill_switch_reads_the_environment(monkeypatch, raw, height):
    if raw is None:
        monkeypatch.delenv(U.HEIGHT_ENV, raising=False)
    else:
        monkeypatch.setenv(U.HEIGHT_ENV, raw)
    assert U.target_height() == height


# ── when the original goes up as it is ─────────────────────────────────


def _no_encode(monkeypatch):
    calls = []
    monkeypatch.setattr(U, "_run", lambda cmd, timeout: calls.append(cmd) or pytest.fail("encoded"))
    return calls


def test_a_source_already_1440_tall_is_uploaded_as_it_is(tmp_path, monkeypatch):
    src = _fake_mp4(tmp_path / "lesson.mp4")
    monkeypatch.setattr(VP, "probe_video", _probe(height=1440, width=2560))
    calls = _no_encode(monkeypatch)
    with U.youtube_upload_file(src) as up:
        assert up == src
    assert calls == [] and src.exists()


@pytest.mark.parametrize("value", ["0", "off"])
def test_the_kill_switch_uploads_the_original_without_probing(tmp_path, monkeypatch, value):
    monkeypatch.setenv(U.HEIGHT_ENV, value)
    src = _fake_mp4(tmp_path / "lesson.mp4")
    monkeypatch.setattr(VP, "probe_video", lambda *a, **k: pytest.fail("probed"))
    _no_encode(monkeypatch)
    with U.youtube_upload_file(src) as up:
        assert up == src


def test_a_file_that_is_not_an_mp4_is_uploaded_as_it_is(tmp_path, monkeypatch, caplog):
    src = tmp_path / "lesson.mp4"
    src.write_bytes(b"mp4-part-1")
    monkeypatch.setattr(VP, "probe_video", lambda *a, **k: pytest.fail("probed"))
    with caplog.at_level(logging.WARNING, logger="worker.publish"):
        assert U.make_youtube_copy(src) is None
    assert "not an MP4" in caplog.text


def test_an_unreadable_source_is_uploaded_as_it_is(tmp_path, monkeypatch, caplog):
    src = _fake_mp4(tmp_path / "lesson.mp4")
    monkeypatch.setattr(VP, "probe_video", _probe(errors=["moov atom not found"]))
    _no_encode(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="worker.publish"):
        assert U.make_youtube_copy(src) is None
    assert "could not be probed" in caplog.text


def test_too_little_disk_uploads_the_original(tmp_path, monkeypatch, caplog):
    src = _fake_mp4(tmp_path / "lesson.mp4")
    monkeypatch.setattr(VP, "probe_video", _probe())
    monkeypatch.setattr(U.shutil, "disk_usage", lambda p: type("D", (), {"free": 1024})())
    _no_encode(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="worker.publish"):
        assert U.make_youtube_copy(src) is None
    assert "too little" in caplog.text


# ── a failed encode never blocks the publish ────────────────────────────


def test_a_failed_encode_uploads_the_original_warns_and_leaves_no_copy(tmp_path, monkeypatch, caplog):
    src = _fake_mp4(tmp_path / "lesson.mp4")
    monkeypatch.setattr(VP, "probe_video", _probe())

    def fail(cmd, timeout):
        Path(cmd[-1]).write_bytes(b"half a file")   # a partial output is left behind
        return subprocess.CompletedProcess(cmd, 1, "", "Unknown encoder 'libx264'")
    monkeypatch.setattr(U, "_run", fail)
    with caplog.at_level(logging.WARNING, logger="worker.publish"):
        with U.youtube_upload_file(src) as up:
            assert up == src
    assert "re-encode of lesson.mp4 failed" in caplog.text and "libx264" in caplog.text
    assert sorted(p.name for p in tmp_path.iterdir()) == ["lesson.mp4"]


def test_a_timed_out_encode_uploads_the_original_and_leaves_no_copy(tmp_path, monkeypatch, caplog):
    src = _fake_mp4(tmp_path / "lesson.mp4")
    monkeypatch.setattr(VP, "probe_video", _probe(duration=300.0))
    seen = {}

    def slow(cmd, timeout):
        seen["timeout"] = timeout
        Path(cmd[-1]).write_bytes(b"partial")
        raise subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(U, "_run", slow)
    with caplog.at_level(logging.WARNING, logger="worker.publish"):
        assert U.make_youtube_copy(src) is None
    assert seen["timeout"] == 900
    assert "passed its 900s limit" in caplog.text
    assert sorted(p.name for p in tmp_path.iterdir()) == ["lesson.mp4"]


def test_a_copy_of_the_wrong_size_is_discarded(tmp_path, monkeypatch, caplog):
    src = _fake_mp4(tmp_path / "lesson.mp4")
    probes = iter([_probe()(src), _probe(height=720)(src)])   # the "copy" came out at 720
    monkeypatch.setattr(VP, "probe_video", lambda path, *, decode=True: next(probes))

    def ok(cmd, timeout):
        Path(cmd[-1]).write_bytes(b"not really 1440")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(U, "_run", ok)
    with caplog.at_level(logging.WARNING, logger="worker.publish"):
        assert U.make_youtube_copy(src) is None
    assert "does not match its source" in caplog.text
    assert sorted(p.name for p in tmp_path.iterdir()) == ["lesson.mp4"]


def test_the_copy_is_removed_when_the_upload_raises(tmp_path, monkeypatch):
    src = _fake_mp4(tmp_path / "lesson.mp4")
    copy = tmp_path / "lesson.youtube1440p.mp4"
    copy.write_bytes(b"the 1440p copy")
    monkeypatch.setattr(U, "make_youtube_copy", lambda s, w=None: copy)
    with pytest.raises(RuntimeError):
        with U.youtube_upload_file(src) as up:
            assert up == copy
            raise RuntimeError("upload refused")
    assert not copy.exists() and src.exists()


# ── the real ffmpeg, on a one-second clip ───────────────────────────────


def _fps(rate) -> Fraction:
    """ffprobe states "24/1", the ``ffmpeg -i`` fallback "24": the same rate."""
    return Fraction(str(rate))


def test_the_probe_reads_the_frame_rate(clip, monkeypatch):
    assert _fps(VP.probe_video(clip, decode=False).frame_rate) == 24   # whichever tool this machine has
    monkeypatch.setattr(VP, "ffprobe_exe", lambda: None)
    assert VP.probe_video(clip, decode=False).frame_rate == "24"


def test_a_real_encode_doubles_the_height_and_keeps_rate_audio_and_length(clip, tmp_path, monkeypatch):
    monkeypatch.setenv(U.HEIGHT_ENV, "360")
    before = _sha(clip.read_bytes())
    copy = U.make_youtube_copy(clip, tmp_path)
    assert copy is not None and copy.parent == tmp_path
    try:
        pr = VP.probe_video(copy, decode=False)
        assert (pr.width, pr.height) == (640, 360)
        assert pr.video_codec == "h264" and pr.audio_codec == "aac"
        assert _fps(pr.frame_rate) == 24
        assert pr.duration == pytest.approx(1.0, abs=0.2)
    finally:
        copy.unlink(missing_ok=True)
    assert _sha(clip.read_bytes()) == before   # the source is never changed


# ── through the publish job ─────────────────────────────────────────────


class _RecordingYouTube(FakeYouTube):
    """Keeps the path it was handed, and can refuse after seeing it."""

    def __init__(self, *, refuse=False, **kw):
        super().__init__(**kw)
        self.refuse, self.paths = refuse, []

    def upload_video(self, path, **kw):
        self.paths.append(Path(path))
        if self.refuse:
            raise RuntimeError("upload refused (fake)")
        return super().upload_video(path, **kw)


def _sb_with_real_video(clip: Path):
    sb = _sb()
    video = next(a["storage_path"] for a in sb.tables["artifacts"] if a["kind"] == "video_mp4")
    sb.files[("artifacts", video)] = clip.read_bytes()
    return sb, video


def test_publish_uploads_the_copy_and_leaves_the_stored_mp4_alone(clip, monkeypatch):
    monkeypatch.setenv(U.HEIGHT_ENV, "360")
    sb, video = _sb_with_real_video(clip)
    stored = _sha(sb.files[("artifacts", video)])
    yt = _RecordingYouTube()
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert summary["published"] == [1]
    up = yt.uploads[0]
    assert Path(up["path"]).name == "lesson_part1.youtube360p.mp4"
    assert _sha(up["bytes"]) != stored                       # YouTube got the copy …
    assert _sha(sb.files[("artifacts", video)]) == stored    # … storage kept the original
    assert not any(k[0] == "artifacts" and k[1] != video and k[1].endswith(".mp4") for k in sb.files)
    assert not yt.paths[0].exists()                          # the copy is gone


def test_publish_with_a_failed_encode_uploads_the_original(clip, monkeypatch, caplog):
    sb, video = _sb_with_real_video(clip)
    monkeypatch.setattr(U, "_run", lambda cmd, timeout: subprocess.CompletedProcess(cmd, 1, "", "boom"))
    yt = _RecordingYouTube()
    with caplog.at_level(logging.WARNING, logger="worker.publish"):
        summary = P.run_publish_job(sb, _job(), transport=yt)
    assert summary["published"] == [1]
    assert Path(yt.uploads[0]["path"]).name == "lesson_part1.mp4"
    assert yt.uploads[0]["bytes"] == sb.files[("artifacts", video)]
    assert "uploading the original" in caplog.text


def test_publish_with_the_kill_switch_uploads_the_original(clip, monkeypatch):
    monkeypatch.setenv(U.HEIGHT_ENV, "0")
    sb, video = _sb_with_real_video(clip)
    _no_encode(monkeypatch)
    yt = _RecordingYouTube()
    P.run_publish_job(sb, _job(), transport=yt)
    assert yt.uploads[0]["bytes"] == sb.files[("artifacts", video)]


def test_publish_removes_the_copy_when_the_upload_fails(clip, monkeypatch):
    monkeypatch.setenv(U.HEIGHT_ENV, "360")
    sb, _video = _sb_with_real_video(clip)
    yt = _RecordingYouTube(refuse=True)
    P.run_publish_job(sb, _job(), transport=yt)
    assert yt.paths and yt.paths[0].name == "lesson_part1.youtube360p.mp4"
    assert not yt.paths[0].exists()
    assert yt.uploads == []
    assert next(r for r in sb.tables["jobs"] if r["params"].get("kit_id") == KIT)["status"] == "error"
