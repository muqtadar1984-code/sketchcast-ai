"""Assemble the playback package: unified timeline + self-contained Scribe player HTML.

The Scribe player uses CSS stroke-dashoffset animation with a follower hand
that traces SVG paths in real time, synced to the narrator audio.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent4_animation.svg_utils import SVGPathConverter
from agent6_player.models import PlayerPackage, PlayerStatus, UnifiedTimeline
from agent6_player.sync_engine import build_unified_timeline

logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
PLAYER_TEMPLATE_DIR = Path(__file__).resolve().parent / "player"

# SVG filter defs for hand-drawn feel (reused from Agent 4 canvas.py)
_SVG_DEFS = """<defs>
  <filter id="hand-drawn" x="-5%" y="-5%" width="110%" height="110%">
    <feTurbulence type="fractalNoise" baseFrequency="0.035" numOctaves="3" seed="2" result="noise"/>
    <feDisplacementMap in="SourceGraphic" in2="noise" scale="2.5" xChannelSelector="R" yChannelSelector="G"/>
  </filter>
  <filter id="hand-drawn-text" x="-5%" y="-5%" width="110%" height="110%">
    <feTurbulence type="fractalNoise" baseFrequency="0.025" numOctaves="2" seed="5" result="noise"/>
    <feDisplacementMap in="SourceGraphic" in2="noise" scale="1.2" xChannelSelector="R" yChannelSelector="G"/>
  </filter>
</defs>"""


def _read_template(filename: str) -> str:
    """Read a player template file."""
    path = PLAYER_TEMPLATE_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Player template not found: {path}")
    return path.read_text(encoding="utf-8")


def _encode_audio_base64(audio_path: str) -> str | None:
    """Read an MP3 file and return its base64 encoding."""
    p = Path(audio_path)
    if not p.exists():
        return None
    with open(p, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _esc(text: str) -> str:
    """Escape XML special characters."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_segment_svg(paths: list[dict]) -> str:
    """Build SVG content for one segment from its path data.

    For each path: generates ghost + ink dual layers using SVGPathConverter.
    For text paths: generates <text> elements with class="text-element".
    Wraps everything in an SVG root with viewBox 0 0 1280 720.
    """
    svg_parts = []

    for p in paths:
        if p.get("element_type") == "text":
            tp = p.get("text_params", {})
            anchor = {
                "left": "start", "center": "middle", "right": "end"
            }.get(tp.get("align", "center"), "middle")
            weight = "bold" if tp.get("bold") else "normal"
            svg_parts.append(
                f'<text id="text-{p["path_id"]}" class="text-element" '
                f'x="{tp.get("x", 0)}" y="{tp.get("y", 0)}" '
                f'font-size="{tp.get("font_size", 16)}" '
                f'fill="{tp.get("color", "#2C3E50")}" '
                f'font-weight="{weight}" text-anchor="{anchor}" '
                f'font-family="Arial, Helvetica, sans-serif" '
                f'dominant-baseline="middle" '
                f'filter="url(#hand-drawn-text)">{_esc(tp.get("text", ""))}</text>'
            )
        else:
            dual = SVGPathConverter.generate_dual_layer_svg(p)
            if dual:
                svg_parts.append(dual)

    inner = "\n  ".join(svg_parts)

    # Paper background
    paper_bg = (
        '<rect width="1280" height="720" fill="#FAFAFA"/>\n'
        '  <rect width="1280" height="720" fill="#A08060" opacity="0.04"/>'
    )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 1280 720" width="1280" height="720">\n'
        f'  {_SVG_DEFS}\n'
        f'  {paper_bg}\n'
        f'  {inner}\n'
        f'</svg>'
    )


def build_player_package(
    audio_manifest: dict,
    animation_manifest: dict,
    script_data: dict | None = None,
    embed_audio: bool = True,
) -> tuple[UnifiedTimeline, str]:
    """
    Build a complete Scribe player package.

    Args:
        audio_manifest: AudioManifest dict from Agent 5
        animation_manifest: AnimationManifest dict from Agent 4
        script_data: ChapterScripts or EpisodeScript dict from Agent 3
        embed_audio: If True, embed master MP3 as base64 in HTML

    Returns:
        (timeline, player_html_string)
    """
    # Validate Agent 5 completed
    if not audio_manifest.get("segments"):
        raise ValueError(
            "Agent 5 audio manifest has no segments. "
            "Generate audio before building the player."
        )
    if not audio_manifest.get("master_audio_path"):
        raise ValueError(
            "Agent 5 audio manifest has no master_audio_path. "
            "Audio generation must complete before building the player."
        )

    # Step 1: Build unified timeline
    timeline = build_unified_timeline(audio_manifest, animation_manifest, script_data)

    # Step 2: Build SCRIBE_DATA — SVG content + keyframes per segment
    scribe_data: dict[str, dict] = {}
    for seg in timeline.segments:
        if seg.has_animation and seg.paths:
            svg_content = _build_segment_svg(seg.paths)
            keyframes = seg.scribe_keyframes or []
            scribe_data[seg.segment_id] = {
                "svg_content": svg_content,
                "keyframes": keyframes,
            }

    # Step 3: Prepare audio for embedding
    audio_b64 = None
    if embed_audio:
        audio_b64 = _encode_audio_base64(audio_manifest["master_audio_path"])

    # Step 4: Build the player HTML
    player_html = _build_player_html(timeline, scribe_data, audio_b64)

    return timeline, player_html


def _build_player_html(
    timeline: UnifiedTimeline,
    scribe_data: dict[str, dict],
    audio_b64: str | None,
) -> str:
    """Build the self-contained Scribe player HTML."""
    try:
        css = _read_template("scribe_player.css")
        js = _read_template("scribe_player.js")
    except FileNotFoundError:
        css = _default_css()
        js = _default_js()

    timeline_json = json.dumps(timeline.model_dump(), ensure_ascii=False)
    scribe_data_json = json.dumps(scribe_data, ensure_ascii=False)

    audio_source = ""
    if audio_b64:
        audio_source = f'data:audio/mp3;base64,{audio_b64}'
    else:
        audio_source = timeline.master_audio_path

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SketchCast Scribe — {timeline.episode_title}</title>
<style>
{css}
</style>
</head>
<body>
<div id="sketchcast-player">
  <!-- Canvas area -->
  <div id="canvas-container">
    <div id="whiteboard">
      <!-- Drawing board: inline SVG replaces iframe -->
      <div id="drawing-board"></div>
      <!-- Marker hand: inline SVG pen icon -->
      <svg id="marker-hand" class="marker-hand" width="28" height="28" viewBox="0 0 28 28"
           xmlns="http://www.w3.org/2000/svg">
        <path d="M14 2 L14 22 M10 18 L14 22 L18 18" stroke="#E94560" stroke-width="2.5"
              fill="none" stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="14" cy="2" r="3" fill="#E94560"/>
      </svg>
      <div id="blank-canvas">
        <div id="blank-logo">SketchCast</div>
      </div>
    </div>
  </div>

  <!-- Segment title bar -->
  <div id="segment-bar">
    <span id="segment-icon"></span>
    <span id="segment-title">Ready to play</span>
    <span id="segment-counter"></span>
  </div>

  <!-- Progress bar -->
  <div id="progress-container">
    <div id="progress-bar"></div>
    <div id="progress-dots"></div>
  </div>

  <!-- Controls -->
  <div id="controls">
    <button id="play-btn" title="Play/Pause">&#9654;</button>
    <span id="time-display">0:00 / 0:00</span>
    <input type="range" id="volume-slider" min="0" max="1" step="0.05" value="0.8" title="Volume">
    <span id="volume-icon">&#128266;</span>
  </div>

  <!-- Question overlay (hidden by default) -->
  <div id="question-overlay" style="display:none;">
    <div id="question-card">
      <p id="question-prompt">The narrator is pausing for you to think...</p>
      <div id="question-input-area">
        <input type="text" id="question-input" placeholder="Ask a question or tap Continue">
        <button id="ask-btn">Ask</button>
        <button id="continue-btn">Continue</button>
      </div>
      <div id="answer-area" style="display:none;">
        <p id="answer-text"></p>
        <button id="resume-btn">Resume Episode</button>
      </div>
    </div>
  </div>
</div>

<script>
// Embedded data
const TIMELINE = {timeline_json};
const SCRIBE_DATA = {scribe_data_json};
const AUDIO_SOURCE = "{audio_source}";
</script>
<script>
{js}
</script>
</body>
</html>"""
    return html


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
    with open(timeline_path, "w", encoding="utf-8") as f:
        json.dump(timeline.model_dump(), f, indent=2, ensure_ascii=False)

    player_path = output_dir / "player.html"
    with open(player_path, "w", encoding="utf-8") as f:
        f.write(player_html)

    logger.info("Player package saved: %s", output_dir)
    return timeline_path, player_path


def _default_css() -> str:
    """Fallback CSS if template file missing."""
    return """/* Fallback styles */
#sketchcast-player { font-family: sans-serif; max-width: 1280px; margin: 0 auto; }
#canvas-container { width: 100%; aspect-ratio: 16/9; background: #FAFAFA; position: relative; }
#whiteboard { width: 100%; height: 100%; position: relative; }
#drawing-board { width: 100%; height: 100%; position: absolute; top: 0; left: 0; }
"""


def _default_js() -> str:
    """Fallback JS if template file missing."""
    return "console.log('Scribe Player loaded — template JS missing');"
