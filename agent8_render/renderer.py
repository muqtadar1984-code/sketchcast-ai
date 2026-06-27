"""Agent 8 Render: Final Video — concatenates segment videos into one MP4.

Entry point
-----------
render_final_video()   Streamlit in-process entry point.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .models import FinalVideoManifest

logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
FINAL_DIR = STORAGE_DIR / "final_videos"


def render_final_video(
    video_manifest: dict,
    progress_callback: Optional[Callable] = None,
) -> FinalVideoManifest:
    """Concatenate all segment videos into one final MP4.

    Parameters
    ----------
    video_manifest : dict
        Output from Agent 6 (VideoManifest.model_dump()).
    progress_callback : callable, optional
        ``fn(current_index, total, segment_id)``

    Returns
    -------
    FinalVideoManifest
    """
    from moviepy import ColorClip, VideoFileClip, concatenate_videoclips

    book_id = video_manifest.get("book_id", "unknown")
    chapter_num = video_manifest.get("chapter_num", 0)
    episode_num = video_manifest.get("episode_num", 1)
    script_id = video_manifest.get("script_id", "")

    final_dir = FINAL_DIR / book_id / f"chapter_{chapter_num}"
    final_dir.mkdir(parents=True, exist_ok=True)

    segments = video_manifest.get("segments", [])
    total = len(segments)

    # Collect video clips
    clips = []
    # Short black transition clip (300ms)
    transition = ColorClip(size=(1280, 720), color=(0, 0, 0), duration=0.3)
    transition = transition.with_fps(24)

    for i, seg in enumerate(segments):
        seg_id = seg.get("segment_id", f"s{i+1:03d}")
        video_path = seg.get("video_path")

        if progress_callback:
            progress_callback(i, total, seg_id)

        if video_path and Path(video_path).exists():
            try:
                clip = VideoFileClip(video_path)
                clips.append(clip)

                # Add transition between segments (not after last)
                if i < total - 1:
                    clips.append(transition)

            except Exception as exc:
                logger.warning("Could not load video %s: %s", video_path, exc)
        else:
            logger.warning("No video file for segment %s", seg_id)

    if not clips:
        raise RuntimeError("No valid video segments found to concatenate")

    # Concatenate all clips
    logger.info("Concatenating %d clips...", len(clips))
    final_clip = concatenate_videoclips(clips, method="compose")

    output_path = final_dir / f"episode_{episode_num}_final.mp4"
    final_clip.write_videofile(
        str(output_path),
        codec="libx264",
        audio_codec="aac",
        fps=24,
        logger=None,  # suppress moviepy progress bar
    )

    total_duration = final_clip.duration

    # Clean up
    for clip in clips:
        try:
            clip.close()
        except Exception:
            pass
    final_clip.close()

    if progress_callback:
        progress_callback(total, total, "done")

    manifest = FinalVideoManifest(
        manifest_id=str(uuid.uuid4()),
        script_id=script_id,
        book_id=book_id,
        chapter_num=chapter_num,
        episode_num=episode_num,
        generated_at=datetime.now(timezone.utc).isoformat(),
        final_video_path=str(output_path),
        total_duration_seconds=round(total_duration, 2),
        total_segments=total,
    )

    # Save manifest to disk
    manifest_path = final_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    logger.info(
        "Final video rendered: %d segments, %.1fs -> %s",
        total, total_duration, output_path.name,
    )
    return manifest


def load_manifest(book_id: str, chapter_num: int) -> Optional[dict]:
    """Load a saved final video manifest from disk."""
    path = FINAL_DIR / book_id / f"chapter_{chapter_num}" / "manifest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
