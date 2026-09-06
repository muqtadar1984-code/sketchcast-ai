"""The curriculum block (catalogue Phase 3, decision 10): ``dx.new_doc(...,
header_lines=[…])`` renders one small line per curriculum under the subtitle,
and EVERY document builder threads ``params.curriculum_header`` into it — for
the student document and the key alike. A document built without header
lines must not carry the block: textbook documents are byte-for-byte what
they were.

Everything here CALLS things — the builders run on stub clients and the saved
documents are read back.
"""

from __future__ import annotations

import re
import zipfile

import pytest
from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from docgen import activity, case_study, exam, exam_paper, lesson_plan, worksheet
from docgen import docx_builder as dx

LINES = ["Cambridge Lower Secondary Science 0893 · 7Bs.01, 7Bs.02",
         "CBSE Science 086 · Class 9 · Cell — the basic unit of life"]


def _iter_paras(doc):
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            for row in Table(child, doc).rows:
                for cell in row.cells:
                    yield from cell.paragraphs


def _texts(doc):
    return [re.sub(r"\s+", " ", p.text).strip() for p in _iter_paras(doc)]


def _blob(doc):
    return "\n".join(t for t in _texts(doc) if t)


def _saved(path):
    return Document(str(path))


# ── the primitive ───────────────────────────────────────────────────────


def test_new_doc_renders_every_header_line_under_the_subtitle():
    doc = dx.new_doc("Cells · Worksheet", "Grade 7 · Science", kind="worksheet", header_lines=LINES)
    texts = [t for t in _texts(doc) if t]
    assert texts.index("Grade 7 · Science") < texts.index(LINES[0]) < texts.index(LINES[1])
    assert texts.index(LINES[1]) == texts.index(LINES[0]) + 1, "one paragraph per line, adjacent"


@pytest.mark.parametrize("kind", ["worksheet", "exam_paper", "lesson_plan", "case_study", "exam", "activity"])
def test_every_style_renders_the_block_and_omits_it_when_not_passed(kind):
    with_block = _blob(dx.new_doc("T", "S", kind=kind, header_lines=LINES))
    without = _blob(dx.new_doc("T", "S", kind=kind))
    assert LINES[0] in with_block and LINES[1] in with_block
    assert LINES[0] not in without and LINES[1] not in without


@pytest.mark.parametrize("value", [None, [], "", "not a list", [None, 3, "  "], {"a": 1}])
def test_a_missing_or_malformed_header_renders_nothing(value):
    doc = dx.new_doc("T", "S", kind="worksheet", header_lines=value)
    assert _blob(doc) == _blob(dx.new_doc("T", "S", kind="worksheet"))
    assert dx.header_lines_of(value) == []


def test_header_lines_are_cleaned_not_str_ed():
    assert dx.header_lines_of(["  A · 1 ", 7, None, "", "B"]) == ["A · 1", "B"]


def test_rtl_header_paragraphs_carry_bidi():
    doc = dx.new_doc("ورقة", "الصف 7", kind="worksheet", language="ar", header_lines=["المنهج · 7Bs.01"])
    p = next(p for p in _iter_paras(doc) if p.text.strip() == "المنهج · 7Bs.01")
    assert p._p.pPr.find(qn("w:bidi")) is not None  # noqa: SLF001
    (run,) = p.runs
    assert run._r.rPr.find(qn("w:rtl")) is not None  # noqa: SLF001


def test_page_break_primitive_adds_a_page_break_run():
    doc = dx.new_doc("T", "S", kind="worksheet")
    dx.page_break(doc)
    brs = [br for br in doc.element.body.iter(qn("w:br")) if br.get(qn("w:type")) == "page"]
    assert len(brs) == 1


# ── every builder threads params.curriculum_header ─────────────────────


class _Stub:
    def __init__(self, data):
        self._data = data

    def analyze(self, prompt, max_tokens=0, **k):
        return {"data": self._data}


_BOOK = {"grade": "Grade 7", "subject": "Science"}
_CHAPTER = {"title": "Cells", "sections": [{"section_title": "Cells", "content": "A cell is the basic unit."}]}

_PAYLOADS = {
    lesson_plan: {"title": "Cells plan", "duration_minutes": 45, "learning_objectives": ["o"], "materials": ["m"],
                  "key_vocabulary": [{"term": "cell", "definition": "unit"}],
                  "lesson_flow": [{"phase": "Hook", "minutes": 5, "teacher_does": "t", "students_do": "s"}],
                  "assessment": ["a"], "homework": ["h"], "differentiation": {"support": "s", "challenge": "c"}},
    activity: {"intro": "i", "activities": [{"name": "Model a cell", "objective": "o", "grouping": "pairs",
                                             "duration_minutes": 10, "materials": ["clay"], "steps": ["s1"],
                                             "teacher_facilitation": "f", "success_looks_like": "w"}]},
    case_study: {"title": "A wilting plant", "scenario": "p1\n\np2", "background": ["b"],
                 "discussion_questions": [{"q": "Why?", "guidance": "g"}], "concepts_applied": ["c"]},
    worksheet: {"title": "Cells worksheet", "instructions": "i",
                "fill_blank": [{"q": "The ____ controls the cell.", "answer": "nucleus"}],
                "true_false": [{"statement": "s", "answer": True}],
                "match_column": [{"left": "L1", "right": "R1"}, {"left": "L2", "right": "R2"}, {"left": "L3", "right": "R3"}],
                "short_answer": [{"q": "Why?", "answer": "a", "work_space_lines": 2}]},
    exam_paper: {"title": "Cells test", "instructions": "i",
                 "fill_blank": [{"q": "The ____ controls the cell.", "answer": "nucleus"}],
                 "true_false": [{"statement": "s", "answer": True}],
                 "match_column": [{"left": "L1", "right": "R1"}, {"left": "L2", "right": "R2"}],
                 "subjective": [{"q": "Explain.", "marks": 5, "answer_outline": "o"}]},
    exam: {"title": "Cells exam", "instructions": "i",
           "mcq": [{"q": "q", "options": ["a", "b", "c", "d"], "answer": "A", "marks": 1}],
           "fill_blank": [{"q": "The ____ controls.", "answer": "nucleus", "marks": 1}],
           "true_false": [{"statement": "s", "answer": True, "marks": 1}],
           "match_column": [{"left": "L1", "right": "R1"}, {"left": "L2", "right": "R2"}],
           "short_answer": [{"q": "q", "answer": "a", "marks": 2}],
           "long_answer": [{"q": "q", "answer_outline": "o", "marks": 5}]},
}

_BASE_PARAMS = {exam: {"counts": {"mcq": 1, "fill_blank": 1, "true_false": 1, "match_column": 2,
                                  "short_answer": 1, "long_answer": 1}}}


def _build(mod, tmp_path, params):
    out = mod.build(_BOOK, _CHAPTER, {}, _Stub(_PAYLOADS[mod]), params, tmp_path)
    return out if isinstance(out, list) else [out]


@pytest.mark.parametrize("mod", list(_PAYLOADS), ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_every_builder_threads_the_curriculum_header_into_both_documents(mod, tmp_path):
    params = {**_BASE_PARAMS.get(mod, {}), "curriculum_header": LINES}
    paths = _build(mod, tmp_path, params)
    assert len(paths) == (1 if mod is lesson_plan else 2)
    for path in paths:
        blob = _blob(_saved(path))
        assert LINES[0] in blob and LINES[1] in blob, path.name
        # The block sits in the body, once — never duplicated by a section.
        assert blob.count(LINES[0]) == 1


@pytest.mark.parametrize("mod", list(_PAYLOADS), ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_every_builder_renders_no_block_without_the_param(mod, tmp_path):
    for path in _build(mod, tmp_path, dict(_BASE_PARAMS.get(mod, {}))):
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        assert "7Bs.01" not in xml and "CBSE" not in xml, path.name
