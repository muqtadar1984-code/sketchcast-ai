"""Build the audio manifest with real measured timings."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent5_audio.models import AudioManifest, SegmentAudio

GAP_MS = 300  # must match stitcher.GAP_MS


def build_manifest(
    script_id: str,
    book_id: str,
    chapter_num: int,
    episode_num: int,
    voice_id: str,
    model: str,
    segment_results: list[dict],
    segment_durations: list[float],
    master_audio_path: str,
    total_duration: float,
) -> AudioManifest:
    """
    Build AudioManifest from measured durations.

    segment_results: list of {segment_id, audio_path, pause_for_question}
    segment_durations: actual duration per segment in seconds (from stitcher)
    """
    gap_seconds = GAP_MS / 1000.0
    cursor = 0.0  # running position in master timeline
    segments: list[SegmentAudio] = []

    for i, (result, dur) in enumerate(zip(segment_results, segment_durations)):
        pause = result.get("pause_for_question", False)
        seg = SegmentAudio(
            segment_id=result["segment_id"],
            audio_path=result["audio_path"],
            actual_duration_seconds=round(dur, 2),
            master_start_seconds=round(cursor, 2),
            master_end_seconds=round(cursor + dur, 2),
            pause_for_question=pause,
            pause_point=pause,
        )
        segments.append(seg)
        cursor += dur
        # Account for 300ms gap after every segment except last
        if i < len(segment_results) - 1:
            cursor += gap_seconds

    manifest = AudioManifest(
        audio_manifest_id=str(uuid.uuid4()),
        script_id=script_id,
        book_id=book_id,
        chapter_num=chapter_num,
        episode_num=episode_num,
        generated_at=datetime.now(timezone.utc).isoformat(),
        voice_id=voice_id,
        model=model,
        master_audio_path=master_audio_path,
        total_duration_seconds=round(total_duration, 2),
        segments=segments,
    )
    return manifest


def save_manifest(manifest: AudioManifest, output_path: str | Path) -> Path:
    """Write manifest JSON to disk."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest.model_dump(), f, indent=2, ensure_ascii=False)
    return output_path


def load_manifest(path: str | Path) -> AudioManifest | None:
    """Load manifest from JSON file."""
    path = Path(path)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return AudioManifest(**json.load(f))
