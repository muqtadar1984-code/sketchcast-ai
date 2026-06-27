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
    visual_action: Optional[str] = None      # DRAW_START, DRAW_CONTINUE, GHOST_ONLY


class VideoManifest(BaseModel):
    """Master manifest for one episode's video segments."""

    manifest_id: str
    script_id: str
    book_id: str
    chapter_num: int
    episode_num: int = 1
    generated_at: str = ""
    total_segments: int = 0
    video_segments_count: int = 0
    total_duration_seconds: float = 0.0
    segments: List[VideoSegment] = Field(default_factory=list)
