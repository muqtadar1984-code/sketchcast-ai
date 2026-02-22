"""Batch image analysis via a single Claude Vision call."""

from __future__ import annotations

from pathlib import Path

from agent2_analysis.models import ImageAnalysis
from agent2_analysis.prompts import BATCH_IMAGE_ANALYSIS_PROMPT
from shared.claude_client import ClaudeClient


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

    # Separate images into available (on disk) vs missing
    available: list[dict] = []    # img_info dicts with existing files
    available_paths: list[Path] = []
    missing: list[dict] = []

    for img_info in images:
        filename = img_info.get("filename", "")
        img_path = images_dir / filename
        if img_path.exists():
            available.append(img_info)
            available_paths.append(img_path)
        else:
            missing.append(img_info)

    # Build placeholder analyses for missing images
    analyses: list[ImageAnalysis] = []
    for img_info in missing:
        analyses.append(ImageAnalysis(
            image_filename=img_info.get("filename", ""),
            description=f"Image from page {img_info.get('page_num', 0) + 1}. "
                        f"Context: {img_info.get('context_label', '')}",
            educational_value="Image could not be analysed (file not found on server).",
            can_be_recreated_as_sketch=False,
        ))

    if not available:
        # No images on disk — skip the API call entirely
        return analyses, {"total_tokens": 0, "estimated_cost_usd": 0}

    # ── Single batch Vision call ──────────────────────────────────────
    # Build the image list text for the prompt
    image_list_parts = []
    for i, img_info in enumerate(available, 1):
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
            image_paths=[str(p) for p in available_paths],
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
            # Map back to the correct filename
            filename = available[i].get("filename", "") if i < len(available) else item.get("image_filename", "")
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
        # If the batch call fails, create error placeholders
        for img_info in available:
            analyses.append(ImageAnalysis(
                image_filename=img_info.get("filename", ""),
                description=f"Batch analysis failed: {str(e)}",
                educational_value="Could not be analysed.",
            ))
        usage = {"total_tokens": 0, "estimated_cost_usd": 0}

    return analyses, {"total_tokens": usage.get("total_tokens", 0), "estimated_cost_usd": usage.get("estimated_cost_usd", 0)}
