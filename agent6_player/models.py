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
    # We pass raw style strings to keep frontend logic simple
    ghost_style: str
    ink_style: str


class TimelineSegment(BaseModel):
    """One segment in the unified timeline."""

    segment_id: str
    type: str
    audio_start: float
    audio_end: float

    # Scribe specific fields
    visual_action: str = "GHOST_ONLY"  # DRAW_START, DRAW_CONTINUE, GHOST_ONLY
    paths: Optional[List[ScribePath]] = None

    # Legacy/Fallback (can be kept or ignored)
    has_animation: bool = False

    pause_for_question: bool = False
    pause_at_second: Optional[float] = None
    segment_text: str = ""


class UnifiedTimeline(BaseModel):
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
    status: PlayerStatus = PlayerStatus.pending
    timeline_path: Optional[str] = None
    player_html_path: Optional[str] = None
