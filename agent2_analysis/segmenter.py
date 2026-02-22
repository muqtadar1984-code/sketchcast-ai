"""Episode builder — one episode per chapter (no segmentation)."""

from __future__ import annotations


def build_single_episode(chapter: dict) -> dict:
    """
    Build a single episode that covers the entire chapter.

    No Claude API call needed — this is a simple mechanical calculation.
    Visual opportunity IDs are populated later by the analyzer from the
    combined analysis response.
    """
    sections = chapter.get("sections", [])
    return {
        "episode_num": 1,
        "title": chapter.get("title", "Untitled"),
        "sections_covered": [s.get("section_title", "") for s in sections],
        "estimated_word_count": sum(len(s.get("content", "").split()) for s in sections),
        "estimated_duration_minutes": round(
            sum(len(s.get("content", "").split()) for s in sections) / 130, 1
        ),
        "visual_opportunities_in_episode": [],  # populated from combined analysis call
    }
