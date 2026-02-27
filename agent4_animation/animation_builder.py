"""Agent 4: Animation Builder — converts a sketch plan into a timed animation JSON.

The animation JSON describes how the SVG elements are progressively revealed
during the segment, synchronized to the narrator's speech timing.

Also provides build_scribe_timeline() for the Scribe/Speed Paint player,
which produces per-path keyframes instead of per-frame element groups.
"""

import uuid
from typing import Dict, List

from .models import AnimationFrame, SegmentAnimation

# How much of the segment duration is used for the animation
_ANIMATION_COVERAGE = {
    "before": (0.0, 0.25),   # play in first 25 % of segment
    "during": (0.05, 0.95),  # spread across 90 % of segment
    "after":  (0.75, 1.0),   # play in last 25 % of segment
}


def build_animation(plan: dict, segment: dict, sketch_cue: dict) -> SegmentAnimation:
    """Build a SegmentAnimation from a Claude sketch plan and segment metadata.

    Parameters
    ----------
    plan:       Sketch plan dict (output from Claude / execute_sketch_plan).
    segment:    Script segment dict (from EpisodeScript.segments).
    sketch_cue: The sketch_cue dict from the segment.

    Returns
    -------
    SegmentAnimation Pydantic model.
    """
    seg_id = segment.get("segment_id", "s000")
    seg_duration = int(segment.get("estimated_duration_seconds", 60))
    timing = sketch_cue.get("timing", "during")

    # Sort elements by draw_order
    elements = sorted(
        plan.get("elements", []),
        key=lambda e: e.get("draw_order", 99),
    )
    el_ids = [e.get("id", f"el_{i}") for i, e in enumerate(elements)]

    if not el_ids:
        return SegmentAnimation(
            segment_id=seg_id,
            sketch_cue_timing=timing,
            total_animation_duration_seconds=0.0,
            sync_to_segment_duration=seg_duration,
            frames=[],
        )

    # Determine animation window within the segment
    start_frac, end_frac = _ANIMATION_COVERAGE.get(timing, (0.05, 0.95))
    anim_start = seg_duration * start_frac
    anim_end = seg_duration * end_frac
    anim_total = anim_end - anim_start

    # Group elements into frames (2-3 elements per frame for smooth pacing)
    group_size = 2
    groups: List[List[str]] = []
    for i in range(0, len(el_ids), group_size):
        groups.append(el_ids[i: i + group_size])

    frame_duration = anim_total / max(len(groups), 1)

    frames: List[AnimationFrame] = []
    for i, group in enumerate(groups):
        t_start = anim_start + i * frame_duration
        t_end = t_start + frame_duration
        # Alternate transition style for variety
        transition = "draw" if i % 3 != 2 else "fade"
        frames.append(AnimationFrame(
            frame_id=f"f{i + 1:03d}",
            start_second=round(t_start, 2),
            end_second=round(t_end, 2),
            elements_to_reveal=group,
            transition=transition,
            easing="ease-in-out",
        ))

    return SegmentAnimation(
        segment_id=seg_id,
        sketch_cue_timing=timing,
        total_animation_duration_seconds=round(anim_total, 2),
        sync_to_segment_duration=seg_duration,
        frames=frames,
    )


# ── Scribe / Speed Paint timeline ─────────────────────────────────────


def build_scribe_timeline(
    paths: List[Dict],
    segment: dict,
    sketch_cue: dict,
) -> List[Dict]:
    """Build per-path keyframes for the Scribe animation engine.

    Each path gets a time window [audio_start, audio_end] within the segment,
    sequenced by draw_order.  Duration is proportional to total_length so that
    longer paths take longer to draw.

    Text elements get a fixed short window (they fade in rather than drawing).

    Parameters
    ----------
    paths:      List of PathData dicts from convert_plan_to_paths().
    segment:    Script segment dict (from EpisodeScript.segments).
    sketch_cue: The sketch_cue dict from the segment.

    Returns
    -------
    List of keyframe dicts ready for JSON serialisation:
        [{"path_id", "audio_start", "audio_end", "element_type"}, ...]
    Times are segment-local (start at 0).  The sync engine offsets them
    to master-audio time later.
    """
    seg_duration = int(segment.get("estimated_duration_seconds", 60))
    timing = sketch_cue.get("timing", "during")

    start_frac, end_frac = _ANIMATION_COVERAGE.get(timing, (0.05, 0.95))
    anim_start = seg_duration * start_frac
    anim_end = seg_duration * end_frac
    anim_total = anim_end - anim_start

    if not paths or anim_total <= 0:
        return []

    # Sort by draw_order
    sorted_paths = sorted(paths, key=lambda p: p.get("draw_order", 99))

    # Calculate weight: path length for shapes, fixed 50 for text
    TEXT_WEIGHT = 50
    total_weight = sum(
        p["total_length"] if p.get("element_type") != "text" else TEXT_WEIGHT
        for p in sorted_paths
    )
    if total_weight <= 0:
        total_weight = 1

    keyframes: List[Dict] = []
    cursor = anim_start

    for p in sorted_paths:
        weight = p["total_length"] if p.get("element_type") != "text" else TEXT_WEIGHT
        duration = (weight / total_weight) * anim_total
        duration = max(duration, 0.2)  # minimum 200ms per path

        keyframes.append({
            "path_id": p["path_id"],
            "audio_start": round(cursor, 3),
            "audio_end": round(cursor + duration, 3),
            "element_type": p.get("element_type", "unknown"),
        })
        cursor += duration

    return keyframes
