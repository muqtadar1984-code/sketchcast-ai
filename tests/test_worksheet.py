"""Worksheet generator — questions must be TYPED and GROUPED BY KIND, and the
match-the-columns exercise must render as a TABLE (not running text). Regression
for the teacher feedback (2026-07-10): "1 fill-in-the-blank followed by 1
subjective ... match the column populated as running text."
"""

from __future__ import annotations

import json
import re

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from docgen import questions, worksheet


def _iter_paras(doc):
    """Every paragraph in document order — the workbook style renders section
    chips and answer panels inside single-cell tables, so cell paragraphs
    count too."""
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            for row in Table(child, doc).rows:
                for cell in row.cells:
                    yield from cell.paragraphs


def _text(p):
    """Whitespace-normalized paragraph text (layout tabs are not content)."""
    return re.sub(r"\s+", " ", p.text).strip()


def _match_table(doc):
    """The match-the-columns table, found by its header row — the style layer
    adds chip/panel tables, so position in doc.tables no longer identifies it."""
    found = [t for t in doc.tables
             if [c.text for c in t.rows[0].cells] == ["Column A", "Column B"]]
    assert len(found) == 1
    return found[0]


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
    # DELIBERATE pin update (2026-08-18 student/teacher split): build() now
    # returns [student_sheet, answer_key]; the grouped sections and the match
    # table live in the student sheet.
    out, _key = worksheet.build(
        book={"grade": "Grade 7", "subject": "Science"},
        chapter={"title": "Cells", "sections": [{"section_title": "Cells", "content": "A cell is the basic unit."}]},
        analysis={},
        client=_StubClient(_PAYLOAD),
        params={"num_questions": 10},
        out_dir=tmp_path,
    )
    doc = Document(str(out))
    headings = [_text(p) for p in _iter_paras(doc) if _text(p)]
    blob = "\n".join(headings)
    # One grouped section per kind, in order.
    assert "Fill in the blanks" in blob
    assert "True or False" in blob
    assert "Match the columns" in blob
    assert "Short answer" in blob
    # The match exercise is a real 2-column TABLE, not running text.
    t = _match_table(doc)
    assert len(t.rows) == 1 + 3  # header + 3 pairs

    # Structured questions.json is typed + grouped (fill → tf → match → short).
    payload = json.loads((tmp_path / "questions.json").read_text(encoding="utf-8"))
    types = [q["type"] for q in payload["questions"]]
    assert types == ["fill_blank", "fill_blank", "true_false", "match", "short"]
    match_q = next(q for q in payload["questions"] if q["type"] == "match")
    assert len(match_q["pairs"]) == 3


# Trips every numbering trap at once: an LLM-self-numbered fill question, a
# blank answer (must keep its slot in the key), and a "Q1)"-prefixed short answer.
_NUMBERING_PAYLOAD = {
    "title": "Numbering Worksheet",
    "instructions": "Answer all questions.",
    "fill_blank": [
        {"q": "1. The ____ controls the cell.", "answer": "nucleus"},
        {"q": "Plant cells have a ____ wall.", "answer": ""},
    ],
    "true_false": [
        {"statement": "Animal cells have chloroplasts.", "answer": False},
        {"statement": "The nucleus stores DNA.", "answer": True},
    ],
    "match_column": [
        {"left": "Nucleus", "right": "Controls the cell"},
        {"left": "Chloroplast", "right": "Captures sunlight"},
        {"left": "Vacuole", "right": "Stores water"},
    ],
    "short_answer": [{"q": "Q1) Why are plant cells rigid?", "answer": "The cell wall.", "work_space_lines": 1}],
}


def _sections(doc):
    """Ordered (heading text, [non-empty paragraph texts under it]) pairs."""
    out = []
    for p in _iter_paras(doc):
        if (p.style.name or "").startswith(("Heading", "Title")):
            out.append((_text(p), []))
        elif out and _text(p):
            out[-1][1].append(_text(p))
    return out


def test_numbering_restarts_per_section_and_key_matches_paper(tmp_path):
    # DELIBERATE pin update (2026-08-18 student/teacher split): the answer key
    # is now a SEPARATE document — the numbering contract (key item N answers
    # paper item N) holds ACROSS the two files, and the student sheet carries
    # no "Answer Key" page at all.
    paths = worksheet.build(
        book={"grade": "Grade 7", "subject": "Science"},
        chapter={"title": "Cells", "sections": [{"section_title": "Cells", "content": "A cell is the basic unit."}]},
        analysis={},
        client=_StubClient(_NUMBERING_PAYLOAD),
        params={"num_questions": 10},
        out_dir=tmp_path,
    )
    doc = Document(str(paths[0]))
    key_doc = Document(str(paths[1]))

    # (c) Word's document-wide "List Number" counter must be gone entirely.
    assert all(p.style.name != "List Number" for p in doc.paragraphs)
    assert all(p.style.name != "List Number" for p in key_doc.paragraphs)

    paper = dict(_sections(doc))
    key = dict(_sections(key_doc))
    assert "Answer Key" not in paper

    # (a) every section restarts at 1 — True/False must not continue fill's count.
    assert paper["Section B — True or False"][0].startswith("1. ")

    # (d) an LLM-emitted "1." / "Q1)" prefix is stripped, not doubled.
    assert paper["Section A — Fill in the blanks"][0] == "1. The ____ controls the cell."
    assert paper["Section D — Short answer"][0] == "1. Why are plant cells rigid?"

    # (b) the key carries EXACTLY the paper's numbers, section by section.
    for name in ("Section A — Fill in the blanks", "Section B — True or False"):
        paper_nums = [t.split(".")[0] for t in paper[name]]
        key_nums = [t.split(".")[0] for t in key[name]]
        assert key_nums == paper_nums, name
    assert key["Section B — True or False"] == ["1. False", "2. True"]
    assert key["Section D — Short answer"] == ["1. The cell wall."]

    # A blank answer keeps its slot (em-dash), so numbering stays aligned.
    assert key["Section A — Fill in the blanks"] == ["1. nucleus", "2. —"]

    # The match key is "N. <letter>" (not the old self-numbered "1. 1 → C"),
    # and each letter points at the correct Column B row of the paper's table.
    match_key = key["Section C — Match the columns"]
    assert all(re.fullmatch(r"\d+\. [A-Z]", t) for t in match_key)
    right_by_letter = {}
    for row in _match_table(doc).rows[1:]:
        letter, right = row.cells[1].text.split(". ", 1)
        right_by_letter[letter] = right
    for i, line in enumerate(match_key):
        n, letter = line.split(". ")
        assert int(n) == i + 1
        assert right_by_letter[letter] == _NUMBERING_PAYLOAD["match_column"][i]["right"]


def test_numeric_forms_recognises_numbers_fractions_pi_and_units_but_never_prose():
    nf = questions.numeric_forms
    assert nf("44") == ["44"]
    assert nf("9π") == ["9π", "9pi", "9 pi"]
    assert nf("9π cm²") == ["9π cm²", "9π", "9pi", "9pi cm²", "9 pi", "9 pi cm²"]
    assert nf("56.52") == ["56.52"]
    assert nf("22/7")[0] == "22/7" and "22/7" in nf("22/7")
    assert nf("9π/2") == ["9π/2", "9pi/2", "9 pi/2"]
    assert nf("−3") == ["−3", "-3"]
    assert nf("x = 3 or x = -3") == ["x = 3 or x = -3", "3", "-3"]
    assert nf("45°") == ["45°", "45", "45 °"]
    assert nf("π") == ["π", "pi"]
    for prose in ("because", "chlorophyll", "cm", "two", "", None, "3 cats and a dog"):
        assert nf(prose) is None, prose


def test_write_worksheet_types_numeric_short_answers_as_fill_blank_with_accepts(tmp_path):
    questions.write_worksheet(
        tmp_path, "T", "i", fill=[], tf=[], match=[],
        short=[
            {"q": "Circumference, r = 7 cm, pi = 22/7?", "answer": "44"},
            {"q": "Area, r = 3 cm, in terms of pi?", "answer": "9π"},
            {"q": "Why are plant cells rigid?", "answer": "because of the cell wall"},
        ],
    )
    payload = json.loads((tmp_path / "questions.json").read_text(encoding="utf-8"))
    qs = payload["questions"]
    assert [q["type"] for q in qs] == ["fill_blank", "fill_blank", "short"]
    assert qs[0] == {"id": "q1", "type": "fill_blank", "prompt": "Circumference, r = 7 cm, pi = 22/7?", "answer": "44", "marks": 1}
    assert qs[1]["answer"] == "9π" and qs[1]["accept"] == ["9pi", "9 pi"]
    assert "accept" not in qs[2] and qs[2]["answer"] == "because of the cell wall"


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


# ── docgen.quiz_sheet: a sheet and its key from a questions.json payload ────

def test_write_worksheet_carries_the_working_of_a_ladder_question(tmp_path):
    short = [{"q": "Solve: 3x + 5 = 20", "answer": "x = 5", "working": ["3x = 15", "x = 5", "Answer: x = 5"]},
             {"q": "Name the longest chord.", "answer": "the diameter"}]
    questions.write_worksheet(tmp_path, "Worksheet", "Answer.", [], [], [], short)
    qs = json.loads((tmp_path / "questions.json").read_text(encoding="utf-8"))["questions"]
    assert qs[0]["type"] == "fill_blank" and qs[0]["working"] == ["3x = 15", "x = 5", "Answer: x = 5"]
    assert qs[1]["type"] == "short" and "working" not in qs[1]


_PNG_1X1 = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAA"
            "AABJRU5ErkJggg==")
_QUIZ_PAYLOAD = {"title": "Worksheet — Circles", "instructions": "Answer every question.", "questions": [
    {"id": "q1", "type": "fill_blank", "prompt": "The longest chord of a circle is called the ____.", "answer": "diameter", "marks": 1},
    {"id": "q2", "type": "true_false", "prompt": "A radius of 7 cm gives a diameter of 14 cm.", "answer": True, "marks": 1},
    {"id": "q3", "type": "match", "prompt": "Match each term to its meaning.", "marks": 2,
     "pairs": [{"left": "radius", "right": "centre to the edge"}, {"left": "chord", "right": "a line across the circle"}]},
    {"id": "q4", "type": "fill_blank", "prompt": "Solve: 3x + 5 = 20", "answer": "x = 5", "accept": ["5"], "marks": 1,
     "working": ["3x = 15", "x = 5", "Answer: x = 5"]},
    {"id": "q5", "type": "fill_blank", "prompt": "Find x in the diagram.", "answer": "40", "marks": 2, "image": _PNG_1X1},
]}


def _all_text(path):
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                parts.extend(p.text for p in cell.paragraphs)
    return "\n".join(parts)


def test_quiz_sheet_sections_the_payload_prints_the_picture_and_keys_the_working(tmp_path):
    from docgen import quiz_sheet

    assert [quiz_sheet.section_of(q) for q in _QUIZ_PAYLOAD["questions"]] == ["fill", "tf", "match", "problem", "figure"]
    sheet, key = quiz_sheet.write_sheet(tmp_path, _QUIZ_PAYLOAD, language="en")
    assert sheet.name == "worksheet.docx" and key.name == "worksheet_answer_key.docx"
    s = _all_text(sheet)
    assert "The longest chord" in s and "Solve: 3x + 5 = 20" in s and "Find x in the diagram." in s
    assert "centre to the edge" in s and "1. radius" in s, "the matching table on the sheet"
    assert "x = 5" not in s and "diameter" not in s.replace("diameter of 14", ""), "the key is a separate document"
    assert len(Document(str(sheet)).inline_shapes) == 1, "the figure question prints its picture"
    k = _all_text(key)
    assert "diameter" in k and "True" in k and "3x = 15" in k and "Answer: x = 5" in k and "40" in k
    assert "Solve: 3x + 5 = 20" not in k, "the key repeats no stem"


def test_quiz_sheet_in_arabic_is_right_to_left_on_every_run(tmp_path):
    import zipfile
    from docgen import quiz_sheet

    sheet, key = quiz_sheet.write_sheet(tmp_path, {**_QUIZ_PAYLOAD, "title": "ورقة عمل — الدوائر"}, language="ar")
    for path in (sheet, key):
        xml = zipfile.ZipFile(str(path)).read("word/document.xml").decode("utf-8")
        assert "<w:rtl/>" in xml and "<w:bidi/>" in xml
    assert "ورقة عمل — الدوائر" in _all_text(sheet)
