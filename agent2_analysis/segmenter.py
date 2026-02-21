"""Episode segmentation logic — divides chapters into podcast episodes."""

from __future__ import annotations

from agent2_analysis.models import EpisodePlan, Episode
from agent2_analysis.prompts import EPISODE_SEGMENTATION_PROMPT
from shared.claude_client import ClaudeClient


def segment_episodes(
    chapter_content: dict,
    concept_ids: list[str],
    visual_ids: list[str],
    level: str = "middle_school",
    client: ClaudeClient | None = None,
) -> tuple[EpisodePlan, dict]:
    """
    Segment a chapter into podcast episodes of 3–7 minutes.

    Returns (episode_plan, usage_dict).
    """
    if client is None:
        client = ClaudeClient()

    title = chapter_content.get("title", "Untitled")
    sections = chapter_content.get("sections", [])

    # Estimate word count
    word_count = 0
    sections_text_parts = []
    for sec in sections:
        content = sec.get("content", "")
        word_count += len(content.split())
        sections_text_parts.append(
            f"- {sec.get('section_title', 'Untitled')} ({len(content.split())} words): "
            f"{content[:200]}..."
        )
        for sub in sec.get("subsections", []):
            sub_content = sub.get("content", "")
            word_count += len(sub_content.split())
            sections_text_parts.append(
                f"  - {sub.get('section_title', '')} ({len(sub_content.split())} words)"
            )

    # Include key boxes in word count
    for box in chapter_content.get("key_boxes", []):
        box_content = box.get("content", "")
        word_count += len(box_content.split())

    sections_text = "\n".join(sections_text_parts)

    prompt = EPISODE_SEGMENTATION_PROMPT.format(
        chapter_title=title,
        level=level,
        word_count=word_count,
        sections_text=sections_text[:8000],
        concept_ids=", ".join(concept_ids[:30]),
        visual_ids=", ".join(visual_ids[:30]),
    )

    result = client.analyze(prompt, max_tokens=3000)
    data = result["data"]
    usage = result["usage"]

    # Parse episodes
    if isinstance(data, dict):
        raw_episodes = data.get("episodes", [])
        plan_title = data.get("chapter_title", title)
    else:
        raw_episodes = data if isinstance(data, list) else []
        plan_title = title

    episodes = []
    for ep in raw_episodes:
        try:
            episodes.append(Episode(**ep))
        except Exception:
            episodes.append(Episode(
                episode_num=ep.get("episode_num", len(episodes) + 1),
                title=ep.get("title", f"Episode {len(episodes) + 1}"),
                estimated_duration_minutes=ep.get("estimated_duration_minutes", 4.0),
            ))

    plan = EpisodePlan(
        chapter_title=plan_title,
        total_episodes=len(episodes),
        episodes=episodes,
    )

    return plan, {"segmentation": usage.get("total_tokens", 0), "estimated_cost_usd": usage.get("estimated_cost_usd", 0)}
