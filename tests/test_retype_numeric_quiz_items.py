"""scripts/retype_numeric_quiz_items.py — the pure retyping of an existing
questions.json: numeric ``short`` items become ``fill_blank`` with their
spellings accepted, everything else is byte-for-byte what it was, and a
second pass changes nothing (idempotent). The database edges are not
exercised here (the script's dry run against production is in its PR).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "retype_numeric_quiz_items.py"
spec = importlib.util.spec_from_file_location("retype_numeric_quiz_items", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# The Circle kit's worksheet of 2026-10-16, abridged, plus a prose short answer
# and the kinds the retype must leave alone.
FIXTURE = {
    "title": "Worksheet — Circumference and Area of a Circle",
    "instructions": "Show your working for every question.",
    "questions": [
        {"id": "q1", "type": "fill_blank", "prompt": "The ____ is the boundary.", "answer": "circumference", "marks": 1},
        {"id": "q2", "type": "true_false", "prompt": "d = 2r.", "answer": True, "marks": 1},
        {"id": "q3", "type": "match", "prompt": "Match.", "pairs": [{"left": "r", "right": "radius"}, {"left": "d", "right": "diameter"}], "marks": 2},
        {"id": "q4", "type": "short", "prompt": "Circumference, r = 7, pi = 22/7?", "answer": "44", "marks": 1},
        {"id": "q5", "type": "short", "prompt": "Area, r = 3, in terms of pi?", "answer": "9π", "marks": 1},
        {"id": "q6", "type": "short", "prompt": "Area of a semicircle, r = 6, pi = 3.14?", "answer": "56.52", "marks": 1, "image": "data:image/png;base64,AAAA"},
        {"id": "q7", "type": "short", "prompt": "Why is π irrational?", "answer": "because it cannot be written as a fraction", "marks": 2},
        {"id": "q8", "type": "subjective", "prompt": "Explain.", "answer_outline": "3", "marks": 4},
        {"id": "q9", "type": "short", "prompt": "Solve x² = 9.", "answer": "x = 3 or x = -3", "marks": 2},
    ],
}


def test_numeric_short_items_become_fill_blank_and_nothing_else_moves():
    before = json.dumps(FIXTURE, sort_keys=True)
    new, n = mod.retype_quiz(FIXTURE)
    assert n == 4
    assert json.dumps(FIXTURE, sort_keys=True) == before, "the input is not mutated"
    qs = new["questions"]
    assert new["title"] == FIXTURE["title"] and new["instructions"] == FIXTURE["instructions"]
    # untouched kinds are the SAME objects
    for i in (0, 1, 2, 6, 7):
        assert qs[i] is FIXTURE["questions"][i]
    assert qs[3] == {"id": "q4", "type": "fill_blank", "prompt": "Circumference, r = 7, pi = 22/7?", "answer": "44", "marks": 1}
    assert qs[4] == {"id": "q5", "type": "fill_blank", "prompt": "Area, r = 3, in terms of pi?", "answer": "9π", "accept": ["9pi", "9 pi"], "marks": 1}
    # key order is kept and the picture stays (accept, when present, goes last like the writer puts it)
    assert list(qs[5].keys()) == ["id", "type", "prompt", "answer", "marks", "image"]
    assert qs[5]["type"] == "fill_blank" and qs[5]["image"] == "data:image/png;base64,AAAA"
    assert qs[8]["type"] == "fill_blank" and qs[8]["answer"] == "x = 3 or x = -3" and qs[8]["accept"] == ["3", "-3"]
    # a prose short answer stays short — the teacher still reads it
    assert qs[6]["type"] == "short"


def test_retype_is_idempotent_and_leaves_a_clean_file_alone():
    once, n1 = mod.retype_quiz(FIXTURE)
    twice, n2 = mod.retype_quiz(once)
    assert n1 == 4 and n2 == 0 and twice is once
    clean = {"title": "T", "instructions": "", "questions": [{"id": "q1", "type": "fill_blank", "prompt": "p", "answer": "44", "marks": 1}]}
    same, n = mod.retype_quiz(clean)
    assert n == 0 and same is clean
    assert mod.retype_quiz({"questions": "nope"}) == ({"questions": "nope"}, 0)
    assert mod.retype_quiz([]) == ([], 0)


def test_serialise_matches_the_writer_byte_for_byte(tmp_path):
    from docgen.questions import write_worksheet

    write_worksheet(tmp_path, "T", "i", fill=[], tf=[], match=[],
                    short=[{"q": "Area, r = 3?", "answer": "9π"}, {"q": "Why?", "answer": "because"}])
    # write_text() on Windows turns LF into CRLF; the worker runs on Linux
    fresh = (tmp_path / "questions.json").read_bytes().replace(b"\r\n", b"\n")
    old = {"title": "T", "instructions": "i", "questions": [
        {"id": "q1", "type": "short", "prompt": "Area, r = 3?", "answer": "9π", "marks": 1},
        {"id": "q2", "type": "short", "prompt": "Why?", "answer": "because", "marks": 1}]}
    new, n = mod.retype_quiz(old)
    assert n == 1
    assert mod.serialise(new) == fresh, "a retyped old file is byte-for-byte a freshly written one"
