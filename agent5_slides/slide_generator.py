"""Agent 5: Slide Generator — one content slide per script segment.

Freemium pipeline: slides are text-based (episode title + the segment's
Socratic narration), rendered with bundled fonts. No AI images — the
``image_manifest`` argument is accepted for backward compatibility but ignored.

Entry point
-----------
generate_episode_slides()   Streamlit in-process entry point.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .models import SlideManifest, SlideSegment
from .slide_builder import export_slide_png

logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
SLIDES_DIR = STORAGE_DIR / "slides"


def generate_episode_slides(
    script_data: dict,
    image_manifest: Optional[dict] = None,  # ignored (no AI images in freemium)
    progress_callback: Optional[Callable] = None,
) -> SlideManifest:
    """Render one lesson slide PNG per segment from the script."""
    episodes = script_data.get("episodes", [script_data])
    episode = episodes[0] if isinstance(episodes, list) and episodes else script_data

    book_id = episode.get("book_id", script_data.get("book_id", "unknown"))
    chapter_num = episode.get("chapter_num", script_data.get("chapter_num", 0))
    episode_num = episode.get("episode_num", script_data.get("episode_num", 1))
    script_id = episode.get("script_id", script_data.get("script_id", str(uuid.uuid4())))
    episode_title = episode.get("episode_title") or "SketchCast AI"

    segments = episode.get("segments", [])
    slide_dir = SLIDES_DIR / book_id / f"chapter_{chapter_num}"
    slide_dir.mkdir(parents=True, exist_ok=True)

    manifest_segments: list[SlideSegment] = []
    total = len(segments)

    for i, seg in enumerate(segments):
        seg_id = seg.get("segment_id", f"s{i + 1:03d}")
        seg_type = seg.get("type", "explore")
        text = (seg.get("text") or "").strip()

        if progress_callback:
            progress_callback(i, total, seg_id)

        png_path = slide_dir / f"{seg_id}_slide.png"
        footer = f"{seg_type} · {i + 1}/{total}"
        export_slide_png(episode_title, text, png_path, footer_text=footer)

        manifest_segments.append(SlideSegment(
            segment_id=seg_id,
            type=seg_type,
            has_slide=True,
            slide_path=None,
            slide_image_path=str(png_path),
            visual_action=seg.get("visual_action", "GHOST_ONLY"),
        ))

    if progress_callback:
        progress_callback(total, total, "done")

    manifest = SlideManifest(
        manifest_id=str(uuid.uuid4()),
        script_id=script_id,
        book_id=book_id,
        chapter_num=chapter_num,
        episode_num=episode_num,
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_segments=total,
        slide_segments=len(manifest_segments),
        segments=manifest_segments,
    )

    manifest_path = slide_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    logger.info("Slide manifest built: %d content slides", total)
    return manifest


def load_manifest(book_id: str, chapter_num: int) -> Optional[dict]:
    """Load a saved slide manifest from disk."""
    path = SLIDES_DIR / book_id / f"chapter_{chapter_num}" / "manifest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
