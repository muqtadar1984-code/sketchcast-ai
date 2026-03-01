"""Agent 4: Sketch Animation — FastAPI standalone server (port 8003)."""

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="SketchCast Agent 4 — Sketch Animation", version="1.0.0")

ANIMATIONS_DIR = Path(__file__).parent.parent / "storage" / "animations"


class AnimateRequest(BaseModel):
    episode_num: int = 1


@app.get("/health")
def health():
    """Health check."""
    return {"status": "ok", "agent": "4", "name": "Sketch Animation"}


SCRIPTS_DIR = Path(__file__).parent.parent / "storage" / "scripts"
ANALYSIS_DIR = Path(__file__).parent.parent / "storage" / "analysis"


@app.post("/animate/{book_id}/chapter/{chapter_num}")
def animate_episode(book_id: str, chapter_num: int, req: AnimateRequest = AnimateRequest()):
    """Generate SVG animations for a script episode.

    Reads Agent 3 script + Agent 2 analysis from disk,
    generates vectorized SVGs via Nano Banana Pro + Vectorizer,
    saves and returns the manifest.
    """
    try:
        from agent4_animation.sketch_generator import (
            generate_episode_animations_from_script,
        )

        # Load script from disk
        script_path = SCRIPTS_DIR / book_id / f"chapter_{chapter_num}" / "script.json"
        if not script_path.exists():
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Script not found for book {book_id} "
                    f"chapter {chapter_num}. Run Agent 3 first."
                ),
            )
        with open(script_path, encoding="utf-8") as f:
            script_data = json.load(f)

        # Load analysis from disk (kept for signature compatibility)
        analysis_dict: dict = {}
        analysis_path = ANALYSIS_DIR / book_id / f"chapter_{chapter_num}" / "analysis.json"
        if analysis_path.exists():
            with open(analysis_path, encoding="utf-8") as f:
                analysis_dict = json.load(f)

        manifest = generate_episode_animations_from_script(
            script_data=script_data,
            analysis_dict=analysis_dict,
            client=None,  # Claude no longer used in Agent 4
        )

        return manifest.model_dump()

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/animate/{book_id}/chapter/{chapter_num}/status")
def animation_status(book_id: str, chapter_num: int):
    """Check if animations have been generated for a chapter."""
    manifest_path = ANIMATIONS_DIR / book_id / f"chapter_{chapter_num}" / "manifest.json"
    if not manifest_path.exists():
        return {"status": "not_generated", "book_id": book_id, "chapter_num": chapter_num}

    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    return {
        "status": "completed",
        "book_id": book_id,
        "chapter_num": chapter_num,
        "total_segments": manifest.get("total_segments", 0),
        "animated_segments": manifest.get("animated_segments", 0),
        "generated_at": manifest.get("generated_at", ""),
    }


@app.get("/animate/{book_id}/chapter/{chapter_num}/manifest")
def get_manifest(book_id: str, chapter_num: int):
    """Get the full animation manifest for a chapter."""
    manifest_path = ANIMATIONS_DIR / book_id / f"chapter_{chapter_num}" / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Manifest not found. Generate animations first.")

    with open(manifest_path, encoding="utf-8") as f:
        return json.load(f)


@app.get("/animate/{book_id}/chapter/{chapter_num}/segment/{segment_id}")
def get_segment_animation(book_id: str, chapter_num: int, segment_id: str):
    """Get animation data for a specific segment."""
    anim_dir = ANIMATIONS_DIR / book_id / f"chapter_{chapter_num}"

    svg_path = anim_dir / f"{segment_id}_sketch.svg"
    anim_path = anim_dir / f"{segment_id}_animation.json"
    blank_path = anim_dir / f"{segment_id}_blank.svg"

    if svg_path.exists():
        with open(svg_path, encoding="utf-8") as f:
            svg = f.read()
        animation = None
        if anim_path.exists():
            with open(anim_path, encoding="utf-8") as f:
                animation = json.load(f)
        return {"segment_id": segment_id, "has_animation": True, "svg": svg, "animation": animation}

    if blank_path.exists():
        with open(blank_path, encoding="utf-8") as f:
            svg = f.read()
        return {"segment_id": segment_id, "has_animation": False, "svg": svg, "animation": None}

    raise HTTPException(status_code=404, detail=f"Segment {segment_id} not found.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)
