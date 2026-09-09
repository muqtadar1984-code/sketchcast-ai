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
        # MEMORY, not speed. libx264 sizes its allocations from the thread
        # count it picks, and left alone it picks ~1.5x the machine's cores —
        # on a many-core container that is dozens of frame threads, each with
        # its own reference and lookahead buffers, PER INSTANCE. Several
        # encoders at once then exhausted the container and libx264 failed at
        # init: "Error while opening encoder — maybe incorrect parameters such
        # as bit_rate, rate, width or height", which is what an allocation
        # failure looks like from the outside. It cost a segment on three
        # separate production runs, each time a DIFFERENT segment, which is
        # the signature of a resource fault rather than a bad scene.
        #
        # 2 frame threads is ample: a 720p whiteboard frame is mostly flat
        # white and encodes far faster than realtime either way. sliced-threads
        # off keeps each thread's working set to its own frame, and a short
        # lookahead is the other large per-instance buffer.
        "-threads", "2",
        "-x264-params", "sliced-threads=0:rc-lookahead=20",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart",
        "-t", f"{total_secs:.2f}",
        str(out),
    ]
    return base


def encode_scene(frames: Iterator[Image.Image], total_secs: float,
                 audio_path: str | None, out: Path, fps: int = FPS) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    # The one encoder input nothing else checks. ffmpeg opens its inputs
    # BEFORE reading a byte of stdin, so a missing or empty audio file makes
    # it exit at startup and the first frame write lands on a closed pipe —
    # EPIPE, with the real reason only on ffmpeg's stderr. Callers upstream
    # never verify the mp3 exists (shared/tts returns a path unchecked), so
    # say so here rather than reporting a broken pipe.
    if audio_path:
        try:
            asz = Path(audio_path).stat().st_size
        except OSError:
            asz = -1
        if asz <= 0:
            logger.error("scene encode for %s has a missing or empty audio "
                         "input %s (size=%s) — ffmpeg will fail at input-open",
                         out, audio_path, asz)
    cmd = encode_args(total_secs, audio_path, out, fps, ffmpeg_exe())
    # stderr goes to a FILE, not a pipe: ffmpeg chatters while we are still
    # writing frames, and a filled stderr pipe would block it from reading
    # stdin — a classic mutual deadlock with no error anywhere.
    import tempfile
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=errf)
        n_frames = n_bytes = 0
        try:
            for img in frames:
                raw = img.tobytes()
                proc.stdin.write(raw)
                n_frames += 1
                n_bytes += len(raw)
            proc.stdin.close()
            rc = proc.wait(timeout=300)
        except Exception:
            # ffmpeg's own stderr is the ONLY place the reason exists, and it
            # lives in errf — which this branch used to drop on the floor when
            # the `with` closed the temp file. A BrokenPipeError then said
            # only "it died", indistinguishable between a bad argument, a
            # missing input and the OOM killer. Collect the exit code too:
            # -9 is the kernel, 1 is ffmpeg's own refusal.
            proc.kill()
            try:
                rc = proc.wait(timeout=10)
            except Exception:
                rc = None
            try:
                errf.seek(0)
                tail = errf.read()[-2000:].decode(errors="replace")
            except Exception:
                tail = "<stderr unavailable>"
            logger.exception(
                "scene encode failed for %s (rc=%s, frames_written=%d, "
                "bytes=%d, audio=%s)\nffmpeg argv: %s\nffmpeg stderr: %s",
                out, rc, n_frames, n_bytes, audio_path, cmd, tail)
            return False
        if rc != 0:
            errf.seek(0)
            logger.error("scene ffmpeg rc=%s: %s", rc,
                         errf.read()[-500:].decode(errors="replace"))
            return False
    if not (out.exists() and out.stat().st_size > 0):
        # ffmpeg exited 0 having written nothing: silent until now, and
        # indistinguishable downstream from a scene that would not compile
        logger.error("scene encode for %s reported success but produced no "
                     "output (frames_written=%d, audio=%s)", out, n_frames,
                     audio_path)
        return False
    return True


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
