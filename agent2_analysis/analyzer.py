"""Orchestrates the full Agent 2 analysis pipeline.

Max 2 API calls per chapter:
  Call 1 — combined text analysis (concepts + difficulty + visuals)
  Call 2 — batch image analysis (skipped if no images)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

from agent2_analysis.image_analyzer import analyze_images_batch
from agent2_analysis.models import (ConceptResult, Episode, EpisodePlan,
                                    MasterAnalysis, TokenUsage,
                                    VisualOpportunity)
from agent2_analysis.prompts import FULL_CHAPTER_ANALYSIS_PROMPT
from agent2_analysis.segmenter import build_single_episode
from shared.claude_client import ClaudeClient, artifact_model

ANALYSIS_DIR = Path(__file__).resolve().parent.parent / "storage" / "analysis"


def run_full_analysis(
    book_id: str,
    chapter_content: dict,
    level: str = "middle_school",
    client: ClaudeClient | None = None,
) -> MasterAnalysis:
    """
    Run the complete analysis pipeline on a single chapter.

    Steps:
      1. ONE combined Claude call  -> concepts, difficulty, visuals
      2. ONE batch Claude Vision call -> all images (skipped if none)
      3. Build single episode mechanically (no API call)

    Returns a MasterAnalysis with all results combined.
    """
    if client is None:
        client = ClaudeClient(model=artifact_model())

    analysis_id = str(uuid.uuid4())
    chapter_num = chapter_content.get("chapter_num", 0)
    chapter_title = chapter_content.get("title", "Untitled")

    # ── Build chapter content string ──────────────────────────────────
    sections = chapter_content.get("sections", [])
    content_parts: list[str] = []
    word_count = 0

    for sec in sections:
        content_parts.append(f"## {sec.get('section_title', '')}")
        text = sec.get("content", "")
        content_parts.append(text)
        word_count += len(text.split())
        for sub in sec.get("subsections", []):
            content_parts.append(f"### {sub.get('section_title', '')}")
            sub_text = sub.get("content", "")
            content_parts.append(sub_text)
            word_count += len(sub_text.split())

    for box in chapter_content.get("key_boxes", []):
        content_parts.append(f"[{box.get('type', 'box').upper()}: {box.get('title', '')}]")
        box_text = box.get("content", "")
        content_parts.append(box_text)
        word_count += len(box_text.split())

    full_content = "\n\n".join(content_parts)

    # ══════════════════════════════════════════════════════════════════
    #  API CALL 1 — Combined chapter analysis
    # ══════════════════════════════════════════════════════════════════
    prompt = FULL_CHAPTER_ANALYSIS_PROMPT.format(
        chapter_title=chapter_title,
        level=level,
        word_count=word_count,
        content=full_content[:15000],  # cap to stay within context window
    )

    result = client.analyze(prompt, max_tokens=16000)
    data = result["data"]
    usage_chapter = result["usage"]
    if not isinstance(data, dict):
        data = {}
    if not data.get("concepts") and word_count > 500:
        # A substantial chapter with zero extracted concepts almost always means
        # the reply truncated or failed to parse — the lesson would then be
        # grounded in nothing but the title. Surface it loudly in worker logs.
        logger.warning(
            "combined analysis returned no concepts for a %d-word chapter — reply began: %r",
            word_count, str(result.get("data"))[:200],
        )

    # ── Parse concepts ────────────────────────────────────────────────
    concept_result = ConceptResult(
        concepts=data.get("concepts", []),
        dependencies=data.get("dependencies", []),
        prerequisites=data.get("prerequisites", []),
    )

    # ── Parse difficulty assessments ──────────────────────────────────
    raw_diff = data.get("difficulty_assessments", [])
    if isinstance(raw_diff, dict):
        difficulty_assessments = [raw_diff]
    elif isinstance(raw_diff, list):
        difficulty_assessments = raw_diff
    else:
        difficulty_assessments = []

    # ── Parse visual opportunities ────────────────────────────────────
    raw_visuals = data.get("visual_opportunities", [])
    if isinstance(raw_visuals, dict):
        raw_visuals = [raw_visuals]
    visual_opportunities: list[VisualOpportunity] = []
    for v in (raw_visuals if isinstance(raw_visuals, list) else []):
        try:
            visual_opportunities.append(VisualOpportunity(**v))
        except Exception:
            visual_opportunities.append(VisualOpportunity(
                opportunity_id=v.get("opportunity_id", f"v{len(visual_opportunities)+1:03d}"),
                title=v.get("title", "Visual"),
                description=v.get("description", ""),
                visual_type=v.get("visual_type", "diagram"),
            ))

    # ══════════════════════════════════════════════════════════════════
    #  EPISODE — built mechanically (no API call)
    # ══════════════════════════════════════════════════════════════════
    episode_dict = build_single_episode(chapter_content)
    # Enrich with IDs from the combined analysis
    episode_dict["visual_opportunities_in_episode"] = [
        v.opportunity_id for v in visual_opportunities
    ]
    episode_dict["key_concepts_introduced"] = [
        c.concept_id for c in concept_result.concepts
    ]

    episode = Episode(**episode_dict)
    episode_plan = EpisodePlan(
        chapter_title=chapter_title,
        total_episodes=1,
        episodes=[episode],
    )

    # ══════════════════════════════════════════════════════════════════
    #  API CALL 2 — Batch image analysis (skipped if no images)
    # ══════════════════════════════════════════════════════════════════
    image_analyses, usage_images = analyze_images_batch(
        chapter_content, book_id=book_id, client=client,
    )

    # ── Token usage ───────────────────────────────────────────────────
    total_tokens = (
        usage_chapter.get("total_tokens", 0)
        + usage_images.get("total_tokens", 0)
    )
    total_cost = (
        usage_chapter.get("estimated_cost_usd", 0)
        + usage_images.get("estimated_cost_usd", 0)
    )

    token_usage = TokenUsage(
        chapter_analysis=usage_chapter.get("total_tokens", 0),
        image_analysis=usage_images.get("total_tokens", 0),
        total=total_tokens,
        estimated_cost_usd=round(total_cost, 6),
    )

    master = MasterAnalysis(
        analysis_id=analysis_id,
        book_id=book_id,
        chapter_num=chapter_num,
        chapter_title=chapter_title,
        difficulty_level_requested=level,
        analyzed_at=datetime.now(timezone.utc).isoformat(),
        token_usage=token_usage,
        concepts=concept_result,
        difficulty_assessments=difficulty_assessments,
        visual_opportunities=visual_opportunities,
        image_analyses=image_analyses,
        episodes=episode_plan,
    )

    # ── Save to disk ─────────────────────────────────────────────────
    try:
        save_analysis(master)
    except Exception:
        pass  # Don't fail if filesystem is read-only (Streamlit Cloud)

    return master


def save_analysis(analysis: MasterAnalysis) -> Path:
    """Persist the analysis as a JSON file."""
    book_dir = ANALYSIS_DIR / analysis.book_id
    book_dir.mkdir(parents=True, exist_ok=True)
    output_path = book_dir / f"chapter_{analysis.chapter_num}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(analysis.model_dump(), f, indent=2, ensure_ascii=False)
    return output_path


def load_analysis(book_id: str, chapter_num: int) -> MasterAnalysis | None:
    """Load a previously saved analysis from disk."""
    path = ANALYSIS_DIR / book_id / f"chapter_{chapter_num}.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return MasterAnalysis(**json.load(f))
