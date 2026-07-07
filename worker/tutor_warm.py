"""Warm the AI Tutor's answer cache at lesson-generation time (M6).

When a lesson is generated we already have the chapter's concept analysis and the
narration script. Spend ONE cheap Claude call to anticipate the handful of
questions a curious student is most likely to ask, answer them in the tutor's own
closed-book style, and bank them in ``tutor_qa`` (marked verified). The FIRST
student to ask a common question then gets an instant, $0 reply instead of paying
for a fresh generation — and every reply is pre-vetted for the same safety fence.

Gated by ``TUTOR_WARM_CACHE=true`` and fully best-effort: any failure here must
never affect the lesson that was generated.
"""

from __future__ import annotations

import logging
import os
import re

from . import client as db

logger = logging.getLogger("worker")

_MAX_CTX = 8000  # cap concept/script text fed into the prompt

# Mirror of the app tutor's voice/safety so seeded answers match live ones
# (src/utils/tutor/models.ts buildSystemPrompt). Closed-book, warm, brief.
_SYSTEM = (
    "You are 'Coach', a warm, encouraging tutor for a school student. You write "
    "short, grounded answers using ONLY the chapter material you are given — never "
    "outside knowledge, never anything unsafe or off-topic. You never do graded "
    "work for the student. Output STRICT JSON only."
)


def _normalize_question(q: str) -> str:
    """Match src/utils/tutor/models.ts normalizeQuestion exactly so a seeded
    question and the same question typed by a student map to one cache key."""
    q = (q or "").lower()
    q = re.sub(r"\s+", " ", q)
    q = re.sub(r"[?!.\s]+$", "", q)
    return q.strip()


def _build_prompt(chapter_title: str, concepts: dict, script_text: str | None, n: int) -> str:
    concepts_str = ""
    try:
        import json

        concepts_str = json.dumps(concepts)[:_MAX_CTX]
    except Exception:  # noqa: BLE001
        concepts_str = str(concepts)[:_MAX_CTX]
    script_str = (script_text or "")[:_MAX_CTX]
    return (
        f'CHAPTER: "{chapter_title}"\n\n'
        f"CONCEPTS:\n{concepts_str}\n\n"
        f"LESSON NARRATION:\n{script_str}\n\n"
        f"Anticipate the {n} questions a curious student is MOST likely to ask about "
        f'this chapter. Answer each ONLY from the material above, in Coach\'s voice: '
        f"2-4 short sentences, plain language, no markdown, and where it helps end with "
        f"a small question that nudges the student to think. Do NOT invent facts beyond "
        f"the material. Return a JSON array of objects with exactly these keys: "
        f'"question" (what a student would type) and "answer". No other text.'
    )


def generate_seed_qa(
    client,
    chapter_title: str,
    concepts: dict,
    script_text: str | None,
    n: int = 8,
) -> list[dict]:
    """Ask Claude for the top-N likely questions + grounded answers. Returns a
    list of {"question_text", "question_norm", "answer_text"} ready to bank."""
    out = client.analyze(
        prompt=_build_prompt(chapter_title, concepts, script_text, n),
        system=_SYSTEM,
        max_tokens=1800,
    )
    data = out.get("data")
    items = data if isinstance(data, list) else (data or {}).get("questions", [])
    rows: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        q = str(it.get("question") or "").strip()
        a = str(it.get("answer") or "").strip()
        norm = _normalize_question(q)
        if q and a and norm:
            rows.append({"question_text": q, "question_norm": norm, "answer_text": a})
    return rows


def warm_tutor_cache(
    sb,
    client,
    book_id: str,
    chapter_num: int,
    chapter_title: str,
    concepts: dict,
    script_text: str | None,
) -> int:
    """Generate + bank seed Q&A for a chapter. Gated + best-effort; returns the
    number of NEW rows banked (0 when disabled or on any failure)."""
    if os.getenv("TUTOR_WARM_CACHE") != "true":
        return 0
    try:
        rows = generate_seed_qa(client, chapter_title, concepts, script_text)
        if not rows:
            return 0
        banked = db.insert_tutor_seed_qa(sb, book_id, chapter_num, rows)
        logger.info("tutor warm-cache: banked %d seed Q&A for %s ch%s", banked, book_id, chapter_num)
        return banked
    except Exception as exc:  # noqa: BLE001
        logger.warning("tutor warm-cache skipped for %s ch%s: %s", book_id, chapter_num, exc)
        return 0
