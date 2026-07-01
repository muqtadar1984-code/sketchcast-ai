"""Agent 6: Video Composer — narration + native object animation → per-segment MP4.

Freemium pipeline (robust, low-memory, free):
  1. Edge TTS turns each segment's narration into an MP3.
  2. The native renderer (``native_render``) animates the slide's objects writing
     on — title, divider, bullets — paced to fit the narration, then freezes the
     finished slide for the remainder and muxes the audio in (libx264 / aac,
     1280x720, 24fps).

This replaces the flat PNG-loop (which itself replaced the OOM-prone cv2
SpeedPaint + moviepy mux): the slide now draws itself on screen, with perfect
text fidelity, deterministically and for free. Every segment ends up h264/aac
and uniform, so Agent 8's ffmpeg concat can stream-copy them with audio intact.

Entry point
-----------
compose_episode_videos()   Streamlit / worker in-process entry point.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .models import VideoManifest, VideoSegment
from .native_render import render_native_segment
from shared.tts import synthesize

from agent5_slides.theme import concept_for

logger = logging.getLogger(__name__)

# Dev-only on-frame label (segment type + index). OFF in production so no debug
# text is ever burned into shipped video. Set DEBUG_VIDEO=1 to enable locally.
DEBUG_VIDEO = os.getenv("DEBUG_VIDEO", "").strip().lower() in ("1", "true", "yes", "on")

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


def compose_episode_videos(
    script_data: dict,
    slide_manifest: dict,
    progress_callback: Optional[Callable] = None,
    branding: Optional[dict] = None,
    tts_voice: Optional[str] = None,
    allow_premium: bool = False,
) -> VideoManifest:
    """Generate a narrated, object-animated MP4 per segment.

    ``branding`` = {accent_rgb, logo_path} applies the school's colour/logo to the
    animated slide (must match what Agent 5 baked into the deck). Any field may be
    None, in which case the default Scholar style is used.

    ``tts_voice`` is a voice-registry id (default = the free Edge voice);
    ``allow_premium`` is the server-resolved gate for ElevenLabs voices. The TTS
    layer enforces the gate + spend cap and falls back to the free voice.
    """
    _b = branding or {}
    _accent = _b.get("accent_rgb")
    _logo = _b.get("logo_path")

    episodes = script_data.get("episodes", [script_data])
    episode = episodes[0] if isinstance(episodes, list) and episodes else script_data

    book_id = episode.get("book_id", script_data.get("book_id", "unknown"))
    chapter_num = episode.get("chapter_num", script_data.get("chapter_num", 0))
    episode_num = episode.get("episode_num", script_data.get("episode_num", 1))
    script_id = episode.get("script_id", script_data.get("script_id", str(uuid.uuid4())))
    episode_title = episode.get("episode_title") or "SketchCast AI"

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
        script_seg = script_segments.get(seg_id, {})
        text = (script_seg.get("text") or "").strip()
        seg_type = script_seg.get("type", slide_seg.get("type", "explore"))
        # Clean string label (e.g. "hook"), never a SegmentType repr ("SegmentType.hook").
        seg_label = getattr(seg_type, "value", None) or str(seg_type)
        est = float(script_seg.get("estimated_duration_seconds", 8) or 8)

        if progress_callback:
            progress_callback(i, total, seg_id)

        # Build the slide spec from the script (same inputs Agent 5 lays out), so
        # the animated slide matches the downloadable deck.
        heading = (script_seg.get("slide_heading") or "").strip() or episode_title
        points = [str(p).strip() for p in (script_seg.get("slide_points") or []) if str(p).strip()]
        spec = {
            "heading": heading,
            "points": points,
            # Footer is a dev label only — empty in production (no debug overlay).
            "footer": f"{seg_label} · {i + 1}/{total}" if DEBUG_VIDEO else "",
            "context": episode_title if heading != episode_title else "",
            "fallback": text,
            "visual": script_seg.get("slide_visual"),
            "number": i + 1,
            "concept": concept_for(heading, i),
        }

        audio_path: str | None = None
        out_mp4 = vid_dir / f"{seg_id}_video.mp4"
        duration = est

        # 1. TTS (provider-agnostic: free Edge default; premium ElevenLabs gated)
        if text:
            mp3 = vid_dir / f"{seg_id}_audio.mp3"
            ssml = script_seg.get("elevenlabs_text") or text
            try:
                synthesize(text, mp3, voice_id=tts_voice, allow_premium=allow_premium, ssml_text=ssml)
                audio_path = str(mp3)
                duration = _audio_duration(audio_path, ffmpeg) or est
            except Exception as exc:
                logger.error("TTS failed for %s: %s", seg_id, exc)
                audio_path = None

        # 2. Native object animation (paced to the narration) + audio → MP4
        ok = render_native_segment(
            spec, audio_path, str(out_mp4), ffmpeg,
            audio_secs=duration if audio_path else 0.0,
            accent=_accent, logo_path=_logo,
        )

        if not ok:
            logger.error("native renderer failed to build segment %s", seg_id)
            continue

        total_duration += duration
        manifest_segments.append(VideoSegment(
            segment_id=seg_id,
            type=seg_label,
            audio_path=audio_path,
            video_path=str(out_mp4),
            slide_image_path=slide_seg.get("slide_image_path"),
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
