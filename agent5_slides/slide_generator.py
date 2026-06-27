"""Agent 5: Slide Generator — orchestrates slide assembly for each segment.

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
from .slide_builder import build_slide_pptx, create_blank_slide_png, export_slide_png

logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
SLIDES_DIR = STORAGE_DIR / "slides"


def generate_episode_slides(
    script_data: dict,
    image_manifest: dict,
    progress_callback: Optional[Callable] = None,
) -> SlideManifest:
    """Build slides for all segments in one episode.

    Parameters
    ----------
    script_data : dict
        Full script dict with ``episodes[0].segments``.
    image_manifest : dict
        Output from Agent 4 (ImageManifest.model_dump()).
    progress_callback : callable, optional
        ``fn(current_index, total, segment_id)``

    Returns
    -------
    SlideManifest
    """
    episodes = script_data.get("episodes", [script_data])
    episode = episodes[0] if isinstance(episodes, list) and episodes else script_data

    book_id = episode.get("book_id", script_data.get("book_id", "unknown"))
    chapter_num = episode.get("chapter_num", script_data.get("chapter_num", 0))
    episode_num = episode.get("episode_num", script_data.get("episode_num", 1))
    script_id = episode.get("script_id", script_data.get("script_id", str(uuid.uuid4())))

    slide_dir = SLIDES_DIR / book_id / f"chapter_{chapter_num}"
    slide_dir.mkdir(parents=True, exist_ok=True)

    # Index image segments by ID
    img_segments = {
        seg["segment_id"]: seg
        for seg in image_manifest.get("segments", [])
    }

    # Index script segments for title text
    script_segments = {
        seg["segment_id"]: seg
        for seg in episode.get("segments", [])
    }

    manifest_segments: list[SlideSegment] = []
    last_slide_png: str | None = None
    total = len(image_manifest.get("segments", []))

    for i, img_seg in enumerate(image_manifest.get("segments", [])):
        seg_id = img_seg["segment_id"]
        visual_action = img_seg.get("visual_action", "GHOST_ONLY")
        image_path = img_seg.get("image_path")

        if progress_callback:
            progress_callback(i, total, seg_id)

        # Get title text from script
        script_seg = script_segments.get(seg_id, {})
        seg_text = script_seg.get("text", "")
        seg_type = script_seg.get("type", "explore")
        title = seg_text[:50] + "..." if len(seg_text) > 50 else seg_text
        if not title:
            title = seg_type.replace("_", " ").title()

        has_slide = False
        slide_path_str: str | None = None
        slide_image_str: str | None = None

        if visual_action == "DRAW_START" and image_path:
            # Build PPTX + export PNG
            pptx_path = slide_dir / f"{seg_id}_slide.pptx"
            png_path = slide_dir / f"{seg_id}_slide.png"

            build_slide_pptx(image_path, title, pptx_path)
            export_slide_png(image_path, title, png_path)

            has_slide = True
            slide_path_str = str(pptx_path)
            slide_image_str = str(png_path)
            last_slide_png = slide_image_str

        elif visual_action == "DRAW_CONTINUE" and last_slide_png:
            # Reuse previous slide image
            has_slide = True
            slide_image_str = last_slide_png

        else:
            # GHOST_ONLY or no image — create blank slide
            png_path = slide_dir / f"{seg_id}_slide.png"
            create_blank_slide_png(title, png_path)
            has_slide = True
            slide_image_str = str(png_path)

        manifest_segments.append(SlideSegment(
            segment_id=seg_id,
            type=seg_type,
            has_slide=has_slide,
            slide_path=slide_path_str,
            slide_image_path=slide_image_str,
            visual_action=visual_action,
        ))

    if progress_callback:
        progress_callback(total, total, "done")

    slide_count = sum(1 for s in manifest_segments if s.has_slide)

    manifest = SlideManifest(
        manifest_id=str(uuid.uuid4()),
        script_id=script_id,
        book_id=book_id,
        chapter_num=chapter_num,
        episode_num=episode_num,
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_segments=total,
        slide_segments=slide_count,
        segments=manifest_segments,
    )

    # Save manifest to disk
    manifest_path = slide_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    logger.info(
        "Slide manifest built: %d segments, %d with slides",
        total, slide_count,
    )
    return manifest


def load_manifest(book_id: str, chapter_num: int) -> Optional[dict]:
    """Load a saved slide manifest from disk."""
    path = SLIDES_DIR / book_id / f"chapter_{chapter_num}" / "manifest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
