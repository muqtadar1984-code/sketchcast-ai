"""Batch image analysis via a single Claude Vision call."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PIL import Image

from agent2_analysis.models import ImageAnalysis
from agent2_analysis.prompts import BATCH_IMAGE_ANALYSIS_PROMPT
from shared.claude_client import ClaudeClient

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg"}
MIN_IMAGE_DIMENSION = 10  # pixels — skip images smaller than 10x10


def _validate_dimensions(path: str) -> bool:
    """Return True if the image is large enough for Claude Vision."""
    try:
        with Image.open(path) as img:
            w, h = img.size
            if w < MIN_IMAGE_DIMENSION or h < MIN_IMAGE_DIMENSION:
                logger.warning("Skipped: too small (%dx%d) — %s", w, h, path)
                return False
            return True
    except Exception:
        logger.warning("Skipped: cannot read dimensions — %s", path)
        return False


def convert_to_supported_format(image_path: str) -> str | None:
    """
    Convert any image to PNG if it's not already PNG or JPEG.
    Also validates that the image meets minimum dimension requirements.

    Returns the path to a usable image, or None if the file is
    unreadable / corrupted / too small.
    """
    ext = os.path.splitext(image_path)[1].lower()

    # Already a supported format — verify readable and big enough
    if ext in SUPPORTED_EXTENSIONS:
        try:
            with Image.open(image_path) as img:
                img.verify()
        except Exception:
            logger.warning("Skipped: unreadable file — %s", image_path)
            return None
        if not _validate_dimensions(image_path):
            return None
        return image_path

    # Unsupported extension (.jpx, .jp2, .bmp, .tiff, etc.) — convert to PNG
    output_path = os.path.splitext(image_path)[0] + "_converted.png"
    try:
        with Image.open(image_path) as img:
            img = img.convert("RGB")
            img.save(output_path, "PNG")
    except Exception:
        logger.warning("Skipped: unsupported or unreadable format — %s", image_path)
        return None
    if not _validate_dimensions(output_path):
        try:
            os.remove(output_path)
        except Exception:
            pass
        return None
    return output_path


def analyze_images_batch(
    chapter_content: dict,
    book_id: str,
    images_base_dir: Path | str | None = None,
    client: ClaudeClient | None = None,
) -> tuple[list[ImageAnalysis], dict]:
    """
    Analyse ALL images in a chapter with a single Claude Vision call.

    If no images exist in the chapter, returns immediately (0 API calls).
    If images exist but none are found on disk, returns placeholders (0 API calls).
    Converts unsupported formats (.jpx, .jp2, etc.) to PNG before sending.
    Otherwise makes exactly 1 API call for all images combined.

    Returns (list_of_analyses, usage_dict).
    """
    if client is None:
        client = ClaudeClient()

    images = chapter_content.get("images", [])
    if not images:
        return [], {"total_tokens": 0, "estimated_cost_usd": 0}

    title = chapter_content.get("title", "Untitled")

    # Resolve images directory
    if images_base_dir is None:
        images_base_dir = Path(__file__).resolve().parent.parent / "storage" / "extracted_images" / book_id
    images_dir = Path(images_base_dir)

    # ── Pre-flight: find, convert, and validate every image ───────────
    valid: list[dict] = []          # img_info dicts with usable files
    valid_paths: list[str] = []     # paths to send (may be converted PNGs)
    converted_paths: list[str] = [] # track converted files for cleanup
    analyses: list[ImageAnalysis] = []

    for img_info in images:
        filename = img_info.get("filename", "")
        img_path = images_dir / filename

        if not img_path.exists():
            # Image not on disk (e.g. Streamlit Cloud ephemeral FS)
            analyses.append(ImageAnalysis(
                image_filename=filename,
                description=f"Image from page {img_info.get('page_num', 0) + 1}. "
                            f"Context: {img_info.get('context_label', '')}",
                educational_value="Image could not be analysed (file not found on server).",
                can_be_recreated_as_sketch=False,
            ))
            logger.warning("Skipped: file not found — %s", img_path)
            continue

        # Convert if needed (.jpx -> .png, etc.) and validate readability
        usable_path = convert_to_supported_format(str(img_path))
        if usable_path is None:
            analyses.append(ImageAnalysis(
                image_filename=filename,
                description=f"Image from page {img_info.get('page_num', 0) + 1}. "
                            f"Skipped: unsupported or unreadable format ({img_path.suffix})",
                educational_value="Could not be analysed (unsupported image format).",
                can_be_recreated_as_sketch=False,
            ))
            continue

        valid.append(img_info)
        valid_paths.append(usable_path)
        if usable_path != str(img_path):
            converted_paths.append(usable_path)

    # If zero valid images remain, skip the API call entirely
    if not valid:
        return analyses, {"total_tokens": 0, "estimated_cost_usd": 0}

    # ── Single batch Vision call ──────────────────────────────────────
    image_list_parts = []
    for i, img_info in enumerate(valid, 1):
        filename = img_info.get("filename", "")
        context = img_info.get("context_label", "No context available")
        image_list_parts.append(f"Image {i}: filename=\"{filename}\", context=\"{context[:300]}\"")
    image_list_text = "\n".join(image_list_parts)

    prompt = BATCH_IMAGE_ANALYSIS_PROMPT.format(
        chapter_title=title,
        image_list=image_list_text,
    )

    try:
        result = client.analyze_images_batch(
            image_paths=valid_paths,
            prompt=prompt,
            max_tokens=4096,
        )
        data = result["data"]
        usage = result["usage"]

        # Parse the array of analyses
        if isinstance(data, dict):
            raw_list = data.get("images", data.get("analyses", [data]))
        elif isinstance(data, list):
            raw_list = data
        else:
            raw_list = []

        for i, item in enumerate(raw_list):
            filename = valid[i].get("filename", "") if i < len(valid) else item.get("image_filename", "")
            analyses.append(ImageAnalysis(
                image_filename=item.get("image_filename", filename),
                visual_type=item.get("visual_type", "image"),
                description=item.get("description", ""),
                key_elements=item.get("key_elements", []),
                educational_value=item.get("educational_value", ""),
                can_be_recreated_as_sketch=item.get("can_be_recreated_as_sketch", False),
                sketch_recreation_notes=item.get("sketch_recreation_notes", ""),
                complexity=item.get("complexity", "medium"),
            ))

    except Exception as e:
        for img_info in valid:
            analyses.append(ImageAnalysis(
                image_filename=img_info.get("filename", ""),
                description=f"Batch analysis failed: {str(e)}",
                educational_value="Could not be analysed.",
            ))
        usage = {"total_tokens": 0, "estimated_cost_usd": 0}

    # ── Cleanup converted files ───────────────────────────────────────
    for cpath in converted_paths:
        try:
            os.remove(cpath)
        except Exception:
            pass

    return analyses, {"total_tokens": usage.get("total_tokens", 0), "estimated_cost_usd": usage.get("estimated_cost_usd", 0)}
