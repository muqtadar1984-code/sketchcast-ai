"""Agent 8 Render: Final Video — concatenates segment videos into one MP4.

Uses ffmpeg's concat demuxer via a subprocess instead of loading every clip
into memory with moviepy. moviepy's ``concatenate_videoclips`` holds all clips
in RAM and re-encodes in-process, which OOM-kills the container on the
Streamlit Community tier (~1 GB). ffmpeg streams from disk, so memory stays flat
regardless of how many segments there are.

Entry point
-----------
render_final_video()   Streamlit in-process entry point.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .models import FinalVideoManifest

logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
FINAL_DIR = STORAGE_DIR / "final_videos"


def _ffmpeg_exe() -> str:
    """Locate an ffmpeg binary.

    Prefers the system ffmpeg (installed via packages.txt on Streamlit Cloud),
    falling back to the binary bundled with imageio-ffmpeg (a moviepy dep, so
    it is always installed).
    """
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def render_final_video(
    video_manifest: dict,
    progress_callback: Optional[Callable] = None,
) -> FinalVideoManifest:
    """Concatenate all segment videos into one final MP4 with ffmpeg.

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
    book_id = video_manifest.get("book_id", "unknown")
    chapter_num = video_manifest.get("chapter_num", 0)
    episode_num = video_manifest.get("episode_num", 1)
    script_id = video_manifest.get("script_id", "")

    final_dir = FINAL_DIR / book_id / f"chapter_{chapter_num}"
    final_dir.mkdir(parents=True, exist_ok=True)

    segments = video_manifest.get("segments", [])
    total = len(segments)

    # Collect existing segment video paths and sum their durations.
    valid_paths: list[Path] = []
    missing: list[str] = []
    total_duration = 0.0
    for i, seg in enumerate(segments):
        seg_id = seg.get("segment_id", f"s{i + 1:03d}")
        video_path = seg.get("video_path")

        if progress_callback:
            progress_callback(i, total, seg_id)

        if video_path and Path(video_path).exists():
            valid_paths.append(Path(video_path).resolve())
            total_duration += float(seg.get("audio_duration_seconds", 0) or 0)
        else:
            logger.warning("No video file for segment %s", seg_id)
            missing.append(seg_id)

    if not valid_paths:
        raise RuntimeError("No valid video segments found to concatenate")

    # A lesson missing part of itself is not a successful lesson. This used to
    # concatenate whatever happened to exist and report success: 27 of 29
    # segments shipped as a finished video, with the two gaps visible only as
    # a log line nobody read. The narration jumps, and the student is simply
    # not taught the missing part.
    if missing:
        raise RuntimeError(
            f"{len(missing)} of {total} segments never rendered "
            f"({', '.join(missing[:8])}{'...' if len(missing) > 8 else ''}) — "
            "refusing to ship a lesson with holes in it")

    output_path = final_dir / f"episode_{episode_num}_final.mp4"

    # Build the ffmpeg concat list file (one `file '<path>'` line per segment).
    list_path = final_dir / "concat_list.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in valid_paths:
            safe = str(p).replace("'", "'\\''")  # escape single quotes
            f.write(f"file '{safe}'\n")

    ffmpeg = _ffmpeg_exe()
    base = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_path)]

    # First try stream copy — near-instant, near-zero memory. Works when every
    # segment shares codec/timebase (they do: all produced by Agent 6's moviepy
    # pipeline at 1280x720 / libx264 / aac / 24fps).
    copy_cmd = base + ["-c", "copy", "-movflags", "+faststart", str(output_path)]
    proc = subprocess.run(copy_cmd, capture_output=True, text=True)

    if proc.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        logger.warning(
            "ffmpeg stream-copy concat failed (rc=%s); re-encoding. stderr tail: %s",
            proc.returncode, (proc.stderr or "")[-500:],
        )
        # Re-encode fallback for non-uniform inputs. Still bounded memory —
        # ffmpeg processes frame-by-frame, unlike moviepy's in-RAM approach.
        reencode_cmd = base + [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-r", "24", "-movflags", "+faststart",
            str(output_path),
        ]
        proc = subprocess.run(reencode_cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg concat failed: {(proc.stderr or '')[-1000:]}"
            )

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
        total_segments=len(valid_paths),
    )

    # Save manifest to disk
    manifest_path = final_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    logger.info(
        "Final video rendered (ffmpeg concat): %d segments, %.1fs -> %s",
        len(valid_paths), total_duration, output_path.name,
    )
    return manifest


def load_manifest(book_id: str, chapter_num: int) -> Optional[dict]:
    """Load a saved final video manifest from disk."""
    path = FINAL_DIR / book_id / f"chapter_{chapter_num}" / "manifest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
