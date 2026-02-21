"""Orchestrates the full Agent 2 analysis pipeline."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent2_analysis.concept_extractor import extract_concepts
from agent2_analysis.image_analyzer import analyze_images
from agent2_analysis.models import MasterAnalysis, TokenUsage
from agent2_analysis.segmenter import segment_episodes
from agent2_analysis.visual_detector import detect_visual_opportunities
from shared.claude_client import ClaudeClient

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
      1. Concept extraction + difficulty assessment
      2. Visual opportunity detection
      3. Image analysis (if images exist)
      4. Episode segmentation

    Returns a MasterAnalysis with all results combined.
    """
    if client is None:
        client = ClaudeClient()

    analysis_id = str(uuid.uuid4())
    chapter_num = chapter_content.get("chapter_num", 0)
    chapter_title = chapter_content.get("title", "Untitled")

    # ── Step 1: Concepts + Difficulty ────────────────────────────────
    concept_result, difficulty_assessments, usage_concepts = extract_concepts(
        chapter_content, level=level, client=client,
    )

    # ── Step 2: Visual Opportunities ─────────────────────────────────
    visual_opportunities, usage_visuals = detect_visual_opportunities(
        chapter_content, level=level, client=client,
    )

    # ── Step 3: Image Analysis ───────────────────────────────────────
    image_analyses, usage_images = analyze_images(
        chapter_content, book_id=book_id, client=client,
    )

    # ── Step 4: Episode Segmentation ─────────────────────────────────
    concept_ids = [c.concept_id for c in concept_result.concepts]
    visual_ids = [v.opportunity_id for v in visual_opportunities]

    episode_plan, usage_episodes = segment_episodes(
        chapter_content,
        concept_ids=concept_ids,
        visual_ids=visual_ids,
        level=level,
        client=client,
    )

    # ── Combine token usage ──────────────────────────────────────────
    total_tokens = (
        usage_concepts.get("concept_extraction", 0)
        + usage_concepts.get("difficulty_assessment", 0)
        + usage_visuals.get("visual_detection", 0)
        + usage_images.get("image_analysis", 0)
        + usage_episodes.get("segmentation", 0)
    )
    total_cost = (
        usage_concepts.get("estimated_cost_usd", 0)
        + usage_visuals.get("estimated_cost_usd", 0)
        + usage_images.get("estimated_cost_usd", 0)
        + usage_episodes.get("estimated_cost_usd", 0)
    )

    token_usage = TokenUsage(
        concept_extraction=usage_concepts.get("concept_extraction", 0),
        difficulty_assessment=usage_concepts.get("difficulty_assessment", 0),
        visual_detection=usage_visuals.get("visual_detection", 0),
        image_analysis=usage_images.get("image_analysis", 0),
        segmentation=usage_episodes.get("segmentation", 0),
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
