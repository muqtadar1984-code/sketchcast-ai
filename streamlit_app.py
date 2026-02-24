"""
SketchCast AI — Streamlit UI
Agent 1: Library & Ingestion  +  Agent 2: Content Analysis
Agent 3: Script Generation    +  Agent 4: Sketch Animation

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

from rapidfuzz import fuzz

from agent1_ingestion.extractor import extract_pdf
from agent1_ingestion.image_extractor import extract_images
from agent1_ingestion.structurer import structure_book

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

if "scripts" not in st.session_state:
    st.session_state.scripts = {}  # key: "{book_id}_{chapter_num}" -> ChapterScripts dict

if "animations" not in st.session_state:
    st.session_state.animations = {}  # key: "{book_id}_{chapter_num}" -> AnimationManifest dict

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
    st.caption(
        f"{len(st.session_state.library)} book(s) | "
        f"{len(st.session_state.analyses)} analysis(es) | "
        f"{len(st.session_state.scripts)} script(s) | "
        f"{len(st.session_state.animations)} animation(s)"
    )
    st.divider()
    page = st.radio(
        "Navigate",
        [
            "📤 Upload Book",
            "📚 Library",
            "🔍 Book Viewer",
            "🧠 Analyse Chapter",
            "📊 Analysis Results",
            "🎙️ Scripts",
            "🎨 Animations",
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
                            from agent2_analysis.analyzer import \
                                run_full_analysis
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
                            m = st.columns(4)
                            with m[0]:
                                st.metric("Concepts", len(result.concepts.concepts))
                            with m[1]:
                                st.metric("Visual Opps", len(result.visual_opportunities))
                            with m[2]:
                                ep = result.episodes.episodes[0] if result.episodes.episodes else None
                                st.metric("Episode Duration", f"{ep.estimated_duration_minutes:.1f} min" if ep else "N/A")
                            with m[3]:
                                st.metric("Tokens", f"{result.token_usage.total:,}")

                            st.write(f"**Estimated cost:** ${result.token_usage.estimated_cost_usd:.4f}")

                            # ── Agent 3: Auto-generate Socratic script ────────────────────
                            st.divider()
                            with st.spinner("🎙️ Generating Socratic script with Agent 3..."):
                                try:
                                    from agent3_scripts.script_generator import (
                                        generate_chapter_scripts_from_analysis,
                                    )

                                    chapter_scripts = generate_chapter_scripts_from_analysis(
                                        book_id=book_id,
                                        chapter_num=chapter_num,
                                        analysis_dict=result.model_dump(),
                                        client=client,
                                    )

                                    st.session_state.scripts[analysis_key] = chapter_scripts.model_dump()

                                    ep_script = chapter_scripts.episodes[0] if chapter_scripts.episodes else None
                                    if ep_script:
                                        st.success("🎙️ Socratic script generated!")
                                        sm = st.columns(3)
                                        with sm[0]:
                                            st.metric("Script Segments", len(ep_script.segments))
                                        with sm[1]:
                                            st.metric("Question Hooks", ep_script.question_hook_count)
                                        with sm[2]:
                                            total_sec = ep_script.total_estimated_duration_seconds
                                            st.metric("Script Duration", f"{total_sec // 60}m {total_sec % 60}s")
                                        st.caption("View the full script on the 🎙️ Scripts page.")

                                except Exception as script_err:
                                    st.warning(f"Script generation failed: {script_err}")
                                    import traceback as _tb
                                    with st.expander("Script error details"):
                                        st.code(_tb.format_exc())

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
            ep_list = eps.get("episodes", [])
            if ep_list:
                dur = ep_list[0].get("estimated_duration_minutes", 0)
                st.metric("Episode Duration", f"{dur:.1f} min")
            else:
                st.metric("Episode Duration", "N/A")
        with mc[3]:
            st.metric("Tokens", f"{tu.get('total', 0):,}")
        with mc[4]:
            st.metric("Cost", f"${tu.get('estimated_cost_usd', 0):.4f}")

        st.divider()

        # Tabs
        tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
            ["Concepts", "Visual Opportunities", "Episode", "Images", "📜 Script", "Raw JSON"]
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

        # ── Tab 3: Episode ───────────────────────────────────────────
        with tab3:
            eps = a.get("episodes", {})
            episodes = eps.get("episodes", [])
            if not episodes:
                st.info("No episode data available.")
            else:
                ep = episodes[0]  # always exactly 1 episode per chapter
                ec = st.columns(3)
                with ec[0]:
                    st.metric("Duration", f"{ep.get('estimated_duration_minutes', 0):.1f} min")
                with ec[1]:
                    st.metric("Word Count", f"{ep.get('estimated_word_count', 0):,}")
                with ec[2]:
                    st.metric("Sections", len(ep.get("sections_covered", [])))
                st.divider()

                st.subheader(ep.get("title", "Episode"))
                st.write(f"**Sections covered:** {', '.join(ep.get('sections_covered', []))}")
                concepts_in = ep.get("key_concepts_introduced", [])
                if concepts_in:
                    st.write(f"**Concepts:** {', '.join(concepts_in)}")
                vis_in = ep.get("visual_opportunities_in_episode", [])
                if vis_in:
                    st.write(f"**Visual opportunities:** {', '.join(vis_in)}")
                st.caption("Opening hook and closing bridge will be generated by Agent 3 (Script Generation).")

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

        # ── Tab 5: Script ────────────────────────────────────────────
        with tab5:
            script_data = st.session_state.scripts.get(key)
            if not script_data:
                st.info("No script generated yet. Re-run analysis from the 🧠 Analyse Chapter page.")
            else:
                episodes = script_data.get("episodes", [])
                if not episodes:
                    st.warning("Script exists but has no episodes.")
                else:
                    ep_script = episodes[0]
                    segments = ep_script.get("segments", [])

                    # Header metrics
                    sc = st.columns(3)
                    with sc[0]:
                        st.metric("Segments", len(segments))
                    with sc[1]:
                        st.metric("Question Hooks", ep_script.get("question_hook_count", 0))
                    with sc[2]:
                        total_sec = ep_script.get("total_estimated_duration_seconds", 0)
                        st.metric("Duration", f"{total_sec // 60}m {total_sec % 60}s")

                    # ElevenLabs full-text export
                    el_full_text = "\n\n".join(
                        seg.get("elevenlabs_text", seg.get("text", ""))
                        for seg in segments
                    )
                    st.download_button(
                        "⬇️ Download ElevenLabs Script",
                        data=el_full_text,
                        file_name=f"script_{ep_script.get('episode_title', 'episode')[:30]}.txt",
                        mime="text/plain",
                    )

                    st.divider()

                    # Type → display config
                    type_config = {
                        "hook":          ("🪝", "Hook",          "#1a1a2e"),
                        "activate":      ("⚡", "Activate",      "#16213e"),
                        "explore":       ("🔍", "Explore",       "#0f3460"),
                        "question_hook": ("❓", "Question Hook", "#533483"),
                        "synthesis":     ("🎯", "Synthesis",     "#e94560"),
                        "preview":       ("👉", "Preview",       "#1a1a2e"),
                    }

                    for seg in segments:
                        seg_type = seg.get("type", "explore")
                        icon, label, _ = type_config.get(seg_type, ("•", seg_type, "#333"))
                        dur = seg.get("estimated_duration_seconds", 0)
                        pauq = seg.get("pause_for_question", False)
                        header = f"{icon} **{label}** — {dur}s"
                        if pauq:
                            header += " ⏸️ *[waits for student question]*"

                        with st.expander(header, expanded=(seg_type == "hook")):
                            st.write(seg.get("text", ""))

                            sketch = seg.get("sketch_cue")
                            if sketch:
                                st.caption(
                                    f"🎨 **Sketch cue ({sketch.get('timing', 'during')}):** "
                                    f"`{sketch.get('action', 'draw')}` — {sketch.get('element', '')}"
                                )

                            with st.expander("ElevenLabs markup"):
                                st.code(
                                    seg.get("elevenlabs_text", seg.get("text", "")),
                                    language=None,
                                )

        # ── Tab 6: Raw JSON ──────────────────────────────────────────
        with tab6:
            st.json(a)
            st.download_button(
                "Download analysis JSON",
                data=json.dumps(a, indent=2, ensure_ascii=False),
                file_name=f"analysis_{a.get('chapter_title', 'chapter')[:20]}.json",
                mime="application/json",
            )


# ══════════════════════════════════════════════════════════════════════
#  PAGE: Scripts (Agent 3)
# ══════════════════════════════════════════════════════════════════════

elif page == "🎙️ Scripts":
    st.header("🎙️ Episode Scripts")
    st.caption("Socratic dialogue scripts generated by Agent 3, ready for ElevenLabs TTS.")

    scripts = st.session_state.scripts
    if not scripts:
        st.info(
            "No scripts generated yet. Go to **🧠 Analyse Chapter** and run an analysis — "
            "scripts are generated automatically after analysis completes."
        )
    else:
        books = st.session_state.library
        analyses = st.session_state.analyses

        # Build selector labels
        labels = {}
        for skey, sc_data in scripts.items():
            bid = sc_data.get("book_id", "")
            book_title = books.get(bid, {}).get("title", "Unknown")
            ch_title = sc_data.get("chapter_title", "")
            ch_num = sc_data.get("chapter_num", 0)
            labels[f"{book_title[:30]} → Ch {ch_num}: {ch_title[:30]}"] = skey

        sel = st.selectbox("Select a script", list(labels.keys()))
        skey = labels[sel]
        sc_data = scripts[skey]

        episodes = sc_data.get("episodes", [])
        if not episodes:
            st.warning("Script has no episodes.")
        else:
            # If multiple episodes, allow selection (future-proof)
            if len(episodes) > 1:
                ep_labels = {f"Episode {ep['episode_num']}: {ep['episode_title'][:40]}": i for i, ep in enumerate(episodes)}
                sel_ep = st.selectbox("Select episode", list(ep_labels.keys()))
                ep_idx = ep_labels[sel_ep]
            else:
                ep_idx = 0

            ep = episodes[ep_idx]
            segments = ep.get("segments", [])
            total_sec = ep.get("total_estimated_duration_seconds", 0)

            st.subheader(ep.get("episode_title", "Episode"))
            st.caption(
                f"Generated: {ep.get('generated_at', '')[:19]} | "
                f"Narrator: {ep.get('narrator_persona', 'Socratic')} | "
                f"Script ID: `{ep.get('script_id', '')[:8]}...`"
            )

            hc = st.columns(4)
            with hc[0]:
                st.metric("Segments", len(segments))
            with hc[1]:
                st.metric("Question Hooks", ep.get("question_hook_count", 0))
            with hc[2]:
                st.metric("Duration", f"{total_sec // 60}m {total_sec % 60}s")
            with hc[3]:
                sketch_count = sum(1 for s in segments if s.get("sketch_cue"))
                st.metric("Sketch Cues", sketch_count)

            st.divider()

            # Download buttons
            dc1, dc2 = st.columns(2)
            with dc1:
                el_full_text = "\n\n".join(
                    seg.get("elevenlabs_text", seg.get("text", "")) for seg in segments
                )
                st.download_button(
                    "⬇️ Download ElevenLabs Script (.txt)",
                    data=el_full_text,
                    file_name=f"elevenlabs_{ep.get('episode_title', 'episode')[:30]}.txt",
                    mime="text/plain",
                )
            with dc2:
                st.download_button(
                    "⬇️ Download Full Script JSON",
                    data=json.dumps(ep, indent=2, ensure_ascii=False),
                    file_name=f"script_ch{ep.get('chapter_num', 0)}_ep{ep.get('episode_num', 1)}.json",
                    mime="application/json",
                )

            st.divider()
            st.subheader("Script Segments")

            type_config = {
                "hook":          ("🪝", "Hook"),
                "activate":      ("⚡", "Activate"),
                "explore":       ("🔍", "Explore"),
                "question_hook": ("❓", "Question Hook"),
                "synthesis":     ("🎯", "Synthesis"),
                "preview":       ("👉", "Preview"),
            }

            for seg in segments:
                seg_type = seg.get("type", "explore")
                icon, label = type_config.get(seg_type, ("•", seg_type))
                dur = seg.get("estimated_duration_seconds", 0)
                pauq = seg.get("pause_for_question", False)

                header = f"{icon} **{label}** `{seg.get('segment_id', '')}` — {dur}s"
                if pauq:
                    header += "  ⏸️ *pause for student question*"

                with st.expander(header, expanded=(seg_type in ("hook", "question_hook"))):
                    st.write(seg.get("text", ""))

                    sketch = seg.get("sketch_cue")
                    if sketch:
                        st.info(
                            f"🎨 **Sketch cue** | timing: `{sketch.get('timing', 'during')}` | "
                            f"action: `{sketch.get('action', 'draw')}` | "
                            f"element: {sketch.get('element', '')}"
                        )

                    with st.expander("ElevenLabs markup text"):
                        st.code(
                            seg.get("elevenlabs_text", seg.get("text", "")),
                            language=None,
                        )


# ══════════════════════════════════════════════════════════════════════
#  PAGE: Animations (Agent 4)
# ══════════════════════════════════════════════════════════════════════

elif page == "🎨 Animations":
    import streamlit.components.v1 as components

    st.header("🎨 Sketch Animations")
    st.caption("SVG whiteboard animations generated by Agent 4, one per script segment.")

    scripts = st.session_state.scripts
    analyses = st.session_state.analyses
    animations = st.session_state.animations
    books = st.session_state.library

    if not scripts:
        st.info(
            "No scripts found. Go to **🧠 Analyse Chapter** to run analysis — "
            "scripts and animations are generated automatically."
        )
    else:
        # Build selector from available scripts
        labels = {}
        for skey, sc_data in scripts.items():
            bid = sc_data.get("book_id", "")
            book_title = books.get(bid, {}).get("title", "Unknown")
            ch_title = sc_data.get("chapter_title", "")
            ch_num = sc_data.get("chapter_num", 0)
            labels[f"{book_title[:30]} → Ch {ch_num}: {ch_title[:30]}"] = skey

        sel_label = st.selectbox("Select a chapter script to animate", list(labels.keys()))
        anim_key = labels[sel_label]
        sc_data = scripts[anim_key]
        bid = sc_data.get("book_id", "")
        ch_num = sc_data.get("chapter_num", 0)

        # Check if animations already exist for this key
        existing_manifest = animations.get(anim_key)

        col_btn, col_status = st.columns([1, 3])
        with col_btn:
            generate_btn = st.button("🎨 Generate Animations", type="primary")
        with col_status:
            if existing_manifest:
                animated = existing_manifest.get("animated_segments", 0)
                total = existing_manifest.get("total_segments", 0)
                st.success(f"Animations ready: {animated}/{total} segments animated.")
            else:
                st.caption("No animations yet for this chapter.")

        if generate_btn:
            analysis_dict = analyses.get(anim_key)
            if not analysis_dict:
                st.error(
                    "Analysis not found in session. Please re-run analysis from "
                    "**🧠 Analyse Chapter** first."
                )
            else:
                episodes = sc_data.get("episodes", [])
                if not episodes:
                    st.error("Script has no episodes.")
                else:
                    ep = episodes[0]
                    total_segs = len(ep.get("segments", []))
                    sketch_count = sum(
                        1 for s in ep.get("segments", []) if s.get("sketch_cue")
                    )
                    st.info(
                        f"Generating animations for **{total_segs}** segments "
                        f"({sketch_count} have sketch cues, rest get blank canvas). "
                        "This calls Claude once per sketch segment — may take a minute."
                    )

                    progress_bar = st.progress(0)
                    status_text = st.empty()

                    def _progress(current, total, seg_id):
                        pct = int(current / max(total, 1) * 100)
                        progress_bar.progress(pct)
                        if seg_id != "done":
                            status_text.caption(
                                f"Processing segment {current + 1}/{total}: `{seg_id}`"
                            )
                        else:
                            status_text.caption("Done!")

                    try:
                        from agent4_animation.sketch_generator import (
                            generate_episode_animations_from_script,
                        )
                        from shared.claude_client import ClaudeClient

                        client = ClaudeClient()
                        manifest = generate_episode_animations_from_script(
                            script_data=sc_data,
                            analysis_dict=analysis_dict,
                            client=client,
                            progress_callback=_progress,
                        )
                        st.session_state.animations[anim_key] = manifest.model_dump()
                        existing_manifest = st.session_state.animations[anim_key]
                        st.success(
                            f"Animations generated: "
                            f"{manifest.animated_segments} sketched + "
                            f"{manifest.blank_segments} blank = "
                            f"{manifest.total_segments} segments total."
                        )

                    except Exception as anim_err:
                        st.error(f"Animation generation failed: {anim_err}")
                        import traceback as _tb
                        with st.expander("Error details"):
                            st.code(_tb.format_exc())

        # ── Show animation results ────────────────────────────────────
        if existing_manifest:
            st.divider()

            # Summary metrics
            mc = st.columns(4)
            with mc[0]:
                st.metric("Total Segments", existing_manifest.get("total_segments", 0))
            with mc[1]:
                st.metric("Animated", existing_manifest.get("animated_segments", 0))
            with mc[2]:
                st.metric("Blank canvas", existing_manifest.get("blank_segments", 0))
            with mc[3]:
                gen_at = existing_manifest.get("generated_at", "")[:19]
                st.metric("Generated", gen_at.replace("T", " ") if gen_at else "—")

            st.divider()

            # Segment table + SVG previews
            seg_list = existing_manifest.get("segments", [])
            type_icons = {
                "hook": "🪝", "activate": "⚡", "explore": "🔍",
                "question_hook": "❓", "synthesis": "🎯", "preview": "👉",
            }

            for ms in seg_list:
                seg_id = ms.get("segment_id", "")
                seg_type = ms.get("type", "explore")
                has_anim = ms.get("has_animation", False)
                dur = ms.get("estimated_duration_seconds", 0)
                timing = ms.get("sketch_cue_timing") or "—"
                icon = type_icons.get(seg_type, "•")
                anim_badge = "🎨 sketch" if has_anim else "⬜ blank"

                header = (
                    f"{icon} `{seg_id}` — **{seg_type}** | {anim_badge} | {dur}s"
                )
                with st.expander(header, expanded=False):
                    if has_anim:
                        st.caption(f"Sketch cue timing: `{timing}`")

                    svg_path = ms.get("svg_path")
                    roughjs_path = ms.get("roughjs_html_path")
                    has_roughjs = bool(roughjs_path and Path(roughjs_path).exists())

                    if has_roughjs:
                        tab_svg, tab_rough = st.tabs(["📐 SVG Preview", "✏️ Rough.js Preview"])
                    else:
                        tab_svg = st.container()
                        tab_rough = None

                    with tab_svg:
                        if svg_path and Path(svg_path).exists():
                            with open(svg_path, encoding="utf-8") as f:
                                svg_content = f.read()
                            display_svg = svg_content.replace(
                                'width="1280"', 'width="100%"'
                            ).replace(
                                'height="720"', 'height="auto"'
                            )
                            components.html(display_svg, height=420, scrolling=False)
                            st.download_button(
                                f"⬇️ Download SVG ({seg_id})",
                                data=svg_content,
                                file_name=f"{seg_id}.svg",
                                mime="image/svg+xml",
                                key=f"dl_svg_{seg_id}_{anim_key}",
                            )
                        else:
                            st.caption("SVG file not found on disk.")

                        anim_path = ms.get("animation_path")
                        if anim_path and Path(anim_path).exists():
                            with open(anim_path, encoding="utf-8") as f:
                                anim_data = json.load(f)
                            with st.expander("Animation timing JSON"):
                                st.json(anim_data)

                    if tab_rough is not None:
                        with tab_rough:
                            with open(roughjs_path, encoding="utf-8") as f:
                                rough_html = f.read()
                            components.html(rough_html, width=1280, height=720, scrolling=False)

            st.divider()
            # Download full manifest
            st.download_button(
                "⬇️ Download Animation Manifest JSON",
                data=json.dumps(existing_manifest, indent=2, ensure_ascii=False),
                file_name=f"animation_manifest_ch{ch_num}.json",
                mime="application/json",
            )
