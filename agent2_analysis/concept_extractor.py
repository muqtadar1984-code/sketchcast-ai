"""Concept identification and dependency mapping."""

from __future__ import annotations

from agent2_analysis.models import ConceptResult
from agent2_analysis.prompts import CONCEPT_EXTRACTION_PROMPT, DIFFICULTY_ASSESSMENT_PROMPT
from shared.claude_client import ClaudeClient


def extract_concepts(
    chapter_content: dict,
    level: str = "middle_school",
    client: ClaudeClient | None = None,
) -> tuple[ConceptResult, list[dict], dict]:
    """
    Extract concepts and difficulty assessments from a chapter.

    Returns (concept_result, difficulty_assessments, combined_usage).
    """
    if client is None:
        client = ClaudeClient()

    title = chapter_content.get("title", "Untitled")
    sections = chapter_content.get("sections", [])

    # Build full content string from sections
    content_parts = []
    for sec in sections:
        content_parts.append(f"## {sec.get('section_title', '')}")
        content_parts.append(sec.get("content", ""))
        for sub in sec.get("subsections", []):
            content_parts.append(f"### {sub.get('section_title', '')}")
            content_parts.append(sub.get("content", ""))
    # Include key boxes
    for box in chapter_content.get("key_boxes", []):
        content_parts.append(f"[{box.get('type', 'box').upper()}: {box.get('title', '')}]")
        content_parts.append(box.get("content", ""))

    full_content = "\n\n".join(content_parts)

    # 1. Concept extraction
    prompt = CONCEPT_EXTRACTION_PROMPT.format(
        chapter_title=title,
        level=level,
        content=full_content[:12000],  # cap to avoid token overflow
    )
    result = client.analyze(prompt, max_tokens=4096)
    data = result["data"]
    usage_concepts = result["usage"]

    concept_result = ConceptResult(
        concepts=data.get("concepts", []),
        dependencies=data.get("dependencies", []),
        prerequisites=data.get("prerequisites", []),
    )

    # 2. Difficulty assessment
    sections_text = "\n".join(
        f"- {sec.get('section_title', 'Untitled')}: {sec.get('content', '')[:300]}"
        for sec in sections
    )
    diff_prompt = DIFFICULTY_ASSESSMENT_PROMPT.format(
        chapter_title=title,
        level=level,
        sections_text=sections_text[:8000],
    )
    diff_result = client.analyze(diff_prompt, max_tokens=3000)
    diff_data = diff_result["data"]
    usage_diff = diff_result["usage"]

    # Normalize: ensure it's a list
    if isinstance(diff_data, dict):
        difficulty_assessments = diff_data.get("assessments", [diff_data])
    elif isinstance(diff_data, list):
        difficulty_assessments = diff_data
    else:
        difficulty_assessments = []

    combined_usage = {
        "concept_extraction": usage_concepts.get("total_tokens", 0),
        "difficulty_assessment": usage_diff.get("total_tokens", 0),
        "estimated_cost_usd": usage_concepts.get("estimated_cost_usd", 0) + usage_diff.get("estimated_cost_usd", 0),
    }

    return concept_result, difficulty_assessments, combined_usage
