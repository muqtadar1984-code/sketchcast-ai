"""Agent 6: SpeedPaint Animation + Audio — Pydantic models."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class VideoSegment(BaseModel):
    """One segment's video composition result."""

    segment_id: str
    type: str = "explore"
    audio_path: Optional[str] = None         # Per-segment MP3
    video_path: Optional[str] = None         # Per-segment MP4 (SpeedPaint + audio)
    slide_image_path: Optional[str] = None   # Source slide PNG
    audio_duration_seconds: float = 0.0
    # The rendered MP4's own length, probed from the file after encoding.
    # The audio length above is what the composer MEANT; this is what it
    # MADE. The difference is the tail: seconds of board with no voice.
    # 0.0 when the probe could not read the file (never fails a segment).
    clip_duration_seconds: float = 0.0
    visual_action: Optional[str] = None      # DRAW_START, DRAW_CONTINUE, GHOST_ONLY
    # which visual system produced this segment: "scene" (planned whiteboard),
    # "whiteboard" (whiteboard-native fallback), or "native" (legacy slides —
    # must be ZERO in a VIDEO_ENGINE=scene lesson, validated downstream)
    renderer: str = "native"
    # per-scene quality audit from SceneRenderer.audit(): unresolved anchors,
    # out-of-bounds text, converging arrows, baked-text assets, timing shifts
    scene_audit: List[str] = Field(default_factory=list)


class VideoManifest(BaseModel):
    """Master manifest for one episode's video segments."""

    manifest_id: str
    script_id: str
    book_id: str
    chapter_num: int
    episode_num: int = 1
    generated_at: str = ""
    # the generation this run belongs to: its working directory is its own
    # (two generations of one chapter once shared a directory and deleted
    # each other's segments mid-concat — Hindi demo, 2026-09-25)
    run_id: str = ""
    total_segments: int = 0
    video_segments_count: int = 0
    total_duration_seconds: float = 0.0
    segments: List[VideoSegment] = Field(default_factory=list)
