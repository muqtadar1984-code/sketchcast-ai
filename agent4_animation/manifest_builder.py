"""Agent 4: Manifest Builder — assembles the master animation manifest."""

from typing import List

from .models import AnimationManifest, ManifestSegment


def build_manifest(
    manifest_id: str,
    script_id: str,
    book_id: str,
    chapter_num: int,
    episode_num: int,
    generated_at: str,
    segments: List[ManifestSegment],
) -> AnimationManifest:
    """Assemble and return the AnimationManifest for an episode."""
    animated = sum(1 for s in segments if s.has_animation)
    blank = len(segments) - animated

    return AnimationManifest(
        manifest_id=manifest_id,
        script_id=script_id,
        book_id=book_id,
        chapter_num=chapter_num,
        episode_num=episode_num,
        generated_at=generated_at,
        canvas_size={"width": 1280, "height": 720},
        total_segments=len(segments),
        animated_segments=animated,
        blank_segments=blank,
        segments=segments,
    )
