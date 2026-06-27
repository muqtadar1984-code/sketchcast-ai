"""Agent 4: Image Generator.

Freemium pipeline uses text-based slides (Agent 5), so AI image generation is
skipped — this returns an empty manifest quickly without calling Gemini. The
Gemini path (gemini_client) is kept in the module for a future paid tier.

Entry point
-----------
generate_episode_images()   Streamlit in-process entry point.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .models import ImageManifest, ImageSegment

logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
IMAGES_DIR = STORAGE_DIR / "images"


def generate_episode_images(
    script_data: dict,
    progress_callback: Optional[Callable] = None,
) -> ImageManifest:
    """Return an empty image manifest (freemium slides are text-based)."""
    episodes = script_data.get("episodes", [script_data])
    episode = episodes[0] if isinstance(episodes, list) and episodes else script_data
    segments = episode.get("segments", [])

    book_id = episode.get("book_id", script_data.get("book_id", "unknown"))
    chapter_num = episode.get("chapter_num", script_data.get("chapter_num", 0))
    episode_num = episode.get("episode_num", script_data.get("episode_num", 1))
    script_id = episode.get("script_id", script_data.get("script_id", str(uuid.uuid4())))

    total = len(segments)
    manifest_segments = [
        ImageSegment(
            segment_id=seg.get("segment_id", f"s{i + 1:03d}"),
            type=seg.get("type", "explore"),
            has_image=False,
            image_path=None,
            prompt_used="",
            source="skipped",
            visual_action=seg.get("visual_action", "GHOST_ONLY"),
        )
        for i, seg in enumerate(segments)
    ]

    if progress_callback:
        progress_callback(total, total, "done")

    manifest = ImageManifest(
        manifest_id=str(uuid.uuid4()),
        script_id=script_id,
        book_id=book_id,
        chapter_num=chapter_num,
        episode_num=episode_num,
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_segments=total,
        image_segments=0,
        segments=manifest_segments,
    )
    logger.info("Image generation skipped (text-based slides): %d segments", total)
    return manifest


def load_manifest(book_id: str, chapter_num: int) -> Optional[dict]:
    """Load a saved image manifest from disk."""
    path = IMAGES_DIR / book_id / f"chapter_{chapter_num}" / "manifest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
