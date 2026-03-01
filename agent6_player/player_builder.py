"""Assemble the Scribe playback package."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

from agent6_player.models import UnifiedTimeline
from agent6_player.sync_engine import build_unified_timeline

logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
PLAYER_TEMPLATE_DIR = Path(__file__).resolve().parent / "player"


def _read_template(filename: str) -> str:
    """Read a player template file."""
    path = PLAYER_TEMPLATE_DIR / filename
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _encode_audio_base64(audio_path: str) -> str | None:
    """Read an MP3 file and return its base64 encoding."""
    p = Path(audio_path)
    if not p.exists():
        return None
    with open(p, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def build_player_package(
    audio_manifest: dict,
    animation_manifest: dict,
    script_data: dict | None = None,
    embed_audio: bool = True,
) -> tuple[UnifiedTimeline, str]:
    """Build a complete Scribe player package.

    Returns (timeline, player_html_string).
    """
    timeline = build_unified_timeline(audio_manifest, animation_manifest, script_data)

    audio_b64 = None
    if embed_audio and timeline.master_audio_path:
        audio_b64 = _encode_audio_base64(timeline.master_audio_path)

    try:
        css = _read_template("player.css")
        js = _read_template("player.js")
    except Exception:
        css, js = "", "console.error('Missing templates')"

    timeline_json = json.dumps(timeline.model_dump(), ensure_ascii=False)
    audio_src = (
        f"data:audio/mp3;base64,{audio_b64}"
        if audio_b64
        else timeline.master_audio_path
    )

    # NEW HTML STRUCTURE — Scribe Speed Paint player
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SketchCast Scribe Player</title>
<style>{css}</style>
</head>
<body>
<div id="sketchcast-player">
  <div id="canvas-container">
    <svg id="drawing-board" viewBox="0 0 1280 720" preserveAspectRatio="xMidYMid meet"></svg>
    <img id="marker-hand" src="https://raw.githubusercontent.com/muqtadar1984-code/sketchcast-ai/main/assets/marker_hand.png" alt="Hand">

    <div id="segment-bar"><span id="segment-title">Ready</span></div>
    <div id="question-overlay" style="display:none">
       <div id="question-card">
         <p>Pause for reflection...</p>
         <button id="resume-btn">Resume</button>
       </div>
    </div>
  </div>

  <div id="controls">
    <button id="play-btn">&#9654;</button>
    <div id="progress-container"><div id="progress-bar"></div></div>
    <span id="time-display">0:00</span>
  </div>
</div>

<script>
const TIMELINE = {timeline_json};
const AUDIO_SOURCE = "{audio_src}";
</script>
<script>{js}</script>
</body>
</html>"""

    return timeline, html


def save_player_package(
    timeline: UnifiedTimeline,
    player_html: str,
    book_id: str,
    chapter_num: int,
) -> tuple[Path, Path]:
    """Save timeline JSON and player HTML to storage."""
    output_dir = STORAGE_DIR / "player" / book_id / f"chapter_{chapter_num}"
    output_dir.mkdir(parents=True, exist_ok=True)

    timeline_path = output_dir / "timeline.json"
    timeline_path.write_text(
        json.dumps(timeline.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    player_path = output_dir / "player.html"
    player_path.write_text(player_html, encoding="utf-8")

    logger.info("Player package saved: %s", output_dir)
    return timeline_path, player_path
