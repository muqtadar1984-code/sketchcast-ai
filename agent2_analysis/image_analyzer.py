"""Image analysis via Claude Vision."""

from __future__ import annotations

from pathlib import Path

from agent2_analysis.models import ImageAnalysis
from agent2_analysis.prompts import IMAGE_ANALYSIS_PROMPT
from shared.claude_client import ClaudeClient


def analyze_images(
    chapter_content: dict,
    book_id: str,
    images_base_dir: Path | str | None = None,
    client: ClaudeClient | None = None,
) -> tuple[list[ImageAnalysis], dict]:
    """
    Analyse all images in a chapter using Claude Vision.

    Returns (list_of_analyses, usage_dict).
    """
    if client is None:
        client = ClaudeClient()

    images = chapter_content.get("images", [])
    if not images:
        return [], {"image_analysis": 0, "estimated_cost_usd": 0}

    title = chapter_content.get("title", "Untitled")
    total_tokens = 0
    total_cost = 0.0
    analyses: list[ImageAnalysis] = []

    # Try to find the images directory
    if images_base_dir is None:
        images_base_dir = Path(__file__).resolve().parent.parent / "storage" / "extracted_images" / book_id

    images_dir = Path(images_base_dir)

    for img_info in images:
        filename = img_info.get("filename", "")
        context = img_info.get("context_label", "")

        # Try to find the image file
        img_path = images_dir / filename
        if not img_path.exists():
            # Image not available — create a placeholder analysis
            analyses.append(ImageAnalysis(
                image_filename=filename,
                description=f"Image from page {img_info.get('page_num', 0) + 1}. Context: {context}",
                educational_value="Image could not be analysed (file not found on server).",
                can_be_recreated_as_sketch=False,
            ))
            continue

        prompt = IMAGE_ANALYSIS_PROMPT.format(
            chapter_title=title,
            context=context[:500],
        )

        try:
            result = client.analyze_image(str(img_path), prompt, max_tokens=2048)
            data = result["data"]
            usage = result["usage"]

            total_tokens += usage.get("total_tokens", 0)
            total_cost += usage.get("estimated_cost_usd", 0)

            analyses.append(ImageAnalysis(
                image_filename=filename,
                visual_type=data.get("visual_type", "image"),
                description=data.get("description", ""),
                key_elements=data.get("key_elements", []),
                educational_value=data.get("educational_value", ""),
                can_be_recreated_as_sketch=data.get("can_be_recreated_as_sketch", False),
                sketch_recreation_notes=data.get("sketch_recreation_notes", ""),
                complexity=data.get("complexity", "medium"),
            ))
        except Exception as e:
            analyses.append(ImageAnalysis(
                image_filename=filename,
                description=f"Analysis failed: {str(e)}",
                educational_value="Could not be analysed.",
            ))

    return analyses, {"image_analysis": total_tokens, "estimated_cost_usd": total_cost}
