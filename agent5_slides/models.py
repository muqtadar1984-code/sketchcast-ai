"""Agent 5: Slide Assembly — Pydantic models."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class SlideSegment(BaseModel):
    """One segment's slide assembly result."""

    segment_id: str
    type: str = "explore"
    has_slide: bool = False
    slide_path: Optional[str] = None        # .pptx per-segment
    slide_image_path: Optional[str] = None  # .png export for video
    visual_action: Optional[str] = None     # DRAW_START, DRAW_CONTINUE, GHOST_ONLY


class SlideManifest(BaseModel):
    """Master manifest for one episode's assembled slides."""

    manifest_id: str
    script_id: str
    book_id: str
    chapter_num: int
    episode_num: int = 1
    generated_at: str = ""
    total_segments: int = 0
    slide_segments: int = 0
    segments: List[SlideSegment] = Field(default_factory=list)
