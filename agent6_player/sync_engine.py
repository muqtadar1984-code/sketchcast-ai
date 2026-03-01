"""Merge audio manifest and Scribe SVGs into unified timeline."""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

from agent6_player.models import ScribePath, TimelineSegment, UnifiedTimeline

logger = logging.getLogger(__name__)


def _parse_svg_paths(svg_path_str: str) -> list[ScribePath]:
    """Read an SVG file and extract path data for the Scribe player."""
    path_obj = Path(svg_path_str)
    if not path_obj.exists():
        return []

    content = path_obj.read_text(encoding="utf-8")

    # Match any <path> tag that has class="ink-layer" (attribute order varies).
    tag_pattern = re.compile(r'<path\s[^>]*class="ink-layer"[^>]*/?>',)

    paths: list[ScribePath] = []

    for match in tag_pattern.finditer(content):
        tag = match.group(0)

        # Extract d="..." and data-length="..." from the matched tag
        d_match = re.search(r'\sd="([^"]+)"', tag)
        len_match = re.search(r'data-length="([^"]+)"', tag)
        if not d_match or not len_match:
            continue

        d = d_match.group(1)
        length = float(len_match.group(1))

        # Construct styles matching the Vectorizer output
        paths.append(ScribePath(
            path_id=f"p_{len(paths)}",
            d=d,
            total_length=length,
            ghost_style=(
                'stroke="#2C3E50" stroke-width="2" stroke-opacity="0.1" '
                'stroke-dasharray="5,5" fill="none"'
            ),
            ink_style=(
                f'stroke="#2C3E50" stroke-width="2" stroke-opacity="1.0" '
                f'fill="none" stroke-dasharray="{length}" '
                f'stroke-dashoffset="{length}"'
            ),
        ))

    return paths


def build_unified_timeline(
    audio_manifest: dict,
    animation_manifest: dict,
    script_data: dict | None = None,
) -> UnifiedTimeline:
    """Build a unified timeline by merging audio and animation manifests.

    Uses actual audio timestamps from Agent 5 only — never estimated durations.
    Reads SVG files from Agent 4 to extract Scribe path data.
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
        svg_path = anim.get("svg_path")
        # In Agent 4, we stored 'visual_action' in 'sketch_cue_timing' field
        visual_action = anim.get("sketch_cue_timing", "GHOST_ONLY")

        # Get Script Text
        script_seg = script_segments.get(seg_id, {})
        seg_text = script_seg.get("text", "")
        seg_type = script_seg.get("type", "explore")

        # Extract Paths if this is a new drawing
        parsed_paths: list[ScribePath] = []
        if svg_path and visual_action == "DRAW_START":
            parsed_paths = _parse_svg_paths(svg_path)

        timeline_segments.append(TimelineSegment(
            segment_id=seg_id,
            type=seg_type,
            audio_start=round(audio_start, 2),
            audio_end=round(audio_end, 2),
            visual_action=visual_action,
            paths=parsed_paths if parsed_paths else None,
            has_animation=bool(parsed_paths),
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
