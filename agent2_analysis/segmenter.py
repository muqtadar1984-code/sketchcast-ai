"""Episode builder — one episode per chapter (no segmentation)."""

from __future__ import annotations


def build_single_episode(chapter: dict) -> dict:
    """
    Build a single episode that covers the entire chapter.

    No Claude API call needed — this is a simple mechanical calculation.
    Counts words from section content, subsection content, and key boxes.
    Visual/concept IDs are populated later by the analyzer.
    """
    sections = chapter.get("sections", [])

    # Count words from actual content text of each section + subsections
    total_words = 0
    for s in sections:
        total_words += len(s.get("content", "").split())
        for sub in s.get("subsections", []):
            total_words += len(sub.get("content", "").split())

    # Also include content from key_boxes
    for box in chapter.get("key_boxes", []):
        total_words += len(box.get("content", "").split())

    return {
        "episode_num": 1,
        "title": chapter.get("title", "Untitled"),
        "sections_covered": [s.get("section_title", "") for s in sections],
        "estimated_word_count": total_words,
        "estimated_duration_minutes": round(total_words / 130, 1),
        "key_concepts_introduced": [],      # populated from combined analysis call
        "visual_opportunities_in_episode": [],  # populated from combined analysis call
    }
