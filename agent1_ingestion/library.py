"""Book library management & duplicate detection."""

from __future__ import annotations

import hashlib
from typing import Optional
from uuid import UUID

from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from agent1_ingestion.config import FUZZY_MATCH_THRESHOLD
from agent1_ingestion.models import DuplicateCheckResponse
from database.schemas import Book


def compute_file_hash(file_bytes: bytes) -> str:
    """Compute SHA-256 hash of file contents."""
    return hashlib.sha256(file_bytes).hexdigest()


def check_duplicate(
    db: Session,
    file_hash: str,
    isbn: Optional[str] = None,
    title: Optional[str] = None,
    author: Optional[str] = None,
) -> DuplicateCheckResponse:
    """
    Check if a book already exists in the library.

    Detection priority:
      1. ISBN exact match
      2. Title + Author fuzzy match (>= threshold)
      3. SHA-256 file hash exact match
    """
    # 1. ISBN lookup
    if isbn:
        existing = db.query(Book).filter(Book.isbn == isbn).first()
        if existing:
            return DuplicateCheckResponse(
                status="existing",
                book_id=UUID(existing.id),
                message="This book already exists in the library (ISBN match).",
            )

    # 2. Title + Author fuzzy match
    if title and author:
        all_books = db.query(Book).all()
        for book in all_books:
            title_score = fuzz.ratio(title.lower(), book.title.lower())
            author_score = fuzz.ratio(author.lower(), book.author.lower())
            combined = (title_score + author_score) / 2
            if combined >= FUZZY_MATCH_THRESHOLD:
                return DuplicateCheckResponse(
                    status="existing",
                    book_id=UUID(book.id),
                    message=(
                        "This book already exists in the library "
                        f"(fuzzy match: {combined:.0f}% similarity)."
                    ),
                )

    # 3. File hash comparison
    existing = db.query(Book).filter(Book.file_hash == file_hash).first()
    if existing:
        return DuplicateCheckResponse(
            status="existing",
            book_id=UUID(existing.id),
            message="This book already exists in the library (identical file).",
        )

    return DuplicateCheckResponse(
        status="new",
        book_id=None,
        message="No duplicate found. Ready to process.",
    )


def get_all_books(db: Session) -> list[Book]:
    """Return all books ordered by upload date descending."""
    return db.query(Book).order_by(Book.upload_date.desc()).all()


def get_book_by_id(db: Session, book_id: str) -> Optional[Book]:
    """Return a single book by its UUID."""
    return db.query(Book).filter(Book.id == book_id).first()
