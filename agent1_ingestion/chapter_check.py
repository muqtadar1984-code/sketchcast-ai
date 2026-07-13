"""Chapter sanity checks backed by one cheap Claude call.

Two moments need a "does the text match the label?" judgement:

* **index time** — before a detected chapter list is stored, sample every
  chapter's opening text and let Claude flag garbled titles and wrong starts
  (and supply the real title when the page shows it). Too many mismatches →
  the caller escalates to the LLM/vision detector instead of storing junk.
* **generation time** — before the full pipeline spends money on a chapter,
  confirm the sliced text actually belongs to the requested title, failing
  loud instead of teaching a different unit (a real, user-reported failure:
  heuristic titles came from unit-opener blurbs, so "Storage" rendered a
  lesson about hardware selection).

Both checks are best-effort: an API hiccup must never break indexing or a
generation on its own, so callers treat errors as "no opinion".
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Strip a bare "Unit 3 / Chapter 3 / Lesson Three" prefix to expose the DESCRIPTIVE
# topic that follows (if any). "Unit 3" → "" (generic, nothing to verify); but
# "Unit 3: Computer storage" → "Computer storage" (a real topic that MUST be
# verified against the page text, or a scanned-book mislabel ships the wrong unit).
_GENERIC_PREFIX = re.compile(r"^\s*(chapter|unit|lesson|topic|module|section)\s+[\w-]+\s*[:.\-–—]?\s*", re.I)

_SNIPPET_CHARS = 320
_VERIFY_CHARS = 1400


def _page_snippet(extraction, page: int, chars: int = _SNIPPET_CHARS) -> str:
    """First words of a page (and its neighbour) in reading order."""
    txt = " ".join(
        it.text for it in extraction.items if it.page_num in (page, page + 1)
    )
    return " ".join(txt.split())[:chars]


def audit_chapter_list(extraction, chapters: list[dict], client) -> dict:
    """One Claude call over every chapter's title + opening snippet.

    Returns ``{"mismatched": [nums], "titles": {num: better_title}}``.
    ``mismatched`` = chapters whose opening text clearly does NOT start a unit
    matching the title (wrong boundary or meaningless title). ``titles`` =
    corrections where the page itself shows the real unit name.
    """
    if client is None or len(chapters) < 2:
        return {"mismatched": [], "titles": {}}

    lines = []
    for c in chapters:
        n = int(c.get("num", c.get("chapter_num", 0)))
        snippet = _page_snippet(extraction, int(c.get("start_page", 0)))
        lines.append(f'#{n} title={c.get("title")!r} opening_text={snippet!r}')

    prompt = (
        "You are auditing the detected chapter list of a school textbook. For each "
        "entry you get the detected TITLE and the OPENING TEXT of the page where the "
        "chapter supposedly starts (running headers/footers may be mixed in).\n\n"
        "Flag an entry as mismatched when the title is clearly NOT a usable label for "
        "a teaching unit starting there: the opening text belongs to a different "
        "topic, or the title is a garbled fragment (a heading glued to body prose). "
        "Generic titles like 'Chapter 3' are acceptable — do not flag those. When the "
        "opening text itself shows the real unit name (e.g. 'Unit 3: Selecting "
        "hardware and software'), supply it as better_title.\n\n"
        "Respond ONLY as JSON: {\"issues\": [{\"num\": <int>, "
        "\"better_title\": <string or null>}]} — entries that are fine are omitted.\n\n"
        + "\n".join(lines)
    )
    data = client.analyze(prompt, max_tokens=1000).get("data") or {}
    issues = data.get("issues") or []
    mismatched: list[int] = []
    titles: dict[int, str] = {}
    for i in issues:
        if not isinstance(i, dict):
            continue
        try:
            n = int(i.get("num"))
        except (TypeError, ValueError):
            continue
        mismatched.append(n)
        better = (i.get("better_title") or "").strip()
        if better and len(better) <= 120:
            titles[n] = better
    return {"mismatched": mismatched, "titles": titles}


def verify_chapter_content(title: str, text: str, client) -> tuple[bool, str]:
    """Generation-time guard: does this chapter text plausibly belong under
    ``title``? Returns ``(ok, actual_topic)``. Permissive by design — only a
    CLEAR different-topic mismatch fails; generic titles always pass; any API
    problem counts as "no opinion" (ok)."""
    title = (title or "").strip()
    text = " ".join((text or "").split())[:_VERIFY_CHARS]
    # Skip only when there's genuinely nothing to verify: no text, the whole-book
    # fallback, or a GENERIC label ("Unit 3", "Chapter 3") once its prefix is
    # stripped. A descriptive label ("Unit 3: Computer storage") keeps its topic
    # and IS verified — this is the guard that must catch a scanned-book mislabel.
    topic = _GENERIC_PREFIX.sub("", title).strip()
    if client is None or not text or not topic or title.lower().startswith("full document"):
        return True, ""
    try:
        prompt = (
            "A lesson is about to be generated from a textbook chapter. Confirm the "
            f"chapter text matches its label.\n\nLabel: {title!r}\n\nChapter text "
            f"(opening): {text!r}\n\n"
            "Answer mismatch ONLY when the text is confidently about a DIFFERENT "
            "topic than the label — partial overlap, sub-topics, intros and activity "
            "prose under a matching theme are all a match. Respond ONLY as JSON: "
            "{\"match\": true|false, \"actual_topic\": \"<=8 words\"}"
        )
        data = client.analyze(prompt, max_tokens=200).get("data") or {}
        if data.get("match") is False:
            return False, str(data.get("actual_topic") or "a different topic")[:80]
        return True, ""
    except Exception as exc:  # noqa: BLE001 — the guard must never be the failure
        logger.warning("chapter verify skipped (%s)", exc)
        return True, ""
