"""Emit a structured questions.json alongside the worksheet/exam .docx.

The Claude response the builders already parse contains the questions + answer
key; this just normalises that into one schema the app's interactive quiz player
can render and auto-grade. Purely additive — the .docx path is unchanged, and
every call is best-effort (a failure never blocks document generation).

Unified schema:
  {"title", "instructions",
   "questions": [
     {"id","type":"fill_blank","prompt","answer","marks", "accept"?: [str]},
     {"id","type":"true_false","prompt","answer":bool,"marks"},
     {"id","type":"match","prompt","pairs":[{"left","right"}],"marks"},
     {"id","type":"mcq","prompt","options":[str],"answer":str,"marks"},
     {"id","type":"short","prompt","answer","marks"},
     {"id","type":"subjective","prompt","answer_outline","marks"}]}
Objective types (fill_blank/true_false/match/mcq) are auto-gradable; short and
subjective are graded by the teacher. ``mcq`` (catalogue Phase D, 2026-10-16)
carries its options IN THE BANK'S ORDER and the correct option's TEXT as the
answer; the app strips ``answer`` and grades a submitted option text against
it. ``accept`` on a fill_blank lists alternative spellings the key also takes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from docgen.strings import _t, match_instruction

logger = logging.getLogger("worker")


def _write(out_dir: Path, title: str, instructions, questions: list[dict],
           language: str = "en") -> Path:
    payload = {"title": title or _t("quiz", language),
               "instructions": str(instructions or ""), "questions": questions}
    path = Path(out_dir) / "questions.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_exam(out_dir: Path, title: str, instructions, fill, tf, match, subj,
               language: str = "en") -> Path:
    qs: list[dict] = []
    i = 0
    for q in fill:
        if isinstance(q, dict) and str(q.get("q", "")).strip():
            i += 1
            qs.append({"id": f"q{i}", "type": "fill_blank", "prompt": str(q.get("q", "")), "answer": str(q.get("answer", "")), "marks": 1})
    for q in tf:
        if isinstance(q, dict) and str(q.get("statement", "")).strip():
            i += 1
            qs.append({"id": f"q{i}", "type": "true_false", "prompt": str(q.get("statement", "")), "answer": bool(q.get("answer")), "marks": 1})
    pairs = [{"left": str(p.get("left", "")), "right": str(p.get("right", ""))}
             for p in (match or []) if isinstance(p, dict) and str(p.get("left", "")).strip()]
    if pairs:
        i += 1
        qs.append({"id": f"q{i}", "type": "match", "prompt": match_instruction(language), "pairs": pairs, "marks": len(pairs)})
    for q in subj:
        if isinstance(q, dict) and str(q.get("q", "")).strip():
            i += 1
            try:
                marks = int(q.get("marks", 5) or 5)
            except (TypeError, ValueError):
                marks = 5
            qs.append({"id": f"q{i}", "type": "subjective", "prompt": str(q.get("q", "")), "answer_outline": str(q.get("answer_outline", "")), "marks": marks})
    return _write(out_dir, title, instructions, qs, language)


def write_worksheet(out_dir: Path, title: str, instructions, fill, tf, match, short,
                    language: str = "en", extra: list[dict] | None = None) -> Path:
    """Typed, grouped worksheet → the quiz player's unified schema. Mirrors
    write_exam but the free-response section is `short` (with answers) rather than
    long-form `subjective`. Grouped so the player renders one section per kind."""
    qs: list[dict] = []
    i = 0
    for q in (fill or []):
        if isinstance(q, dict) and str(q.get("q", "")).strip():
            i += 1
            qs.append({"id": f"q{i}", "type": "fill_blank", "prompt": str(q.get("q", "")), "answer": str(q.get("answer", "")), "marks": 1})
    for q in (tf or []):
        if isinstance(q, dict) and str(q.get("statement", "")).strip():
            i += 1
            qs.append({"id": f"q{i}", "type": "true_false", "prompt": str(q.get("statement", "")), "answer": bool(q.get("answer")), "marks": 1})
    pairs = [{"left": str(p.get("left", "")), "right": str(p.get("right", ""))}
             for p in (match or []) if isinstance(p, dict) and str(p.get("left", "")).strip()]
    if pairs:
        i += 1
        qs.append({"id": f"q{i}", "type": "match", "prompt": match_instruction(language), "pairs": pairs, "marks": len(pairs)})
    for q in (short or []):
        if isinstance(q, dict) and str(q.get("q", "")).strip():
            i += 1
            qs.append({"id": f"q{i}", "type": "short", "prompt": str(q.get("q", "")), "answer": str(q.get("answer", "")), "marks": 1})
    # prebuilt questions in the player's own schema — the maths figure
    # questions, each with its picture as a data URL — numbered on
    for q in (extra or []):
        if isinstance(q, dict) and q.get("type") and str(q.get("prompt", "")).strip():
            i += 1
            qs.append({**q, "id": f"q{i}"})
    return _write(out_dir, title, instructions, qs, language)


# ── question-bank items (catalogue Phase D) ───────────────────────────────────
# ``topic_questions`` rows → the player's schema. Only the OBJECTIVE types
# are gradable online; the subjective ones (short_answer, long_answer,
# numerical, diagram_label) stay on the printed sheet and its key and are
# NOT written here — a numerical answer with a tolerance, or a labelled
# diagram, is not what the engine's exact-match grader marks.

BANK_OBJECTIVE_TYPES = frozenset({"mcq", "true_false", "fill_blank", "match", "assertion_reason"})


def _bank_answer(item: dict) -> dict:
    a = item.get("answer")
    return a if isinstance(a, dict) else {}


def _bank_marks(item: dict, default: int = 1) -> int:
    try:
        return max(1, int(item.get("marks")))
    except (TypeError, ValueError):
        return default


def bank_item_to_question(item: dict) -> dict | None:
    """One approved bank item as a player question (no id — the caller
    numbers), or None when the item is subjective or malformed (an MCQ with
    no option under its answer key, a match with no pairs): a question the
    engine cannot mark must not reach the paper as a free mark."""
    if not isinstance(item, dict):
        return None
    t = str(item.get("item_type") or "").strip()
    stem = str(item.get("stem") or "").strip()
    if t not in BANK_OBJECTIVE_TYPES or not stem:
        return None
    a = _bank_answer(item)
    if t in ("mcq", "assertion_reason"):
        opts = [o for o in (item.get("options") or []) if isinstance(o, dict) and str(o.get("text") or "").strip()]             if isinstance(item.get("options"), list) else []
        key = str(a.get("key") or "").strip().upper()[:1]
        correct = next((str(o.get("text")).strip() for o in opts if str(o.get("key") or "").strip().upper()[:1] == key), "")
        if len(opts) < 2 or not correct:
            return None
        return {"type": "mcq", "prompt": stem, "options": [str(o.get("text")).strip() for o in opts],
                "answer": correct, "marks": _bank_marks(item)}
    if t == "true_false":
        if not isinstance(a.get("value"), bool):
            return None
        return {"type": "true_false", "prompt": stem, "answer": a["value"], "marks": _bank_marks(item)}
    if t == "fill_blank":
        text = str(a.get("text") or "").strip()
        if not text:
            return None
        q = {"type": "fill_blank", "prompt": stem, "answer": text, "marks": _bank_marks(item)}
        accept = [str(x).strip() for x in (a.get("accept") or []) if str(x or "").strip()] if isinstance(a.get("accept"), list) else []
        if accept:
            q["accept"] = accept
        return q
    if t == "match":
        opts = item.get("options") if isinstance(item.get("options"), dict) else {}
        raw = opts.get("pairs") if isinstance(opts.get("pairs"), list) else a.get("pairs")
        pairs = [{"left": str(p.get("left")).strip(), "right": str(p.get("right")).strip()}
                 for p in (raw or []) if isinstance(p, dict) and str(p.get("left") or "").strip() and str(p.get("right") or "").strip()]
        if len(pairs) < 2:
            return None
        # One mark per pair: that is how the engine scores a match.
        return {"type": "match", "prompt": stem, "pairs": pairs, "marks": len(pairs)}
    return None


def bank_questions(items: list[dict]) -> list[dict]:
    """The gradable questions of a composed set, numbered q1.. in the set's
    order with the subjective items SKIPPED (not renumbered around — the
    printed sheet has its own numbering per section, so the two never
    matched anyway)."""
    out: list[dict] = []
    for it in items:
        q = bank_item_to_question(it)
        if q is not None:
            out.append({"id": f"q{len(out) + 1}", **q})
    return out


def write_bank_set(out_dir: Path, title: str, instructions, items: list[dict],
                   language: str = "en") -> Path | None:
    """questions.json for a composed question set — the objective items
    only. Returns None (and writes nothing) when the set has no gradable
    item, so an all-subjective set never gets an empty online quiz."""
    qs = bank_questions(items)
    if not qs:
        return None
    return _write(out_dir, title, instructions, qs, language)
