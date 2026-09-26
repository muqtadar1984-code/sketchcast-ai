"""The final video is PROBED before it is shipped.

A lesson used to be marked done the moment ffmpeg's concat returned 0 and
the file was non-empty. Nothing ever read the file back: a container whose
streams cannot be decoded, a moov atom that never landed, a duration that
does not match the manifest — all of them shipped to the reviewer as a
finished video, and the first reader was a phone's video player (States of
Matter, 2026-09-26: a broken-media icon on a kit that read `done`). The
render is the one place that has the bytes, ffmpeg and the expected length
in hand at once, so it is where the check belongs.

Two passes. The HEADER pass reads the container's streams and duration —
`ffprobe` where it exists (the apt ffmpeg in production ships it), else
`ffmpeg -i` parsed the way it has always been parsed. The DECODE pass runs
`ffmpeg -v error -i file -f null -`: every frame is decoded and thrown away,
and any error ffmpeg prints is a defect in the file, not a warning.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# How far the container's duration may sit from the manifest's before the
# file is refused: the concat of N segments is never exact to the frame.
DURATION_TOLERANCE_SECS = 2.0
DURATION_TOLERANCE_FRAC = 0.03
DECODE_TIMEOUT_SECS = 600


@dataclass
class VideoProbe:
    path: Path
    duration: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None
    errors: list[str] = field(default_factory=list)
    tool: str = "ffprobe"

    @property
    def ok(self) -> bool:
        return (not self.errors and self.duration is not None
                and self.video_codec is not None and self.audio_codec is not None)

    def summary(self) -> str:
        size = f"{self.width}x{self.height}" if self.width and self.height else "?x?"
        dur = f"{self.duration:.1f}s" if self.duration is not None else "no duration"
        return f"{dur} {self.video_codec or 'no video'}/{self.audio_codec or 'no audio'} {size}"


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def ffprobe_exe() -> str | None:
    """ffprobe when the system has one; the bundled imageio binary ships none."""
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    sibling = Path(ffmpeg_exe()).with_name("ffprobe")
    return str(sibling) if sibling.exists() else None


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\d+:\d+.*?: Video: (\w+).*?(\d{2,5})x(\d{2,5})")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*?: Audio: (\w+)")


def _header_ffprobe(path: Path, exe: str) -> VideoProbe:
    import json
    out = VideoProbe(path=path, tool="ffprobe")
    proc = subprocess.run([exe, "-v", "error", "-print_format", "json", "-show_format",
                           "-show_streams", str(path)], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        out.errors.append(f"ffprobe rc={proc.returncode}: {(proc.stderr or '').strip()[-300:]}")
        return out
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        out.errors.append(f"ffprobe output unreadable: {exc}")
        return out
    try:
        out.duration = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        out.duration = None
    for st in data.get("streams") or []:
        kind = st.get("codec_type")
        if kind == "video" and out.video_codec is None:
            out.video_codec = str(st.get("codec_name") or "") or None
            try:
                out.width, out.height = int(st.get("width") or 0) or None, int(st.get("height") or 0) or None
            except (TypeError, ValueError):
                pass
        elif kind == "audio" and out.audio_codec is None:
            out.audio_codec = str(st.get("codec_name") or "") or None
    return out


def _header_ffmpeg(path: Path, exe: str) -> VideoProbe:
    """`ffmpeg -i` prints the container summary to stderr and exits 1 for
    want of an output — the exit code says nothing, the text everything."""
    out = VideoProbe(path=path, tool="ffmpeg")
    proc = subprocess.run([exe, "-hide_banner", "-i", str(path)], capture_output=True,
                          text=True, timeout=120)
    text = proc.stderr or ""
    m = _DURATION_RE.search(text)
    if m:
        out.duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    v = _VIDEO_RE.search(text)
    if v:
        out.video_codec = v.group(1)
        out.width, out.height = int(v.group(2)), int(v.group(3))
    a = _AUDIO_RE.search(text)
    if a:
        out.audio_codec = a.group(1)
    if out.video_codec is None and out.audio_codec is None:
        tail = text.strip().splitlines()[-1] if text.strip() else "no output"
        out.errors.append(f"no streams found: {tail[-300:]}")
    return out


def _decode_errors(path: Path, exe: str) -> list[str]:
    proc = subprocess.run([exe, "-v", "error", "-i", str(path), "-f", "null", "-"],
                          capture_output=True, text=True, timeout=DECODE_TIMEOUT_SECS)
    lines = [ln.strip() for ln in (proc.stderr or "").splitlines() if ln.strip()]
    if proc.returncode != 0 and not lines:
        lines.append(f"decode rc={proc.returncode}")
    # one line per distinct message: a truncated file repeats its complaint
    # once per damaged packet
    seen: list[str] = []
    for ln in lines:
        if ln not in seen:
            seen.append(ln)
    return seen[:8]


def probe_video(path: Path | str, *, decode: bool = True) -> VideoProbe:
    """Header, then (by default) a full decode. Never raises on a bad file:
    the verdict is in `errors` and `ok`; a missing file is an error too."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return VideoProbe(path=path, errors=["file missing or empty"])
    ffmpeg = ffmpeg_exe()
    probe_exe = ffprobe_exe()
    try:
        out = _header_ffprobe(path, probe_exe) if probe_exe else _header_ffmpeg(path, ffmpeg)
        if decode:
            out.errors.extend(_decode_errors(path, ffmpeg))
    except (subprocess.TimeoutExpired, OSError) as exc:
        return VideoProbe(path=path, errors=[f"probe failed to run: {exc}"])
    return out


def check_final_video(path: Path | str, expected_secs: float | None) -> VideoProbe:
    """The gate the renderer runs after concat. Raises RuntimeError naming
    the defect; returns the probe when the file is a video of the expected
    length with a decodable video and audio stream."""
    probe = probe_video(path)
    if probe.errors:
        raise RuntimeError(f"final video is unreadable ({probe.tool}): " + "; ".join(probe.errors))
    if probe.video_codec is None:
        raise RuntimeError("final video has no video stream")
    if probe.audio_codec is None:
        raise RuntimeError("final video has no audio stream")
    if probe.duration is None:
        raise RuntimeError("final video reports no duration")
    if expected_secs:
        allowed = max(DURATION_TOLERANCE_SECS, DURATION_TOLERANCE_FRAC * float(expected_secs))
        if abs(probe.duration - float(expected_secs)) > allowed:
            raise RuntimeError(
                f"final video is {probe.duration:.1f}s but the manifest expects "
                f"{float(expected_secs):.1f}s (tolerance {allowed:.1f}s)")
    return probe


__all__ = ["VideoProbe", "probe_video", "check_final_video", "ffprobe_exe", "ffmpeg_exe",
           "DURATION_TOLERANCE_SECS", "DURATION_TOLERANCE_FRAC"]
