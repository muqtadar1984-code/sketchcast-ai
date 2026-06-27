"""Agent 4: Image Generator — orchestrates image creation for each segment.

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

from .gemini_client import generate_image
from .models import ImageManifest, ImageSegment
from .pillow_fallback import generate_placeholder

logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
IMAGES_DIR = STORAGE_DIR / "images"


def generate_episode_images(
    script_data: dict,
    progress_callback: Optional[Callable] = None,
) -> ImageManifest:
    """Generate images for all segments in one episode.

    Parameters
    ----------
    script_data : dict
        Full script dict with ``episodes[0].segments``.
    progress_callback : callable, optional
        ``fn(current_index, total, segment_id)``

    Returns
    -------
    ImageManifest
    """
    episodes = script_data.get("episodes", [script_data])
    episode = episodes[0] if isinstance(episodes, list) and episodes else script_data
    segments = episode.get("segments", [])

    book_id = episode.get("book_id", script_data.get("book_id", "unknown"))
    chapter_num = episode.get("chapter_num", script_data.get("chapter_num", 0))
    episode_num = episode.get("episode_num", script_data.get("episode_num", 1))
    script_id = episode.get("script_id", script_data.get("script_id", str(uuid.uuid4())))

    img_dir = IMAGES_DIR / book_id / f"chapter_{chapter_num}"
    img_dir.mkdir(parents=True, exist_ok=True)

    manifest_segments: list[ImageSegment] = []
    last_draw_start_path: str | None = None
    total = len(segments)

    for i, seg in enumerate(segments):
        seg_id = seg.get("segment_id", f"s{i + 1:03d}")
        visual_action = seg.get("visual_action", "GHOST_ONLY")
        visual_request = seg.get("visual_request")

        if progress_callback:
            progress_callback(i, total, seg_id)

        has_image = False
        image_path_str: str | None = None
        prompt_used = ""
        source = "none"

        if visual_action == "DRAW_START" and visual_request:
            prompt = visual_request.get("prompt", "")
            negative_prompt = visual_request.get("negative_prompt", "")
            prompt_used = prompt

            out_path = img_dir / f"{seg_id}_image.png"

            # Try Gemini first
            success = generate_image(prompt, out_path, negative_prompt)
            if success and out_path.exists():
                source = "gemini"
                has_image = True
                image_path_str = str(out_path)
            else:
                # Fallback to Pillow placeholder
                success = generate_placeholder(prompt, out_path)
                if success and out_path.exists():
                    source = "pillow_fallback"
                    has_image = True
                    image_path_str = str(out_path)

            if has_image:
                last_draw_start_path = image_path_str

        elif visual_action == "DRAW_CONTINUE":
            # Reuse the previous DRAW_START image
            if last_draw_start_path:
                has_image = True
                image_path_str = last_draw_start_path
                source = "reuse"

        # GHOST_ONLY: no image generated

        manifest_segments.append(ImageSegment(
            segment_id=seg_id,
            type=seg.get("type", "explore"),
            has_image=has_image,
            image_path=image_path_str,
            prompt_used=prompt_used,
            source=source,
            visual_action=visual_action,
        ))

    if progress_callback:
        progress_callback(total, total, "done")

    image_count = sum(1 for s in manifest_segments if s.has_image)

    manifest = ImageManifest(
        manifest_id=str(uuid.uuid4()),
        script_id=script_id,
        book_id=book_id,
        chapter_num=chapter_num,
        episode_num=episode_num,
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_segments=total,
        image_segments=image_count,
        segments=manifest_segments,
    )

    # Save manifest to disk
    manifest_path = img_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    logger.info(
        "Image manifest built: %d segments, %d with images",
        total, image_count,
    )
    return manifest


def load_manifest(book_id: str, chapter_num: int) -> Optional[dict]:
    """Load a saved image manifest from disk."""
    path = IMAGES_DIR / book_id / f"chapter_{chapter_num}" / "manifest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
