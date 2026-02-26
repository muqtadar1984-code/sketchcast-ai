"""FastAPI endpoints for Agent 8: Q&A Interjection Handler (port 8006)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

from agent8_qa import question_bank
from agent8_qa.answer_generator import generate_answer_stream_async
from agent8_qa.models import QuestionRequest
from agent8_qa.streaming_tts import stream_answer_audio

logger = logging.getLogger(__name__)

app = FastAPI(title="SketchCast Agent 8 — Q&A Interjection Handler", version="1.0.0")

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"


@app.post("/ask/{script_id}/{segment_id}")
async def ask_question(
    script_id: str,
    segment_id: str,
    q: str = Query(..., description="The student's question"),
    book_id: str = Query("", description="Book ID for context"),
    chapter_num: int = Query(0, description="Chapter number for context"),
    background_tasks: BackgroundTasks = BackgroundTasks(),
):
    """
    Submit a question and receive an SSE stream of text + audio.

    Checks question bank first (zero LLM cost), then falls back to Claude streaming.
    """
    # 1. Check question bank first
    cached = question_bank.find_similar(q, book_id, chapter_num)
    if cached:
        async def stream_cached():
            yield f"event: text\ndata: {cached.answer_text}\n\n"
            yield "event: complete\ndata: done\n\n"

        return StreamingResponse(
            stream_cached(),
            media_type="text/event-stream",
        )

    # 2. Generate new answer with streaming
    segment_context = _get_segment_context(script_id, segment_id)
    analysis = _load_analysis(book_id, chapter_num)

    async def stream_response():
        full_answer = []
        text_stream = generate_answer_stream_async(
            question=q,
            segment_context=segment_context,
            chapter_analysis=analysis,
        )
        async for chunk in text_stream:
            full_answer.append(chunk)
            # Escape newlines for SSE
            safe_chunk = chunk.replace("\n", " ")
            yield f"event: text\ndata: {safe_chunk}\n\n"

        yield "event: complete\ndata: done\n\n"

        # Bank the answer in background
        answer_text = "".join(full_answer)
        if answer_text.strip():
            question_bank.save_qa(
                question=q,
                answer=answer_text,
                book_id=book_id,
                chapter_num=chapter_num,
                segment_id=segment_id,
            )

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
    )


@app.get("/bank/{book_id}/{chapter_num}")
async def get_bank(book_id: str, chapter_num: int):
    """Get all banked Q&A pairs for a chapter."""
    pairs = question_bank.get_all(book_id, chapter_num)
    return [p.model_dump() for p in pairs]


@app.get("/bank/{book_id}/stats")
async def get_bank_stats(book_id: str):
    """Get question bank coverage stats."""
    stats = question_bank.get_stats(book_id)
    return stats.model_dump()


@app.get("/health")
async def health():
    return {"status": "ok", "agent": "8_qa"}


# ── helpers ──────────────────────────────────────────────


def _get_segment_context(script_id: str, segment_id: str) -> dict:
    """Load segment context from stored scripts."""
    scripts_dir = STORAGE_DIR / "scripts"
    if not scripts_dir.exists():
        return {"text": "", "type": "explore"}

    for script_path in scripts_dir.rglob("*.json"):
        try:
            with open(script_path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("script_id") == script_id:
                for seg in data.get("segments", []):
                    if seg.get("segment_id") == segment_id:
                        return {"text": seg.get("text", ""), "type": seg.get("type", "explore")}
        except (json.JSONDecodeError, OSError):
            continue
    return {"text": "", "type": "explore"}


def _load_analysis(book_id: str, chapter_num: int) -> dict:
    """Load chapter analysis for context."""
    path = STORAGE_DIR / "analysis" / book_id / f"chapter_{chapter_num}.json"
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"chapter_title": "Unknown"}
