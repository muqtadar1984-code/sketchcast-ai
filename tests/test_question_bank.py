"""The printable question bank (catalogue Phase D): ``docgen.bank_worksheet.
write_bank`` and ``catalogue.question_bank.render_question_bank``.

Pinned: the type → difficulty grouping with per-band numbering mirrored in
the key; the student/key leak gate (sentinel answers never reach the sheet);
the renderer reads the APPROVED bank of the topic and language, refuses an
empty one, titles "<topic> · Question Bank", and uploads the two documents
under the same kinds and order as every split document.
"""

from __future__ import annotations

import zipfile

import pytest

from catalogue import question_bank as qb
from docgen import bank_worksheet as bw
from docgen.strings import _t
from tests.catalogue_fakes import FakeSB

HEADER = ["Cambridge Lower Secondary Science 0893 · 7Bs.01", "CBSE Science 086 · Class 9 · Cell"]


def _item(i, item_type, difficulty, **extra):
    base = {"id": f"q{i:03d}", "topic_id": "t-cell", "language": "en", "status": "approved",
            "item_type": item_type, "difficulty": difficulty, "marks": 1, "est_seconds": 60,
            "stem": f"Stem {i} {item_type}", "answer": {"text": f"XQZ{i}"}, "marking_scheme": [],
            "explanation": f"XQZEXPL{i}"}
    if item_type == "mcq":
        base["options"] = [{"key": k, "text": f"Opt{k}{i}"} for k in "ABCD"]
        base["answer"] = {"key": "B"}
    if item_type == "true_false":
        base["answer"] = {"value": True}
    return {**base, **extra}


ITEMS = [
    _item(1, "mcq", 1), _item(2, "mcq", 3), _item(3, "mcq", 5), _item(4, "mcq", 2),
    _item(5, "true_false", 1),
    _item(6, "short_answer", 4), _item(7, "short_answer", 2),
]


def _xml(path):
    with zipfile.ZipFile(path) as z:
        return z.read("word/document.xml").decode("utf-8")


def test_bands_easy_medium_hard_with_unknown_as_medium():
    assert bw.band_of({"difficulty": 1}) == "diff_easy"
    assert bw.band_of({"difficulty": 2}) == "diff_easy"
    assert bw.band_of({"difficulty": 3}) == "diff_medium"
    assert bw.band_of({"difficulty": 4}) == "diff_hard"
    assert bw.band_of({"difficulty": 5}) == "diff_hard"
    assert bw.band_of({}) == "diff_medium" and bw.band_of({"difficulty": "x"}) == "diff_medium"
    bands = bw.group_by_band([_item(1, "mcq", 5), _item(2, "mcq", 1), _item(3, "mcq", 3)])
    assert [k for k, _ in bands] == ["diff_easy", "diff_medium", "diff_hard"]
    assert [[it["id"] for it in g] for _, g in bands] == [["q002"], ["q003"], ["q001"]]


def test_write_bank_groups_by_type_then_band_and_keeps_answers_off_the_sheet(tmp_path):
    paths = bw.write_bank(tmp_path, "Cells · Question Bank", "7 questions · Total marks: 7", ITEMS,
                          header_lines=HEADER, language="en", template=None)
    assert [p.name for p in paths] == ["question_bank.docx", "question_bank_answer_key.docx"]
    sheet, key = _xml(paths[0]), _xml(paths[1])
    # Title, curriculum block, every stem on the sheet.
    assert "Cells · Question Bank" in sheet and "Cambridge Lower Secondary Science 0893" in sheet
    for it in ITEMS:
        assert it["stem"] in sheet
    # Sections in TYPE_ORDER; difficulty sub-headings in band order inside the MCQ section.
    i_mcq, i_tf, i_sa = (sheet.index(_t(k, "en")) for k in ("sec_mcq", "sec_true_false", "sec_short"))
    assert i_mcq < i_tf < i_sa
    mcq_block = sheet[i_mcq:i_tf]
    assert mcq_block.index("Difficulty: easy") < mcq_block.index("Difficulty: medium") < mcq_block.index("Difficulty: hard")
    # Easy MCQs are q001 (d1) and q004 (d2), numbered 1 and 2 within their band.
    easy = mcq_block[mcq_block.index("Difficulty: easy"):mcq_block.index("Difficulty: medium")]
    assert "Stem 1 mcq" in easy and "Stem 4 mcq" in easy and "Stem 2 mcq" not in easy
    # Leak gate: no answer sentinel on the sheet; every one in the key.
    assert "XQZ" not in sheet
    for it in ITEMS:
        assert it["explanation"] in key
    assert "Difficulty: easy" in key and "Difficulty: hard" in key


def test_write_bank_localizes_headings(tmp_path):
    paths = bw.write_bank(tmp_path, "الخلايا · بنك الأسئلة", "x", ITEMS[:2], header_lines=[], language="ar", template=None)
    sheet = _xml(paths[0])
    assert "مستوى الصعوبة: سهل" in sheet and "مستوى الصعوبة: متوسط" in sheet


# ── the renderer against the fake Supabase ────────────────────────────────────

GEN = {"id": "gen-qb", "kind": "question_bank", "owner_id": "sys", "status": "processing",
       "params": {"catalogue": True, "topic_id": "t-cell", "kit_id": "kit-1", "article_id": "art-1",
                  "language": "en", "curriculum_header": HEADER}}


def _sb(items):
    sb = FakeSB()
    sb.tables["topics"] = [{"id": "t-cell", "title": "Cells", "subject": "Science", "status": "video_approved"}]
    sb.tables["topic_questions"] = list(items)
    return sb


@pytest.fixture
def uploads(monkeypatch):
    done = []

    def fake_upload(sb, local_path, dest):
        done.append((str(local_path), dest))
    monkeypatch.setattr(qb.db, "upload_artifact", fake_upload)
    return done


def test_render_reads_the_approved_bank_sorts_easy_to_hard_and_uploads_both(tmp_path, uploads):
    items = ITEMS + [_item(8, "mcq", 1, status="draft"), _item(9, "mcq", 1, language="ar"), _item(10, "mcq", 1, topic_id="t-other")]
    sb = _sb(items)
    title = qb.render_question_bank(sb, GEN, "gen-qb", tmp_path, "sys/gen-qb", {"docx_template": None}, "en")
    assert title == "Cells · Question Bank"
    assert uploads == [(str(tmp_path / "question_bank.docx"), "sys/gen-qb/question_bank.docx"),
                       (str(tmp_path / "question_bank_answer_key.docx"), "sys/gen-qb/answer_key.docx")]
    assert [(a["generation_id"], a["kind"], a["storage_path"]) for a in sb.tables["artifacts"]] == [
        ("gen-qb", "docx", "sys/gen-qb/question_bank.docx"), ("gen-qb", "answer_key_docx", "sys/gen-qb/answer_key.docx")]
    sheet = _xml(tmp_path / "question_bank.docx")
    assert "7 questions" in sheet
    for it in ITEMS:
        assert it["stem"] in sheet
    for stranger in ("Stem 8 mcq", "Stem 9 mcq", "Stem 10 mcq"):
        assert stranger not in sheet, "draft, other-language and other-topic items never print"
    assert sb.writes("generations") == [], "the renderer never touches the generation row"


def test_render_refuses_an_empty_bank_and_writes_nothing(tmp_path, uploads):
    sb = _sb([_item(1, "mcq", 1, status="draft")])
    with pytest.raises(RuntimeError, match="no approved en questions"):
        qb.render_question_bank(sb, GEN, "gen-qb", tmp_path, "sys/gen-qb", {}, "en")
    assert uploads == [] and sb.tables["artifacts"] == []


def test_render_refuses_a_row_with_no_topic(tmp_path, uploads):
    with pytest.raises(RuntimeError, match="topic_id"):
        qb.render_question_bank(_sb(ITEMS), {**GEN, "params": {"catalogue": True}}, "gen-qb", tmp_path, "sys/gen-qb", {}, "en")
