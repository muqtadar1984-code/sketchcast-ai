"""Merge audio manifest (Agent 5) and animation manifest (Agent 4) into unified timeline."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from agent6_player.models import TimelineSegment, UnifiedTimeline

logger = logging.getLogger(__name__)


def build_unified_timeline(
    audio_manifest: dict,
    animation_manifest: dict,
    script_data: dict | None = None,
) -> UnifiedTimeline:
    """
    Build a unified timeline by merging audio and animation manifests.

    Uses actual audio timestamps from Agent 5 only — never estimated durations.
    Animation trigger times are calculated from sketch_cue_timing.
    """
    # Index animation segments by segment_id
    anim_segments = {
        seg["segment_id"]: seg
        for seg in animation_manifest.get("segments", [])
    }

    # Index script segments for text display
    script_segments = {}
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
        actual_dur = audio_seg.get("actual_duration_seconds", audio_end - audio_start)
        pause = audio_seg.get("pause_for_question", False)

        # Look up animation data
        anim = anim_segments.get(seg_id, {})
        has_animation = anim.get("has_animation", False)
        roughjs_path = anim.get("roughjs_html_path")
        cue_timing = anim.get("sketch_cue_timing")
        anim_duration = anim.get("estimated_duration_seconds", 0)
        seg_type = anim.get("type", "explore")

        # Look up script text
        script_seg = script_segments.get(seg_id, {})
        seg_text = script_seg.get("text", "")
        if not seg_type or seg_type == "explore":
            seg_type = script_seg.get("type", seg_type)

        # Calculate animation trigger time based on sketch_cue_timing.
        # Supports both legacy values (before/during/after) and new
        # visual_action values (DRAW_START/DRAW_CONTINUE/GHOST_ONLY).
        animation_trigger = None
        if has_animation and cue_timing:
            if cue_timing in ("before",):
                animation_trigger = max(0, audio_start - anim_duration)
            elif cue_timing in ("during", "DRAW_START", "DRAW_CONTINUE"):
                animation_trigger = audio_start
            elif cue_timing in ("after",):
                animation_trigger = audio_end
            elif cue_timing == "GHOST_ONLY":
                animation_trigger = audio_start  # ghost visible immediately
            else:
                animation_trigger = audio_start  # default to during

        # Pause point
        pause_at = audio_end if pause else None

        # Scribe path data (carried from Agent 4 manifest)
        paths_data = anim.get("paths") if has_animation else None

        # Scribe keyframes: offset from segment-local time to master audio time
        scribe_kf = anim.get("scribe_keyframes")
        if scribe_kf and animation_trigger is not None:
            offset = animation_trigger
            scribe_kf = [
                {
                    **kf,
                    "audio_start": round(kf["audio_start"] + offset, 3),
                    "audio_end": round(kf["audio_end"] + offset, 3),
                }
                for kf in scribe_kf
            ]

        ts = TimelineSegment(
            segment_id=seg_id,
            type=seg_type,
            audio_start=round(audio_start, 2),
            audio_end=round(audio_end, 2),
            has_animation=has_animation,
            roughjs_html_path=roughjs_path if has_animation else None,
            animation_trigger=round(animation_trigger, 2) if animation_trigger is not None else None,
            sketch_cue_timing=cue_timing,
            pause_for_question=pause,
            pause_at_second=round(pause_at, 2) if pause_at is not None else None,
            segment_text=seg_text,
            paths=paths_data,
            scribe_keyframes=scribe_kf if has_animation else None,
        )
        timeline_segments.append(ts)

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
        total_duration_seconds=round(audio_manifest.get("total_duration_seconds", 0), 2),
        master_audio_path=audio_manifest.get("master_audio_path", ""),
        segments=timeline_segments,
    )

    logger.info(
        "Unified timeline built: %d segments, %.1fs total, %d animated",
        len(timeline_segments),
        timeline.total_duration_seconds,
        sum(1 for s in timeline_segments if s.has_animation),
    )
    return timeline
