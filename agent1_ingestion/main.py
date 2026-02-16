"""FastAPI application — Agent 1: Library & Ingestion."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session, sessionmaker

from agent1_ingestion.config import BOOKS_DIR, MAX_UPLOAD_SIZE_MB
from agent1_ingestion.extractor import extract_pdf
from agent1_ingestion.image_extractor import extract_images
from agent1_ingestion.library import check_duplicate, compute_file_hash, get_all_books, get_book_by_id
from agent1_ingestion.models import (
    BookListItem,
    BookListResponse,
    BookResponse,
    BookStatus,
    ChapterResponse,
    ProcessingStatusResponse,
    StructuredBook,
    UploadResponse,
)
from agent1_ingestion.structurer import save_structured_book, structure_book
from database.db import SessionLocal, get_db, init_db
from database.schemas import Book, Chapter, Image


def _get_session_factory(app: FastAPI) -> sessionmaker:
    """Return the session factory attached to the app, or the default."""
    return getattr(app, "_session_factory", SessionLocal)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="SketchCast AI — Agent 1: Library & Ingestion",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Background processing ───────────────────────────────────────────


def process_book(
    book_id: str,
    pdf_path: str,
    title: str,
    author: str,
    isbn: Optional[str],
    session_factory: sessionmaker = SessionLocal,
):
    """Run the full extraction pipeline in a background thread."""
    db = session_factory()
    try:
        book = db.query(Book).filter(Book.id == book_id).first()
        if not book:
            return

        # 1. Extract text
        extraction = extract_pdf(pdf_path)

        # 2. Extract images
        images = extract_images(pdf_path, book_id)

        # 3. Structure content
        structured = structure_book(
            book_id=book_id,
            title=title,
            author=author,
            isbn=isbn,
            extraction=extraction,
            images=images,
        )

        # 4. Save JSON
        output_path = save_structured_book(structured)

        # 5. Update database
        book.total_pages = extraction.total_pages
        book.total_chapters = structured.total_chapters
        book.readability_score = extraction.readability_score
        book.processed_path = str(output_path)
        book.status = BookStatus.COMPLETED

        # Persist chapter records
        for ch in structured.chapters:
            chapter_images = [
                img for img in images if ch.start_page <= img.page_num <= ch.end_page
            ]
            chapter_row = Chapter(
                book_id=book_id,
                chapter_num=ch.chapter_num,
                title=ch.title,
                start_page=ch.start_page,
                end_page=ch.end_page,
                section_count=len(ch.sections),
                image_count=len(ch.images),
            )
            db.add(chapter_row)
            db.flush()

            for img in chapter_images:
                db.add(Image(
                    book_id=book_id,
                    chapter_id=chapter_row.id,
                    filename=img.filename,
                    page_num=img.page_num,
                    context_label=img.context_label,
                    file_path=img.file_path,
                ))

        db.commit()

    except Exception:
        db.rollback()
        try:
            book = db.query(Book).filter(Book.id == book_id).first()
            if book:
                book.status = BookStatus.FAILED
                db.commit()
        except Exception:
            pass
        raise
    finally:
        db.close()


# ── Endpoints ────────────────────────────────────────────────────────


@app.get("/health")
def health_check():
    return {"status": "healthy", "agent": "library_ingestion", "version": "0.1.0"}


@app.post("/upload", response_model=UploadResponse)
async def upload_book(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    author: Optional[str] = Form(None),
    isbn: Optional[str] = Form(None),
    uploaded_by: str = Form("admin"),
    db: Session = Depends(get_db),
):
    # Validate file type
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    # Read file
    contents = await file.read()

    # Validate size
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_SIZE_MB:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size_mb:.1f} MB). Max is {MAX_UPLOAD_SIZE_MB} MB.",
        )

    # Compute hash
    file_hash = compute_file_hash(contents)

    # Duplicate detection
    dup = check_duplicate(db, file_hash, isbn=isbn, title=title, author=author)
    if dup.status == "existing":
        return UploadResponse(
            status="existing",
            book_id=dup.book_id,
            message=dup.message,
        )

    # Save PDF to storage
    book_id = str(uuid.uuid4())
    safe_name = f"{book_id}.pdf"
    pdf_path = BOOKS_DIR / safe_name
    with open(pdf_path, "wb") as f:
        f.write(contents)

    resolved_title = title or (file.filename.rsplit(".", 1)[0] if file.filename else "Untitled")
    resolved_author = author or "Unknown"

    # Create DB record
    book = Book(
        id=book_id,
        title=resolved_title,
        author=resolved_author,
        isbn=isbn,
        file_hash=file_hash,
        file_path=str(pdf_path),
        uploaded_by=uploaded_by,
        status=BookStatus.PROCESSING,
    )
    db.add(book)
    db.commit()

    # Use the app-level session factory so tests can override it
    sf = _get_session_factory(app)

    # Kick off background processing
    background_tasks.add_task(
        process_book, book_id, str(pdf_path), resolved_title, resolved_author, isbn, sf,
    )

    return UploadResponse(
        status="processing",
        book_id=uuid.UUID(book_id),
        message="Upload received. Processing in background.",
    )


@app.get("/library", response_model=BookListResponse)
def list_books(db: Session = Depends(get_db)):
    books = get_all_books(db)
    items = [
        BookListItem(
            book_id=uuid.UUID(b.id),
            title=b.title,
            author=b.author,
            isbn=b.isbn,
            total_pages=b.total_pages,
            total_chapters=b.total_chapters,
            readability_score=b.readability_score,
            upload_date=b.upload_date,
            status=b.status,
        )
        for b in books
    ]
    return BookListResponse(total=len(items), books=items)


@app.get("/library/{book_id}", response_model=BookResponse)
def get_book(book_id: str, db: Session = Depends(get_db)):
    book = get_book_by_id(db, book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found.")

    structured = None
    if book.processed_path:
        json_path = Path(book.processed_path)
        if json_path.exists():
            with open(json_path, "r", encoding="utf-8") as f:
                structured = StructuredBook(**json.load(f))

    return BookResponse(
        book_id=uuid.UUID(book.id),
        title=book.title,
        author=book.author,
        isbn=book.isbn,
        total_pages=book.total_pages,
        total_chapters=book.total_chapters,
        readability_score=book.readability_score,
        upload_date=book.upload_date,
        status=book.status,
        structured_content=structured,
    )


@app.get("/library/{book_id}/chapter/{chapter_num}", response_model=ChapterResponse)
def get_chapter(book_id: str, chapter_num: int, db: Session = Depends(get_db)):
    book = get_book_by_id(db, book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found.")
    if book.status != BookStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="Book is still processing or failed.")

    if not book.processed_path:
        raise HTTPException(status_code=404, detail="Processed content not found.")

    json_path = Path(book.processed_path)
    if not json_path.exists():
        raise HTTPException(status_code=404, detail="Processed JSON file missing.")

    with open(json_path, "r", encoding="utf-8") as f:
        structured = StructuredBook(**json.load(f))

    for ch in structured.chapters:
        if ch.chapter_num == chapter_num:
            return ChapterResponse(book_id=uuid.UUID(book.id), chapter=ch)

    raise HTTPException(status_code=404, detail=f"Chapter {chapter_num} not found.")


@app.get("/library/{book_id}/status", response_model=ProcessingStatusResponse)
def get_status(book_id: str, db: Session = Depends(get_db)):
    book = get_book_by_id(db, book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found.")

    messages = {
        BookStatus.PROCESSING: "Book is being processed.",
        BookStatus.COMPLETED: "Processing complete. Content is ready.",
        BookStatus.FAILED: "Processing failed. Please re-upload.",
    }
    return ProcessingStatusResponse(
        book_id=uuid.UUID(book.id),
        status=book.status,
        message=messages.get(book.status, "Unknown status."),
    )
