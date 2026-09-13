"""The book chapter as an ARTICLE — the deck's source on the teacher path.

Until 2026-09-12 a book deck was authored as slides (`deck_notes.author_deck`)
and its slide prose fell back to the model's teacher notes whenever a slide
carried a visual instead of points: five slides of the founder's Materials
deck were paragraphs of narration. The catalogue deck never had that problem
because it is built from an article — objectives, sections, one-sentence
claims that become the points, a glossary, misconceptions, worked examples
and figure specs with named parts — through `shared.lesson_model.from_article`
and the same storyboard.

This module authors THAT shape from the chapter part's own text, in one call
on the deck's artifact model, and validates it with the catalogue's own
validator (`catalogue.article.validate_article`) so a book deck and a
catalogue deck are the same object from here on. The reply's prose goes to
the speaker notes; what a class sees is the claims, the glossary, the
diagrams — never a paragraph.

The grounding block is `docgen.docx_builder.chapter_grounding`, byte-identical
across a chapter's artifacts, so the prompt cache the documents wrote is
re-read here exactly as `author_deck` re-read it.
"""
from __future__ import annotations

import logging
from typing import Optional

from catalogue.article import (MAX_PARTS_PER_FIGURE, MAX_TOKENS, RESPONSE_SCHEMA,
                               SYSTEM_PROMPT, ArticleInvalid, validate_article)
from docgen.docx_builder import chapter_grounding
from shared.languages import prompt_directive

logger = logging.getLogger("worker.deck_article")

# A chapter PART is shorter than a catalogue topic: the catalogue asks for
# 900-1600 words over 4-8 sections; a part of a chapter is taught in 3-6.
# Below the floor the reply is a refusal, a truncation or a stub.
WORDS_MIN, WORDS_MAX = 600, 1400
WORDS_FLOOR = 200

PROMPT = f"""TASK: from the chapter above, write the complete explanation a strong teacher would give of THIS chapter (or this part of it), in the form of a knowledge article. The article is the source of the slide deck the teacher projects: its claims become the points on the slides, its glossary the vocabulary slide, its figures the diagrams. It must be complete, correct and self-contained, and it must teach what the chapter teaches — at the chapter's own depth, in the chapter's own order, and nothing the chapter does not cover.

RULES
1. Teach at the depth of the chapter above: the same ideas, the same level, the same terms the book uses. Say in "depth_rationale" (one sentence) what the chapter covers and for whom.
2. Length {WORDS_MIN}-{WORDS_MAX} words of body text across 3-8 sections, following the chapter's order. Each section has a SHORT heading (under 8 words) and a "body_md" in markdown: paragraphs, bullet lists, bold key terms, simple tables where they help. No images, no HTML, no links. The body is the teacher's notes; it is never shown on a slide.
3. Write the learning objectives in "objectives" (ids "o1", "o2", ...), 2-5 of them, one sentence each, in the form a teacher would put on the board.
4. Define every key term the chapter uses in "glossary" as {{term, definition}} — a definition is one plain sentence a learner can quote (under 25 words).
5. Give 2-5 common misconceptions in "misconceptions" (ids "m1", ...), each as {{misconception, correction}}: what learners wrongly believe, and the correct idea with its reason.
6. Give 1-3 worked examples in "worked_examples" (ids "w1", ...) where the subject allows — a calculation, an application, a step-by-step classification — each as {{problem, solution_md}}. Where the subject has none, return an empty list.
7. "claims" are THE SLIDES' BULLET POINTS. For EVERY section list 2-5 claims (ids "c1", ...) as {{text, section_id}}: each a SHORT, complete sentence of at most 14 words stating one fact, rule or definition the section teaches. Precise and checkable — questions will be written from them. Never a question, never a narration line ("Now let us look at..."), never a heading repeated.
8. Plan 2-5 figures in "figures". Each is a labelled whiteboard diagram: {{figure_key, caption, spec: {{subject, parts, style, notes}}}} where "figure_key" is a short snake_case identity ("particle_states", "water_cycle"), "caption" is a short title (under 8 words), "subject" says what is drawn, "parts" lists the parts the diagram must show so they can be labelled (2-{MAX_PARTS_PER_FIGURE} short names, singular, no articles), "style" is "whiteboard diagram", and "notes" are drawing instructions. Reference a figure from the section that uses it by putting its figure_key in that section's "figure_keys". Never plan a figure no section uses. Prefer the diagrams the chapter itself shows.
9. Explain in your own words at the chapter's level; use the chapter's own terms and examples. No first person and no address to the reader ("I", "we", "you"). No exclamation marks, no filler, no closing summary of what was said.
10. The reply is JSON only, exactly this shape:
{{"title": "...", "objectives": [{{"id": "o1", "text": "..."}}], "sections": [{{"id": "s1", "heading": "...", "body_md": "...", "figure_keys": ["particle_states"], "covers": []}}], "glossary": [{{"term": "...", "definition": "..."}}], "misconceptions": [{{"id": "m1", "misconception": "...", "correction": "..."}}], "worked_examples": [{{"id": "w1", "problem": "...", "solution_md": "..."}}], "claims": [{{"id": "c1", "text": "...", "section_id": "s1"}}], "figures": [{{"figure_key": "particle_states", "caption": "...", "spec": {{"subject": "...", "parts": ["solid", "liquid", "gas"], "style": "whiteboard diagram", "notes": "..."}}}}], "depth_rationale": "..."}}"""

JAWI_RULE = (
    "\n\nLANGUAGE — this is a JAWI deck, written in TWO scripts:\n"
    "• every section's `body_md`, every `solution_md` and `depth_rationale` (what the teacher "
    "SAYS while the slide is up): write in RUMI (Latin) Malay — ordinary Bahasa Melayu.\n"
    "• EVERY field a class will READ — `title`, `heading`, each objective, each claim, each "
    "glossary term and definition, each misconception and correction, each worked-example "
    "problem, each figure caption and part name: write in the JAWI script (the Arabic-derived "
    "script for Malay), using the Jawi-specific letters where they belong (چ ڠ ڤ ݢ ۏ ڽ).\n"
    "The on-screen Jawi and the spoken Rumi are the SAME Malay words in two scripts."
)


def build_article_prompt(language: Optional[str] = "en") -> str:
    """The authoring prompt in `language`. The language directive is the one
    docgen appends to every document prompt; Jawi swaps in its two-script
    rule (the notes are spoken, the slides are read)."""
    if language == "ms-arab":
        return PROMPT + JAWI_RULE
    return PROMPT + prompt_directive(language)


def _reply_payload(reply) -> object:
    if isinstance(reply, dict) and "data" in reply:
        return reply.get("data")
    return reply


def author_article(book: dict, chapter: dict, analysis: dict, client, params: dict,
                   language: Optional[str] = "en", title: str = "") -> dict:
    """One authoring call → the article dict `from_article` consumes:
    ``{title, language, objectives, sections, glossary, misconceptions,
    worked_examples, claims, figures}``. Raises ``RuntimeError`` when the
    reply cannot become an article — a deck is the whole artifact of its
    job, so a thin reply fails the job rather than shipping three slides."""
    grounding = chapter_grounding(book, chapter, analysis)
    prompt = build_article_prompt(language)
    reply = client.analyze(prompt, system=SYSTEM_PROMPT, max_tokens=MAX_TOKENS,
                           cache_prefix=grounding, response_schema=RESPONSE_SCHEMA)
    raw = _reply_payload(reply)
    try:
        art = validate_article(raw, coverage_codes=[], fallback_title=title or str(chapter.get("title") or ""),
                               words_floor=WORDS_FLOOR)
    except ArticleInvalid as exc:
        raise RuntimeError(f"deck article authoring: {exc}") from exc
    for line in art.repairs:
        logger.info("deck article repair: %s", line)
    logger.info("deck article authored: %d sections, %d claims, %d figures, %d words — %s",
                len(art.sections), len(art.claims), len(art.figures), art.word_count, art.title)
    return {
        "id": None,
        "title": art.title,
        "language": (language or "en").lower(),
        "objectives": art.objectives,
        "sections": art.sections,
        "glossary": art.glossary,
        "misconceptions": art.misconceptions,
        "worked_examples": art.worked_examples,
        "claims": art.claims,
        "figures": art.figures,
        "depth_rationale": art.depth_rationale,
        "word_count": art.word_count,
    }


def coverage_segments(article: dict) -> list[dict]:
    """The article as script-shaped segments, for `coverage.script_text`: a
    heading, the section's claims as points, its prose as the text."""
    claims: dict[str, list[str]] = {}
    for c in article.get("claims") or []:
        if isinstance(c, dict) and c.get("text"):
            claims.setdefault(str(c.get("section_id") or ""), []).append(str(c["text"]))
    out = []
    for s in article.get("sections") or []:
        if not isinstance(s, dict):
            continue
        out.append({"slide_heading": str(s.get("heading") or ""),
                    "slide_points": claims.get(str(s.get("id") or ""), []),
                    "text": str(s.get("body_md") or "")})
    return out
