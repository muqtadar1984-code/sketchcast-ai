"""Worksheet generator — questions must be TYPED and GROUPED BY KIND, and the
match-the-columns exercise must render as a TABLE (not running text). Regression
for the teacher feedback (2026-07-10): "1 fill-in-the-blank followed by 1
subjective ... match the column populated as running text."
"""

from __future__ import annotations

import json

from docx import Document

from docgen import questions, worksheet


class _StubClient:
    """Returns a fixed typed worksheet payload (no network)."""

    def __init__(self, data):
        self._data = data

    def analyze(self, prompt, max_tokens=0, **k):
        return {"data": self._data}


_PAYLOAD = {
    "title": "Cells Worksheet",
    "instructions": "Answer all questions.",
    "fill_blank": [
        {"q": "The ____ controls the cell.", "answer": "nucleus"},
        {"q": "Plant cells have a ____ wall.", "answer": "cell"},
    ],
    "true_false": [{"statement": "Animal cells have chloroplasts.", "answer": False}],
    "match_column": [
        {"left": "Nucleus", "right": "Controls the cell"},
        {"left": "Chloroplast", "right": "Captures sunlight"},
        {"left": "Vacuole", "right": "Stores water"},
    ],
    "short_answer": [{"q": "Why are plant cells rigid?", "answer": "The cell wall.", "work_space_lines": 2}],
}


def test_worksheet_docx_groups_by_type_and_tables_the_match(tmp_path):
    out = worksheet.build(
        book={"grade": "Grade 7", "subject": "Science"},
        chapter={"title": "Cells", "sections": [{"section_title": "Cells", "content": "A cell is the basic unit."}]},
        analysis={},
        client=_StubClient(_PAYLOAD),
        params={"num_questions": 10, "include_answer_key": True},
        out_dir=tmp_path,
    )
    doc = Document(str(out))
    headings = [p.text for p in doc.paragraphs if p.text.strip()]
    blob = "\n".join(headings)
    # One grouped section per kind, in order.
    assert "Fill in the blanks" in blob
    assert "True or False" in blob
    assert "Match the columns" in blob
    assert "Short answer" in blob
    # The match exercise is a real 2-column TABLE, not running text.
    assert len(doc.tables) == 1
    t = doc.tables[0]
    assert [c.text for c in t.rows[0].cells] == ["Column A", "Column B"]
    assert len(t.rows) == 1 + 3  # header + 3 pairs

    # Structured questions.json is typed + grouped (fill → tf → match → short).
    payload = json.loads((tmp_path / "questions.json").read_text(encoding="utf-8"))
    types = [q["type"] for q in payload["questions"]]
    assert types == ["fill_blank", "fill_blank", "true_false", "match", "short"]
    match_q = next(q for q in payload["questions"] if q["type"] == "match")
    assert len(match_q["pairs"]) == 3


def test_write_worksheet_orders_kinds_and_keeps_match_whole(tmp_path):
    questions.write_worksheet(
        tmp_path, "T", "i",
        fill=[{"q": "a ____", "answer": "x"}],
        tf=[{"statement": "s", "answer": True}],
        match=[{"left": "L1", "right": "R1"}, {"left": "L2", "right": "R2"}],
        short=[{"q": "why?", "answer": "because"}],
    )
    payload = json.loads((tmp_path / "questions.json").read_text(encoding="utf-8"))
    assert [q["type"] for q in payload["questions"]] == ["fill_blank", "true_false", "match", "short"]
    assert payload["questions"][1]["answer"] is True  # true_false coerced to bool
