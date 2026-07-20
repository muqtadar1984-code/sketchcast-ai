"""Agent 5: Slide Generator — one content slide per script segment.

Each slide shows the textbook CHAPTER CONTENT (heading + key bullet points)
from the segment's slide_heading/slide_points. The spoken Socratic narration
(segment.text) becomes the video voiceover (Agent 6) and the PPTX speaker notes
in the downloadable deck.

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
from .slide_builder import build_episode_deck, export_slide_png
from .theme import concepts_for_slides

logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
SLIDES_DIR = STORAGE_DIR / "slides"


def _visual_to_points(visual: Optional[dict]) -> list[str]:
    """Flatten a slide_visual's labels into deck bullet points (text-only deck)."""
    if not isinstance(visual, dict):
        return []
    labels = [str(it.get("label")).strip() for it in (visual.get("items") or [])
              if isinstance(it, dict) and str(it.get("label") or "").strip()]
    if labels:
        return labels[:6]
    nodes = [str(n).strip() for n in (visual.get("nodes") or []) if str(n).strip()]
    if nodes:
        return nodes[:6]
    out: list[str] = []
    for g in visual.get("groups") or []:
        head = str(g.get("heading") or "").strip()
        items = ", ".join(str(it).strip() for it in (g.get("items") or []) if str(it).strip())
        if head or items:
            out.append(f"{head}: {items}" if head and items else (head or items))
    return out[:6]


def generate_episode_slides(
    script_data: dict,
    image_manifest: Optional[dict] = None,  # ignored (no AI images in freemium)
    progress_callback: Optional[Callable] = None,
    branding: Optional[dict] = None,
    direction: str = "ltr",
) -> SlideManifest:
    """Render one chapter-content slide PNG per segment + a combined editable deck.

    ``branding`` = {pptx_template, accent_rgb, logo_path} applies the school's
    theme/colour/logo to the deck + video slides (any field may be None).
    """
    _b = branding or {}
    _accent = _b.get("accent_rgb")
    _logo = _b.get("logo_path")
    _pptx_template = _b.get("pptx_template")
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
    deck_slides: list[dict] = []
    total = len(segments)

    # One art-direction pass over the whole chapter → the SAME coherent,
    # non-repeating concept set the video uses (video_composer computes it
    # identically from the same heading order), so deck slide images and video
    # frames match glyph-for-glyph.
    seg_concepts = concepts_for_slides([
        (seg.get("slide_heading") or "").strip() or episode_title for seg in segments
    ])

    for i, seg in enumerate(segments):
        seg_id = seg.get("segment_id", f"s{i + 1:03d}")
        seg_type = seg.get("type", "explore")
        narration = (seg.get("text") or "").strip()
        heading = (seg.get("slide_heading") or "").strip() or episode_title
        points = [str(p).strip() for p in (seg.get("slide_points") or []) if str(p).strip()]
        visual = seg.get("slide_visual")

        if progress_callback:
            progress_callback(i, total, seg_id)

        png_path = slide_dir / f"{seg_id}_slide.png"
        export_slide_png(
            heading=heading,
            points=points,
            output_png_path=png_path,
            # Footer is a dev-only label. The deck now embeds THIS exact PNG, so
            # it stays empty in production (the video ships no footer either).
            footer_text="",
            context_title=episode_title if heading != episode_title else "",
            fallback_text=narration,
            accent=_accent,
            logo_path=_logo,
            visual=visual,
            number=i + 1,
            concept=seg_concepts[i],
            direction=direction,
        )

        # The designed deck now embeds the SAME rendered slide image the video
        # animates (diagrams and all), so the download shows exactly what the
        # lesson shows — no more flattening a diagram to bullet labels. `points`
        # still rides along for the branded-template path, which lays out its
        # own text; a diagram-only segment falls back to its labels there so a
        # branded deck never goes blank.
        deck_slides.append({
            "heading": heading,
            "points": points or _visual_to_points(visual),
            "narration": narration,
            "image": str(png_path),
        })
        manifest_segments.append(SlideSegment(
            segment_id=seg_id,
            type=seg_type,
            has_slide=True,
            slide_path=None,
            slide_image_path=str(png_path),
            visual_action=seg.get("visual_action", "GHOST_ONLY"),
        ))

    # Combined editable deck (heading + bullets on slide, Socratic notes in notes)
    deck_path: Optional[str] = None
    try:
        deck_file = slide_dir / f"episode_{episode_num}_deck.pptx"
        build_episode_deck(deck_slides, deck_file, episode_title=episode_title,
                           template=_pptx_template, accent=_accent, direction=direction)
        deck_path = str(deck_file)
        for s in manifest_segments:
            s.slide_path = deck_path
    except Exception as exc:  # deck is a bonus; never fail the slide step on it
        logger.warning("Deck build failed: %s", exc)

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
        deck_path=deck_path,
        segments=manifest_segments,
    )

    manifest_path = slide_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    logger.info("Slide manifest built: %d slides, deck=%s", total, bool(deck_path))
    return manifest


def load_manifest(book_id: str, chapter_num: int) -> Optional[dict]:
    """Load a saved slide manifest from disk."""
    path = SLIDES_DIR / book_id / f"chapter_{chapter_num}" / "manifest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
