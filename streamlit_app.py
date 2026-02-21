"""
SketchCast AI — Streamlit UI
Agent 1: Library & Ingestion  +  Agent 2: Content Analysis

Standalone mode: all processing runs in-process (no separate API server needed).
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
    page_title="SketchCast AI",
    page_icon="📚",
    layout="wide",
)

# ── Session state initialization ─────────────────────────────────────

if "library" not in st.session_state:
    st.session_state.library = {}

if "analyses" not in st.session_state:
    st.session_state.analyses = {}  # key: "{book_id}_{chapter_num}" -> MasterAnalysis dict

if "selected_book_id" not in st.session_state:
    st.session_state.selected_book_id = None

FUZZY_THRESHOLD = 85


# ── Agent 1 helpers ──────────────────────────────────────────────────

def compute_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_duplicate(file_hash, title, author, isbn):
    for bid, rec in st.session_state.library.items():
        if isbn and rec.get("isbn") == isbn:
            return bid, "ISBN match"
        if title and author and rec.get("title") and rec.get("author"):
            t = fuzz.ratio(title.lower(), rec["title"].lower())
            a = fuzz.ratio(author.lower(), rec["author"].lower())
            if (t + a) / 2 >= FUZZY_THRESHOLD:
                return bid, f"Fuzzy match ({(t + a) / 2:.0f}%)"
        if rec.get("file_hash") == file_hash:
            return bid, "Identical file"
    return None, None


def process_pdf(file_bytes, filename, title, author, isbn):
    book_id = str(uuid.uuid4())
    resolved_title = title or filename.rsplit(".", 1)[0]
    resolved_author = author or "Unknown"
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        extraction = extract_pdf(tmp_path)
        images = extract_images(tmp_path, book_id)
        structured = structure_book(
            book_id=book_id, title=resolved_title, author=resolved_author,
            isbn=isbn, extraction=extraction, images=images,
        )
        record = {
            "book_id": book_id, "title": resolved_title, "author": resolved_author,
            "isbn": isbn, "file_hash": compute_hash(file_bytes),
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
    st.caption("Multi-Agent EdTech Platform")
    st.divider()
    st.success("Embedded mode — no API server needed", icon="✅")
    st.caption(f"{len(st.session_state.library)} book(s) | {len(st.session_state.analyses)} analysis(es)")
    st.divider()
    page = st.radio(
        "Navigate",
        [
            "📤 Upload Book",
            "📚 Library",
            "🔍 Book Viewer",
            "🧠 Analyse Chapter",
            "📊 Analysis Results",
        ],
        label_visibility="collapsed",
    )


# ══════════════════════════════════════════════════════════════════════
#  PAGE: Upload Book
# ══════════════════════════════════════════════════════════════════════

if page == "📤 Upload Book":
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
        dup_id, dup_reason = check_duplicate(file_hash, title, author, isbn)
        if dup_id:
            st.info(f"**Duplicate detected** — {dup_reason}")
            st.write(f"Existing Book ID: `{dup_id}`")
            st.session_state.selected_book_id = dup_id
        else:
            progress = st.progress(10, text="Extracting text and images...")
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


# ══════════════════════════════════════════════════════════════════════
#  PAGE: Library
# ══════════════════════════════════════════════════════════════════════

elif page == "📚 Library":
    st.header("Book Library")
    books = list(st.session_state.library.values())
    if not books:
        st.info("No books in the library yet. Upload a PDF to get started.")
    else:
        st.write(f"**{len(books)} book(s)** in the library")
        st.divider()
        for book in books:
            col1, col2, col3 = st.columns([3, 1, 1])
            with col1:
                st.subheader(book["title"])
                st.caption(f"by {book['author']}")
            with col2:
                st.success("Completed")
            with col3:
                st.metric("Pages", book["total_pages"])
            meta = st.columns(4)
            with meta[0]:
                st.write(f"**Chapters:** {book['total_chapters']}")
            with meta[1]:
                st.write(f"**Readability:** {book['readability_score']:.0%}")
            with meta[2]:
                st.write(f"**ISBN:** {book.get('isbn') or 'N/A'}")
            with meta[3]:
                st.write(f"**ID:** `{book['book_id'][:8]}...`")
            if st.button("View details", key=f"view_{book['book_id']}"):
                st.session_state.selected_book_id = book["book_id"]
                st.rerun()
            st.divider()


# ══════════════════════════════════════════════════════════════════════
#  PAGE: Book Viewer
# ══════════════════════════════════════════════════════════════════════

elif page == "🔍 Book Viewer":
    st.header("Book Viewer")
    books = st.session_state.library
    options = {f"{b['title'][:40]} ({bid[:8]}...)": bid for bid, b in books.items()}
    if not options:
        st.info("No books available. Upload a PDF first.")
    else:
        default_idx = 0
        if st.session_state.selected_book_id in books:
            bid = st.session_state.selected_book_id
            lbl = f"{books[bid]['title'][:40]} ({bid[:8]}...)"
            if lbl in options:
                default_idx = list(options.keys()).index(lbl)
        sel = st.selectbox("Select a book", list(options.keys()), index=default_idx)
        book_id = options[sel]
        book = books[book_id]
        sc = book.get("structured")
        st.subheader(book["title"])
        st.caption(f"by {book['author']}")
        cols = st.columns(4)
        with cols[0]:
            st.metric("Pages", book["total_pages"])
        with cols[1]:
            st.metric("Chapters", book["total_chapters"])
        with cols[2]:
            st.metric("Readability", f"{book['readability_score']:.0%}")
        with cols[3]:
            st.metric("Status", "Completed")
        if sc:
            st.divider()
            toc = sc.get("table_of_contents", [])
            if toc:
                with st.expander("Table of Contents", expanded=True):
                    for entry in toc:
                        st.write(f"**Ch {entry['chapter_num']}:** {entry['title']} (page {entry['start_page'] + 1})")
            chapters = sc.get("chapters", [])
            if chapters:
                st.divider()
                tabs = st.tabs([f"Ch {ch['chapter_num']}: {ch['title'][:30]}" for ch in chapters])
                for tab, ch in zip(tabs, chapters):
                    with tab:
                        st.subheader(ch["title"])
                        st.caption(f"Pages {ch['start_page'] + 1} – {ch['end_page'] + 1}")
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
                        key_boxes = ch.get("key_boxes", [])
                        if key_boxes:
                            st.divider()
                            st.write("**Special Content Boxes:**")
                            for box in key_boxes:
                                icon_map = {"activity": "🧪", "definition": "📖", "info": "💡", "exercise": "✏️", "quote": "💬"}
                                icon = icon_map.get(box["type"], "📌")
                                with st.expander(f"{icon} {box['title']}"):
                                    st.write(box["content"])
                                    st.caption(f"Page {box['page_num'] + 1} | Type: {box['type']}")
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
            st.divider()
            c1, c2 = st.columns(2)
            with c1:
                with st.expander("Raw JSON"):
                    st.json(sc)
            with c2:
                st.download_button(
                    "Download structured JSON",
                    data=json.dumps(sc, indent=2, ensure_ascii=False),
                    file_name=f"{book['title'][:30]}_structured.json",
                    mime="application/json",
                )


# ══════════════════════════════════════════════════════════════════════
#  PAGE: Analyse Chapter (Agent 2)
# ══════════════════════════════════════════════════════════════════════

elif page == "🧠 Analyse Chapter":
    st.header("Analyse Chapter with AI")

    books = st.session_state.library
    if not books:
        st.info("No books available. Upload a PDF first on the Upload page.")
    else:
        book_labels = {f"{b['title'][:50]} ({bid[:8]}...)": bid for bid, b in books.items()}
        sel_book = st.selectbox("Choose a book", list(book_labels.keys()), key="a2_book")
        book_id = book_labels[sel_book]
        book = books[book_id]
        sc = book.get("structured", {})
        chapters = sc.get("chapters", [])

        if not chapters:
            st.warning("This book has no chapters. Try a different book.")
        else:
            ch_labels = {f"Ch {ch['chapter_num']}: {ch['title'][:50]}": ch["chapter_num"] for ch in chapters}
            sel_ch = st.selectbox("Choose a chapter", list(ch_labels.keys()), key="a2_ch")
            chapter_num = ch_labels[sel_ch]

            level = st.selectbox(
                "Target student level",
                ["primary_school", "middle_school", "high_school"],
                index=1,
                format_func=lambda x: x.replace("_", " ").title(),
            )

            # Check if already analysed
            analysis_key = f"{book_id}_{chapter_num}"
            already_done = analysis_key in st.session_state.analyses

            if already_done:
                st.info("This chapter has already been analysed. You can re-analyse or view results.")

            # Check API key availability
            _api_key_available = False
            try:
                _key = st.secrets.get("ANTHROPIC_API_KEY", "")
                if _key:
                    _api_key_available = True
            except Exception:
                pass
            if not _api_key_available:
                import os as _os
                if _os.getenv("ANTHROPIC_API_KEY"):
                    _api_key_available = True

            if not _api_key_available:
                st.warning(
                    "**API key not configured.** To use AI analysis, add your Anthropic API key:\n\n"
                    "**Streamlit Cloud:** Go to app Settings → Secrets → add:\n"
                    "```\nANTHROPIC_API_KEY = \"sk-ant-...\"\n```\n\n"
                    "**Locally:** Create `.streamlit/secrets.toml` with the same line."
                )

            if st.button("🚀 Run Analysis", type="primary", disabled=not _api_key_available):
                # Get the chapter content
                chapter_content = None
                for ch in chapters:
                    if ch["chapter_num"] == chapter_num:
                        chapter_content = ch
                        break

                if chapter_content is None:
                    st.error("Chapter not found.")
                else:
                    with st.spinner("Analysing chapter with Claude AI... this may take 30–60 seconds"):
                        try:
                            from agent2_analysis.analyzer import run_full_analysis
                            from shared.claude_client import ClaudeClient

                            client = ClaudeClient()
                            result = run_full_analysis(
                                book_id=book_id,
                                chapter_content=chapter_content,
                                level=level,
                                client=client,
                            )

                            # Store in session state
                            st.session_state.analyses[analysis_key] = result.model_dump()

                            st.success("Analysis complete!")

                            # Summary metrics
                            m = st.columns(5)
                            with m[0]:
                                st.metric("Concepts", len(result.concepts.concepts))
                            with m[1]:
                                st.metric("Visual Opps", len(result.visual_opportunities))
                            with m[2]:
                                st.metric("Episodes", result.episodes.total_episodes)
                            with m[3]:
                                total_dur = sum(ep.estimated_duration_minutes for ep in result.episodes.episodes)
                                st.metric("Est. Duration", f"{total_dur:.0f} min")
                            with m[4]:
                                st.metric("Tokens", f"{result.token_usage.total:,}")

                            st.write(f"**Estimated cost:** ${result.token_usage.estimated_cost_usd:.4f}")

                        except Exception as e:
                            st.error(f"Analysis failed: {str(e)}")
                            import traceback
                            st.code(traceback.format_exc())

            st.caption("Analysis uses Claude AI and consumes API tokens. Each chapter costs approximately $0.03–$0.08.")


# ══════════════════════════════════════════════════════════════════════
#  PAGE: Analysis Results (Agent 2)
# ══════════════════════════════════════════════════════════════════════

elif page == "📊 Analysis Results":
    st.header("Analysis Results")

    analyses = st.session_state.analyses
    if not analyses:
        st.info("No analyses yet. Go to 'Analyse Chapter' to run one.")
    else:
        # Build selector
        books = st.session_state.library
        analysed_keys = list(analyses.keys())

        labels = {}
        for key in analysed_keys:
            a = analyses[key]
            bid = a.get("book_id", "")
            book_title = books.get(bid, {}).get("title", "Unknown")
            ch_title = a.get("chapter_title", "")
            ch_num = a.get("chapter_num", 0)
            labels[f"{book_title[:30]} → Ch {ch_num}: {ch_title[:30]}"] = key

        sel = st.selectbox("Select an analysis", list(labels.keys()))
        key = labels[sel]
        a = analyses[key]

        # Header
        st.subheader(a.get("chapter_title", ""))
        st.caption(f"Level: {a.get('difficulty_level_requested', '').replace('_', ' ').title()} | "
                   f"Analysed: {a.get('analyzed_at', '')[:19]}")

        tu = a.get("token_usage", {})
        mc = st.columns(5)
        with mc[0]:
            st.metric("Concepts", len(a.get("concepts", {}).get("concepts", [])))
        with mc[1]:
            st.metric("Visuals", len(a.get("visual_opportunities", [])))
        with mc[2]:
            eps = a.get("episodes", {})
            st.metric("Episodes", eps.get("total_episodes", 0))
        with mc[3]:
            st.metric("Tokens", f"{tu.get('total', 0):,}")
        with mc[4]:
            st.metric("Cost", f"${tu.get('estimated_cost_usd', 0):.4f}")

        st.divider()

        # Tabs
        tab1, tab2, tab3, tab4, tab5 = st.tabs(
            ["Concepts", "Visual Opportunities", "Episodes", "Images", "Raw JSON"]
        )

        # ── Tab 1: Concepts ──────────────────────────────────────────
        with tab1:
            concepts_data = a.get("concepts", {})
            concepts = concepts_data.get("concepts", [])
            if not concepts:
                st.info("No concepts extracted.")
            else:
                st.write(f"**{len(concepts)} concepts** extracted")

                # Dependencies
                deps = concepts_data.get("dependencies", [])
                if deps:
                    with st.expander("Concept Dependency Map"):
                        for dep in deps:
                            requires = ", ".join(dep.get("requires", []))
                            st.write(f"**{dep.get('concept', '')}** requires [{requires}] — {dep.get('reason', '')}")

                # Prerequisites
                prereqs = concepts_data.get("prerequisites", [])
                if prereqs:
                    with st.expander("Prerequisites"):
                        for p in prereqs:
                            st.write(f"• {p.get('topic', '')} (assumed: {p.get('assumed_grade', '')})")

                st.divider()
                for c in concepts:
                    importance = c.get("importance", "supporting")
                    color_map = {"foundational": "🔴", "supporting": "🟡", "application": "🟢"}
                    badge = color_map.get(importance, "⚪")

                    with st.expander(f"{badge} {c.get('name', '')} [{importance}]"):
                        st.write(f"**Definition:** {c.get('definition', '')}")
                        st.write(f"**ID:** `{c.get('concept_id', '')}`")
                        related = c.get("related_concepts", [])
                        if related:
                            st.write(f"**Related:** {', '.join(related)}")

                # Difficulty assessments
                diffs = a.get("difficulty_assessments", [])
                if diffs:
                    st.divider()
                    st.write("**Difficulty Assessments:**")
                    for d in diffs:
                        score = d.get("difficulty_score", 5)
                        label = d.get("difficulty_label", "moderate")
                        with st.expander(f"📏 {d.get('section_title', '')} — {label} ({score}/10)"):
                            st.write(f"**Reasons:** {', '.join(d.get('reasons', []))}")
                            st.write(f"**Vocabulary load:** {d.get('vocabulary_load', '')}")
                            st.write(f"**New concepts:** {d.get('new_concepts_count', 0)}")
                            st.write(f"**Pacing:** {d.get('recommended_pacing', '')}")
                            analogies = d.get("suggested_analogies", [])
                            if analogies:
                                st.write(f"**Analogies:** {', '.join(analogies)}")

        # ── Tab 2: Visual Opportunities ──────────────────────────────
        with tab2:
            visuals = a.get("visual_opportunities", [])
            if not visuals:
                st.info("No visual opportunities detected.")
            else:
                st.write(f"**{len(visuals)} visual opportunities** detected")
                for v in visuals:
                    complexity = v.get("complexity", "medium")
                    comp_badge = {"simple": "🟢", "medium": "🟡", "complex": "🔴"}.get(complexity, "⚪")
                    with st.expander(f"{comp_badge} {v.get('title', '')} [{v.get('visual_type', '')}]"):
                        st.write(f"**Section:** {v.get('section', '')}")
                        st.write(f"**Trigger:** _{v.get('trigger_text', '')}_")
                        st.write(f"**Description:** {v.get('description', '')}")
                        st.write(f"**Duration:** {v.get('estimated_duration_seconds', 0)}s | Complexity: {complexity}")
                        seq = v.get("animation_sequence", [])
                        if seq:
                            st.write("**Animation sequence:**")
                            for step in seq:
                                st.write(f"  {step.get('step', '')}. `{step.get('action', '')}` — {step.get('details', '')} ({step.get('duration_ms', 0)}ms)")
                        elements = v.get("sketch_elements", [])
                        if elements:
                            st.write(f"**Elements:** {', '.join(elements)}")

        # ── Tab 3: Episodes ──────────────────────────────────────────
        with tab3:
            eps = a.get("episodes", {})
            episodes = eps.get("episodes", [])
            if not episodes:
                st.info("No episodes planned.")
            else:
                total_dur = sum(ep.get("estimated_duration_minutes", 0) for ep in episodes)
                ec = st.columns(3)
                with ec[0]:
                    st.metric("Total Episodes", len(episodes))
                with ec[1]:
                    st.metric("Total Duration", f"{total_dur:.1f} min")
                with ec[2]:
                    st.metric("Avg Duration", f"{total_dur / len(episodes):.1f} min")
                st.divider()

                for ep in episodes:
                    with st.expander(f"Episode {ep.get('episode_num', '')}: {ep.get('title', '')}"):
                        st.write(f"**Duration:** {ep.get('estimated_duration_minutes', 0)} min "
                                 f"({ep.get('estimated_word_count', 0)} words)")
                        st.write(f"**Sections:** {', '.join(ep.get('sections_covered', []))}")
                        st.write(f"**Opening hook:** _{ep.get('opening_hook', '')}_")
                        st.write(f"**Closing bridge:** _{ep.get('closing_bridge', '')}_")
                        concepts_in = ep.get("key_concepts_introduced", [])
                        if concepts_in:
                            st.write(f"**Concepts:** {', '.join(concepts_in)}")
                        vis_in = ep.get("visual_opportunities_in_episode", [])
                        if vis_in:
                            st.write(f"**Visuals:** {', '.join(vis_in)}")

        # ── Tab 4: Images ────────────────────────────────────────────
        with tab4:
            img_analyses = a.get("image_analyses", [])
            if not img_analyses:
                st.info("No images analysed.")
            else:
                st.write(f"**{len(img_analyses)} image(s)** analysed")
                for img in img_analyses:
                    can_sketch = img.get("can_be_recreated_as_sketch", False)
                    sketch_badge = "✅ Can recreate" if can_sketch else "❌ Cannot recreate"
                    with st.expander(f"🖼️ {img.get('image_filename', '')} [{sketch_badge}]"):
                        st.write(f"**Type:** {img.get('visual_type', '')}")
                        st.write(f"**Description:** {img.get('description', '')}")
                        elements = img.get("key_elements", [])
                        if elements:
                            st.write(f"**Key elements:** {', '.join(elements)}")
                        st.write(f"**Educational value:** {img.get('educational_value', '')}")
                        if can_sketch:
                            st.write(f"**Sketch notes:** {img.get('sketch_recreation_notes', '')}")
                        st.write(f"**Complexity:** {img.get('complexity', '')}")

        # ── Tab 5: Raw JSON ──────────────────────────────────────────
        with tab5:
            st.json(a)
            st.download_button(
                "Download analysis JSON",
                data=json.dumps(a, indent=2, ensure_ascii=False),
                file_name=f"analysis_{a.get('chapter_title', 'chapter')[:20]}.json",
                mime="application/json",
            )
