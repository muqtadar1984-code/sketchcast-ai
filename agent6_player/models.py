"""Pydantic models for Agent 6: Live Playback Engine."""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class PlayerStatus(str, Enum):
    pending = "pending"
    building = "building"
    ready = "ready"
    failed = "failed"


class ScribePath(BaseModel):
    """Represents a single vector path to be drawn."""

    path_id: str
    d: str
    total_length: float
    stroke_color: str = "#2C3E50"
    stroke_width: int = 2
    fill: str = "none"
    draw_order: int = 0
    # Type allows frontend to apply specific logic (e.g. text vs shape)
    element_type: str = "path"

    text_params: Optional[dict] = None


class TimelineSegment(BaseModel):
    """One segment in the unified timeline."""

    segment_id: str
    type: str
    audio_start: float
    audio_end: float

    # Scribe specific fields
    visual_action: str = "DRAW_START"  # DRAW_START, DRAW_CONTINUE, GHOST_ONLY
    paths: Optional[List[ScribePath]] = None
    scribe_keyframes: Optional[List[dict]] = None

    # Legacy/Fallback (must be kept for compatibility but ignored by new player)
    has_animation: bool = False
    roughjs_html_path: Optional[str] = None
    sketch_cue_timing: Optional[str] = None

    pause_for_question: bool = False
    pause_at_second: Optional[float] = None
    segment_text: str = ""


class UnifiedTimeline(BaseModel):
    """Merged timeline consumed by player.js."""

    timeline_id: str
    script_id: str
    book_id: str
    chapter_num: int
    episode_num: int = 1
    episode_title: str = ""
    total_duration_seconds: float
    master_audio_path: str
    segments: List[TimelineSegment] = Field(default_factory=list)


class PlayerPackage(BaseModel):
    package_id: str
    status: str = "pending"
    timeline_path: Optional[str] = None
    player_html_path: Optional[str] = None
