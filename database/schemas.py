"""SQLAlchemy models for the book library."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text
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
