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
    # Lazy imports so the worker module stays light and matches the app's flow.
    from agent1_ingestion.extractor import extract_pdf
    from agent1_ingestion.image_extractor import extract_images
    from agent1_ingestion.structurer import structure_book
    from agent2_analysis.analyzer import run_full_analysis
    from agent3_scripts.script_generator import generate_chapter_scripts_from_analysis
    from agent4_image_gen.image_generator import generate_episode_images
    from agent5_slides.slide_generator import generate_episode_slides
    from agent6_animation.video_composer import compose_episode_videos
    from agent8_render.renderer import render_final_video
    from shared.claude_client import ClaudeClient

    job_id = job["id"]
    gen = db.get_generation(sb, generation_id)
    book = db.get_book(sb, gen["book_id"])
    owner_id = gen["owner_id"]
    book_id = book["id"]

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = db.download_book(sb, book["storage_path"], Path(tmp) / "book.pdf")
        db.set_progress(sb, job_id, 10)

        # Agent 1 — ingest
        extraction = extract_pdf(str(pdf_path))
        images = extract_images(str(pdf_path), book_id)
        structured = structure_book(
            book_id=book_id, title=book.get("title") or "Untitled",
            author=book.get("author") or "Unknown", isbn=None, extraction=extraction, images=images,
        ).model_dump()
        chapter = _pick_chapter(structured.get("chapters", []), gen.get("chapter_ref"))
        chapter_num = int(chapter.get("chapter_num", 0))
        db.set_progress(sb, job_id, 20)

        client = ClaudeClient()

        # Agent 2 — analysis
        analysis = run_full_analysis(
            book_id=book_id, chapter_content=chapter, level=DEFAULT_LEVEL, client=client,
        ).model_dump()
        db.set_progress(sb, job_id, 45)

        # Agent 3 — Socratic script (+ on-slide chapter points)
        scripts = generate_chapter_scripts_from_analysis(
            book_id=book_id, chapter_num=chapter_num, analysis_dict=analysis, client=client,
        ).model_dump()
        ep_title = (scripts.get("episodes") or [{}])[0].get("episode_title") or f"Chapter {chapter_num}"
        db.set_progress(sb, job_id, 60)

        # Agents 4/5 — (no-op images) + content slides + editable deck
        images_manifest = generate_episode_images(script_data=scripts).model_dump()
        slides = generate_episode_slides(script_data=scripts, image_manifest=images_manifest).model_dump()
        db.set_progress(sb, job_id, 72)

        # Agent 6 — narration + per-segment video
        video = compose_episode_videos(script_data=scripts, slide_manifest=slides).model_dump()
        db.set_progress(sb, job_id, 90)

        # Agent 8 — concatenate into final MP4
        final = render_final_video(video_manifest=video).model_dump()
        db.set_progress(sb, job_id, 96)

        # Upload artifacts
        base = f"{owner_id}/{generation_id}"
        deck_path = slides.get("deck_path")
        if deck_path and Path(deck_path).exists():
            db.upload_artifact(sb, deck_path, f"{base}/deck.pptx")
            db.add_artifact_row(sb, generation_id, "deck_pptx", f"{base}/deck.pptx")
        final_video = final.get("final_video_path")
        if final_video and Path(final_video).exists():
            db.upload_artifact(sb, final_video, f"{base}/lesson.mp4")
            db.add_artifact_row(sb, generation_id, "video_mp4", f"{base}/lesson.mp4")

        db.set_generation_title(sb, generation_id, f"{book.get('title','Lesson')} · {ep_title}")

    db.finish_job(sb, job_id, generation_id)
    logger.info("Generation %s done", generation_id)


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

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = db.download_book(sb, book["storage_path"], Path(tmp) / "book.pdf")
        extraction = extract_pdf(str(pdf_path))
        structured = structure_book(
            book_id=book_id, title=book.get("title") or "Untitled",
            author=book.get("author") or "Unknown", isbn=None,
            extraction=extraction, images=[],
        ).model_dump()

    chapters = [
        {"num": int(c["chapter_num"]),
         "title": (c.get("title") or f"Chapter {c['chapter_num']}").strip()}
        for c in structured.get("chapters", [])
    ]
    db.set_book_chapters(sb, book_id, chapters, "ready")
    logger.info("Indexed book %s: %d chapter(s)", book_id, len(chapters))
