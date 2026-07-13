"""Process one generation: book PDF -> lesson (slides + video + deck) -> Supabase.

Reuses the existing agents end-to-end. Files are processed in a temp dir and the
final deck (.pptx) + video (.mp4) are uploaded to the `artifacts` bucket under
{owner_id}/{generation_id}/.
"""

from __future__ import annotations

import logging
import re
import tempfile
import uuid
from pathlib import Path

from supabase import Client

from . import client as db

# Book-title cleanup — uploaded PDFs often carry junk filenames
# (e.g. "pdfcoffee.com_cambridge-maths-5-learner-book-pdf-free"). Mirrors the app's
# src/utils/book.ts so worker and UI agree on what a "filename-like" title is.
_DOMAIN_HEAD = re.compile(r"^[a-z0-9-]+\.(?:com|net|org|pub|io|in|co|info|xyz)[._-]+", re.I)
_JUNK_TAIL = re.compile(r"[\s._-]*(pdf[\s._-]*free|free[\s._-]*pdf|ebook|pdf|free|download)\s*$", re.I)


def _looks_like_filename(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return True
    return (
        " " not in s
        or s.lower().endswith(".pdf")
        or bool(_DOMAIN_HEAD.search(s))
        or bool(re.search(r"[\s._-](pdf|free)", s, re.I))
    )


def _clean_title_fallback(s: str) -> str:
    t = re.sub(r"\.pdf$", "", s or "", flags=re.I)
    t = _DOMAIN_HEAD.sub("", t)
    t = _JUNK_TAIL.sub("", _JUNK_TAIL.sub("", t))
    t = re.sub(r"[_-]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t.title() if t else "Untitled book"

logger = logging.getLogger("worker")

DEFAULT_LEVEL = "middle_school"


def _elevenlabs_enabled() -> bool:
    """Worker-side gate for premium (ElevenLabs) TTS — defense in depth. Premium
    voices run ONLY when the deployment enables the flag AND the key is present,
    no matter what voice the request asked for."""
    import os

    on = os.getenv("ELEVENLABS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
    return on and bool(os.getenv("ELEVENLABS_API_KEY"))


def _pick_chapter(chapters: list[dict], chapter_ref: str | None) -> dict:
    real = [c for c in chapters if c.get("chapter_num") is not None]
    pool = real or chapters
    if chapter_ref:
        try:
            want = int(str(chapter_ref).strip())
            for c in pool:
                if int(c.get("chapter_num", -999)) == want:
                    return c
        except ValueError:
            pass
    return pool[0]


def process_generation(sb: Client, job: dict, generation_id: str) -> None:
    # Lazy imports shared by every generation kind.
    from agent1_ingestion.extractor import extract_pdf
    from agent1_ingestion.image_extractor import extract_images
    from agent1_ingestion.structurer import structure_book
    from agent2_analysis.analyzer import run_full_analysis
    from shared.claude_client import ClaudeClient
    from worker.branding import load_branding

    job_id = job["id"]
    db.set_generation_status(sb, generation_id, "processing")  # so the UI shows progress, not "queued"
    gen = db.get_generation(sb, generation_id)
    book = db.get_book(sb, gen["book_id"])
    # Console takedown racing a queued job: taken-down content must not be
    # (re)generated. Columns arrive with app migration 0015 — .get() is safe
    # either way.
    if gen.get("removed_at") or book.get("removed_at"):
        raise RuntimeError("content removed")
    owner_id = gen["owner_id"]
    book_id = book["id"]
    kind = gen.get("kind") or "presentation"

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = db.download_book(sb, book["storage_path"], Path(tmp) / "book.pdf")
        db.set_progress(sb, job_id, 10)

        client = ClaudeClient()

        # Agent 1 — ingest. Chapter boundaries stored at indexing time are reused
        # (known_chapters) so every generation splits the book identically without
        # re-running detection; client+pdf_path enable the Claude fallback for
        # books the heuristics can't read (see structure_book's cascade).
        extraction = extract_pdf(str(pdf_path))
        images = extract_images(str(pdf_path), book_id)
        structured = structure_book(
            book_id=book_id, title=book.get("title") or "Untitled",
            author=book.get("author") or "Unknown", isbn=None, extraction=extraction, images=images,
            pdf_path=str(pdf_path), client=client, known_chapters=book.get("chapters"),
        ).model_dump()
        chapter = _pick_chapter(structured.get("chapters", []), gen.get("chapter_ref"))
        chapter_num = int(chapter.get("chapter_num", 0))
        chapter_title = chapter.get("title") or f"Chapter {chapter_num}"

        # Scanned book (no text layer) → the chapter has no content for the
        # pipeline to teach from. Transcribe its pages with Claude vision once,
        # up front, so every generation kind gets real chapter text.
        section_chars = sum(
            len(s.get("content") or "")
            + sum(len(ss.get("content") or "") for ss in (s.get("subsections") or []))
            for s in (chapter.get("sections") or [])
        )
        if section_chars < 200:
            # Scanned chapter: transcribe the pages with Claude vision ONCE and
            # cache the text (chapter_grounding.source_text, keyed book+chapter).
            # Every later generation of this chapter — any kind, any owner —
            # reuses it instead of re-running the multi-call, ~minutes-long OCR.
            ocr_text = db.get_chapter_source_text(sb, book_id, chapter_num)
            if not ocr_text:
                from agent1_ingestion.vision_chapters import chapter_text_vision

                ocr_text = chapter_text_vision(
                    str(pdf_path), int(chapter.get("start_page", 0)),
                    int(chapter.get("end_page", 0)), client,
                )
                if ocr_text:
                    db.set_chapter_source_text(sb, book_id, chapter_num, ocr_text)
            if ocr_text:
                chapter["sections"] = [{
                    "section_title": "Content", "section_type": "body",
                    "content": ocr_text, "page_num": chapter.get("start_page", 0),
                    "subsections": [],
                }]

        # Guard: the sliced text must actually belong to the requested chapter.
        # Wrong stored boundaries/titles fail LOUD here instead of shipping a
        # lesson about a different unit (user-reported failure mode).
        from agent1_ingestion.chapter_check import verify_chapter_content

        sample = " ".join(
            (s.get("content") or "") + " "
            + " ".join(ss.get("content") or "" for ss in (s.get("subsections") or []))
            for s in (chapter.get("sections") or [])
        )
        ok, actual = verify_chapter_content(chapter_title, sample, client)
        if not ok:
            raise RuntimeError(
                f"Chapter check failed: \"{chapter_title}\" was requested but its pages "
                f"read as \"{actual}\". The book's chapter list looks stale or wrong — "
                "delete and re-upload the book to re-detect chapters, then generate again."
            )
        db.set_progress(sb, job_id, 20)

        # Agent 2 — analysis (shared by every kind)
        analysis = run_full_analysis(
            book_id=book_id, chapter_content=chapter, level=DEFAULT_LEVEL, client=client,
        ).model_dump()
        db.set_progress(sb, job_id, 45)

        # Persist chapter grounding for the AI Tutor — the concept analysis now
        # (covers every kind); the narrated lesson's script is added below for
        # the presentation kind. This is the tutor's curriculum fence. Best-effort.
        db.set_chapter_grounding(sb, book_id, chapter_num, chapter_title, analysis)

        # School branding (templates + derived accent/logo) — falls back to defaults.
        branding = load_branding(sb, owner_id, Path(tmp))
        base = f"{owner_id}/{generation_id}"

        if kind == "presentation":
            # Narrated deck + video.
            from agent3_scripts.script_generator import generate_chapter_scripts_from_analysis
            from agent5_slides.slide_generator import generate_episode_slides
            from agent6_animation.video_composer import compose_episode_videos
            from agent8_render.renderer import render_final_video

            params = gen.get("params") or {}
            narration_style = params.get("narration_style") or "socratic"
            tts_voice = params.get("tts_voice")  # voice-registry id; None → free Edge default
            allow_premium = _elevenlabs_enabled()

            scripts = generate_chapter_scripts_from_analysis(
                book_id=book_id, chapter_num=chapter_num, analysis_dict=analysis, client=client,
                narration_style=narration_style,
            ).model_dump()
            ep_title = (scripts.get("episodes") or [{}])[0].get("episode_title") or chapter_title
            db.set_progress(sb, job_id, 60)

            # Enrich the tutor grounding with the lesson's own narration text —
            # the best source for answers that "sound like the lesson".
            _script_text = " ".join(
                (seg.get("text") or "")
                for ep in (scripts.get("episodes") or [])
                for seg in (ep.get("segments") or [])
            ).strip()
            db.set_chapter_grounding(sb, book_id, chapter_num, chapter_title, analysis, _script_text)

            # Warm the AI Tutor cache: pre-compute the questions a student is most
            # likely to ask so the first "Ask Coach" is instant + $0. Gated
            # (TUTOR_WARM_CACHE) and best-effort — never affects this lesson.
            from worker.tutor_warm import warm_tutor_cache
            warm_tutor_cache(sb, client, book_id, chapter_num, chapter_title, analysis, _script_text)

            # (No AI-image step in the freemium path — slides are rendered natively.)
            slides = generate_episode_slides(
                script_data=scripts, branding=branding,
            ).model_dump()
            db.set_progress(sb, job_id, 72)

            voice_report: dict = {}
            video = compose_episode_videos(
                script_data=scripts, slide_manifest=slides, branding=branding,
                tts_voice=tts_voice, allow_premium=allow_premium, voice_report=voice_report,
            ).model_dump()
            db.set_progress(sb, job_id, 90)

            # Record the voice that ACTUALLY rendered so a silent premium→free
            # downgrade (ElevenLabs not enabled/keyed on the worker, spend cap, or
            # an API failure) is visible in the app — not discovered by listening.
            if voice_report:
                db.merge_generation_params(sb, generation_id, {
                    "tts_voice_used": (voice_report.get("used") or [None])[0],
                    "tts_voice_downgraded": bool(voice_report.get("downgraded")),
                })
                if voice_report.get("downgraded"):
                    logger.warning(
                        "generation %s: requested premium voice %r but rendered %s — "
                        "set ELEVENLABS_ENABLED=true + ELEVENLABS_API_KEY on the worker.",
                        generation_id, tts_voice, voice_report.get("used"),
                    )

            final = render_final_video(video_manifest=video).model_dump()
            db.set_progress(sb, job_id, 96)

            deck_path = slides.get("deck_path")
            if deck_path and Path(deck_path).exists():
                db.upload_artifact(sb, deck_path, f"{base}/deck.pptx")
                db.add_artifact_row(sb, generation_id, "deck_pptx", f"{base}/deck.pptx")
            final_video = final.get("final_video_path")
            if final_video and Path(final_video).exists():
                db.upload_artifact(sb, final_video, f"{base}/lesson.mp4")
                db.add_artifact_row(sb, generation_id, "video_mp4", f"{base}/lesson.mp4")
            title = f"{book.get('title', 'Lesson')} · {ep_title}"

        elif kind in ("lesson_plan", "activity", "exam_paper", "worksheet", "case_study"):
            # Claude-authored teacher document → editable .docx.
            from docgen import generate_document
            from shared.claude_client import artifact_model

            # Authoring runs on the cheaper artifact model (Haiku by default); the
            # ingestion/analysis above stay on Sonnet. Fold its spend into the job so
            # jobs.usage still reflects the full per-lesson cost.
            gen_client = ClaudeClient(model=artifact_model(kind))
            out_path = generate_document(
                kind=kind, book=book, chapter=chapter, analysis=analysis,
                client=gen_client, params=gen.get("params") or {}, out_dir=Path(tmp),
                template=branding.get("docx_template"),
            )
            for _k, _v in gen_client.session_usage.items():
                client.session_usage[_k] = client.session_usage.get(_k, 0) + _v
            db.set_progress(sb, job_id, 90)
            dest = f"{base}/{kind}.docx"
            db.upload_artifact(sb, str(out_path), dest)
            db.add_artifact_row(sb, generation_id, "docx", dest)

            # Structured questions for the interactive quiz player (worksheet/exam).
            # Additive + best-effort: a missing 'questions_json' enum value (migration
            # not yet applied) must not fail the generation.
            qpath = Path(tmp) / "questions.json"
            if kind in ("worksheet", "exam_paper") and qpath.exists():
                try:
                    qdest = f"{base}/{kind}_questions.json"
                    db.upload_artifact(sb, str(qpath), qdest)
                    db.add_artifact_row(sb, generation_id, "questions_json", qdest)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("questions_json upload skipped for %s: %s", generation_id, exc)

            db.set_progress(sb, job_id, 96)
            label = {
                "lesson_plan": "Lesson plan", "activity": "Activities", "exam_paper": "Test paper",
                "worksheet": "Worksheet", "case_study": "Case study",
            }[kind]
            title = f"{book.get('title', 'Document')} · {chapter_title} · {label}"

        else:
            raise RuntimeError(f"Unsupported generation kind: {kind}")

        db.set_generation_title(sb, generation_id, title)

    # Attribute this job's Claude spend to the row (unit economics per lesson).
    db.set_job_usage(sb, job_id, client.session_usage)
    db.finish_job(sb, job_id, generation_id)
    logger.info("Generation %s (%s) done — %s", generation_id, kind, client.session_usage)


def index_book(sb: Client, job: dict) -> None:
    """Extract a book's chapter list (Agent 1) and store it on the book.

    Runs once per uploaded book so the dashboard can offer a lesson per chapter.
    Reuses the same chapter detection the generator uses (so `num` here matches
    what `_pick_chapter` selects), but skips image extraction — only chapter
    numbers + titles are needed for the list, which keeps indexing fast.
    """
    from agent1_ingestion.extractor import extract_pdf
    from agent1_ingestion.structurer import structure_book

    book_id = job["book_id"]
    book = db.get_book(sb, book_id)
    if book.get("removed_at"):
        raise RuntimeError("content removed")

    # Claude enables chapter detection for books the text heuristics can't read
    # (scanned pages, unconventional labels). Best-effort: without a key,
    # indexing still works for conventional books.
    try:
        from shared.claude_client import ClaudeClient
        client = ClaudeClient()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Claude unavailable for indexing (%s) — heuristics only", exc)
        client = None

    cover_dest = None
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = db.download_book(sb, book["storage_path"], Path(tmp) / "book.pdf")
        extraction = extract_pdf(str(pdf_path))
        structured = structure_book(
            book_id=book_id, title=book.get("title") or "Untitled",
            author=book.get("author") or "Unknown", isbn=None,
            extraction=extraction, images=[],
            pdf_path=str(pdf_path), client=client,
        ).model_dump()

        # Audit the detected list BEFORE it becomes the book's stored truth:
        # Claude reads each chapter's opening text, fixes garbled titles, and —
        # when most entries look wrong — the whole list is re-detected by the
        # vision reader. Best-effort: any failure keeps the heuristic result.
        chapter_defs = structured.get("chapters", [])
        if client is not None and len(chapter_defs) > 1:
            try:
                from agent1_ingestion.chapter_check import audit_chapter_list

                audit = audit_chapter_list(extraction, chapter_defs, client)
                for c in chapter_defs:
                    fix = audit["titles"].get(int(c.get("chapter_num", 0)))
                    if fix:
                        c["title"] = fix
                bad = audit["mismatched"]
                if len(bad) * 2 >= len(chapter_defs):
                    from agent1_ingestion.vision_chapters import detect_chapters_vision

                    logger.warning(
                        "chapter audit flagged %d/%d entries for %s — escalating to vision",
                        len(bad), len(chapter_defs), book_id,
                    )
                    smart = detect_chapters_vision(str(pdf_path), extraction.total_pages, client)
                    if smart:
                        re_audit = audit_chapter_list(extraction, smart, client)
                        if len(re_audit["mismatched"]) < len(bad):
                            for c in smart:
                                fix = re_audit["titles"].get(int(c.get("chapter_num", 0)))
                                if fix:
                                    c["title"] = fix
                            chapter_defs = smart
                structured["chapters"] = chapter_defs
            except Exception as exc:  # noqa: BLE001 — audit must never block indexing
                logger.warning("chapter audit skipped for %s: %s", book_id, exc)

        # Book Health Score — a predictive read from signals we already have,
        # shown the moment indexing finishes so a bad scan is caught before it
        # generates failed lessons. Best-effort: never block indexing.
        health = None
        try:
            from agent1_ingestion.book_health import compute_book_health

            health = compute_book_health(extraction, structured.get("chapters", []))
        except Exception as exc:  # noqa: BLE001
            logger.warning("book health skipped for %s: %s", book_id, exc)

        # Cover thumbnail (page 0) for the library UI — best-effort.
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(pdf_path))
            pix = doc.load_page(0).get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
            cover_png = Path(tmp) / "cover.png"
            pix.save(str(cover_png))
            doc.close()
            cover_dest = f"{book['owner_id']}/covers/{book_id}.png"
            db.upload_artifact(sb, str(cover_png), cover_dest)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cover render failed for %s: %s", book_id, exc)
            cover_dest = None

    # Persist page boundaries too, so generations reuse the exact same split
    # without re-running detection (crucial for scanned books, where detection
    # is a vision pass).
    chapters = [
        {"num": int(c["chapter_num"]),
         "title": (c.get("title") or f"Chapter {c['chapter_num']}").strip(),
         "start_page": int(c.get("start_page", 0)),
         "end_page": int(c.get("end_page", 0))}
        for c in structured.get("chapters", [])
    ]

    # Auto-detect grade + subject + a clean title/author from the (often filename-derived)
    # title and chapter list (best-effort; never block indexing). Identified for the teacher.
    grade = subject = None
    detected_title = detected_author = None
    try:
        if client is None:
            raise RuntimeError("Claude unavailable")
        sample = "\n".join(c["title"] for c in chapters[:25])
        prompt = (
            "From this textbook's filename-derived title and chapter list, identify metadata. "
            "Respond ONLY as JSON with keys: "
            "\"grade\" (short canonical, e.g. \"Grade 5\"), "
            "\"subject\" (canonical, e.g. \"Mathematics\", \"Science\", \"History\"), "
            "\"title\" (a clean, human-readable book title: REMOVE download-site names, file "
            "extensions and slug dashes — e.g. "
            "\"pdfcoffee.com_cambridge-maths-5-learner-book-pdf-free\" becomes "
            "\"Cambridge Primary Mathematics Learner's Book 5\" if you recognise it, else a tidied "
            "version like \"Cambridge Maths 5 Learner Book\"), "
            "and \"author\" (the book's author OR publisher only if clearly identifiable, e.g. "
            "\"Cambridge University Press\"; use null if you are not reasonably sure — do NOT invent "
            "a person's name). Best guess for grade/subject if unsure.\n\n"
            f"Filename-derived title: {book.get('title') or 'Unknown'}\n\nChapters:\n{sample}"
        )
        data = client.analyze(prompt, max_tokens=300).get("data", {}) or {}
        grade = (str(data.get("grade") or "").strip() or None)
        subject = (str(data.get("subject") or "").strip() or None)
        detected_title = (str(data.get("title") or "").strip() or None)
        _a = data.get("author")
        _a_str = str(_a).strip() if _a is not None else ""
        detected_author = _a_str if _a_str and _a_str.lower() not in ("null", "none", "unknown", "n/a") else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("metadata detection failed for %s: %s", book_id, exc)

    # Only replace the title when the stored one looks like a filename — never clobber a title
    # the teacher deliberately typed. Fill author only when it is currently empty.
    current_title = (book.get("title") or "").strip()
    new_title = (detected_title or _clean_title_fallback(current_title)) if _looks_like_filename(current_title) else None
    new_author = detected_author if (not (book.get("author") or "").strip() and detected_author) else None

    db.set_book_meta(sb, book_id, grade, subject, title=new_title, author=new_author)
    if cover_dest:
        db.set_book_cover(sb, book_id, cover_dest)
    if health is not None:
        db.set_book_health(sb, book_id, health)
    if client is not None:
        db.set_job_usage(sb, job["id"], client.session_usage)
    db.set_book_chapters(sb, book_id, chapters, "ready")
    logger.info("Indexed book %s: %d chapter(s), grade=%s subject=%s cover=%s",
                book_id, len(chapters), grade, subject, bool(cover_dest))
