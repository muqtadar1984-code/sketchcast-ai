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


class TimelineSegment(BaseModel):
    """One segment in the unified timeline."""

    segment_id: str
    type: str  # hook, activate, explore, question_hook, synthesis, preview
    audio_start: float  # seconds in master audio
    audio_end: float
    has_animation: bool = False
    roughjs_html_path: Optional[str] = None
    animation_trigger: Optional[float] = None  # second to start animation
    sketch_cue_timing: Optional[str] = None  # before | during | after
    pause_for_question: bool = False
    pause_at_second: Optional[float] = None  # auto-pause at this second
    segment_text: str = ""  # narration text for display


class UnifiedTimeline(BaseModel):
    """Merged audio + animation timeline consumed by player.js."""

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
    """Metadata for a built playback package."""

    package_id: str
    script_id: str
    book_id: str
    chapter_num: int
    episode_num: int = 1
    status: PlayerStatus = PlayerStatus.pending
    timeline_path: Optional[str] = None
    player_html_path: Optional[str] = None
    built_at: str = ""
