"""Pydantic models for Agent 2: Content Analysis & Structuring."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class AnalysisStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class DifficultyLevel(str, enum.Enum):
    PRIMARY = "primary_school"
    MIDDLE = "middle_school"
    HIGH = "high_school"


# ── Concept extraction ───────────────────────────────────────────────

class Concept(BaseModel):
    concept_id: str
    name: str
    definition: str
    first_introduced_page: int = 0
    importance: str = "supporting"  # foundational, supporting, application
    related_concepts: list[str] = Field(default_factory=list)


class ConceptDependency(BaseModel):
    concept: str
    requires: list[str] = Field(default_factory=list)
    reason: str = ""


class Prerequisite(BaseModel):
    topic: str
    assumed_grade: str = ""


class ConceptResult(BaseModel):
    concepts: list[Concept] = Field(default_factory=list)
    dependencies: list[ConceptDependency] = Field(default_factory=list)
    prerequisites: list[Prerequisite] = Field(default_factory=list)


# ── Difficulty assessment ────────────────────────────────────────────

class DifficultyAssessment(BaseModel):
    section_title: str
    difficulty_score: int = 5
    difficulty_label: str = "moderate"
    reasons: list[str] = Field(default_factory=list)
    vocabulary_load: str = "medium"
    new_concepts_count: int = 0
    recommended_pacing: str = "medium"
    suggested_analogies: list[str] = Field(default_factory=list)


# ── Visual opportunities ────────────────────────────────────────────

class AnimationStep(BaseModel):
    step: int
    action: str
    details: str
    duration_ms: int = 500


class VisualOpportunity(BaseModel):
    opportunity_id: str
    section: str = ""
    trigger_text: str = ""
    visual_type: str = "diagram"
    title: str
    description: str
    animation_sequence: list[AnimationStep] = Field(default_factory=list)
    sketch_elements: list[str] = Field(default_factory=list)
    estimated_duration_seconds: int = 5
    complexity: str = "medium"


# ── Image analysis ───────────────────────────────────────────────────

class ImageAnalysis(BaseModel):
    image_filename: str
    visual_type: str = "image"
    description: str = ""
    key_elements: list[str] = Field(default_factory=list)
    educational_value: str = ""
    can_be_recreated_as_sketch: bool = False
    sketch_recreation_notes: str = ""
    complexity: str = "medium"


# ── Episode segmentation ────────────────────────────────────────────

class Episode(BaseModel):
    episode_num: int
    title: str
    sections_covered: list[str] = Field(default_factory=list)
    estimated_word_count: int = 0
    estimated_duration_minutes: float = 4.0
    opening_hook: str = ""
    closing_bridge: str = ""
    key_concepts_introduced: list[str] = Field(default_factory=list)
    visual_opportunities_in_episode: list[str] = Field(default_factory=list)


class EpisodePlan(BaseModel):
    chapter_title: str
    total_episodes: int = 0
    episodes: list[Episode] = Field(default_factory=list)


# ── Token usage ──────────────────────────────────────────────────────

class TokenUsage(BaseModel):
    concept_extraction: int = 0
    difficulty_assessment: int = 0
    visual_detection: int = 0
    image_analysis: int = 0
    segmentation: int = 0
    total: int = 0
    estimated_cost_usd: float = 0.0


# ── Master output ───────────────────────────────────────────────────

class MasterAnalysis(BaseModel):
    analysis_id: str
    book_id: str
    chapter_num: int
    chapter_title: str
    difficulty_level_requested: str = "middle_school"
    analyzed_at: str = ""
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    concepts: ConceptResult = Field(default_factory=ConceptResult)
    difficulty_assessments: list[DifficultyAssessment] = Field(default_factory=list)
    visual_opportunities: list[VisualOpportunity] = Field(default_factory=list)
    image_analyses: list[ImageAnalysis] = Field(default_factory=list)
    episodes: EpisodePlan = Field(default_factory=EpisodePlan)
