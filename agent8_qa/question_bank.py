"""Question bank — SQLite-backed Q&A storage with fuzzy matching."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz

from agent8_qa.models import BankedQA, BankStats

logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
BANK_DIR = STORAGE_DIR / "question_bank"
SIMILARITY_THRESHOLD = 85  # rapidfuzz score threshold for cache hit


def _bank_path(book_id: str, chapter_num: int) -> Path:
    """Path to the JSON-based question bank for a chapter."""
    return BANK_DIR / book_id / f"chapter_{chapter_num}_bank.json"


def _load_bank(book_id: str, chapter_num: int) -> list[dict]:
    """Load all Q&A pairs for a chapter."""
    path = _bank_path(book_id, chapter_num)
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_bank(book_id: str, chapter_num: int, bank: list[dict]) -> None:
    """Persist the question bank to disk."""
    path = _bank_path(book_id, chapter_num)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(bank, f, indent=2, ensure_ascii=False)


def find_similar(
    question: str,
    book_id: str,
    chapter_num: int = 0,
    threshold: int = SIMILARITY_THRESHOLD,
) -> BankedQA | None:
    """
    Search the question bank for a similar question.

    Uses rapidfuzz fuzzy matching. Returns the best match above threshold,
    or None if no match found.
    """
    bank = _load_bank(book_id, chapter_num)
    if not bank:
        return None

    best_score = 0
    best_entry = None

    for entry in bank:
        score = fuzz.ratio(question.lower(), entry["question_text"].lower())
        if score > best_score:
            best_score = score
            best_entry = entry

    if best_score >= threshold and best_entry is not None:
        # Update usage count and last_used_at
        best_entry["usage_count"] = best_entry.get("usage_count", 0) + 1
        best_entry["last_used_at"] = datetime.now(timezone.utc).isoformat()
        if best_entry["usage_count"] >= 10:
            best_entry["is_verified"] = True
        _save_bank(book_id, chapter_num, bank)

        logger.info(
            "Question bank hit (%.0f%%): '%s' matched '%s'",
            best_score, question[:50], best_entry["question_text"][:50],
        )
        return BankedQA(**best_entry)

    return None


def save_qa(
    question: str,
    answer: str,
    book_id: str,
    chapter_num: int,
    segment_id: str = "",
    tokens_used: int = 0,
) -> BankedQA:
    """Save a new Q&A pair to the bank."""
    bank = _load_bank(book_id, chapter_num)

    entry = {
        "id": str(uuid.uuid4()),
        "book_id": book_id,
        "chapter_num": chapter_num,
        "segment_id": segment_id,
        "question_text": question,
        "answer_text": answer,
        "usage_count": 1,
        "is_verified": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_used_at": datetime.now(timezone.utc).isoformat(),
        "tokens_used": tokens_used,
    }
    bank.append(entry)
    _save_bank(book_id, chapter_num, bank)

    logger.info("Banked new Q&A: '%s' → '%s...'", question[:50], answer[:50])
    return BankedQA(**entry)


def get_all(book_id: str, chapter_num: int) -> list[BankedQA]:
    """Get all banked Q&A pairs for a chapter."""
    bank = _load_bank(book_id, chapter_num)
    return [BankedQA(**e) for e in bank]


def get_stats(book_id: str, chapter_num: int = 0) -> BankStats:
    """Get question bank stats for a book (or specific chapter)."""
    chapters_to_check = []
    if chapter_num > 0:
        chapters_to_check = [chapter_num]
    else:
        # Check all chapter banks for this book
        book_dir = BANK_DIR / book_id
        if book_dir.exists():
            for p in book_dir.glob("chapter_*_bank.json"):
                try:
                    ch = int(p.stem.split("_")[1])
                    chapters_to_check.append(ch)
                except (IndexError, ValueError):
                    pass

    total_q = 0
    verified_q = 0
    total_served = 0
    total_tokens_saved = 0

    for ch in chapters_to_check:
        bank = _load_bank(book_id, ch)
        for entry in bank:
            total_q += 1
            if entry.get("is_verified"):
                verified_q += 1
            usage = entry.get("usage_count", 0)
            total_served += usage
            # Each cached hit saves ~200 tokens at ~$0.001
            if usage > 1:
                total_tokens_saved += (usage - 1) * 200

    cache_rate = 0.0
    if total_served > 0 and total_q > 0:
        # Approximate: total_served - total_q = cache hits
        cache_hits = max(0, total_served - total_q)
        cache_rate = cache_hits / total_served if total_served > 0 else 0.0

    savings = total_tokens_saved * 18 / 1_000_000  # combined input+output cost

    return BankStats(
        book_id=book_id,
        chapter_num=chapter_num,
        total_questions=total_q,
        verified_questions=verified_q,
        total_served=total_served,
        cache_hit_rate=round(cache_rate, 3),
        estimated_savings_usd=round(savings, 4),
    )


def delete_qa(qa_id: str, book_id: str, chapter_num: int) -> bool:
    """Delete a Q&A pair by ID."""
    bank = _load_bank(book_id, chapter_num)
    new_bank = [e for e in bank if e.get("id") != qa_id]
    if len(new_bank) < len(bank):
        _save_bank(book_id, chapter_num, new_bank)
        return True
    return False


def update_answer(qa_id: str, book_id: str, chapter_num: int, new_answer: str) -> bool:
    """Update a banked answer by ID."""
    bank = _load_bank(book_id, chapter_num)
    for entry in bank:
        if entry.get("id") == qa_id:
            entry["answer_text"] = new_answer
            _save_bank(book_id, chapter_num, bank)
            return True
    return False
