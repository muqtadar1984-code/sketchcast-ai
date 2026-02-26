"""FastAPI endpoints for Agent 6: Live Playback Engine (port 8004)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException

from agent6_player.models import PlayerStatus, UnifiedTimeline
from agent6_player.player_builder import build_player_package, save_player_package

logger = logging.getLogger(__name__)

app = FastAPI(title="SketchCast Agent 6 — Live Playback Engine", version="1.0.0")

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"

# In-memory status tracker
_status: dict[str, dict] = {}


@app.post("/player/{script_id}")
async def build_player(script_id: str):
    """Build playback package for an episode."""
    # Load required manifests
    audio_manifest = _find_audio_manifest(script_id)
    if not audio_manifest:
        raise HTTPException(
            status_code=400,
            detail="Agent 5 audio manifest not found. Generate audio first.",
        )

    animation_manifest = _find_animation_manifest(audio_manifest)
    if not animation_manifest:
        raise HTTPException(
            status_code=400,
            detail="Agent 4 animation manifest not found. Generate animations first.",
        )

    _status[script_id] = {"status": PlayerStatus.building}

    try:
        timeline, player_html = build_player_package(
            audio_manifest=audio_manifest,
            animation_manifest=animation_manifest,
        )
        book_id = audio_manifest.get("book_id", "unknown")
        chapter_num = audio_manifest.get("chapter_num", 0)
        timeline_path, player_path = save_player_package(
            timeline, player_html, book_id, chapter_num
        )
        _status[script_id] = {
            "status": PlayerStatus.ready,
            "timeline_path": str(timeline_path),
            "player_path": str(player_path),
        }
        return {"status": "ready", "timeline_path": str(timeline_path)}
    except Exception as e:
        _status[script_id] = {"status": PlayerStatus.failed, "error": str(e)}
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/player/{script_id}")
async def get_player(script_id: str):
    """Get the player HTML for embedding."""
    info = _status.get(script_id)
    if not info or info.get("status") != PlayerStatus.ready:
        raise HTTPException(status_code=404, detail="Player not built yet.")
    player_path = info.get("player_path")
    if not player_path or not Path(player_path).exists():
        raise HTTPException(status_code=404, detail="Player HTML file not found.")
    from fastapi.responses import HTMLResponse

    return HTMLResponse(content=Path(player_path).read_text(encoding="utf-8"))


@app.get("/player/{script_id}/timeline")
async def get_timeline(script_id: str):
    """Get the unified timeline JSON."""
    info = _status.get(script_id)
    if not info:
        raise HTTPException(status_code=404, detail="Player not built yet.")
    timeline_path = info.get("timeline_path")
    if not timeline_path or not Path(timeline_path).exists():
        raise HTTPException(status_code=404, detail="Timeline file not found.")
    with open(timeline_path, encoding="utf-8") as f:
        return json.load(f)


@app.get("/health")
async def health():
    return {"status": "ok", "agent": "6_player"}


# ── helpers ──────────────────────────────────────────────────


def _find_audio_manifest(script_id: str) -> dict | None:
    """Search storage for an audio manifest matching this script_id."""
    audio_dir = STORAGE_DIR / "audio"
    if not audio_dir.exists():
        return None
    for manifest_path in audio_dir.rglob("audio_manifest.json"):
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        if manifest.get("script_id") == script_id:
            return manifest
    return None


def _find_animation_manifest(audio_manifest: dict) -> dict | None:
    """Find the matching animation manifest."""
    book_id = audio_manifest.get("book_id", "")
    chapter_num = audio_manifest.get("chapter_num", 0)
    path = STORAGE_DIR / "animations" / book_id / f"chapter_{chapter_num}" / "manifest.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None
