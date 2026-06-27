"""Agent 6: Video Composer — narration + static slide → per-segment MP4.

Freemium pipeline (robust, low-memory, free):
  1. Edge TTS turns each segment's narration into an MP3.
  2. A single ffmpeg call loops the slide PNG for the audio's length and muxes
     the audio in (libx264 / aac, 1280x720, 24fps).

This replaces the old cv2 SpeedPaint + moviepy mux, which OOM'd and produced
silent, blank video. Every segment ends up h264/aac and uniform, so Agent 8's
ffmpeg concat can stream-copy them with audio intact.

Entry point
-----------
compose_episode_videos()   Streamlit in-process entry point.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .models import VideoManifest, VideoSegment
from .tts_client import synthesize

logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
VIDEO_DIR = STORAGE_DIR / "video_segments"

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")


def _ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _audio_duration(audio_path: str, ffmpeg: str) -> float:
    """Read an audio file's duration (seconds) by parsing ffmpeg output."""
    proc = subprocess.run([ffmpeg, "-i", audio_path], capture_output=True, text=True)
    m = _DUR_RE.search(proc.stderr or "")
    if m:
        h, mnt, s = m.groups()
        return int(h) * 3600 + int(mnt) * 60 + float(s)
    return 0.0


def _segment_with_audio(image: str, audio: str, out: str, ffmpeg: str) -> bool:
    cmd = [
        ffmpeg, "-y", "-loop", "1", "-i", image, "-i", audio,
        "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
        "-vf", "scale=1280:720", "-r", "24",
        "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", out,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode == 0 and Path(out).exists() and Path(out).stat().st_size > 0


def _segment_silent(image: str, duration: float, out: str, ffmpeg: str) -> bool:
    """Fallback when TTS fails — still produces video + a silent audio track."""
    dur = max(2.0, duration)
    cmd = [
        ffmpeg, "-y", "-loop", "1", "-i", image,
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
        "-vf", "scale=1280:720", "-r", "24",
        "-c:a", "aac", "-b:a", "128k", "-t", f"{dur:.2f}", "-movflags", "+faststart", out,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode == 0 and Path(out).exists() and Path(out).stat().st_size > 0


def compose_episode_videos(
    script_data: dict,
    slide_manifest: dict,
    progress_callback: Optional[Callable] = None,
) -> VideoManifest:
    """Generate a narrated MP4 per segment (slide background + Edge TTS voice)."""
    episodes = script_data.get("episodes", [script_data])
    episode = episodes[0] if isinstance(episodes, list) and episodes else script_data

    book_id = episode.get("book_id", script_data.get("book_id", "unknown"))
    chapter_num = episode.get("chapter_num", script_data.get("chapter_num", 0))
    episode_num = episode.get("episode_num", script_data.get("episode_num", 1))
    script_id = episode.get("script_id", script_data.get("script_id", str(uuid.uuid4())))

    vid_dir = VIDEO_DIR / book_id / f"chapter_{chapter_num}"
    vid_dir.mkdir(parents=True, exist_ok=True)

    slide_segments = slide_manifest.get("segments", [])
    script_segments = {seg["segment_id"]: seg for seg in episode.get("segments", [])}

    ffmpeg = _ffmpeg_exe()
    manifest_segments: list[VideoSegment] = []
    total = len(slide_segments)
    total_duration = 0.0

    for i, slide_seg in enumerate(slide_segments):
        seg_id = slide_seg["segment_id"]
        slide_png = slide_seg.get("slide_image_path")
        script_seg = script_segments.get(seg_id, {})
        text = (script_seg.get("text") or "").strip()
        seg_type = script_seg.get("type", slide_seg.get("type", "explore"))
        est = float(script_seg.get("estimated_duration_seconds", 8) or 8)

        if progress_callback:
            progress_callback(i, total, seg_id)

        if not slide_png or not Path(slide_png).exists():
            logger.warning("No slide image for %s — skipping", seg_id)
            continue

        audio_path: str | None = None
        out_mp4 = vid_dir / f"{seg_id}_video.mp4"
        duration = est

        # 1. TTS
        if text:
            mp3 = vid_dir / f"{seg_id}_audio.mp3"
            try:
                synthesize(text, mp3)
                audio_path = str(mp3)
                duration = _audio_duration(audio_path, ffmpeg) or est
            except Exception as exc:
                logger.error("TTS failed for %s: %s", seg_id, exc)
                audio_path = None

        # 2. Slide + audio (or silent fallback) → MP4
        if audio_path:
            ok = _segment_with_audio(slide_png, audio_path, str(out_mp4), ffmpeg)
        else:
            ok = _segment_silent(slide_png, duration, str(out_mp4), ffmpeg)

        if not ok:
            logger.error("ffmpeg failed to build segment %s", seg_id)
            continue

        total_duration += duration
        manifest_segments.append(VideoSegment(
            segment_id=seg_id,
            type=seg_type,
            audio_path=audio_path,
            video_path=str(out_mp4),
            slide_image_path=slide_png,
            audio_duration_seconds=round(duration, 2),
            visual_action=slide_seg.get("visual_action", "GHOST_ONLY"),
        ))

    if progress_callback:
        progress_callback(total, total, "done")

    vid_count = sum(1 for s in manifest_segments if s.video_path)
    manifest = VideoManifest(
        manifest_id=str(uuid.uuid4()),
        script_id=script_id,
        book_id=book_id,
        chapter_num=chapter_num,
        episode_num=episode_num,
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_segments=total,
        video_segments_count=vid_count,
        total_duration_seconds=round(total_duration, 2),
        segments=manifest_segments,
    )

    manifest_path = vid_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    logger.info("Video manifest: %d segments, %d built, %.1fs", total, vid_count, total_duration)
    return manifest


def load_manifest(book_id: str, chapter_num: int) -> Optional[dict]:
    """Load a saved video manifest from disk."""
    path = VIDEO_DIR / book_id / f"chapter_{chapter_num}" / "manifest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
