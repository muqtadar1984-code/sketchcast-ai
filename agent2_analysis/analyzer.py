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
from shared.claude_client import ClaudeClient, artifact_model

ANALYSIS_DIR = Path(__file__).resolve().parent.parent / "storage" / "analysis"

# Per-analysis-call content budget (chars). This used to be a silent truncation
# of the WHOLE chapter (`full_content[:15000]`); it is now the chunk size —
# chapters longer than this become multiple parts, each fully analysed.
MAX_ANALYSIS_CHARS = 15000

# Per-PART teaching budget (words). A part's video duration is planned at
# words/130wpm clamped to 12 minutes — so any chunk beyond ~1560 words gets
# COMPRESSED, not covered. Splitting at 1500 words keeps every part fully
# teachable in its ~12-15 minute runtime (this is what actually guarantees
# "the whole chapter is covered": chars bound the LLM context, words bound
# the lesson).
MAX_PART_WORDS = 1500


def run_full_analysis(
    book_id: str,
    chapter_content: dict,
    level: str = "middle_school",
    client: ClaudeClient | None = None,
    on_progress=None,
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

    # ── Build chapter content UNITS (block text + owning section title) ──────
    # Units, not one string: long chapters are CHUNKED so every part of the
    # chapter reaches an analysis call. The old single `full_content[:15000]`
    # silently dropped everything past ~15k chars — the "video doesn't cover
    # the whole chapter" bug: concepts from the back of the chapter never
    # existed, so no script could teach them.
    sections = chapter_content.get("sections", [])
    units: list[tuple[str | None, str]] = []  # (section_title, block_text)
    word_count = 0

    for sec in sections:
        sec_title = sec.get("section_title", "")
        text = sec.get("content", "")
        units.append((sec_title, f"## {sec_title}\n\n{text}"))
        word_count += len(text.split())
        for sub in sec.get("subsections", []):
            sub_text = sub.get("content", "")
            units.append((sec_title, f"### {sub.get('section_title', '')}\n\n{sub_text}"))
            word_count += len(sub_text.split())

    for box in chapter_content.get("key_boxes", []):
        box_text = box.get("content", "")
        units.append((None, f"[{box.get('type', 'box').upper()}: {box.get('title', '')}]\n\n{box_text}"))
        word_count += len(box_text.split())

    # Greedy chunking on unit boundaries. A chunk closes when EITHER budget
    # would overflow: MAX_ANALYSIS_CHARS (the LLM context bound — same figure
    # the old truncation used) or MAX_PART_WORDS (the lesson-duration bound —
    # a ~2,400-word chapter fits in 15k chars but NOT in one 12-minute video,
    # which is exactly the "video doesn't cover the chapter" complaint).
    # An oversized single unit (one giant OCR'd section) is hard-split.
    chunks: list[dict] = []  # {text, section_titles, words}
    cur_parts: list[str] = []
    cur_titles: list[str] = []
    cur_len = 0
    cur_words = 0

    def _flush() -> None:
        nonlocal cur_parts, cur_titles, cur_len, cur_words
        if cur_parts:
            text = "\n\n".join(cur_parts)
            chunks.append({
                "text": text,
                "section_titles": list(dict.fromkeys(t for t in cur_titles if t)),
                "words": len(text.split()),
            })
        cur_parts, cur_titles, cur_len, cur_words = [], [], 0, 0

    for sec_title, block in units:
        pieces = [block[i:i + MAX_ANALYSIS_CHARS] for i in range(0, len(block), MAX_ANALYSIS_CHARS)] or [""]
        for piece in pieces:
            piece_words = len(piece.split())
            if cur_parts and (cur_len + len(piece) > MAX_ANALYSIS_CHARS or cur_words + piece_words > MAX_PART_WORDS):
                _flush()
            cur_parts.append(piece)
            if sec_title:
                cur_titles.append(sec_title)
            cur_len += len(piece) + 2
            cur_words += piece_words
    _flush()
    if not chunks:
        chunks = [{"text": "", "section_titles": [], "words": 0}]
    total_parts = len(chunks)

    # ══════════════════════════════════════════════════════════════════
    #  API CALL(s) 1..N — combined analysis per chunk (N=1 for most chapters)
    # ══════════════════════════════════════════════════════════════════
    concepts_all: list = []
    dependencies_all: list = []
    prerequisites_all: list = []
    difficulty_assessments: list = []
    visual_opportunities: list[VisualOpportunity] = []
    episodes: list[Episode] = []
    usage_chapter = {"total_tokens": 0, "estimated_cost_usd": 0}

    for part_idx, chunk in enumerate(chunks, start=1):
        part_title = chapter_title if total_parts == 1 else f"{chapter_title} (Part {part_idx} of {total_parts})"
        prompt = FULL_CHAPTER_ANALYSIS_PROMPT.format(
            chapter_title=part_title,
            level=level,
            word_count=chunk["words"],
            content=chunk["text"],
        )

        result = client.analyze(prompt, max_tokens=16000)
        data = result["data"]
        part_usage = result.get("usage", {})
        usage_chapter["total_tokens"] += part_usage.get("total_tokens", 0)
        usage_chapter["estimated_cost_usd"] += part_usage.get("estimated_cost_usd", 0)
        if not isinstance(data, dict):
            data = {}
        if not data.get("concepts") and chunk["words"] > 500:
            # A substantial chunk with zero extracted concepts almost always means
            # the reply truncated or failed to parse — the lesson would then be
            # grounded in nothing but the title. Surface it loudly in worker logs.
            logger.warning(
                "combined analysis returned no concepts for a %d-word chunk (part %d/%d) — reply began: %r",
                chunk["words"], part_idx, total_parts, str(result.get("data"))[:200],
            )

        # Ids restart at c001/v001 in every call — prefix per part so episode →
        # concept/visual links stay unambiguous across the whole chapter.
        pfx = "" if total_parts == 1 else f"p{part_idx}-"
        part_concept_ids: list[str] = []
        for i, c in enumerate(data.get("concepts", []) or []):
            if isinstance(c, dict):
                c["concept_id"] = f"{pfx}{c.get('concept_id') or f'c{i + 1:03d}'}"
                part_concept_ids.append(c["concept_id"])
                concepts_all.append(c)
        dependencies_all.extend(data.get("dependencies", []) or [])
        prerequisites_all.extend(data.get("prerequisites", []) or [])

        # A multi-part chapter spends MINUTES per chunk here — report each one
        # so the job's progress bar moves instead of sitting frozen ("stuck at
        # 20%" report, 2026-07-18). Called as on_progress(part_idx, total_parts)
        # so the caller can also label the stage ("reading part 2/4").
        # Best-effort: progress must never kill a job.
        if on_progress is not None:
            try:
                on_progress(part_idx, total_parts)
            except Exception:  # noqa: BLE001
                pass

        raw_diff = data.get("difficulty_assessments", [])
        if isinstance(raw_diff, dict):
            difficulty_assessments.append(raw_diff)
        elif isinstance(raw_diff, list):
            difficulty_assessments.extend(raw_diff)

        raw_visuals = data.get("visual_opportunities", [])
        if isinstance(raw_visuals, dict):
            raw_visuals = [raw_visuals]
        part_visual_ids: list[str] = []
        for v in (raw_visuals if isinstance(raw_visuals, list) else []):
            if not isinstance(v, dict):
                continue
            v["opportunity_id"] = f"{pfx}{v.get('opportunity_id') or f'v{len(visual_opportunities) + 1:03d}'}"
            try:
                vo = VisualOpportunity(**v)
            except Exception:
                vo = VisualOpportunity(
                    opportunity_id=v["opportunity_id"],
                    title=v.get("title", "Visual"),
                    description=v.get("description", ""),
                    visual_type=v.get("visual_type", "diagram"),
                )
            visual_opportunities.append(vo)
            part_visual_ids.append(vo.opportunity_id)

        # ── One EPISODE per chunk (mechanical, no API call) ────────────
        # Same 3-12 min lesson clamp as before, per PART — a long chapter
        # becomes Part 1..N of ~teachable length instead of one video that
        # only covers the front of the chapter.
        duration = min(12.0, max(3.0, round(chunk["words"] / 130, 1)))
        episodes.append(Episode(
            episode_num=part_idx,
            title=chapter_title if total_parts == 1 else f"{chapter_title} — Part {part_idx}",
            sections_covered=chunk["section_titles"],
            estimated_word_count=chunk["words"],
            estimated_duration_minutes=duration,
            key_concepts_introduced=part_concept_ids,
            visual_opportunities_in_episode=part_visual_ids,
        ))

    if total_parts > 1:
        logger.info(
            "chapter %s '%s': %d words split into %d parts for full coverage",
            chapter_num, chapter_title, word_count, total_parts,
        )

    concept_result = ConceptResult(
        concepts=concepts_all,
        dependencies=dependencies_all,
        prerequisites=prerequisites_all,
    )
    episode_plan = EpisodePlan(
        chapter_title=chapter_title,
        total_episodes=total_parts,
        episodes=episodes,
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
        analyzer_version=2,  # chunked full-coverage analyzer — cache-reusable
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
