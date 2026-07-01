"""
Agent 3: Script & Dialogue Generation.
Generates Socratic episode scripts from Agent 2 analysis output.
"""

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .models import (
    ChapterScripts,
    EpisodeScript,
    ScriptSegment,
    SegmentType,
    SketchCue,
    SlideVisual,
    VisualAssetRequest,
    VisualGroup,
    VisualItem,
)
from .prompts import build_episode_prompt, normalize_style

logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).parent.parent / "storage" / "scripts"

_VALID_VISUAL_ACTIONS = {"DRAW_START", "DRAW_CONTINUE", "GHOST_ONLY"}
_VALID_VISUAL_KINDS = {"flow", "cycle", "hierarchy", "compare", "icons"}


def _parse_slide_visual(data) -> Optional[SlideVisual]:
    """Validate + normalise Claude's slide_visual into a renderable spec, or None.

    Clamps node/group/item counts, degrades a too-short ``cycle`` to ``flow``, and
    rejects specs that can't render (e.g. a flow/icons with fewer than two entries)
    so the slide falls back to bullet points.
    """
    if not isinstance(data, dict):
        return None
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in _VALID_VISUAL_KINDS:
        return None

    nodes = [str(n).strip() for n in (data.get("nodes") or []) if str(n).strip()][:5]
    groups: list[VisualGroup] = []
    for g in (data.get("groups") or [])[:2]:
        if not isinstance(g, dict):
            continue
        gitems = [str(it).strip() for it in (g.get("items") or []) if str(it).strip()][:5]
        groups.append(VisualGroup(heading=str(g.get("heading") or "").strip()[:48], items=gitems))
    items: list[VisualItem] = []
    for it in (data.get("items") or [])[:6]:
        if not isinstance(it, dict):
            continue
        label = str(it.get("label") or "").strip()[:40]
        if label:
            items.append(VisualItem(icon=str(it.get("icon") or "").strip().lower()[:24], label=label))
    caption = str(data.get("caption") or "").strip()[:120]

    if kind == "icons":
        if len(items) < 2:
            return None
    elif kind == "compare":
        if not groups:
            return None
    else:
        if kind == "cycle" and len(nodes) < 3:
            kind = "flow"  # not enough steps for a ring — render as a process
        if len(nodes) < 2:
            return None

    return SlideVisual(kind=kind, nodes=nodes, groups=groups, items=items, caption=caption)


def _synthesize_sketch_cue(visual_request: Optional[VisualAssetRequest]) -> Optional[SketchCue]:
    """BRIDGE: Synthesize a backward-compatible sketch_cue from visual_request.

    Allows Agent 4 to continue reading sketch_cue until it is refactored
    for Nanobana Pro.  Remove when Agent 4 reads visual_request directly.
    """
    if visual_request is None:
        return None
    return SketchCue(
        action="draw",
        element=visual_request.prompt[:200],
        timing="during",
    )


def process_director_manifest(segments: List[ScriptSegment]) -> List[ScriptSegment]:
    """Post-process segments to enforce Scribe Director invariants.

    1. Forces visual_action=None for segments without a visual_request.
    2. Forces visual_action="GHOST_ONLY" for question_hook segments.
    3. Inserts a 300ms <break> at the start of elevenlabs_text for DRAW_START
       segments (hand travel time before the pen starts tracing).
    4. Synthesizes bridge sketch_cue from visual_request for Agent 4 compat.
    """
    travel_break = '<break time="0.3s"/>'

    for seg in segments:
        # No visual_request → no visual action, clear bridge
        if seg.visual_request is None:
            seg.visual_action = None
            seg.sketch_cue = None
            continue

        # Ensure bridge sketch_cue is populated
        if seg.sketch_cue is None:
            seg.sketch_cue = _synthesize_sketch_cue(seg.visual_request)

        # question_hook must be GHOST_ONLY
        if seg.type == SegmentType.question_hook:
            seg.visual_action = "GHOST_ONLY"
            continue

        # Validate visual_action; default to DRAW_START if invalid/missing
        if seg.visual_action not in _VALID_VISUAL_ACTIONS:
            seg.visual_action = "DRAW_START"

        # Insert travel-time break for DRAW_START segments
        if seg.visual_action == "DRAW_START":
            if not seg.elevenlabs_text.startswith(travel_break):
                seg.elevenlabs_text = travel_break + " " + seg.elevenlabs_text

    logger.debug(
        "Director manifest processed: %s",
        [(s.segment_id, s.visual_action) for s in segments],
    )
    return segments


def _build_episode_context(episode: dict, analysis: dict) -> str:
    """Build structured context string for the Claude prompt."""
    lines = [
        f"EPISODE: {episode.get('title', 'Episode 1')}",
        f"Target duration: {episode.get('estimated_duration_minutes', 5):.1f} minutes",
        f"Word count: {episode.get('estimated_word_count', 600):,}",
        f"Sections covered: {', '.join(episode.get('sections_covered', []))}",
        "",
    ]

    # Concepts relevant to this episode
    ep_concept_names = set(episode.get("key_concepts_introduced", []))
    all_concepts = analysis.get("concepts", {}).get("concepts", [])
    relevant_concepts = [
        c for c in all_concepts
        if c.get("name") in ep_concept_names
        or any(name in c.get("name", "") for name in ep_concept_names)
    ]

    if relevant_concepts:
        lines.append("KEY CONCEPTS TO TEACH (in order of introduction):")
        for c in relevant_concepts:
            importance = c.get("importance", "supporting")
            lines.append(f"  [{importance.upper()}] {c['name']}: {c.get('definition', '')}")
            related = c.get("related_concepts", [])
            if related:
                lines.append(f"    Related: {', '.join(related)}")
        lines.append("")

    # Visual opportunities for this episode
    ep_visual_ids = set(episode.get("visual_opportunities_in_episode", []))
    all_visuals = analysis.get("visual_opportunities", [])
    relevant_visuals = [v for v in all_visuals if v.get("opportunity_id") in ep_visual_ids]

    if relevant_visuals:
        lines.append("VISUAL OPPORTUNITIES (use these for visual_request prompts):")
        for v in relevant_visuals:
            lines.append(f"  [{v.get('opportunity_id', '')}] {v.get('title', '')}")
            lines.append(f"    What to draw: {v.get('description', '')}")
            lines.append(f"    Trigger text: \"{v.get('trigger_text', '')}\"")
            elements = v.get("sketch_elements", [])
            if elements:
                lines.append(f"    Sketch elements: {', '.join(elements)}")
        lines.append("")

    # Teaching notes — analogies and vocabulary
    diffs = analysis.get("difficulty_assessments", [])
    teaching_notes = []
    for d in diffs:
        analogies = d.get("suggested_analogies", [])
        vocab = d.get("vocabulary_load", "")
        if analogies:
            teaching_notes.append(f"  Analogies for '{d.get('section_title', '')}': {', '.join(analogies)}")
        if vocab:
            teaching_notes.append(f"  Vocabulary load: {vocab}")
        pacing = d.get("recommended_pacing", "")
        if pacing:
            teaching_notes.append(f"  Pacing advice: {pacing}")

    if teaching_notes:
        lines.append("TEACHING NOTES (use analogies in your Socratic questions):")
        lines.extend(teaching_notes)
        lines.append("")

    # Prerequisites
    prereqs = analysis.get("concepts", {}).get("prerequisites", [])
    if prereqs:
        lines.append("PRIOR KNOWLEDGE TO BUILD ON:")
        for p in prereqs:
            lines.append(f"  - {p.get('topic', '')} (grade: {p.get('assumed_grade', '')})")
        lines.append("")

    return "\n".join(lines)


def generate_episode_script(
    episode: dict,
    analysis: dict,
    chapter_num: int,
    client,
    narration_style: str = "socratic",
) -> EpisodeScript:
    """Generate a complete episode script via Claude in the chosen narration style.

    ``narration_style`` changes persona/arc/tone only — the emitted segment schema
    (and thus slide/video rendering) is identical for every style.
    """
    style = normalize_style(narration_style)
    episode_context = _build_episode_context(episode, analysis)
    target_duration = episode.get("estimated_duration_minutes", 5)

    prompt = build_episode_prompt(
        style,
        chapter_title=analysis.get("chapter_title", f"Chapter {chapter_num}"),
        difficulty_level=analysis.get("difficulty_level_requested", "middle_school").replace("_", " ").title(),
        target_duration=f"{target_duration:.1f}",
        episode_context=episode_context,
    )

    system = (
        "You are a master Socratic educator and script writer for SketchCast AI. "
        "You write engaging, question-driven educational scripts that guide students "
        "to discover ideas themselves. Always return valid JSON only."
    ) if style == "socratic" else (
        "You are a master educator and script writer for SketchCast AI. You write "
        "engaging educational scripts in the requested narration style, keeping the "
        "on-screen slide content and the JSON schema exactly as instructed. "
        "Always return valid JSON only."
    )
    result = client.analyze(prompt=prompt, system=system, max_tokens=8000)

    raw_segments = result.get("data", result).get("segments", [])

    segments = []
    for i, seg in enumerate(raw_segments):
        # Parse segment type
        seg_type_str = seg.get("type", "explore")
        try:
            seg_type = SegmentType(seg_type_str)
        except ValueError:
            seg_type = SegmentType.explore

        # Parse visual_request (Art Director image prompt)
        visual_request = None
        vr_data = seg.get("visual_request")
        if vr_data and isinstance(vr_data, dict) and vr_data.get("prompt"):
            visual_request = VisualAssetRequest(
                prompt=vr_data["prompt"],
                negative_prompt=vr_data.get(
                    "negative_prompt",
                    "color, shading, 3d, realistic, photo, gradient, "
                    "complex background, text, watermark",
                ),
                style_preset=vr_data.get("style_preset", "line-art"),
            )

        # Bridge: synthesize sketch_cue for Agent 4 backward compatibility
        sketch_cue = _synthesize_sketch_cue(visual_request)

        plain_text = seg.get("text", "")
        el_text = seg.get("elevenlabs_text") or plain_text

        # Parse visual_action from Claude output
        raw_va = seg.get("visual_action")
        visual_action = raw_va if raw_va in _VALID_VISUAL_ACTIONS else None

        slide_points = [
            str(p).strip()
            for p in (seg.get("slide_points") or [])
            if str(p).strip()
        ]
        slide_visual = _parse_slide_visual(seg.get("slide_visual"))

        segments.append(ScriptSegment(
            segment_id=f"s{i + 1:03d}",
            type=seg_type,
            text=plain_text,
            elevenlabs_text=el_text,
            slide_heading=(seg.get("slide_heading") or "").strip(),
            slide_points=slide_points,
            slide_visual=slide_visual,
            visual_request=visual_request,
            sketch_cue=sketch_cue,
            visual_action=visual_action,
            pause_for_question=bool(seg.get("pause_for_question", False)),
            estimated_duration_seconds=int(seg.get("estimated_duration_seconds", 30)),
        ))

    # Post-process: enforce Scribe Director invariants + travel-time breaks
    segments = process_director_manifest(segments)

    total_duration = sum(s.estimated_duration_seconds for s in segments)
    question_hook_count = sum(1 for s in segments if s.type == SegmentType.question_hook)

    return EpisodeScript(
        script_id=str(uuid.uuid4()),
        book_id=analysis.get("book_id", ""),
        chapter_num=chapter_num,
        episode_num=episode.get("episode_num", 1),
        episode_title=episode.get("title", "Episode 1"),
        generated_at=datetime.now().isoformat(),
        narrator_persona=style,
        segments=segments,
        total_estimated_duration_seconds=total_duration,
        question_hook_count=question_hook_count,
    )


def generate_chapter_scripts_from_analysis(
    book_id: str,
    chapter_num: int,
    analysis_dict: dict,
    client,
    narration_style: str = "socratic",
) -> ChapterScripts:
    """Generate scripts for all episodes using an already-loaded analysis dict.

    This is the primary entry point used by the worker (in-process).
    ``narration_style`` selects the persona/arc; the segment schema is unchanged.
    """
    episodes = analysis_dict.get("episodes", {}).get("episodes", [])
    chapter_title = analysis_dict.get("chapter_title", f"Chapter {chapter_num}")

    episode_scripts = []
    for episode in episodes:
        script = generate_episode_script(episode, analysis_dict, chapter_num, client, narration_style)
        episode_scripts.append(script)
        save_script(script)

    return ChapterScripts(
        book_id=book_id,
        chapter_num=chapter_num,
        chapter_title=chapter_title,
        total_episodes=len(episode_scripts),
        generated_at=datetime.now().isoformat(),
        episodes=episode_scripts,
    )


def generate_chapter_scripts(
    book_id: str,
    chapter_num: int,
    client,
) -> Optional[ChapterScripts]:
    """Load analysis from disk and generate scripts. Used by FastAPI endpoint."""
    from agent2_analysis.analyzer import load_analysis

    analysis = load_analysis(book_id, chapter_num)
    if not analysis:
        return None

    analysis_dict = analysis.model_dump() if hasattr(analysis, "model_dump") else analysis
    return generate_chapter_scripts_from_analysis(book_id, chapter_num, analysis_dict, client)


def save_script(script: EpisodeScript):
    """Persist an episode script to disk."""
    script_dir = STORAGE_DIR / script.book_id
    script_dir.mkdir(parents=True, exist_ok=True)
    path = script_dir / f"chapter_{script.chapter_num}_episode_{script.episode_num}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(script.model_dump(), f, indent=2, ensure_ascii=False)


def load_script(book_id: str, chapter_num: int, episode_num: int) -> Optional[dict]:
    """Load a script from disk. Returns dict or None."""
    path = STORAGE_DIR / book_id / f"chapter_{chapter_num}_episode_{episode_num}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
