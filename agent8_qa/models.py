"""Pydantic models for Agent 8: Q&A Interjection Handler."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class QuestionRequest(BaseModel):
    """Incoming question from the player."""

    text: str
    book_id: str = ""
    chapter_num: int = 0
    segment_id: str = ""
    narrator_persona: str = "Socratic"


class BankedQA(BaseModel):
    """A question-answer pair stored in the bank."""

    id: str
    book_id: str
    chapter_num: int
    segment_id: str = ""
    question_text: str
    answer_text: str
    usage_count: int = 0
    is_verified: bool = False
    created_at: str = ""
    last_used_at: str = ""
    tokens_used: int = 0  # 0 for cached answers


class QuestionLogEntry(BaseModel):
    """Log entry for analytics."""

    id: str
    book_id: str
    chapter_num: int
    segment_id: str = ""
    student_id: str = "anonymous"
    question_text: str
    served_from_cache: bool = False
    response_latency_seconds: float = 0.0
    asked_at: str = ""


class BankStats(BaseModel):
    """Question bank coverage stats."""

    book_id: str
    chapter_num: int = 0
    total_questions: int = 0
    verified_questions: int = 0
    total_served: int = 0
    cache_hit_rate: float = 0.0
    estimated_savings_usd: float = 0.0
