"""Orchestrates the full Agent 2 analysis pipeline.

Max 2 API calls per chapter:
  Call 1 — combined text analysis (concepts + difficulty + visuals)
  Call 2 — batch image analysis (skipped if no images)
"""

from __future__ import annotations

import json
import logging
import re
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

# Per-PART teaching budget (words), set from the CLASSROOM, not from the model.
#
# Founder's design: a 15-minute video inside a 45-minute lesson leaves ~30
# minutes to work through the parts the class still needs to discuss. At the
# 130 wpm narration rate that is 15 x 130 = 1,950 words, and LESSON_MAX_MINUTES
# below is the matching ceiling. The two numbers must move together — they are
# the same decision expressed twice.
#
# They used to disagree, and scanned books paid for it. A monolithic OCR block
# is hard-split, and the split used to cut on characters alone: 15,000 chars is
# ~2,541 words ~ 19.5 minutes of narration, clamped down to a 12-minute video.
# Teachers got ~61% of the material, raced through — and because the rest of the
# week (worksheet, activity, case study, test) is built on the assumption that
# the video taught the topic, every following day stood on ground the video
# never covered. Digital books mostly escaped it: their sections arrive as
# separate units, so this budget could actually fire.
MAX_PART_WORDS = 1950

# The video-duration ceiling that MAX_PART_WORDS is derived from. A part is
# planned at words/130wpm and clamped here; keeping one part inside the budget
# means the clamp never has to compress it.
LESSON_MAX_MINUTES = 15.0
LESSON_MIN_MINUTES = 3.0
NARRATION_WPM = 130


# A trailing chunk below this many words is folded into the part before it
# rather than becoming a part of its own. Sized well under MAX_PART_WORDS: a
# real part carries up to MAX_PART_WORDS, so this can only catch a hard-split
# remainder, never a legitimately short final section.
_MIN_TAIL_WORDS = 400


def _hard_split(block: str, max_words: int = MAX_PART_WORDS) -> list[str]:
    """Split one oversized unit so every piece honours BOTH budgets.

    ``max_words`` is the teaching budget; the default is the classroom's 15
    minutes. The catalogue's YouTube kits (2026-09-06) pass a longer one —
    the 17-to-20-minute deep-dive video — through build_chapter_parts, and
    it must reach every place the words bound is applied or a section would
    still be cut at 1,950 words while the accumulator packs 2,600.

    This is where a scanned book's whole chapter arrives: the OCR is stored as a
    SINGLE "Content" section, so there is nothing to split on but the block
    itself, and the accumulator below never gets a second unit to close a part
    on. Splitting on characters alone therefore decided the lesson length by
    accident — 15,000 chars is ~2,541 words of English, half again over the
    teaching budget, and the duration clamp silently absorbed the difference.

    Two properties this must have and the old slice did not:
      * WORDS bound it too, not just chars, so a part is a lesson-sized piece of
        teaching rather than a context-sized piece of text;
      * cuts land between words. ``block[i:i+15000]`` cut mid-word, handing the
        analysis a fragment at each seam.

    Splitting on the whitespace-preserving token list makes it LOSSLESS —
    ``"".join(pieces) == block`` — which matters because everything else here is
    about not dropping teaching material.
    """
    tokens = re.split(r"(\s+)", block)
    total_words = sum(1 for t in tokens if t.strip())
    if not total_words:
        return [block] if block else [""]

    # BALANCED, not greedy. Filling each piece to the brim and letting the last
    # one take the remainder produces a runt — 4,060 words at a 1,950 budget
    # gives 1950/1950/160, and a 160-word "lesson" is a 3-minute video and a
    # billed credit for nothing. Deciding the piece COUNT first and dividing
    # evenly gives 3 x ~1,353 words: no runt, nothing over budget, and every
    # video in a chapter roughly the same length, which is what a teacher
    # planning a week of classes actually wants.
    n = max(
        -(-total_words // max_words),           # lesson bound (the classroom)
        -(-len(block) // MAX_ANALYSIS_CHARS),   # context bound (the model)
        1,
    )
    per = -(-total_words // n)

    pieces: list[str] = []
    cur: list[str] = []
    cur_chars = cur_words = 0
    for tok in tokens:
        is_word = bool(tok.strip())
        # The hard budgets still bind — `per` only decides where to aim, so an
        # unusually long run of characters cannot smuggle a piece past the
        # model's context bound.
        if cur and (cur_chars + len(tok) > MAX_ANALYSIS_CHARS
                    or (is_word and cur_words + 1 > max_words)
                    or (is_word and cur_words >= per and len(pieces) < n - 1)):
            pieces.append("".join(cur))
            cur, cur_chars, cur_words = [], 0, 0
        cur.append(tok)
        cur_chars += len(tok)
        cur_words += 1 if is_word else 0
    if cur:
        pieces.append("".join(cur))
    return pieces or [""]


def build_chapter_parts(chapter_content: dict, max_words: int = MAX_PART_WORDS) -> list[dict]:
    """The SINGLE source of truth for how a chapter splits into parts.

    ``max_words`` defaults to MAX_PART_WORDS, so every existing caller — the
    indexer's part map, the generation's part narrowing, the exam's units —
    splits exactly as before. Only the catalogue kit (catalogue/kit.py)
    passes a larger budget, for the longer YouTube video; the char bound
    (MAX_ANALYSIS_CHARS) is the model's and is never widened.

    Used by INDEXING (to show "Part 1..N" per chapter the moment a book is
    uploaded) and by GENERATION (whole-chapter analysis, and on-demand
    single-part jobs via params.part) — both run this same deterministic
    chunker on the same structured chapter, so they always agree.

    Units, not one string: long chapters are CHUNKED so every part of the
    chapter reaches an analysis call. The old single `full_content[:15000]`
    silently dropped everything past ~15k chars — the "video doesn't cover
    the whole chapter" bug. A chunk closes when EITHER budget would overflow:
    MAX_ANALYSIS_CHARS (the LLM context bound) or MAX_PART_WORDS (the
    lesson-duration bound — 1,950 words is exactly the 15-minute video the
    classroom design asks for). An oversized single unit goes through
    _hard_split, which honours both budgets and cuts between words.

    Returns [{"text", "section_titles", "words"}], always at least one.
    """
    sections = chapter_content.get("sections", [])
    units: list[tuple[str | None, str]] = []  # (section_title, block_text)

    for sec in sections:
        sec_title = sec.get("section_title", "")
        text = sec.get("content", "")
        units.append((sec_title, f"## {sec_title}\n\n{text}"))
        for sub in sec.get("subsections", []):
            sub_text = sub.get("content", "")
            units.append((sec_title, f"### {sub.get('section_title', '')}\n\n{sub_text}"))

    for box in chapter_content.get("key_boxes", []):
        box_text = box.get("content", "")
        units.append((None, f"[{box.get('type', 'box').upper()}: {box.get('title', '')}]\n\n{box_text}"))

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
        pieces = _hard_split(block, max_words)
        for piece in pieces:
            piece_words = len(piece.split())
            if cur_parts and (cur_len + len(piece) > MAX_ANALYSIS_CHARS or cur_words + piece_words > max_words):
                _flush()
            cur_parts.append(piece)
            if sec_title:
                cur_titles.append(sec_title)
            cur_len += len(piece) + 2
            cur_words += piece_words
    _flush()
    if not chunks:
        chunks = [{"text": "", "section_titles": [], "words": 0}]

    # Fold a RUNT tail back into the part before it.
    #
    # The hard split above cuts at a budget boundary, so unless a chapter's
    # text lands near a multiple of that budget the last chunk is whatever was
    # left over — measured at 3, 20, 37, 44, 87 and 172 words on realistic
    # prose, at every size tried. That runt was a full PART: its own
    # analysis call, its own script, its own slides, its own ~3-minute video
    # (the duration floor keeps it there), its own upload — and its own CREDIT,
    # since credit_ledger_sync resets a presentation's units to the number of
    # videos actually rendered. A teacher paid a credit for a video built from
    # twenty words.
    #
    # Merging is CONTENT-NEUTRAL — the text is concatenated into the previous
    # part, never dropped, which is the whole reason this is safe to do while
    # the surrounding work is about making sure nothing is lost. It also always
    # moves the credit count DOWN, never up.
    # _hard_split balances a single oversized unit so it cannot leave a runt,
    # but a chapter made of MANY small sections can still end on one. Merge only
    # when the result stays inside the teaching budget — absorbing a tail into
    # an already-full part would push that video back over 15 minutes, which is
    # the exact compression this file exists to prevent.
    if (len(chunks) > 1 and chunks[-1]["words"] < _MIN_TAIL_WORDS
            and chunks[-2]["words"] + chunks[-1]["words"] <= max_words):
        tail = chunks.pop()
        prev = chunks[-1]
        prev["text"] = f"{prev['text']}\n\n{tail['text']}".strip()
        prev["section_titles"] = list(dict.fromkeys(prev["section_titles"] + tail["section_titles"]))
        prev["words"] = len(prev["text"].split())
    return chunks


def run_full_analysis(
    book_id: str,
    chapter_content: dict,
    level: str = "middle_school",
    client: ClaudeClient | None = None,
    on_progress=None,
    chunks_override: list[dict] | None = None,
    language: str = "en",
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

    word_count = sum(
        len((sec.get("content", "") or "").split())
        + sum(len((sub.get("content", "") or "").split()) for sub in sec.get("subsections", []))
        for sec in chapter_content.get("sections", [])
    ) + sum(len((box.get("content", "") or "").split()) for box in chapter_content.get("key_boxes", []))
    # chunks_override: a single-part job passes ITS chunk verbatim — the text
    # already carries its "## title" prefixes, and re-chunking a chunk that
    # sits exactly at the char budget would split off a junk tail episode.
    chunks = chunks_override if chunks_override else build_chapter_parts(chapter_content)
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

    from shared.languages import prompt_directive

    lang_directive = prompt_directive(language)

    for part_idx, chunk in enumerate(chunks, start=1):
        part_title = chapter_title if total_parts == 1 else f"{chapter_title} (Part {part_idx} of {total_parts})"
        prompt = FULL_CHAPTER_ANALYSIS_PROMPT.format(
            chapter_title=part_title,
            level=level,
            word_count=chunk["words"],
            content=chunk["text"],
        ) + lang_directive

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
        # The classroom clamp, per PART — a long chapter
        # becomes Part 1..N of ~teachable length instead of one video that
        # only covers the front of the chapter.
        duration = min(LESSON_MAX_MINUTES,
                       max(LESSON_MIN_MINUTES,
                           round(chunk["words"] / NARRATION_WPM, 1)))
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
