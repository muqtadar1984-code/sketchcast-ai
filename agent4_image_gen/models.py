"""Agent 4: Image Generation — Pydantic models."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class ImageSegment(BaseModel):
    """One segment's image generation result."""

    segment_id: str
    type: str = "explore"
    has_image: bool = False
    image_path: Optional[str] = None
    prompt_used: str = ""
    source: str = ""  # "gemini", "pillow_fallback", "none"
    visual_action: Optional[str] = None  # DRAW_START, DRAW_CONTINUE, GHOST_ONLY


class ImageManifest(BaseModel):
    """Master manifest for one episode's generated images."""

    manifest_id: str
    script_id: str
    book_id: str
    chapter_num: int
    episode_num: int = 1
    generated_at: str = ""
    total_segments: int = 0
    image_segments: int = 0
    segments: List[ImageSegment] = Field(default_factory=list)
