"""A 1440p copy of a lesson video, made only for the YouTube upload.

The lessons render at 1280x720 (agent6_animation/native_render.py). YouTube
gives a 720p upload its basic, low-bitrate encode, so on the channel the
lessons look soft and dull; an upload at 1440p or more gets the VP9/AV1
high-bitrate encode at EVERY playback size, 720p included. Founder decision
2026-10-30: new uploads go up at 1440p; the videos already on the channel
are not re-uploaded.

What this module holds in place:
  * ONLY YouTube gets the copy. The stored MP4 in Supabase storage is never
    touched: the app plays it in-app, and a file twice the size would double
    the egress. The copy is a temporary file next to the downloaded one, in
    the publish run's own temp dir, and it is deleted the moment the upload
    returns (or raises).
  * A FAILED UPSCALE NEVER BLOCKS A PUBLISH. A probe that cannot read the
    file, too little disk, an ffmpeg error or a timeout: the warning is
    logged and the ORIGINAL is uploaded, exactly as before this module.
  * A source already at the target height is uploaded as it is.
  * ffmpeg streams the file (decode, scale, encode, write); nothing is held
    in memory, and the encoder's threads are capped so a small Railway
    worker that reports the host's core count does not spawn dozens of
    frame threads (each one holds 1440p frames).

Kill switch: ``YOUTUBE_UPLOAD_HEIGHT`` (default 1440; ``0`` / ``off`` uploads
the original). ``YOUTUBE_UPLOAD_THREADS`` caps the encoder (default 2).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger("worker.publish")

HEIGHT_ENV = "YOUTUBE_UPLOAD_HEIGHT"
THREADS_ENV = "YOUTUBE_UPLOAD_THREADS"
DEFAULT_HEIGHT = 1440
DEFAULT_THREADS = 2
_OFF = {"0", "off", "false", "no", "none", "disabled"}

# x264: lanczos upscale, near-transparent quality, tuned for flat-colour
# drawn content (tune animation: more reference frames, lighter deblock).
PRESET = "medium"
CRF = 18
TUNE = "animation"

# The encode may take this many times the video's own length, never less
# than the floor: a 5-minute lesson gets 15 minutes before it is abandoned.
TIMEOUT_PER_SECOND = 3.0
MIN_TIMEOUT_SECS = 120.0

# Free disk wanted before encoding: the copy is roughly 2-4x the 720p
# source (4x the pixels at a high-quality CRF), plus headroom.
DISK_FACTOR = 6
DISK_HEADROOM_BYTES = 256 * 1024 * 1024

# An MP4 container can carry these audio codecs as they are; anything else
# is re-encoded to AAC.
_COPYABLE_AUDIO = {"aac", "mp3", "alac", "ac3", "eac3"}


def target_height() -> int:
    """The height YouTube uploads are raised to; 0 means "upload the original"."""
    raw = str(os.getenv(HEIGHT_ENV, "") or "").strip().lower()
    if not raw:
        return DEFAULT_HEIGHT
    if raw in _OFF:
        return 0
    try:
        value = int(float(raw))
    except ValueError:
        log.warning("publish: %s=%r is not a height; using %d", HEIGHT_ENV, raw, DEFAULT_HEIGHT)
        return DEFAULT_HEIGHT
    return max(0, value)


def encoder_threads() -> int:
    raw = str(os.getenv(THREADS_ENV, "") or "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_THREADS
    except ValueError:
        return DEFAULT_THREADS


def target_width(height: int, src_width: int, src_height: int) -> int:
    """The width that keeps the source's aspect ratio at ``height``, rounded
    to an even number (yuv420p needs one): 1280x720 → 2560 at 1440."""
    return max(2, int(round(height * src_width / src_height / 2.0)) * 2)


def encode_timeout(duration_secs: float) -> float:
    return max(MIN_TIMEOUT_SECS, TIMEOUT_PER_SECOND * float(duration_secs))


def build_command(ffmpeg: str, src: Path, dst: Path, *, width: int, height: int,
                  frame_rate: Optional[str], audio_codec: Optional[str], threads: int) -> list[str]:
    """The ffmpeg argv for the YouTube copy. ``-r`` repeats the source's own
    frame rate (omitted when the probe did not read one: ffmpeg then keeps
    the input's); the audio is copied when the container allows it."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-y",
           "-i", str(src),
           "-map", "0:v:0", "-map", "0:a:0?",
           "-vf", f"scale={width}:{height}:flags=lanczos",
           "-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF), "-tune", TUNE,
           "-pix_fmt", "yuv420p", "-profile:v", "high",
           "-threads", str(int(threads))]
    if frame_rate:
        cmd += ["-r", str(frame_rate)]
    if (audio_codec or "").lower() in _COPYABLE_AUDIO:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-movflags", "+faststart", str(dst)]
    return cmd


def _looks_like_mp4(path: Path) -> bool:
    """An ISO-BMFF file names its ``ftyp`` box in bytes 4..8."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return False
    return len(head) >= 8 and head[4:8] == b"ftyp"


def _unlink(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("publish: the YouTube copy %s was not removed: %s", path, exc)


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    """One seam for the tests: they replace this, never ffmpeg itself."""
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=timeout)


def make_youtube_copy(src: Path, work_dir: Optional[Path] = None) -> Optional[Path]:
    """The 1440p copy of ``src`` for YouTube, or None when the original
    should be uploaded as it is (switched off, already tall enough) or the
    copy could not be made (logged as a warning). Never raises."""
    dst: Optional[Path] = None
    try:
        height = target_height()
        if height <= 0:
            log.info("publish: %s is off; uploading %s as rendered", HEIGHT_ENV, src.name)
            return None
        if not _looks_like_mp4(src):
            log.warning("publish: %s is not an MP4 container; uploading it as it is", src.name)
            return None

        from agent8_render.probe import ffmpeg_exe, probe_video
        probe = probe_video(src, decode=False)
        if probe.errors or not probe.height or not probe.width or not probe.duration:
            log.warning("publish: %s could not be probed (%s); uploading the original",
                        src.name, "; ".join(probe.errors) or probe.summary())
            return None
        if probe.height >= height:
            log.info("publish: %s is already %dp; uploading it as it is", src.name, probe.height)
            return None

        out_dir = Path(work_dir) if work_dir else src.parent
        src_bytes = src.stat().st_size
        free = shutil.disk_usage(out_dir).free
        if free < DISK_FACTOR * src_bytes + DISK_HEADROOM_BYTES:
            log.warning("publish: %d MB free is too little for the %dp copy of %s (%d MB); "
                        "uploading the original", free // 2**20, height, src.name, src_bytes // 2**20)
            return None

        width = target_width(height, probe.width, probe.height)
        dst = out_dir / f"{src.stem}.youtube{height}p.mp4"
        cmd = build_command(ffmpeg_exe(), src, dst, width=width, height=height,
                            frame_rate=probe.frame_rate, audio_codec=probe.audio_codec,
                            threads=encoder_threads())
        timeout = encode_timeout(probe.duration)
        started = time.monotonic()
        try:
            proc = _run(cmd, timeout)
        except subprocess.TimeoutExpired:
            log.warning("publish: the %dp re-encode of %s passed its %.0fs limit; uploading the original",
                        height, src.name, timeout)
            _unlink(dst)
            return None
        elapsed = time.monotonic() - started
        if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
            tail = (proc.stderr or "").strip()[-300:]
            log.warning("publish: the %dp re-encode of %s failed (rc=%s: %s); uploading the original",
                        height, src.name, proc.returncode, tail or "no output")
            _unlink(dst)
            return None

        check = probe_video(dst, decode=False)
        if check.errors or check.height != height or not check.duration \
                or abs(check.duration - probe.duration) > max(2.0, 0.03 * probe.duration):
            log.warning("publish: the %dp copy of %s does not match its source (%s vs %s); "
                        "uploading the original", height, src.name, check.summary(), probe.summary())
            _unlink(dst)
            return None

        log.info("publish: %s re-encoded %dx%d -> %dx%d for YouTube in %.0fs (%.1fx real time), "
                 "%d MB -> %d MB", src.name, probe.width, probe.height, width, height, elapsed,
                 elapsed / probe.duration, src_bytes // 2**20, dst.stat().st_size // 2**20)
        return dst
    except Exception as exc:  # noqa: BLE001 — a failed upscale never blocks a publish
        log.warning("publish: the YouTube copy of %s was not made (%s); uploading the original",
                    src.name, exc)
        _unlink(dst)
        return None


@contextmanager
def youtube_upload_file(src: Path, work_dir: Optional[Path] = None) -> Iterator[Path]:
    """The file to hand ``videos.insert``: the 1440p copy when one could be
    made, else ``src`` itself. The copy is deleted when the block exits,
    whether the upload returned or raised; ``src`` is never modified."""
    copy = make_youtube_copy(Path(src), work_dir)
    try:
        yield copy or Path(src)
    finally:
        _unlink(copy)


__all__ = ["HEIGHT_ENV", "THREADS_ENV", "DEFAULT_HEIGHT", "target_height", "target_width",
           "encode_timeout", "build_command", "make_youtube_copy", "youtube_upload_file"]
