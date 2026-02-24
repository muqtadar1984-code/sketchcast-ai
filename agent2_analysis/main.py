"""FastAPI endpoints for Agent 2: Content Analysis (port 8001)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query

from agent2_analysis.analyzer import (ANALYSIS_DIR, load_analysis,
                                      run_full_analysis)

app = FastAPI(
    title="SketchCast AI — Agent 2: Content Analysis",
    version="0.1.0",
)


@app.get("/health")
def health():
    return {"status": "healthy", "agent": "content_analysis", "version": "0.1.0"}


@app.post("/analyze/{book_id}/chapter/{chapter_num}")
def analyze_chapter(
    book_id: str,
    chapter_num: int,
    level: str = Query("middle_school", regex="^(primary_school|middle_school|high_school)$"),
):
    """Trigger analysis on a single chapter. Reads Agent 1 output from storage."""
    # Load structured content from Agent 1
    processed_path = Path(__file__).resolve().parent.parent / "storage" / "processed" / f"{book_id}.json"
    if not processed_path.exists():
        raise HTTPException(404, f"Book {book_id} not found in processed storage.")

    with open(processed_path, "r", encoding="utf-8") as f:
        book_data = json.load(f)

    chapter = None
    for ch in book_data.get("chapters", []):
        if ch["chapter_num"] == chapter_num:
            chapter = ch
            break

    if chapter is None:
        raise HTTPException(404, f"Chapter {chapter_num} not found in book {book_id}.")

    try:
        result = run_full_analysis(book_id, chapter, level=level)
        return result.model_dump()
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/analyze/{analysis_id}")
def get_analysis(analysis_id: str):
    """Get analysis results by analysis ID (searches all files)."""
    for json_file in ANALYSIS_DIR.rglob("chapter_*.json"):
        with open(json_file, "r") as f:
            data = json.load(f)
        if data.get("analysis_id") == analysis_id:
            return data
    raise HTTPException(404, "Analysis not found.")


@app.get("/analyze/{book_id}/status")
def book_analysis_status(book_id: str):
    """Check analysis status for all chapters of a book."""
    book_dir = ANALYSIS_DIR / book_id
    if not book_dir.exists():
        return {"book_id": book_id, "chapters_analysed": 0, "chapters": []}

    chapters = []
    for json_file in sorted(book_dir.glob("chapter_*.json")):
        with open(json_file, "r") as f:
            data = json.load(f)
        chapters.append({
            "chapter_num": data.get("chapter_num"),
            "chapter_title": data.get("chapter_title"),
            "status": "completed",
            "analysis_id": data.get("analysis_id"),
        })

    return {"book_id": book_id, "chapters_analysed": len(chapters), "chapters": chapters}
