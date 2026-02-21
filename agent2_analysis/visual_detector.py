"""Visual opportunity detection for whiteboard sketch animations."""

from __future__ import annotations

from agent2_analysis.models import VisualOpportunity
from agent2_analysis.prompts import VISUAL_OPPORTUNITY_PROMPT
from shared.claude_client import ClaudeClient


def detect_visual_opportunities(
    chapter_content: dict,
    level: str = "middle_school",
    client: ClaudeClient | None = None,
) -> tuple[list[VisualOpportunity], dict]:
    """
    Detect visual animation opportunities in a chapter.

    Returns (list_of_opportunities, usage_dict).
    """
    if client is None:
        client = ClaudeClient()

    title = chapter_content.get("title", "Untitled")
    sections = chapter_content.get("sections", [])

    content_parts = []
    for sec in sections:
        content_parts.append(f"## {sec.get('section_title', '')}")
        content_parts.append(sec.get("content", ""))
        for sub in sec.get("subsections", []):
            content_parts.append(f"### {sub.get('section_title', '')}")
            content_parts.append(sub.get("content", ""))

    full_content = "\n\n".join(content_parts)

    prompt = VISUAL_OPPORTUNITY_PROMPT.format(
        chapter_title=title,
        level=level,
        content=full_content[:12000],
    )

    result = client.analyze(prompt, max_tokens=4096)
    data = result["data"]
    usage = result["usage"]

    # Normalize
    if isinstance(data, dict):
        raw_list = data.get("visual_opportunities", data.get("opportunities", [data]))
    elif isinstance(data, list):
        raw_list = data
    else:
        raw_list = []

    opportunities = []
    for item in raw_list:
        try:
            opportunities.append(VisualOpportunity(**item))
        except Exception:
            # Best-effort: create with available fields
            opportunities.append(VisualOpportunity(
                opportunity_id=item.get("opportunity_id", f"v{len(opportunities)+1:03d}"),
                title=item.get("title", "Visual"),
                description=item.get("description", ""),
                visual_type=item.get("visual_type", "diagram"),
            ))

    return opportunities, {"visual_detection": usage.get("total_tokens", 0), "estimated_cost_usd": usage.get("estimated_cost_usd", 0)}
