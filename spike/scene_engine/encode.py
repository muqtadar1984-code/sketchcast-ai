"""Frames + narration -> segment MP4, honouring the Agent 8 codec contract.

The contract (audit facts 2-3, copied from native_render._encode):
  * libx264 / yuv420p / 1280x720 / 24fps
  * AAC 128k / 44100 / stereo — a REAL audio track even for silent scenes
    (anullsrc), so every segment is concat-uniform
  * +faststart
  * clip length set with explicit -t (never -shortest)

Difference from native_render: frames arrive over a PIPE (rawvideo rgb24 on
stdin) instead of a PNG tempdir — a full-narration animation is thousands of
frames and must never exist on disk or in memory at once (the moviepy OOM
lesson). Each frame is written and dropped.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Iterator

from PIL import Image

from .schema import WORLD_H, WORLD_W

logger = logging.getLogger(__name__)

FPS = 24


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def encode_args(total_secs: float, audio_path: str | None, out: Path,
                fps: int = FPS, ffmpeg: str = "ffmpeg") -> list[str]:
    """The full ffmpeg argv — split out so tests can pin the contract without
    running ffmpeg."""
    base = [
        ffmpeg, "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{WORLD_W}x{WORLD_H}", "-r", str(fps), "-i", "pipe:0",
    ]
    if audio_path:
        base += ["-i", str(audio_path)]
    else:
        base += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    base += [
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart",
        "-t", f"{total_secs:.2f}",
        str(out),
    ]
    return base


def encode_scene(frames: Iterator[Image.Image], total_secs: float,
                 audio_path: str | None, out: Path, fps: int = FPS) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = encode_args(total_secs, audio_path, out, fps, ffmpeg_exe())
    # stderr goes to a FILE, not a pipe: ffmpeg chatters while we are still
    # writing frames, and a filled stderr pipe would block it from reading
    # stdin — a classic mutual deadlock with no error anywhere.
    import tempfile
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=errf)
        try:
            for img in frames:
                proc.stdin.write(img.tobytes())
            proc.stdin.close()
            rc = proc.wait(timeout=300)
        except Exception:
            proc.kill()
            logger.exception("scene encode failed for %s", out)
            return False
        if rc != 0:
            errf.seek(0)
            logger.error("scene ffmpeg rc=%s: %s", rc,
                         errf.read()[-500:].decode(errors="replace"))
            return False
    return out.exists() and out.stat().st_size > 0


def concat_segments(segment_paths: Iterable[Path], out: Path) -> bool:
    """Agent 8's concat-demuxer stream-copy — used by the demo CLI to PROVE the
    segments honour the uniformity contract: -c copy hard-fails on mismatched
    streams instead of silently re-encoding."""
    paths = [Path(p) for p in segment_paths]
    out.parent.mkdir(parents=True, exist_ok=True)
    lst = out.with_suffix(".txt")
    # single quotes in a path must be escaped the concat-demuxer way — the
    # same escaping agent8_render/renderer.py ships (a user named O'Brien
    # renders to a path with an apostrophe)
    def q(p: Path) -> str:
        return p.resolve().as_posix().replace("'", "'\\''")
    lst.write_text("".join(f"file '{q(p)}'\n" for p in paths), encoding="utf-8")
    cmd = [ffmpeg_exe(), "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
           "-c", "copy", "-movflags", "+faststart", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    lst.unlink(missing_ok=True)
    if proc.returncode != 0:
        logger.error("concat rc=%s: %s", proc.returncode, (proc.stderr or "")[-500:])
        return False
    return out.exists() and out.stat().st_size > 0
