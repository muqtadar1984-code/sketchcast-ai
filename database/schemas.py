"""SQLAlchemy models for the book library."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agent1_ingestion.models import BookStatus, UploaderRole
from database.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Book(Base):
    __tablename__ = "books"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="Untitled")
    author: Mapped[str] = mapped_column(String(500), nullable=False, default="Unknown")
    isbn: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    total_pages: Mapped[int] = mapped_column(Integer, default=0)
    total_chapters: Mapped[int] = mapped_column(Integer, default=0)
    readability_score: Mapped[float] = mapped_column(Float, default=0.0)
    upload_date: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    uploaded_by: Mapped[str] = mapped_column(
        Enum(UploaderRole), default=UploaderRole.ADMIN
    )
    status: Mapped[str] = mapped_column(
        Enum(BookStatus), default=BookStatus.PROCESSING
    )
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    processed_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    chapters: Mapped[list["Chapter"]] = relationship(
        back_populates="book", cascade="all, delete-orphan"
    )
    images: Mapped[list["Image"]] = relationship(
        back_populates="book", cascade="all, delete-orphan"
    )


class Chapter(Base):
    __tablename__ = "chapters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    book_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("books.id", ondelete="CASCADE"), nullable=False
    )
    chapter_num: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    start_page: Mapped[int] = mapped_column(Integer, nullable=False)
    end_page: Mapped[int] = mapped_column(Integer, nullable=False)
    section_count: Mapped[int] = mapped_column(Integer, default=0)
    image_count: Mapped[int] = mapped_column(Integer, default=0)

    book: Mapped["Book"] = relationship(back_populates="chapters")
    images: Mapped[list["Image"]] = relationship(
        back_populates="chapter", cascade="all, delete-orphan"
    )


class Image(Base):
    __tablename__ = "images"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    book_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("books.id", ondelete="CASCADE"), nullable=False
    )
    chapter_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("chapters.id", ondelete="SET NULL"), nullable=True
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    page_num: Mapped[int] = mapped_column(Integer, nullable=False)
    context_label: Mapped[str] = mapped_column(Text, default="")
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)

    book: Mapped["Book"] = relationship(back_populates="images")
    chapter: Mapped["Chapter"] = relationship(back_populates="images")


class ChapterAnalysis(Base):
    __tablename__ = "chapter_analyses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    book_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("books.id", ondelete="CASCADE"), nullable=False
    )
    chapter_num: Mapped[int] = mapped_column(Integer, nullable=False)
    difficulty_level: Mapped[str] = mapped_column(String(50), default="middle_school")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    total_concepts: Mapped[int] = mapped_column(Integer, default=0)
    total_visual_opportunities: Mapped[int] = mapped_column(Integer, default=0)
    total_episodes: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    analysis_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class EpisodeAudio(Base):
    __tablename__ = "episode_audio"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    script_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    book_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("books.id", ondelete="CASCADE"), nullable=False
    )
    chapter_num: Mapped[int] = mapped_column(Integer, nullable=False)
    episode_num: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/processing/completed/failed
    voice_id: Mapped[str] = mapped_column(String(100), default="")
    model: Mapped[str] = mapped_column(String(100), default="eleven_turbo_v2")
    total_segments: Mapped[int] = mapped_column(Integer, default=0)
    completed_segments: Mapped[int] = mapped_column(Integer, default=0)
    total_duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    master_audio_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    manifest_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EpisodePlayer(Base):
    __tablename__ = "episode_player"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    script_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    book_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("books.id", ondelete="CASCADE"), nullable=False
    )
    chapter_num: Mapped[int] = mapped_column(Integer, nullable=False)
    episode_num: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/building/ready/failed
    timeline_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    player_package_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    built_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class QuestionBankEntry(Base):
    __tablename__ = "question_bank"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    book_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("books.id", ondelete="CASCADE"), nullable=False
    )
    chapter_num: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_id: Mapped[str] = mapped_column(String(50), default="")
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)


class QuestionLog(Base):
    __tablename__ = "question_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    book_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("books.id", ondelete="CASCADE"), nullable=False
    )
    chapter_num: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_id: Mapped[str] = mapped_column(String(50), default="")
    student_id: Mapped[str] = mapped_column(String(100), default="anonymous")
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    served_from_cache: Mapped[bool] = mapped_column(Boolean, default=False)
    response_latency_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    asked_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
