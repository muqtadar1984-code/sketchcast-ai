"""
Streamlit UI for SketchCast AI — Agent 1: Library & Ingestion.

Standalone mode: processes PDFs directly in-process (no separate API server needed).
Works on Streamlit Cloud and locally.
"""

import hashlib
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent1_ingestion.extractor import extract_pdf
from agent1_ingestion.image_extractor import extract_images
from agent1_ingestion.structurer import structure_book
from rapidfuzz import fuzz

st.set_page_config(
    page_title="SketchCast AI — Library",
    page_icon="📚",
    layout="wide",
)

# ── Session state initialization ─────────────────────────────────────

if "library" not in st.session_state:
    st.session_state.library = {}  # book_id -> book_record

if "selected_book_id" not in st.session_state:
    st.session_state.selected_book_id = None

FUZZY_THRESHOLD = 85


# ── Processing helpers ───────────────────────────────────────────────


def compute_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_duplicate(file_hash: str, title: str | None, author: str | None, isbn: str | None):
    """Check session-state library for duplicates. Returns book_id or None."""
    for bid, rec in st.session_state.library.items():
        # ISBN match
        if isbn and rec.get("isbn") == isbn:
            return bid, "ISBN match"
        # Fuzzy title+author
        if title and author and rec.get("title") and rec.get("author"):
            t_score = fuzz.ratio(title.lower(), rec["title"].lower())
            a_score = fuzz.ratio(author.lower(), rec["author"].lower())
            if (t_score + a_score) / 2 >= FUZZY_THRESHOLD:
                return bid, f"Fuzzy match ({(t_score + a_score) / 2:.0f}%)"
        # Hash match
        if rec.get("file_hash") == file_hash:
            return bid, "Identical file"
    return None, None


def process_pdf(file_bytes: bytes, filename: str, title: str | None, author: str | None, isbn: str | None):
    """Run the full extraction pipeline and store results in session state."""
    book_id = str(uuid.uuid4())
    resolved_title = title or filename.rsplit(".", 1)[0]
    resolved_author = author or "Unknown"

    # Write to a temp file for PyMuPDF
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        # Extract
        extraction = extract_pdf(tmp_path)
        images = extract_images(tmp_path, book_id)

        # Structure
        structured = structure_book(
            book_id=book_id,
            title=resolved_title,
            author=resolved_author,
            isbn=isbn,
            extraction=extraction,
            images=images,
        )

        record = {
            "book_id": book_id,
            "title": resolved_title,
            "author": resolved_author,
            "isbn": isbn,
            "file_hash": compute_hash(file_bytes),
            "total_pages": extraction.total_pages,
            "total_chapters": structured.total_chapters,
            "readability_score": extraction.readability_score,
            "upload_date": datetime.now(timezone.utc).isoformat(),
            "status": "completed",
            "structured": structured.model_dump(),
        }

        st.session_state.library[book_id] = record
        return book_id, None

    except Exception as e:
        return None, str(e)
    finally:
        os.unlink(tmp_path)


# ── Sidebar ──────────────────────────────────────────────────────────

with st.sidebar:
    st.title("SketchCast AI")
    st.caption("Agent 1: Library & Ingestion")
    st.divider()
    st.success("Embedded mode — no API server needed", icon="✅")
    st.caption(f"{len(st.session_state.library)} book(s) in session")
    st.divider()
    page = st.radio(
        "Navigate",
        ["Upload Book", "Library", "Book Viewer"],
        label_visibility="collapsed",
    )


# ── Page: Upload ─────────────────────────────────────────────────────

if page == "Upload Book":
    st.header("Upload a Textbook PDF")

    uploaded_file = st.file_uploader("Choose a PDF file", type=["pdf"])

    col1, col2 = st.columns(2)
    with col1:
        title = st.text_input("Title (optional)")
    with col2:
        author = st.text_input("Author (optional)")

    isbn = st.text_input("ISBN (optional)")

    if st.button("Upload & Process", type="primary", disabled=uploaded_file is None):
        file_bytes = uploaded_file.getvalue()
        file_hash = compute_hash(file_bytes)

        # Duplicate check
        dup_id, dup_reason = check_duplicate(file_hash, title, author, isbn)
        if dup_id:
            st.info(f"**Duplicate detected** — {dup_reason}")
            st.write(f"Existing Book ID: `{dup_id}`")
            st.session_state.selected_book_id = dup_id
        else:
            progress = st.progress(0, text="Extracting text and images...")
            progress.progress(10, text="Extracting text and images...")

            book_id, error = process_pdf(
                file_bytes, uploaded_file.name, title or None, author or None, isbn or None,
            )

            if error:
                progress.progress(100, text="Processing failed.")
                st.error(f"Processing failed: {error}")
            else:
                progress.progress(100, text="Processing complete!")
                st.success("Book processed successfully!")
                st.write(f"Book ID: `{book_id}`")
                st.session_state.selected_book_id = book_id
                st.balloons()


# ── Page: Library ────────────────────────────────────────────────────

elif page == "Library":
    st.header("Book Library")

    books = list(st.session_state.library.values())

    if not books:
        st.info("No books in the library yet. Upload a PDF to get started.")
    else:
        st.write(f"**{len(books)} book(s)** in the library")
        st.divider()

        for book in books:
            with st.container():
                col1, col2, col3 = st.columns([3, 1, 1])
                with col1:
                    st.subheader(book["title"])
                    st.caption(f"by {book['author']}")
                with col2:
                    st.success("Completed")
                with col3:
                    st.metric("Pages", book["total_pages"])

                meta_cols = st.columns(4)
                with meta_cols[0]:
                    st.write(f"**Chapters:** {book['total_chapters']}")
                with meta_cols[1]:
                    st.write(f"**Readability:** {book['readability_score']:.0%}")
                with meta_cols[2]:
                    st.write(f"**ISBN:** {book.get('isbn') or 'N/A'}")
                with meta_cols[3]:
                    st.write(f"**ID:** `{book['book_id'][:8]}...`")

                if st.button("View details", key=f"view_{book['book_id']}"):
                    st.session_state.selected_book_id = book["book_id"]
                    st.rerun()

                st.divider()


# ── Page: Book Viewer ────────────────────────────────────────────────

elif page == "Book Viewer":
    st.header("Book Viewer")

    # Build a selector from available books
    books = st.session_state.library
    options = {f"{b['title'][:40]} ({bid[:8]}...)": bid for bid, b in books.items()}

    if not options:
        st.info("No books available. Upload a PDF first.")
    else:
        default_idx = 0
        if st.session_state.selected_book_id in books:
            bid = st.session_state.selected_book_id
            label = f"{books[bid]['title'][:40]} ({bid[:8]}...)"
            if label in options:
                default_idx = list(options.keys()).index(label)

        selected_label = st.selectbox("Select a book", list(options.keys()), index=default_idx)
        book_id = options[selected_label]
        book = books[book_id]
        sc = book.get("structured")

        # Header
        st.subheader(book["title"])
        st.caption(f"by {book['author']}")

        info_cols = st.columns(4)
        with info_cols[0]:
            st.metric("Pages", book["total_pages"])
        with info_cols[1]:
            st.metric("Chapters", book["total_chapters"])
        with info_cols[2]:
            st.metric("Readability", f"{book['readability_score']:.0%}")
        with info_cols[3]:
            st.metric("Status", "Completed")

        if not sc:
            st.warning("No structured content available.")
        else:
            st.divider()

            # Table of Contents
            toc = sc.get("table_of_contents", [])
            if toc:
                with st.expander("Table of Contents", expanded=True):
                    for entry in toc:
                        st.write(
                            f"**Ch {entry['chapter_num']}:** {entry['title']} "
                            f"(page {entry['start_page'] + 1})"
                        )

            # Chapters
            chapters = sc.get("chapters", [])
            if chapters:
                st.divider()
                tabs = st.tabs(
                    [f"Ch {ch['chapter_num']}: {ch['title'][:30]}" for ch in chapters]
                )

                for tab, ch in zip(tabs, chapters):
                    with tab:
                        st.subheader(ch["title"])
                        st.caption(f"Pages {ch['start_page'] + 1} – {ch['end_page'] + 1}")

                        # Sections
                        for sec in ch.get("sections", []):
                            st.markdown(f"### {sec['section_title']}")
                            if sec["content"]:
                                st.write(sec["content"][:2000])
                                if len(sec["content"]) > 2000:
                                    with st.expander("Show full content"):
                                        st.write(sec["content"])

                            for sub in sec.get("subsections", []):
                                st.markdown(f"**{sub['section_title']}**")
                                if sub["content"]:
                                    st.write(sub["content"])

                        # Key Boxes
                        key_boxes = ch.get("key_boxes", [])
                        if key_boxes:
                            st.divider()
                            st.write("**Special Content Boxes:**")
                            for box in key_boxes:
                                box_type = box["type"]
                                icon_map = {
                                    "activity": "🧪",
                                    "definition": "📖",
                                    "info": "💡",
                                    "exercise": "✏️",
                                    "quote": "💬",
                                }
                                icon = icon_map.get(box_type, "📌")
                                with st.expander(f"{icon} {box['title']}"):
                                    st.write(box["content"])
                                    st.caption(f"Page {box['page_num'] + 1} | Type: {box_type}")

                        # Images
                        images = ch.get("images", [])
                        if images:
                            st.divider()
                            st.write(f"**Extracted Images ({len(images)}):**")
                            img_cols = st.columns(min(len(images), 3))
                            for i, img in enumerate(images):
                                with img_cols[i % 3]:
                                    st.write(f"**{img['filename']}**")
                                    st.caption(f"Page {img['page_num'] + 1}")
                                    if img["context_label"]:
                                        st.write(f"*Context:* {img['context_label'][:200]}")

            # Raw JSON download
            st.divider()
            col_a, col_b = st.columns(2)
            with col_a:
                with st.expander("Raw JSON"):
                    st.json(sc)
            with col_b:
                st.download_button(
                    "Download structured JSON",
                    data=json.dumps(sc, indent=2, ensure_ascii=False),
                    file_name=f"{book['title'][:30]}_structured.json",
                    mime="application/json",
                )
