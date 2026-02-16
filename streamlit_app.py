"""Streamlit UI for SketchCast AI — Agent 1: Library & Ingestion."""

import json
import time

import requests
import streamlit as st

API_BASE = "http://localhost:8000"

st.set_page_config(
    page_title="SketchCast AI — Library",
    page_icon="📚",
    layout="wide",
)

# ── Helpers ──────────────────────────────────────────────────────────


def api_healthy() -> bool:
    try:
        r = requests.get(f"{API_BASE}/health", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def poll_status(book_id: str, timeout: int = 60) -> str:
    deadline = time.time() + timeout
    progress = st.progress(0, text="Processing PDF...")
    while time.time() < deadline:
        r = requests.get(f"{API_BASE}/library/{book_id}/status", timeout=5)
        data = r.json()
        status = data["status"]
        if status == "completed":
            progress.progress(100, text="Processing complete!")
            return "completed"
        if status == "failed":
            progress.progress(100, text="Processing failed.")
            return "failed"
        elapsed = time.time() - (deadline - timeout)
        progress.progress(min(int(elapsed / timeout * 90), 90), text="Processing PDF...")
        time.sleep(1)
    progress.progress(100, text="Timed out waiting for processing.")
    return "timeout"


# ── Sidebar ──────────────────────────────────────────────────────────

with st.sidebar:
    st.title("SketchCast AI")
    st.caption("Agent 1: Library & Ingestion")
    st.divider()

    healthy = api_healthy()
    if healthy:
        st.success("API connected", icon="✅")
    else:
        st.error("API offline — start the server first", icon="🔴")
        st.code("uvicorn agent1_ingestion.main:app --reload --port 8000", language="bash")

    st.divider()
    page = st.radio("Navigate", ["Upload Book", "Library", "Book Viewer"], label_visibility="collapsed")


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

    if st.button("Upload & Process", type="primary", disabled=not healthy or uploaded_file is None):
        data = {}
        if title:
            data["title"] = title
        if author:
            data["author"] = author
        if isbn:
            data["isbn"] = isbn

        files = {"file": (uploaded_file.name, uploaded_file.getvalue(), "application/pdf")}

        with st.spinner("Uploading..."):
            r = requests.post(f"{API_BASE}/upload", files=files, data=data, timeout=30)

        if r.status_code != 200:
            st.error(f"Upload failed: {r.text}")
        else:
            result = r.json()
            book_id = result["book_id"]

            if result["status"] == "existing":
                st.info(f"**Duplicate detected.** {result['message']}")
                st.write(f"Existing Book ID: `{book_id}`")
            else:
                st.write(f"Book ID: `{book_id}`")
                status = poll_status(book_id)
                if status == "completed":
                    st.success("Book processed successfully!")
                    st.balloons()
                elif status == "failed":
                    st.error("Processing failed. Check server logs.")
                else:
                    st.warning("Processing is taking longer than expected. Check status in Library.")


# ── Page: Library ────────────────────────────────────────────────────

elif page == "Library":
    st.header("Book Library")

    if not healthy:
        st.warning("Start the API server to view the library.")
    else:
        r = requests.get(f"{API_BASE}/library", timeout=10)
        data = r.json()

        if data["total"] == 0:
            st.info("No books in the library yet. Upload a PDF to get started.")
        else:
            st.write(f"**{data['total']} book(s)** in the library")
            st.divider()

            for book in data["books"]:
                with st.container():
                    col1, col2, col3 = st.columns([3, 1, 1])
                    with col1:
                        st.subheader(book["title"])
                        st.caption(f"by {book['author']}")
                    with col2:
                        status = book["status"]
                        if status == "completed":
                            st.success("Completed")
                        elif status == "processing":
                            st.warning("Processing")
                        else:
                            st.error("Failed")
                    with col3:
                        st.metric("Pages", book["total_pages"])

                    meta_cols = st.columns(4)
                    with meta_cols[0]:
                        st.write(f"**Chapters:** {book['total_chapters']}")
                    with meta_cols[1]:
                        score = book["readability_score"]
                        st.write(f"**Readability:** {score:.0%}")
                    with meta_cols[2]:
                        st.write(f"**ISBN:** {book.get('isbn') or 'N/A'}")
                    with meta_cols[3]:
                        st.write(f"**ID:** `{book['book_id'][:8]}...`")

                    if status == "completed":
                        if st.button(f"View details", key=f"view_{book['book_id']}"):
                            st.session_state["selected_book_id"] = book["book_id"]
                            st.rerun()

                    st.divider()


# ── Page: Book Viewer ────────────────────────────────────────────────

elif page == "Book Viewer":
    st.header("Book Viewer")

    if not healthy:
        st.warning("Start the API server first.")
    else:
        book_id = st.session_state.get("selected_book_id", "")
        book_id_input = st.text_input("Book ID", value=book_id)

        if book_id_input:
            r = requests.get(f"{API_BASE}/library/{book_id_input}", timeout=10)
            if r.status_code == 404:
                st.error("Book not found.")
            elif r.status_code != 200:
                st.error(f"Error: {r.text}")
            else:
                book = r.json()

                # Header info
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
                    status = book["status"]
                    if status == "completed":
                        st.metric("Status", "Completed")
                    elif status == "processing":
                        st.metric("Status", "Processing")
                    else:
                        st.metric("Status", "Failed")

                sc = book.get("structured_content")
                if not sc:
                    if status == "processing":
                        st.info("Still processing. Refresh in a moment.")
                    else:
                        st.warning("No structured content available.")
                else:
                    st.divider()

                    # Table of Contents
                    toc = sc.get("table_of_contents", [])
                    if toc:
                        with st.expander("Table of Contents", expanded=True):
                            for entry in toc:
                                st.write(f"**Ch {entry['chapter_num']}:** {entry['title']} (page {entry['start_page'] + 1})")

                    # Chapters
                    chapters = sc.get("chapters", [])
                    if chapters:
                        st.divider()
                        tabs = st.tabs([f"Ch {ch['chapter_num']}: {ch['title'][:30]}" for ch in chapters])

                        for tab, ch in zip(tabs, chapters):
                            with tab:
                                st.subheader(ch["title"])
                                st.caption(f"Pages {ch['start_page'] + 1}–{ch['end_page'] + 1}")

                                # Sections
                                sections = ch.get("sections", [])
                                if sections:
                                    for sec in sections:
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

                    # Raw JSON export
                    st.divider()
                    with st.expander("Raw JSON"):
                        st.json(sc)
        else:
            st.info("Enter a Book ID above, or select a book from the Library page.")
