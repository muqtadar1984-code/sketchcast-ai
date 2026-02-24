"""
Agent 3: Script Generation — FastAPI standalone server (port 8002).
"""

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="SketchCast Agent 3 — Script Generation", version="1.0.0")

STORAGE_DIR = Path(__file__).parent.parent / "storage" / "scripts"


class GenerateRequest(BaseModel):
    level: str = "middle_school"


@app.get("/health")
def health():
    """Health check."""
    return {"status": "ok", "agent": "3", "name": "Script Generation"}


@app.post("/scripts/{book_id}/chapter/{chapter_num}")
def generate_scripts(book_id: str, chapter_num: int, req: GenerateRequest = GenerateRequest()):
    """Trigger script generation for all episodes in a chapter.

    Reads Agent 2 analysis from disk, generates Socratic scripts, saves and returns them.
    """
    try:
        from agent3_scripts.script_generator import generate_chapter_scripts
        from shared.claude_client import ClaudeClient

        client = ClaudeClient()
        chapter_scripts = generate_chapter_scripts(book_id, chapter_num, client)

        if chapter_scripts is None:
            raise HTTPException(
                status_code=404,
                detail=f"No analysis found for book {book_id} chapter {chapter_num}. Run Agent 2 first.",
            )

        return chapter_scripts.model_dump()

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/scripts/{book_id}/chapter/{chapter_num}/episode/{episode_num}")
def get_script(book_id: str, chapter_num: int, episode_num: int):
    """Retrieve a specific episode script."""
    from agent3_scripts.script_generator import load_script

    script = load_script(book_id, chapter_num, episode_num)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found. Generate it first.")
    return script


@app.get("/scripts/{book_id}/status")
def list_scripts(book_id: str):
    """List all generated scripts for a book."""
    script_dir = STORAGE_DIR / book_id
    if not script_dir.exists():
        return {"book_id": book_id, "scripts": []}

    scripts = []
    for path in sorted(script_dir.glob("chapter_*_episode_*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            scripts.append({
                "chapter_num": data.get("chapter_num"),
                "episode_num": data.get("episode_num"),
                "episode_title": data.get("episode_title"),
                "generated_at": data.get("generated_at"),
                "segments": len(data.get("segments", [])),
                "duration_seconds": data.get("total_estimated_duration_seconds"),
            })
        except Exception:
            pass

    return {"book_id": book_id, "total": len(scripts), "scripts": scripts}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
