"""Process one generation: book PDF -> lesson (slides + video + deck) -> Supabase.

Reuses the existing agents end-to-end. Files are processed in a temp dir and the
final deck (.pptx) + video (.mp4) are uploaded to the `artifacts` bucket under
{owner_id}/{generation_id}/.
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path

from supabase import Client

from . import client as db

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
            from agent1_ingestion.vision_chapters import chapter_text_vision

            ocr_text = chapter_text_vision(
                str(pdf_path), int(chapter.get("start_page", 0)),
                int(chapter.get("end_page", 0)), client,
            )
            if ocr_text:
                chapter["sections"] = [{
                    "section_title": "Content", "section_type": "body",
                    "content": ocr_text, "page_num": chapter.get("start_page", 0),
                    "subsections": [],
                }]
        db.set_progress(sb, job_id, 20)

        # Agent 2 — analysis (shared by every kind)
        analysis = run_full_analysis(
            book_id=book_id, chapter_content=chapter, level=DEFAULT_LEVEL, client=client,
        ).model_dump()
        db.set_progress(sb, job_id, 45)

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

            # (No AI-image step in the freemium path — slides are rendered natively.)
            slides = generate_episode_slides(
                script_data=scripts, branding=branding,
            ).model_dump()
            db.set_progress(sb, job_id, 72)

            video = compose_episode_videos(
                script_data=scripts, slide_manifest=slides, branding=branding,
                tts_voice=tts_voice, allow_premium=allow_premium,
            ).model_dump()
            db.set_progress(sb, job_id, 90)

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

            out_path = generate_document(
                kind=kind, book=book, chapter=chapter, analysis=analysis,
                client=client, params=gen.get("params") or {}, out_dir=Path(tmp),
                template=branding.get("docx_template"),
            )
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

    db.finish_job(sb, job_id, generation_id)
    logger.info("Generation %s (%s) done", generation_id, kind)


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

    # Auto-detect grade + subject from the title + chapter list (best-effort;
    # never block indexing). This is identified for the teacher, not entered.
    grade = subject = None
    try:
        if client is None:
            raise RuntimeError("Claude unavailable")
        sample = "\n".join(c["title"] for c in chapters[:25])
        prompt = (
            "From this textbook's title and chapter list, identify the school GRADE/level "
            "and the SUBJECT. Respond ONLY as JSON: {\"grade\": \"...\", \"subject\": \"...\"}. "
            "Use a short canonical grade label (e.g. \"Grade 5\") and a canonical subject "
            "(e.g. \"Mathematics\", \"Science\", \"History\"). Best guess if unsure.\n\n"
            f"Title: {book.get('title') or 'Unknown'}\n\nChapters:\n{sample}"
        )
        data = client.analyze(prompt, max_tokens=200).get("data", {}) or {}
        grade = (str(data.get("grade") or "").strip() or None)
        subject = (str(data.get("subject") or "").strip() or None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("grade/subject detection failed for %s: %s", book_id, exc)

    db.set_book_meta(sb, book_id, grade, subject)
    if cover_dest:
        db.set_book_cover(sb, book_id, cover_dest)
    db.set_book_chapters(sb, book_id, chapters, "ready")
    logger.info("Indexed book %s: %d chapter(s), grade=%s subject=%s cover=%s",
                book_id, len(chapters), grade, subject, bool(cover_dest))
