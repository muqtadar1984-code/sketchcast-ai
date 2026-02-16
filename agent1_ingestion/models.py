"""Pydantic models for request/response validation."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


# --- Enums ---

class BookStatus(str, enum.Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class UploaderRole(str, enum.Enum):
    ADMIN = "admin"
    STUDENT = "student"


# --- Nested content models ---

class ImagePosition(BaseModel):
    x: float
    y: float
    width: float
    height: float


class ImageInfo(BaseModel):
    filename: str
    page_num: int
    context_label: str
    position: ImagePosition


class KeyBox(BaseModel):
    type: str  # e.g. "activity", "definition", "info", "exercise", "quote"
    title: str
    content: str
    page_num: int


class Subsection(BaseModel):
    section_title: str
    content: str
    page_num: int


class Section(BaseModel):
    section_title: str
    section_type: str  # "heading", "subheading", "body"
    content: str
    page_num: int
    subsections: list[Subsection] = Field(default_factory=list)


class TOCEntry(BaseModel):
    chapter_num: int
    title: str
    start_page: int


class ChapterContent(BaseModel):
    chapter_num: int
    title: str
    start_page: int
    end_page: int
    sections: list[Section] = Field(default_factory=list)
    images: list[ImageInfo] = Field(default_factory=list)
    key_boxes: list[KeyBox] = Field(default_factory=list)


class StructuredBook(BaseModel):
    """Full structured representation of a processed book."""
    book_id: str
    title: str
    author: str
    isbn: Optional[str] = None
    total_pages: int
    total_chapters: int
    readability_score: float
    table_of_contents: list[TOCEntry] = Field(default_factory=list)
    chapters: list[ChapterContent] = Field(default_factory=list)


# --- Request models ---

class BookUploadRequest(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None
    isbn: Optional[str] = None
    uploaded_by: UploaderRole = UploaderRole.ADMIN


# --- Response models ---

class BookListItem(BaseModel):
    book_id: UUID
    title: str
    author: str
    isbn: Optional[str] = None
    total_pages: int
    total_chapters: int
    readability_score: float
    upload_date: datetime
    status: BookStatus

    model_config = {"from_attributes": True}


class BookListResponse(BaseModel):
    total: int
    books: list[BookListItem]


class BookResponse(BaseModel):
    book_id: UUID
    title: str
    author: str
    isbn: Optional[str] = None
    total_pages: int
    total_chapters: int
    readability_score: float
    upload_date: datetime
    status: BookStatus
    structured_content: Optional[StructuredBook] = None

    model_config = {"from_attributes": True}


class ChapterResponse(BaseModel):
    book_id: UUID
    chapter: ChapterContent


class DuplicateCheckResponse(BaseModel):
    status: str  # "existing" or "new"
    book_id: Optional[UUID] = None
    message: str


class ProcessingStatusResponse(BaseModel):
    book_id: UUID
    status: BookStatus
    message: str


class UploadResponse(BaseModel):
    status: str  # "processing" or "existing"
    book_id: UUID
    message: str
