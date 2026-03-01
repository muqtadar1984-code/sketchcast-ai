"""Agent 4: Sketch Generator — converts AI-generated raster images into Scribe SVGs.

Entry points
------------
generate_episode_animations_from_script()   Streamlit in-process entry point.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .manifest_builder import build_manifest
from .models import AnimationManifest, ManifestSegment
from .vectorizer import Vectorizer

logger = logging.getLogger(__name__)

ANIMATIONS_DIR = Path(__file__).parent.parent / "storage" / "animations"

_BLANK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" '
    'width="1280" height="720">'
    '<rect width="1280" height="720" fill="#FAFAFA"/>'
    '</svg>\n'
)


def _generate_image_with_nanobana(visual_request: dict, output_path: Path) -> bool:
    """Simulate calling the Nano Banana Pro API to generate a line-art image.

    In a production environment, you would replace this with:
    1. The actual Nano Banana Pro API call (or Gemini 3.1 Pro image generation).
    2. Downloading the resulting byte stream.
    3. Writing the bytes to ``output_path``.
    """
    prompt = visual_request.get("prompt", "")
    negative_prompt = visual_request.get("negative_prompt", "")

    # --- MOCK API CALL ---
    # print(f"Calling Nano Banana Pro API with prompt: {prompt}")
    # response = requests.post("...", json={"prompt": prompt, ...})
    # with open(output_path, 'wb') as f:
    #     f.write(response.content)
    # ---------------------

    # For now, if the file doesn't exist (because we are mocking),
    # the vectorizer will fail. You must ensure a valid image is saved here.
    return True


def _generate_for_episode(
    episode: dict,
    client,  # Kept for signature compatibility, though Claude is no longer used here
    progress_callback: Optional[Callable] = None,
) -> AnimationManifest:
    """Core logic: generate vector animations for every segment in one episode."""
    book_id = episode.get("book_id", "unknown")
    chapter_num = episode.get("chapter_num", 0)
    episode_num = episode.get("episode_num", 1)
    script_id = episode.get("script_id", str(uuid.uuid4()))

    # Ensure output directory exists
    anim_dir = ANIMATIONS_DIR / book_id / f"chapter_{chapter_num}"
    anim_dir.mkdir(parents=True, exist_ok=True)

    segments = episode.get("segments", [])
    manifest_segments: list[ManifestSegment] = []

    for i, seg in enumerate(segments):
        seg_id = seg.get("segment_id", f"s{i + 1:03d}")
        visual_action = seg.get("visual_action", "GHOST_ONLY")
        visual_request = seg.get("visual_request")

        if progress_callback:
            progress_callback(i, len(segments), seg_id)

        has_animation = False
        svg_path_str = None
        paths_data = None

        # Only generate a new image if the action is DRAW_START
        if visual_action == "DRAW_START" and visual_request:
            raw_img_path = anim_dir / f"{seg_id}_raw.png"
            svg_path = anim_dir / f"{seg_id}_sketch.svg"

            # 1. Generate Image with Nano Banana Pro
            success = _generate_image_with_nanobana(visual_request, raw_img_path)

            if success and raw_img_path.exists():
                # 2. Real image exists — vectorize it
                paths_data = Vectorizer.image_to_scribe_paths(str(raw_img_path))
            elif success:
                # Stub mode — API returned OK but no PNG on disk yet.
                # Use Vectorizer placeholder so the pipeline still produces
                # visible SVGs during development.
                logger.info("Nano Banana Pro stub active — using placeholder SVG for %s", seg_id)
                paths_data = Vectorizer._placeholder_paths()

            if paths_data:
                # 3. Save the dual-layer SVG
                Vectorizer.save_svg_from_paths(paths_data, str(svg_path))
                has_animation = True
                svg_path_str = str(svg_path)

        else:
            # Non-animated segment — write a blank canvas SVG so the
            # Streamlit preview has something to display.
            blank_path = anim_dir / f"{seg_id}_blank.svg"
            blank_path.write_text(_BLANK_SVG, encoding="utf-8")
            svg_path_str = str(blank_path)

        # Append to manifest
        manifest_segments.append(ManifestSegment(
            segment_id=seg_id,
            type=seg.get("type", "explore"),
            has_animation=has_animation,
            svg_path=svg_path_str,
            paths=paths_data,  # Scribe path data for Agent 6
            animation_path=None,  # Deprecated in Scribe style
            roughjs_html_path=None,  # Deprecated in Scribe style
            estimated_duration_seconds=int(seg.get("estimated_duration_seconds", 30)),
            # Inject the new visual action so Agent 6 knows how to play it
            sketch_cue_timing=visual_action,
        ))

    # Fire final progress tick
    if progress_callback:
        progress_callback(len(segments), len(segments), "done")

    manifest = build_manifest(
        manifest_id=str(uuid.uuid4()),
        script_id=script_id,
        book_id=book_id,
        chapter_num=chapter_num,
        episode_num=episode_num,
        generated_at=datetime.now(timezone.utc).isoformat(),
        segments=manifest_segments,
    )

    manifest_path = anim_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return manifest


# ── public entry points ───────────────────────────────────────────────

def generate_episode_animations_from_script(
    script_data: dict,
    analysis_dict: dict,  # Kept for signature compatibility
    client,
    progress_callback: Optional[Callable] = None,
) -> AnimationManifest:
    """Streamlit entry point — takes already-loaded dicts."""
    episodes = script_data.get("episodes", [])
    if not episodes:
        raise ValueError("No episodes found in script data.")

    episode = episodes[0]
    return _generate_for_episode(episode, client, progress_callback)


def load_manifest(book_id: str, chapter_num: int) -> Optional[dict]:
    """Load a saved animation manifest from disk. Returns dict or None."""
    path = ANIMATIONS_DIR / book_id / f"chapter_{chapter_num}" / "manifest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
