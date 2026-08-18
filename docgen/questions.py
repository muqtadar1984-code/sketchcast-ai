"""Emit a structured questions.json alongside the worksheet/exam .docx.

The Claude response the builders already parse contains the questions + answer
key; this just normalises that into one schema the app's interactive quiz player
can render and auto-grade. Purely additive — the .docx path is unchanged, and
every call is best-effort (a failure never blocks document generation).

Unified schema:
  {"title", "instructions",
   "questions": [
     {"id","type":"fill_blank","prompt","answer","marks"},
     {"id","type":"true_false","prompt","answer":bool,"marks"},
     {"id","type":"match","prompt","pairs":[{"left","right"}],"marks"},
     {"id","type":"short","prompt","answer","marks"},
     {"id","type":"subjective","prompt","answer_outline","marks"}]}
Objective types (fill_blank/true_false/match) are auto-gradable; short and
subjective are graded by the teacher.
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
                    language: str = "en") -> Path:
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
    return _write(out_dir, title, instructions, qs, language)
