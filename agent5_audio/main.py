"""
Agent 5: Audio Generation — FastAPI standalone server (port 8003).
"""

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException

app = FastAPI(title="SketchCast Agent 5 — Audio Generation", version="1.0.0")

STORAGE_DIR = Path(__file__).parent.parent / "storage"
SCRIPTS_DIR = STORAGE_DIR / "scripts"
AUDIO_DIR = STORAGE_DIR / "audio"


def _load_script(book_id: str, chapter_num: int, episode_num: int = 1) -> dict | None:
    path = SCRIPTS_DIR / book_id / f"chapter_{chapter_num}_episode_{episode_num}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _manifest_path(book_id: str, chapter_num: int) -> Path:
    return AUDIO_DIR / book_id / f"chapter_{chapter_num}" / "audio_manifest.json"


@app.get("/health")
def health():
    return {"status": "ok", "agent": "5", "name": "Audio Generation"}


@app.post("/audio/{script_id}")
def generate_audio(script_id: str, book_id: str = "", chapter_num: int = 0):
    """Trigger audio generation for a full episode."""
    try:
        from agent5_audio.audio_generator import generate_episode_audio

        manifest = generate_episode_audio(
            script_id=script_id,
            book_id=book_id,
            chapter_num=chapter_num,
        )
        return manifest.model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/audio/{script_id}/status")
def audio_status(script_id: str, book_id: str = "", chapter_num: int = 0):
    """Check generation status."""
    manifest_file = _manifest_path(book_id, chapter_num)
    if manifest_file.exists():
        with open(manifest_file, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("script_id") == script_id:
            return {"status": "completed", "manifest": data}
    return {"status": "not_found"}


@app.get("/audio/{script_id}/manifest")
def get_manifest(script_id: str, book_id: str = "", chapter_num: int = 0):
    """Get audio manifest with actual timings."""
    manifest_file = _manifest_path(book_id, chapter_num)
    if not manifest_file.exists():
        raise HTTPException(status_code=404, detail="Audio manifest not found.")
    with open(manifest_file, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("script_id") != script_id:
        raise HTTPException(status_code=404, detail="Script ID mismatch.")
    return data


@app.get("/audio/{script_id}/segment/{segment_id}")
def get_segment(script_id: str, segment_id: str, book_id: str = "", chapter_num: int = 0):
    """Get path to a segment MP3."""
    seg_dir = AUDIO_DIR / book_id / f"chapter_{chapter_num}"
    mp3 = seg_dir / f"{segment_id}.mp3"
    if not mp3.exists():
        raise HTTPException(status_code=404, detail="Segment MP3 not found.")
    return {"segment_id": segment_id, "audio_path": str(mp3)}


@app.get("/audio/{script_id}/master")
def get_master(script_id: str, book_id: str = "", chapter_num: int = 0):
    """Get path to master MP3."""
    master = AUDIO_DIR / book_id / f"chapter_{chapter_num}" / "master.mp3"
    if not master.exists():
        raise HTTPException(status_code=404, detail="Master MP3 not found.")
    return {"audio_path": str(master)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)
