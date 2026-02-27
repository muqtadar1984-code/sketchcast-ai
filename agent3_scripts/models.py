"""
Pydantic models for Agent 3: Script & Dialogue Generation.
"""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel


class SegmentType(str, Enum):
    hook = "hook"
    activate = "activate"
    explore = "explore"
    question_hook = "question_hook"
    synthesis = "synthesis"
    preview = "preview"


class SketchCue(BaseModel):
    """Instruction for Agent 5 (animation) — what to draw and when."""

    action: str  # draw | highlight | label | clear | point | annotate
    element: str  # specific description of what to draw
    timing: str  # before | during | after  (relative to narrator speaking this segment)


class ScriptSegment(BaseModel):
    """One narration unit within an episode script."""

    segment_id: str
    type: SegmentType
    text: str  # plain text — what the narrator says
    elevenlabs_text: str  # same text with ElevenLabs <break> markup
    sketch_cue: Optional[SketchCue] = None
    visual_action: Optional[str] = None  # DRAW_START | DRAW_CONTINUE | GHOST_ONLY
    pause_for_question: bool = False
    estimated_duration_seconds: int


class EpisodeScript(BaseModel):
    """Complete script for one episode."""

    script_id: str
    book_id: str
    chapter_num: int
    episode_num: int
    episode_title: str
    generated_at: str
    narrator_persona: str = "Socratic"
    segments: List[ScriptSegment]
    total_estimated_duration_seconds: int
    question_hook_count: int


class ChapterScripts(BaseModel):
    """All episode scripts for a chapter."""

    book_id: str
    chapter_num: int
    chapter_title: str
    total_episodes: int
    generated_at: str
    episodes: List[EpisodeScript]
