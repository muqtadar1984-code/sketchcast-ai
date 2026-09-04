"""The slide deck's OWN authoring call — the "study notes" the video dropped.

Until 2026-09 the deck was a by-product of the presentation job: every video
segment carried ``slide_heading`` / ``slide_points`` / ``slide_visual`` and the
deck embedded one rendered slide per segment. The semantic video prompt no
longer asks for those fields (founder decision 2026-09-02: the deck and the
video are generated independently), so a deck built from the video script is
heading-plus-glyph with no bullets at all. This module is the replacement: one
model call, grounded on the same chapter block every other artifact uses, that
returns a DECK — slides in teaching order, each with a heading, 2-4 points or a
diagram, and speaker notes a teacher reads while the slide is up.

The output is normalised into SCRIPT-SHAPED segments so the existing slide and
deck code (``slide_generator.generate_episode_slides`` → ``compose_slide`` →
``build_episode_deck``) is reused unchanged: a deck slide *is* a segment whose
narration is the teacher's notes.
"""

from __future__ import annotations

import logging

from agent3_scripts.script_generator import _parse_slide_visual
from docgen.docx_builder import chapter_grounding
from shared.languages import prompt_directive

logger = logging.getLogger(__name__)

DEFAULT_SLIDES = 10
MIN_SLIDES = 6
MAX_SLIDES = 16
# Fewer usable slides than this is a failed authoring call, not a short deck —
# fail loud (the zero-segment rule of the script path), never ship a stub.
MIN_SURVIVING = 3

# Lifted from the legacy Agent-3 STUDY-NOTES + slide_visual catalogue
# (agent3_scripts/prompts.py) — the wording the renderer was tuned against —
# but asked for as a DECK rather than a narrated script. ``{n}`` is replaced
# at build time (the JSON example below has its own braces, so no str.format).
PROMPT = """You are writing the SLIDE DECK a teacher projects while teaching the chapter above.
This deck is NOT a narration transcript: each slide shows the TEXTBOOK CHAPTER CONTENT in
brief — the study notes a student reads on screen — and its speaker notes are what the
teacher says while that slide is up.

Produce exactly {n} slides in teaching order, first = the chapter's big question, last = takeaways.

For EVERY slide, produce:
- "heading": a short on-screen title (3-7 words) naming the concept or topic this slide covers.
- "points": 2-4 SHORT bullet points (each under ~12 words) stating the KEY FACTS / IDEAS /
  DEFINITIONS from the chapter that this slide teaches. Concise, factual, drawn from the
  chapter material — the things a student should remember. These are NOT sentences of narration.
- "visual": an on-screen FORMAT object (see below) or null.
- "notes": 60-120 words a teacher says while this slide is up — plain prose, no markup.

Rules:
- points must be factual chapter content, not questions or narration lines.
- The opening slide (the big question) may carry a single short framing line as its only
  point, or an empty list; every other slide teaches something.

=== ON-SCREEN FORMAT (visual) — CHOOSE THE BEST ONE PER SLIDE ===
Plain bullet points are the LAST resort, not the default. For each teaching slide, pick the
on-screen FORMAT that best fits the idea and put it in the "visual" object INSTEAD of points
(you may still give a short heading). Keep every label SHORT (2-5 words). At most one visual
per slide.

** ANTI-MONOTONY RULE (important): NEVER put plain points on more than TWO slides in a row.
A deck that is all bullet lists has failed. Vary the rhythm — e.g. a diagram, then a
definition, then a quick check, then a comparison. **

STRUCTURAL DIAGRAMS — the shape of an idea:
- "flow"      — a process / sequence / cause→effect chain. "nodes": 2-5 ordered steps.
- "cycle"     — a repeating loop (water cycle, feedback). "nodes": 3-5 stages (loops back).
- "hierarchy" — classification / part-whole. "nodes": [root, child1, child2, ...] (2-4 children).
- "compare"   — contrast two things. "groups": exactly 2 objects, each {"heading", "items":[2-4 short]}.
- "icons"     — 2-6 key items/terms/examples, each shown as an icon + short label.
                "items": [{"icon": "<name>", "label": "<2-4 words>"}, ...]
                Choose each icon name from this catalogue (pick the closest meaning;
                an unknown name just shows a neutral mark):
                idea, book, search, target, globe, check, cross, star, heart, sun, cloud,
                rain, snow, gear, scale, music, flower, sparkle, atom, recycle, warning,
                pencil, arrow, clock, phone, flag, scissors, airplane, anchor, crown,
                infinity, bolt, sum, sqrt, plus, question

CONTENT LAYOUTS — fill the slide instead of a few thin bullets:
- "definition" — introduce ONE key term. Put the TERM in "heading" and its plain-language
                 meaning (one sentence, under 25 words) in "body". Use this for EVERY
                 important vocabulary word.
- "quiz"       — a quick comprehension check. Put the QUESTION in "heading", give 2-4 short
                 "options", and set "answer" to the 0-based index of the correct option.
                 Add one every few slides to keep students active.
- "takeaways"  — a recap / summary. 2-4 short "nodes", each one key point to remember.
                 Use this for the closing slide instead of bullets.

Optional "caption": one short line under a structural diagram.
Only fall back to plain "points" when NONE of the above fits — and never twice in a row.

=== OUTPUT FORMAT ===
Return ONLY valid JSON — no preamble, no markdown fences, no explanation:
{"title": "...", "slides": [{"heading": "...", "points": ["..."], "visual": {...}|null, "notes": "60-120 words a teacher says while this slide is up"}]}
"""

# Jawi deck — same two-script rule the video uses (agent3_scripts/
# script_generator): the on-screen text is Jawi, the notes the teacher reads
# may stay Rumi. prompt_directive("ms-arab") would demand Jawi everywhere,
# which is wrong for the notes, so the deck installs this rule instead.
JAWI_RULE = (
    "\n\nLANGUAGE — this is a JAWI deck, written in TWO scripts:\n"
    "• `notes` (what the teacher SAYS while the slide is up): write in RUMI (Latin) "
    "Malay — ordinary Bahasa Melayu.\n"
    "• EVERY on-screen field — `heading`, each `points` entry, and the `visual` "
    "heading(s), items, nodes, body, options and caption — write in the JAWI script (the "
    "Arabic-derived script for Malay), using the Jawi-specific letters where they "
    "belong (چ ڠ ڤ ݢ ۏ ڽ).\n"
    "The on-screen Jawi and the spoken Rumi are the SAME Malay words in two "
    "scripts. Do not mix them: notes stay Rumi, on-screen stays Jawi."
)


def clamp_slides(raw) -> int:
    """``params.num_slides`` → a deck length the renderer and the model both
    handle: 6..16, default 10, garbage → default."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = DEFAULT_SLIDES
    return max(MIN_SLIDES, min(MAX_SLIDES, n))


def build_deck_prompt(n: int, language: str | None = "en") -> str:
    """The authoring prompt for an ``n``-slide deck in ``language``. The
    language directive is the same one docgen appends to every document
    prompt; Jawi swaps in its two-script rule."""
    prompt = PROMPT.replace("{n}", str(int(n)))
    if language == "ms-arab":
        return prompt + JAWI_RULE
    return prompt + prompt_directive(language)


def _normalise_slide(i: int, raw) -> dict | None:
    """One model slide → one script-shaped segment, or None when there is
    nothing to put on a slide."""
    if not isinstance(raw, dict):
        return None
    heading = str(raw.get("heading") or "").strip()
    points = [str(p).strip() for p in (raw.get("points") or []) if str(p).strip()][:4]
    # The SAME validation the video path applies: clamps counts, degrades a
    # 2-step cycle to a flow, rejects specs that cannot render (→ bullets).
    visual = _parse_slide_visual(raw.get("visual"))
    notes = str(raw.get("notes") or "").strip()
    if not heading and not points and visual is None:
        return None
    return {
        "segment_id": f"d{i:03d}",
        "type": "explore",
        # The teacher's notes ride where the narration rode: compose_slide's
        # fallback text and the .pptx speaker notes both read `text`.
        "text": notes,
        "elevenlabs_text": notes,
        "slide_heading": heading,
        "slide_points": points,
        "slide_visual": visual.model_dump() if visual is not None else None,
        "estimated_duration_seconds": 30,
    }


def author_deck_slides(book: dict, chapter: dict, analysis: dict, client, params: dict,
                       language: str | None = "en") -> list[dict]:
    """Author the deck's slides for one chapter (or part) and return them as
    script-shaped segments for ``generate_episode_slides``.

    Grounding is ``chapter_grounding(book, chapter, analysis)`` — byte-identical
    across a chapter's artifacts, so the 1h prompt cache the documents wrote is
    re-read here. Raises when fewer than ``MIN_SURVIVING`` slides come back
    usable: a deck is the whole artifact of its job, so a thin reply must fail
    the job rather than ship two slides.
    """
    n = clamp_slides((params or {}).get("num_slides", DEFAULT_SLIDES))
    grounding = chapter_grounding(book, chapter, analysis)
    prompt = build_deck_prompt(n, language)
    data = client.analyze(prompt, max_tokens=8192, cache_prefix=grounding).get("data") or {}
    raw_slides = data.get("slides") if isinstance(data, dict) else None
    if not isinstance(raw_slides, list):
        raw_slides = []
    slides: list[dict] = []
    for raw in raw_slides:
        seg = _normalise_slide(len(slides) + 1, raw)
        if seg is not None:
            slides.append(seg)
    if len(slides) < MIN_SURVIVING:
        raise RuntimeError(f"deck authoring returned {len(slides)} slides")
    title = str(data.get("title") or "").strip() if isinstance(data, dict) else ""
    logger.info("deck authored: %d slides (asked %d)%s", len(slides), n,
                f" — {title}" if title else "")
    return slides
