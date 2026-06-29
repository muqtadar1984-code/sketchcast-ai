"""Agent 6: native object renderer — the freemium lesson-video engine.

Productionized from the spike (``spike/native_render.py``). Instead of looping a
flat slide PNG for the narration's length, this animates the slide's OBJECTS in
teaching order — the context line and title write on, the divider grows, then
each bullet writes on left→right with a pen at the frontier — then the finished
slide freezes for the rest of the narration. It draws from the SAME canonical
layout as the static slide (``agent5_slides.slide_builder.compose_slide``), so
text fidelity is perfect, it is multilingual, deterministic, instant and free,
and the final frame is pixel-identical to the downloadable deck's slide.

Key production concerns handled here (vs. the spike):
  * the write-on phase is sized to fit *inside* the narration so the slide is
    finished before the voice moves on, and the clip length == the audio length
    (the last frame is frozen with ffmpeg ``tpad`` and trimmed with ``-shortest``);
  * a silent fallback (``anullsrc`` + ``-t``) when TTS is unavailable;
  * branding (accent colour + logo) threaded through;
  * output codec/format identical to the old engine (libx264 / yuv420p / 1280x720
    / 24 fps / aac 128k) so Agent 8's concat can still stream-copy the segments.
"""

from __future__ import annotations

import logging
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from agent5_slides.slide_builder import _BG, _HEIGHT as H, _WIDTH as W, compose_slide

logger = logging.getLogger(__name__)

FPS = 24
_MIN_HOLD_SECS = 0.8     # finished slide must dwell at least this long after writing
_MIN_WRITE_SECS = 1.2    # don't rush the whole slide on in under ~a second
_MAX_WRITE_SECS = 7.0    # nor crawl forever on long narration
_SILENT_HOLD_SECS = 1.4  # hold after writing when there is no audio to pace against
_FREEZE_SECS = 3600      # tpad clone duration; -shortest / -t does the real trimming


def _draw_pen(draw: ImageDraw.ImageDraw, x: int, y: int) -> None:
    """A simple pen/marker at (x, y), tip pointing down-left — the write frontier."""
    body = (x + 30, y - 84)
    draw.line([(x + 3, y + 4), (body[0] + 3, body[1] + 4)], fill=(0, 0, 0), width=10)   # shadow
    draw.line([(x, y), body], fill=(35, 35, 40), width=12)                              # barrel
    draw.line([(x, y), body], fill=(120, 120, 130), width=4)                            # highlight
    draw.polygon([(x - 7, y - 1), (x + 7, y + 1), (x, y + 14)], fill=(15, 15, 18))      # nib


def _write_speed(total_width_px: int, audio_secs: float, fps: int) -> int:
    """Pixels revealed per frame, so the write-on phase fits inside the narration.

    Picks a target write duration (a fraction of the audio, clamped), then sets
    the pen speed so revealing ``total_width_px`` takes about that long. When
    there is no audio, paces off the content alone.
    """
    if audio_secs and audio_secs > 0:
        budget = max(_MIN_WRITE_SECS, audio_secs - _MIN_HOLD_SECS)
        target = min(budget, _MAX_WRITE_SECS, max(_MIN_WRITE_SECS, audio_secs * 0.55))
    else:
        # No narration to pace against: a steady, readable pace.
        target = max(_MIN_WRITE_SECS, min(_MAX_WRITE_SECS, total_width_px / (28 * fps)))
    target_frames = max(1, int(round(target * fps)))
    return max(8, math.ceil(total_width_px / target_frames))


def _ffmpeg_filter() -> str:
    # Freeze the last frame, then normalise size + pixel format for clean concat.
    return (
        f"[0:v]tpad=stop_mode=clone:stop_duration={_FREEZE_SECS},"
        "scale=1280:720,format=yuv420p[v]"
    )


def render_native_segment(
    spec: dict,
    audio_path: str | None,
    output_video: str | Path,
    ffmpeg: str,
    audio_secs: float = 0.0,
    *,
    accent: tuple[int, int, int] | None = None,
    logo_path: str | None = None,
    fps: int = FPS,
) -> bool:
    """Render one narrated lesson segment as an object-animated MP4.

    ``spec`` = ``{heading, points, footer, context, fallback, visual}`` (the same
    inputs Agent 5 lays out; ``visual`` is an optional composable diagram).
    ``audio_secs`` is the narration length used to pace the animation. Returns True
    on success. Never raises on a render/encode failure — the caller decides
    whether to skip the segment.
    """
    try:
        canvas, anim, static = compose_slide(
            heading=spec.get("heading", ""),
            points=spec.get("points") or [],
            footer_text=spec.get("footer", ""),
            context_title=spec.get("context", ""),
            fallback_text=spec.get("fallback", ""),
            accent=accent,
            logo_path=logo_path,
            visual=spec.get("visual"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("native render layout failed: %s", exc)
        return False

    total_width = sum((x1 - x0) for el in anim for (x0, _y0, x1, _y1) in el) or 1
    px = _write_speed(total_width, audio_secs, fps)

    out = Path(output_video)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="native_"))
    try:
        mask = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        for (x0, y0, x1, y1) in static:  # logo/footer visible from the first frame
            md.rectangle([x0, y0, x1, y1], fill=255)

        idx = 0

        def emit(pen: tuple[int, int] | None) -> None:
            nonlocal idx
            comp = Image.composite(canvas, Image.new("RGB", (W, H), _BG), mask)
            if pen:
                _draw_pen(ImageDraw.Draw(comp), int(pen[0]), int(pen[1]))
            comp.save(tmp / f"f{idx:05d}.png")
            idx += 1

        for element in anim:
            for (x0, y0, x1, y1) in element:
                x = x0
                while x < x1:
                    md.rectangle([x0, y0, x, y1], fill=255)
                    emit((x, (y0 + y1) // 2))
                    x += px
                md.rectangle([x0, y0, x1, y1], fill=255)  # snap the line fully on
        emit(None)  # final clean frame (no pen) — this is what tpad freezes onto

        write_secs = idx / fps
        ok = _encode(tmp, fps, audio_path, write_secs, audio_secs, out, ffmpeg)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return ok


def _encode(
    frame_dir: Path,
    fps: int,
    audio_path: str | None,
    write_secs: float,
    audio_secs: float,
    out: Path,
    ffmpeg: str,
) -> bool:
    """Encode the write-on frames, freeze the last frame to fill the clip, mux audio.

    The clip length is set explicitly with ``-t`` (not ``-shortest``, which is
    unreliable through a ``tpad`` filtergraph): the narration length for a voiced
    segment, or write + a short hold when silent. ``tpad`` clones the final frame
    so the finished slide dwells while the rest of the narration plays.
    """
    seq = str(frame_dir / "f%05d.png")
    base = [ffmpeg, "-y", "-framerate", str(fps), "-i", seq]
    common = [
        "-filter_complex", _ffmpeg_filter(), "-map", "[v]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps),
        # Force uniform audio params so every segment (voiced or silent) shares a
        # codec/rate/layout and Agent 8's concat can stream-copy them.
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", "-movflags", "+faststart",
    ]
    if audio_path:
        # Run at least until the narration ends (and never cut a still-drawing slide).
        total = max(audio_secs if audio_secs > 0 else write_secs, write_secs + 0.2)
        cmd = base + ["-i", str(audio_path)] + common + ["-map", "1:a", "-t", f"{total:.2f}", str(out)]
    else:
        total = write_secs + _SILENT_HOLD_SECS
        cmd = (
            base
            + ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
            + common
            + ["-map", "1:a", "-t", f"{total:.2f}", str(out)]
        )
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        logger.error("native render ffmpeg failed (rc=%s): %s", proc.returncode, (proc.stderr or "")[-500:])
        return False
    return out.exists() and out.stat().st_size > 0
