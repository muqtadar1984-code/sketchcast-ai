"""Merge audio manifest and Scribe SVGs into unified timeline."""

from __future__ import annotations

import logging
import uuid

from agent6_player.models import TimelineSegment, UnifiedTimeline, ScribePath

logger = logging.getLogger(__name__)


def build_unified_timeline(
    audio_manifest: dict,
    animation_manifest: dict,
    script_data: dict | None = None,
) -> UnifiedTimeline:
    """Build a unified timeline by merging audio and animation manifests.

    Reads paths directly from the animation manifest JSON (populated by Agent 4).
    Uses actual audio timestamps from Agent 5 only.
    """
    # Index animation segments
    anim_segments = {
        seg["segment_id"]: seg
        for seg in animation_manifest.get("segments", [])
    }

    # Index script segments
    script_segments: dict[str, dict] = {}
    if script_data:
        episodes = script_data.get("episodes", [script_data])
        ep = episodes[0] if isinstance(episodes, list) and episodes else script_data
        for seg in ep.get("segments", []):
            script_segments[seg["segment_id"]] = seg

    timeline_segments: list[TimelineSegment] = []

    for audio_seg in audio_manifest.get("segments", []):
        seg_id = audio_seg["segment_id"]
        audio_start = audio_seg["master_start_seconds"]
        audio_end = audio_seg["master_end_seconds"]

        # Get Animation Data
        anim = anim_segments.get(seg_id, {})

        # KEY: Retrieve the paths list directly from the manifest JSON
        raw_paths = anim.get("paths", [])
        parsed_paths: list[ScribePath] = []
        if raw_paths:
            for p in raw_paths:
                parsed_paths.append(ScribePath(**p))

        visual_action = anim.get(
            "visual_action",
            "DRAW_START" if raw_paths else "GHOST_ONLY",
        )

        # Get Script Text
        script_seg = script_segments.get(seg_id, {})
        seg_text = script_seg.get("text", "")
        seg_type = script_seg.get("type", "explore")

        timeline_segments.append(TimelineSegment(
            segment_id=seg_id,
            type=seg_type,
            audio_start=round(audio_start, 2),
            audio_end=round(audio_end, 2),
            visual_action=visual_action,
            paths=parsed_paths if parsed_paths else None,
            scribe_keyframes=anim.get("scribe_keyframes"),
            pause_for_question=audio_seg.get("pause_for_question", False),
            pause_at_second=audio_end if audio_seg.get("pause_for_question", False) else None,
            segment_text=seg_text,
        ))

    # Get episode title from script
    episode_title = ""
    if script_data:
        episodes = script_data.get("episodes", [script_data])
        ep = episodes[0] if isinstance(episodes, list) and episodes else script_data
        episode_title = ep.get("episode_title", "")

    timeline = UnifiedTimeline(
        timeline_id=str(uuid.uuid4()),
        script_id=audio_manifest.get("script_id", ""),
        book_id=audio_manifest.get("book_id", ""),
        chapter_num=audio_manifest.get("chapter_num", 0),
        episode_num=audio_manifest.get("episode_num", 1),
        episode_title=episode_title,
        total_duration_seconds=round(
            audio_manifest.get("total_duration_seconds", 0), 2,
        ),
        master_audio_path=audio_manifest.get("master_audio_path", ""),
        segments=timeline_segments,
    )

    logger.info(
        "Unified timeline built: %d segments, %.1fs total, %d with paths",
        len(timeline_segments),
        timeline.total_duration_seconds,
        sum(1 for s in timeline_segments if s.paths),
    )
    return timeline
