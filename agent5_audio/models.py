"""Pydantic models for Agent 5: Audio Generation."""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class AudioStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class SegmentAudio(BaseModel):
    """Audio data for a single script segment."""

    segment_id: str
    audio_path: str = ""
    actual_duration_seconds: float = 0.0
    master_start_seconds: float = 0.0
    master_end_seconds: float = 0.0
    pause_for_question: bool = False
    pause_point: bool = False


class AudioManifest(BaseModel):
    """Complete audio manifest for one episode — consumed by Agent 6."""

    audio_manifest_id: str
    script_id: str
    book_id: str
    chapter_num: int
    episode_num: int = 1
    generated_at: str = ""
    voice_id: str = ""
    model: str = "eleven_turbo_v2"
    master_audio_path: str = ""
    total_duration_seconds: float = 0.0
    segments: List[SegmentAudio] = Field(default_factory=list)


class AudioGenerationProgress(BaseModel):
    """Progress update during generation."""

    script_id: str
    status: AudioStatus = AudioStatus.pending
    total_segments: int = 0
    completed_segments: int = 0
    current_segment_id: Optional[str] = None
    error: Optional[str] = None
