"""Core Q&A logic — question bank lookup first, then Claude + TTS."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from agent8_qa import question_bank
from agent8_qa.answer_generator import generate_answer_stream
from agent8_qa.models import BankedQA, QuestionLogEntry, QuestionRequest
from agent8_qa.streaming_tts import synthesize_answer

logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"


def handle_question(
    request: QuestionRequest,
    script_data: dict | None = None,
    analysis_data: dict | None = None,
) -> dict:
    """
    Handle a student question: check bank first, then generate with Claude.

    Returns {answer_text, served_from_cache, latency_seconds, audio_bytes?}
    """
    start = time.time()

    # Step 1: Check question bank — zero LLM cost if cached
    cached = question_bank.find_similar(
        question=request.text,
        book_id=request.book_id,
        chapter_num=request.chapter_num,
    )

    if cached:
        latency = time.time() - start
        logger.info(
            "Serving cached answer (%.2fs): '%s'",
            latency, request.text[:50],
        )
        return {
            "answer_text": cached.answer_text,
            "served_from_cache": True,
            "latency_seconds": round(latency, 3),
            "tokens_used": 0,
        }

    # Step 2: Build context for Claude
    segment_context = _get_segment_context(
        request.segment_id, script_data
    )
    chapter_analysis = analysis_data or _load_chapter_analysis(
        request.book_id, request.chapter_num
    )

    # Step 3: Generate answer with Claude
    answer_text, tokens_used = generate_answer_stream(
        question=request.text,
        segment_context=segment_context,
        chapter_analysis=chapter_analysis,
        narrator_persona=request.narrator_persona,
    )

    # Step 4: Bank the new answer
    question_bank.save_qa(
        question=request.text,
        answer=answer_text,
        book_id=request.book_id,
        chapter_num=request.chapter_num,
        segment_id=request.segment_id,
        tokens_used=tokens_used,
    )

    latency = time.time() - start
    logger.info(
        "Generated new answer (%.2fs, %d tokens): '%s'",
        latency, tokens_used, request.text[:50],
    )

    return {
        "answer_text": answer_text,
        "served_from_cache": False,
        "latency_seconds": round(latency, 3),
        "tokens_used": tokens_used,
    }


def _get_segment_context(segment_id: str, script_data: dict | None) -> dict:
    """Extract context for the current segment from script data."""
    if not script_data:
        return {"text": "", "type": "explore"}

    episodes = script_data.get("episodes", [script_data])
    ep = episodes[0] if isinstance(episodes, list) and episodes else script_data

    for seg in ep.get("segments", []):
        if seg.get("segment_id") == segment_id:
            return {
                "text": seg.get("text", ""),
                "type": seg.get("type", "explore"),
                "segment_id": segment_id,
            }

    return {"text": "", "type": "explore"}


def _load_chapter_analysis(book_id: str, chapter_num: int) -> dict:
    """Load chapter analysis from storage."""
    path = STORAGE_DIR / "analysis" / book_id / f"chapter_{chapter_num}.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"chapter_title": "Unknown", "concepts": {}}
