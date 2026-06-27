"""Agent 6: Video Composer — orchestrates TTS + SpeedPaint + final compositing.

Entry point
-----------
compose_episode_videos()   Streamlit in-process entry point.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from pydub import AudioSegment

from .models import VideoManifest, VideoSegment
from .speedpaint import create_speedpaint_video, create_static_video
from .tts_client import synthesize_segment

logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
VIDEO_DIR = STORAGE_DIR / "video_segments"


def _get_audio_duration(audio_path: str) -> float:
    """Return audio duration in seconds using pydub."""
    try:
        audio = AudioSegment.from_file(audio_path)
        return audio.duration_seconds
    except Exception as exc:
        logger.warning("Could not read audio duration for %s: %s", audio_path, exc)
        return 10.0  # fallback 10 seconds


def _combine_video_audio(video_path: str, audio_path: str, output_path: str) -> str:
    """Merge a silent video with an audio track using moviepy."""
    from moviepy import AudioFileClip, VideoFileClip

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    video_clip = VideoFileClip(video_path)
    audio_clip = AudioFileClip(audio_path)

    # Trim video to audio duration if needed
    if video_clip.duration > audio_clip.duration + 0.5:
        video_clip = video_clip.subclipped(0, audio_clip.duration)

    final = video_clip.with_audio(audio_clip)
    final.write_videofile(
        str(out),
        codec="libx264",
        audio_codec="aac",
        fps=24,
        logger=None,  # suppress moviepy progress bar
    )

    # Clean up clips
    video_clip.close()
    audio_clip.close()
    final.close()

    logger.info("Combined video+audio: %s", out.name)
    return str(out)


def compose_episode_videos(
    script_data: dict,
    slide_manifest: dict,
    progress_callback: Optional[Callable] = None,
) -> VideoManifest:
    """Generate TTS audio + SpeedPaint video for all segments.

    Parameters
    ----------
    script_data : dict
        Full script dict with ``episodes[0].segments``.
    slide_manifest : dict
        Output from Agent 5 (SlideManifest.model_dump()).
    progress_callback : callable, optional
        ``fn(current_index, total, segment_id)``

    Returns
    -------
    VideoManifest
    """
    episodes = script_data.get("episodes", [script_data])
    episode = episodes[0] if isinstance(episodes, list) and episodes else script_data

    book_id = episode.get("book_id", script_data.get("book_id", "unknown"))
    chapter_num = episode.get("chapter_num", script_data.get("chapter_num", 0))
    episode_num = episode.get("episode_num", script_data.get("episode_num", 1))
    script_id = episode.get("script_id", script_data.get("script_id", str(uuid.uuid4())))

    vid_dir = VIDEO_DIR / book_id / f"chapter_{chapter_num}"
    vid_dir.mkdir(parents=True, exist_ok=True)

    # Index slide segments by ID
    slide_segments = {
        seg["segment_id"]: seg
        for seg in slide_manifest.get("segments", [])
    }

    # Index script segments for text
    script_segments = {
        seg["segment_id"]: seg
        for seg in episode.get("segments", [])
    }

    manifest_segments: list[VideoSegment] = []
    total = len(slide_manifest.get("segments", []))
    total_duration = 0.0

    for i, slide_seg in enumerate(slide_manifest.get("segments", [])):
        seg_id = slide_seg["segment_id"]
        visual_action = slide_seg.get("visual_action", "GHOST_ONLY")
        slide_image_path = slide_seg.get("slide_image_path")

        if progress_callback:
            progress_callback(i, total, seg_id)

        # Get script text for TTS
        script_seg = script_segments.get(seg_id, {})
        tts_text = script_seg.get("elevenlabs_text", script_seg.get("text", ""))
        seg_type = script_seg.get("type", "explore")

        audio_path_str: str | None = None
        video_path_str: str | None = None
        audio_duration = 0.0

        # Step 1: Generate TTS audio
        if tts_text.strip():
            mp3_path = vid_dir / f"{seg_id}_audio.mp3"
            try:
                synthesize_segment(tts_text, mp3_path)
                audio_path_str = str(mp3_path)
                audio_duration = _get_audio_duration(str(mp3_path))
            except Exception as exc:
                logger.error("TTS failed for %s: %s", seg_id, exc)
                audio_duration = float(script_seg.get("estimated_duration_seconds", 10))
        else:
            audio_duration = float(script_seg.get("estimated_duration_seconds", 10))

        # Step 2: Generate video
        if slide_image_path and Path(slide_image_path).exists():
            silent_mp4 = vid_dir / f"{seg_id}_silent.mp4"

            if visual_action == "DRAW_START":
                create_speedpaint_video(
                    slide_image_path, audio_duration, str(silent_mp4),
                )
            else:
                # DRAW_CONTINUE, GHOST_ONLY: static hold
                create_static_video(
                    slide_image_path, audio_duration, str(silent_mp4),
                )

            # Step 3: Combine video + audio
            if audio_path_str:
                final_mp4 = vid_dir / f"{seg_id}_video.mp4"
                try:
                    _combine_video_audio(str(silent_mp4), audio_path_str, str(final_mp4))
                    video_path_str = str(final_mp4)
                except Exception as exc:
                    logger.error("Video compositing failed for %s: %s", seg_id, exc)
                    video_path_str = str(silent_mp4)  # fallback: silent video
            else:
                video_path_str = str(silent_mp4)

        total_duration += audio_duration

        manifest_segments.append(VideoSegment(
            segment_id=seg_id,
            type=seg_type,
            audio_path=audio_path_str,
            video_path=video_path_str,
            slide_image_path=slide_image_path,
            audio_duration_seconds=round(audio_duration, 2),
            visual_action=visual_action,
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

    # Save manifest to disk
    manifest_path = vid_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    logger.info(
        "Video manifest built: %d segments, %d with video, %.1fs total",
        total, vid_count, total_duration,
    )
    return manifest


def load_manifest(book_id: str, chapter_num: int) -> Optional[dict]:
    """Load a saved video manifest from disk."""
    path = VIDEO_DIR / book_id / f"chapter_{chapter_num}" / "manifest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
