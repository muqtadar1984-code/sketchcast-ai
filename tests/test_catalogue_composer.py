"""``catalogue.composer``: a blueprint + the approved bank → the exact items
of a set, deterministically; and the worker's rendering of a composed set.

Everything here CALLS things: compose() on hand-built banks, and
render_question_set() against the fake Supabase (tests/catalogue_fakes.py)
with the storage upload replaced by a recorder — the DOCX files it renders
are real and read back.
"""

from __future__ import annotations

import random
import zipfile

import pytest

from catalogue import composer
from catalogue.composer import (
    Unsatisfiable, bucket_plan, compose, largest_remainder, load_bank, load_items_by_id, parse_spec,
    render_question_set,
)
from tests.catalogue_fakes import FakeSB

OBJ = ("mcq", "true_false", "fill_blank", "match", "assertion_reason")
SUBJ = ("short_answer", "long_answer", "numerical", "diagram_label")


def _bank(n_per=2, objectives=("o1", "o2", "o3"), difficulties=(1, 2, 3, 4, 5), status="approved", topic="t-cell", lang="en"):
    """A bank with ``n_per`` items per (mode, difficulty, objective)."""
    items = []
    k = 0
    for mode, types in (("objective", OBJ), ("subjective", SUBJ)):
        for d in difficulties:
            for o in objectives:
                for i in range(n_per):
                    k += 1
                    items.append({"id": f"q{k:03d}", "topic_id": topic, "language": lang, "status": status,
                                  "item_type": types[(k + i) % len(types)], "answer_mode": mode, "difficulty": d,
                                  "objective_ref": o, "marks": 1, "est_seconds": 60, "stem": f"Stem {k}",
                                  "answer": {"text": f"a{k}"}, "marking_scheme": []})
    return items


# ── arithmetic ─────────────────────────────────────────────────────────


def test_largest_remainder_apportions_exactly_with_stable_ties():
    assert largest_remainder(7, {"objective": 0.5, "subjective": 0.5}) == {"objective": 4, "subjective": 3}
    assert largest_remainder(10, {"1": 0.5, "2": 0.3, "3": 0.2}) == {"1": 5, "2": 3, "3": 2}
    assert largest_remainder(10, {"1": 1, "2": 1, "3": 1}) == {"1": 4, "2": 3, "3": 3}
    assert sum(largest_remainder(13, {"a": 0.33, "b": 0.33, "c": 0.34}).values()) == 13
    assert largest_remainder(5, {}) == {} and largest_remainder(5, {"a": 0, "b": "x"}) == {"a": 0, "b": 0}
    assert largest_remainder(0, {"a": 1}) == {"a": 0}


def test_parse_spec_and_bucket_plan():
    assert parse_spec({"count": 10, "objective_ratio": 0.6, "difficulty_mix": {"1": 0.5, "3": 0.5}}) == (10, 0.6, {1: 0.5, 3: 0.5})
    assert parse_spec({"count": "12", "objective_ratio": 7, "difficulty_mix": {"9": 1, "x": 1, "2": -1}}) == (12, 1.0, {})
    assert parse_spec({"count": 500}) == (composer.MAX_COUNT, 0.5, {})
    with pytest.raises(ValueError):
        parse_spec({})
    assert bucket_plan({"count": 10, "objective_ratio": 0.6, "difficulty_mix": {"1": 0.5, "3": 0.5}}) == [
        ("objective", 1, 3), ("objective", 3, 3), ("subjective", 1, 2), ("subjective", 3, 2)]
    assert bucket_plan({"count": 5, "objective_ratio": 1.0}) == [("objective", None, 5)]
    assert bucket_plan({"count": 3, "objective_ratio": 0.0, "difficulty_mix": {"5": 1}}) == [("subjective", 5, 3)]


# ── compose ────────────────────────────────────────────────────────────


SPEC = {"preset": "quick_check", "count": 12, "objective_ratio": 0.5,
        "difficulty_mix": {"1": 0.25, "2": 0.25, "3": 0.5}, "total_marks": 12}


def test_compose_fills_every_bucket_exactly_and_never_pads():
    chosen = compose(_bank(), SPEC, seed=11)
    assert len(chosen) == 12 == len({c["id"] for c in chosen})
    counts = {}
    for c in chosen:
        counts[(c["answer_mode"], c["difficulty"])] = counts.get((c["answer_mode"], c["difficulty"]), 0) + 1
    # 12 × 0.5 = 6 a mode; 6 × (0.25, 0.25, 0.5) = (1, 2, 3) by largest remainder: 1.5, 1.5, 3 → 2, 1, 3 (tie → key order).
    assert counts == {("objective", 1): 2, ("objective", 2): 1, ("objective", 3): 3,
                      ("subjective", 1): 2, ("subjective", 2): 1, ("subjective", 3): 3}
    assert all(c["difficulty"] in (1, 2, 3) for c in chosen), "no item from a difficulty the mix did not ask for"


def test_compose_is_deterministic_in_the_seed_and_blind_to_input_order():
    bank = _bank()
    a = [c["id"] for c in compose(bank, SPEC, seed=42)]
    b = [c["id"] for c in compose(list(reversed(bank)), SPEC, seed=42)]
    shuffled = list(bank)
    random.Random(999).shuffle(shuffled)
    c = [c["id"] for c in compose(shuffled, SPEC, seed=42)]
    assert a == b == c
    assert a != [c["id"] for c in compose(bank, SPEC, seed=43)], "a different seed draws a different paper"
    assert a == [c["id"] for c in compose(bank, SPEC, seed="42")]


def test_compose_spreads_a_bucket_round_robin_over_objectives():
    # One bucket of 6 objective items at difficulty 3 on a 3-objective topic with 4 items each: 2 per objective.
    spec = {"count": 6, "objective_ratio": 1.0, "difficulty_mix": {"3": 1}}
    chosen = compose(_bank(n_per=4, difficulties=(3,)), spec, seed=5)
    per = {}
    for c in chosen:
        per[c["objective_ref"]] = per.get(c["objective_ref"], 0) + 1
    assert per == {"o1": 2, "o2": 2, "o3": 2}
    # When one objective has a single item, the others take up the slack — still exactly 6, still never padded.
    full = _bank(n_per=4, difficulties=(3,))
    o3 = [it for it in full if it["objective_ref"] == "o3" and it["answer_mode"] == "objective"]
    bank = [it for it in full if it["objective_ref"] != "o3"] + o3[:1]
    chosen = compose(bank, spec, seed=5)
    per = {}
    for c in chosen:
        per[c["objective_ref"]] = per.get(c["objective_ref"], 0) + 1
    assert len(chosen) == 6 and per in ({"o1": 3, "o2": 2, "o3": 1}, {"o1": 2, "o2": 3, "o3": 1})


def test_compose_raises_unsatisfiable_listing_every_short_bucket():
    bank = _bank(n_per=1, difficulties=(1, 2))  # 3 items per (mode, difficulty); nothing at 3..5
    spec = {"count": 10, "objective_ratio": 0.5, "difficulty_mix": {"1": 0.2, "3": 0.4, "5": 0.4}}
    with pytest.raises(Unsatisfiable) as ei:
        compose(bank, spec, seed=1)
    exc = ei.value
    assert isinstance(exc, RuntimeError)
    assert exc.shortfalls == [
        {"mode": "objective", "difficulty": 3, "need": 2, "have": 0},
        {"mode": "objective", "difficulty": 5, "need": 2, "have": 0},
        {"mode": "subjective", "difficulty": 3, "need": 2, "have": 0},
        {"mode": "subjective", "difficulty": 5, "need": 2, "have": 0},
    ]
    assert "objective difficulty 3: need 2, have 0" in str(exc) and "subjective difficulty 5" in str(exc)
    # A bank that is one item short is short, not rounded: 5 objective items exist, 6 asked.
    five = [it for it in _bank(n_per=2, difficulties=(1,)) if it["answer_mode"] == "objective"][:5]
    with pytest.raises(Unsatisfiable) as ei2:
        compose(five, {"count": 6, "objective_ratio": 1.0}, seed=1)
    assert ei2.value.shortfalls == [{"mode": "objective", "difficulty": None, "need": 6, "have": 5}]


def test_compose_without_a_mix_takes_any_difficulty_and_derives_the_mode_from_the_type():
    bank = _bank(n_per=1)
    for it in bank:
        it.pop("answer_mode")  # rows without the column: the type decides
    chosen = compose(bank, {"count": 4, "objective_ratio": 0.5}, seed=2)
    assert sorted(composer.mode_of(c) for c in chosen) == ["objective", "objective", "subjective", "subjective"]
    assert all(c["item_type"] in OBJ for c in chosen if composer.mode_of(c) == "objective")


# ── database edges + rendering ─────────────────────────────────────────


def _sb(bank, set_row, blueprint):
    sb = FakeSB()
    sb.tables["topics"] = [{"id": "t-cell", "title": "Cells", "subject": "Biology"}]
    sb.tables["topic_questions"] = [dict(it) for it in bank]
    sb.tables["question_sets"] = [dict(set_row)]
    sb.tables["question_set_blueprints"] = [dict(blueprint)]
    sb.tables["generations"] = [{"id": "gen-ws", "kind": "worksheet", "status": "processing"}]
    sb.tables["artifacts"] = []
    return sb


BLUEPRINT = {"id": "bp-quick", "name": "Quick check", "scope": "topic", "min_maturity": "basic",
             "spec": {"preset": "quick_check", "count": 6, "objective_ratio": 0.5, "difficulty_mix": {"1": 0.5, "2": 0.5}}}
SET = {"id": "qs-1", "blueprint_id": "bp-quick", "topic_ids": ["t-cell"], "language": "en", "question_ids": [],
       "seed": 17, "rendered_generation_id": "gen-ws", "requested_by": "u-founder"}
GEN = {"id": "gen-ws", "kind": "worksheet", "params": {"catalogue": True, "topic_id": "t-cell", "question_set_id": "qs-1",
                                                       "language": "en", "curriculum_header": ["Cambridge 0893 · 7Bs.01"]}}


@pytest.fixture
def uploads(monkeypatch):
    seen = []

    def fake_upload(sb, local_path, dest_path):
        seen.append((str(local_path), dest_path))
        return dest_path

    monkeypatch.setattr(composer.db, "upload_artifact", fake_upload)
    return seen


def test_load_bank_returns_only_approved_items_of_the_topic_and_language():
    bank = _bank(n_per=1, difficulties=(1,)) + _bank(n_per=1, difficulties=(1,), status="draft") \
        + _bank(n_per=1, difficulties=(1,), lang="fr") + _bank(n_per=1, difficulties=(1,), topic="t-other")
    for i, it in enumerate(bank):
        it["id"] = f"row{i}"
    sb = _sb(bank, SET, BLUEPRINT)
    got = load_bank(sb, "t-cell", "en")
    assert len(got) == 6 and all(g["status"] == "approved" and g["language"] == "en" and g["topic_id"] == "t-cell" for g in got)
    assert [r["id"] for r in load_items_by_id(sb, ["row3", "row0"])] == ["row3", "row0"]
    with pytest.raises(RuntimeError):
        load_items_by_id(sb, ["row0", "gone"])


def test_render_composes_writes_the_ids_back_first_then_uploads_both_documents(tmp_path, uploads):
    bank = _bank(n_per=1, difficulties=(1, 2))
    sb = _sb(bank, SET, BLUEPRINT)
    title = render_question_set(sb, GEN, "gen-ws", tmp_path, "owner/gen-ws", {"docx_template": None}, "en")
    assert title == "Cells · Quick check"
    (row,) = sb.tables["question_sets"]
    ids = row["question_ids"]
    assert ids == [c["id"] for c in compose(load_bank(sb, "t-cell", "en"), BLUEPRINT["spec"], 17)]
    assert len(ids) == 6
    # The write-back precedes every artifact write: a crash after it re-renders THIS paper.
    kinds = [(e[0], e[1]) for e in sb.log]
    assert kinds.index(("update", "question_sets")) < kinds.index(("insert", "artifacts"))
    # The two documents first, then (Phase D) the online quiz — same path and
    # kind the document block gives a worksheet's questions.json.
    assert uploads == [(str(tmp_path / "worksheet.docx"), "owner/gen-ws/worksheet.docx"),
                       (str(tmp_path / "worksheet_answer_key.docx"), "owner/gen-ws/answer_key.docx"),
                       (str(tmp_path / "questions.json"), "owner/gen-ws/worksheet_questions.json")]
    assert [(a["generation_id"], a["kind"], a["storage_path"]) for a in sb.tables["artifacts"]] == [
        ("gen-ws", "docx", "owner/gen-ws/worksheet.docx"), ("gen-ws", "answer_key_docx", "owner/gen-ws/answer_key.docx"),
        ("gen-ws", "questions_json", "owner/gen-ws/worksheet_questions.json")]
    with zipfile.ZipFile(tmp_path / "worksheet.docx") as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert "Cells · Quick check" in xml and "Cambridge 0893 · 7Bs.01" in xml and "6 questions" in xml
    for it in bank:
        if it["id"] in ids:
            assert it["stem"] in xml
        else:
            assert it["stem"] not in xml
    assert sb.writes("generations") == [], "the composer never touches the generation row"


def test_render_with_question_ids_renders_exactly_those_in_order_and_composes_nothing(tmp_path, uploads):
    bank = _bank(n_per=1, difficulties=(1,))  # q001..q006
    chosen = ["q004", "q001", "q006"]
    sb = _sb(bank, {**SET, "question_ids": chosen}, BLUEPRINT)
    title = render_question_set(sb, GEN, "gen-ws", tmp_path, "owner/gen-ws", {}, "en")
    assert title == "Cells · Quick check"
    assert sb.writes("question_sets") == [], "an already-composed set is not recomposed"
    with zipfile.ZipFile(tmp_path / "worksheet.docx") as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert "Stem 4 " in xml and "Stem 1 " in xml and "Stem 6 " in xml and "Stem 2 " not in xml
    assert "3 questions" in xml
    # q004/q006 are subjective and the fixture's q001 (a true_false whose
    # answer is prose) is not gradable: no questions.json, two uploads.
    assert len(uploads) == 2


def test_a_set_naming_an_item_no_longer_approved_is_refused_before_any_upload(tmp_path, uploads):
    """compose() only ever picks approved items, but the ids are re-read on a
    re-run: an item rejected or retired in between must not be printed on
    the student sheet with its answer in the key."""
    sb = _sb(_bank(n_per=1, difficulties=(1,)), {**SET, "question_ids": ["q004", "q001", "q006"]}, BLUEPRINT)  # q001..q006
    q004 = next(it for it in sb.tables["topic_questions"] if it["id"] == "q004")
    q004["status"] = "retired"
    with pytest.raises(RuntimeError, match=r"1 item\(s\) no longer approved \(retired\): \['q004'\]"):
        render_question_set(sb, GEN, "gen-ws", tmp_path, "owner/gen-ws", {}, "en")
    assert uploads == [] and sb.tables["artifacts"] == [] and sb.writes("question_sets") == []
    # rejected reads the same way; approved renders
    q004["status"] = "rejected"
    with pytest.raises(RuntimeError, match="rejected"):
        load_items_by_id(sb, ["q004"])
    q004["status"] = "approved"
    assert [r["id"] for r in load_items_by_id(sb, ["q004", "q001"])] == ["q004", "q001"]


def test_a_set_naming_another_topic_s_or_language_s_item_is_refused(tmp_path, uploads):
    bank = _bank(n_per=1, difficulties=(1,)) + _bank(n_per=1, difficulties=(1,), lang="fr") \
        + _bank(n_per=1, difficulties=(1,), topic="t-other")
    for i, it in enumerate(bank):
        it["id"] = f"row{i}"
    sb = _sb(bank, {**SET, "question_ids": ["row0", "row7"]}, BLUEPRINT)   # row7: the French bank
    with pytest.raises(RuntimeError, match=r"another topic or language: \['row7'\]"):
        render_question_set(sb, GEN, "gen-ws", tmp_path, "owner/gen-ws", {}, "en")
    assert uploads == []
    with pytest.raises(RuntimeError, match=r"\['row13'\]"):
        load_items_by_id(sb, ["row13"], topic_id="t-cell", language="en")    # row13: the other topic
    assert [r["id"] for r in load_items_by_id(sb, ["row7", "row13"])] == ["row7", "row13"], "unscoped: the ids alone"


def test_render_refuses_a_missing_set_blueprint_or_id(tmp_path, uploads):
    sb = _sb(_bank(n_per=1), SET, BLUEPRINT)
    with pytest.raises(RuntimeError, match="question_set_id"):
        render_question_set(sb, {"params": {"topic_id": "t-cell"}}, "gen-ws", tmp_path, "b", {}, "en")
    with pytest.raises(RuntimeError, match="not found"):
        render_question_set(sb, {"params": {"question_set_id": "qs-none"}}, "gen-ws", tmp_path, "b", {}, "en")
    sb.tables["question_set_blueprints"] = []
    with pytest.raises(RuntimeError, match="blueprint"):
        render_question_set(sb, GEN, "gen-ws", tmp_path, "b", {}, "en")
    assert uploads == [] and sb.tables["artifacts"] == []


def test_render_surfaces_unsatisfiable_and_writes_nothing(tmp_path, uploads):
    sb = _sb(_bank(n_per=1, difficulties=(1,))[:2], SET, BLUEPRINT)
    with pytest.raises(Unsatisfiable):
        render_question_set(sb, GEN, "gen-ws", tmp_path, "b", {}, "en")
    assert sb.tables["question_sets"][0]["question_ids"] == [] and uploads == [] and sb.tables["artifacts"] == []


def test_render_survives_a_missing_answer_key_kind(tmp_path, uploads, monkeypatch):
    real = composer.db.add_artifact_row

    def flaky(sb, generation_id, kind, path):
        if kind == "answer_key_docx":
            raise RuntimeError('invalid input value for enum artifact_kind: "answer_key_docx"')
        real(sb, generation_id, kind, path)

    monkeypatch.setattr(composer.db, "add_artifact_row", flaky)
    sb = _sb(_bank(n_per=1, difficulties=(1, 2)), SET, BLUEPRINT)
    assert render_question_set(sb, GEN, "gen-ws", tmp_path, "b", {}, "en") == "Cells · Quick check"
    assert [a["kind"] for a in sb.tables["artifacts"]] == ["docx", "questions_json"] and len(uploads) == 3


# ── Phase D: the online quiz (questions_json) and the nearest fill ────────


def _objective_bank(spec_items):
    """``[(item_type, difficulty, answer, extra)]`` → approved rows with
    sentinel answers (XQZ…) so a leak is greppable in the saved files."""
    rows = []
    for i, (t, d, answer, extra) in enumerate(spec_items, 1):
        row = {"id": f"b{i:03d}", "topic_id": "t-cell", "language": "en", "status": "approved",
               "item_type": t, "answer_mode": "objective" if t in OBJ else "subjective", "difficulty": d,
               "objective_ref": f"o{i % 3}", "marks": 1, "est_seconds": 60, "stem": f"Stem {i} {t}",
               "answer": answer, "marking_scheme": [], "explanation": f"XQZEXPL{i}"}
        row.update(extra)
        rows.append(row)
    return rows


def _obj(difficulty, n, prefix):
    """``n`` approved OBJECTIVE items of one difficulty, ids ``<prefix><i>``."""
    base = [it for it in _bank(n_per=n, objectives=("o1", "o2"), difficulties=(difficulty,)) if it["answer_mode"] == "objective"][:n]
    return [{**it, "id": f"{prefix}{i}"} for i, it in enumerate(base)]


QUIZ_BLUEPRINT = {"id": "bp-quiz", "name": "Online quiz", "scope": "worksheet", "min_maturity": "basic",
                  "spec": {"preset": "quiz", "count": 6, "objective_ratio": 1.0, "fill": "nearest",
                           "difficulty_mix": {"1": 0.5, "2": 0.5}, "total_marks": 6}}


def test_nearest_fill_tops_a_short_bucket_up_from_the_closest_difficulty_and_stays_deterministic():
    # 6 wanted: 3 of d1, 3 of d2. The bank has 1 × d1, 2 × d2, 4 × d3, 2 × d5.
    bank = _obj(1, 1, "a") + _obj(2, 2, "b") + _obj(3, 4, "c") + _obj(5, 2, "e")
    assert sorted(composer._difficulty(it) for it in bank) == [1, 2, 2, 3, 3, 3, 3, 5, 5]
    with pytest.raises(Unsatisfiable):
        compose(bank, {**QUIZ_BLUEPRINT["spec"], "fill": "exact"}, 5)
    got = compose(bank, QUIZ_BLUEPRINT["spec"], 5)
    assert len(got) == 6 and len({it["id"] for it in got}) == 6
    diffs = sorted(composer._difficulty(it) for it in got)
    # every d1 and d2 item, then d3s (nearest to both short buckets) — never a d5
    assert diffs == [1, 2, 2, 3, 3, 3]
    assert [it["id"] for it in compose(bank, QUIZ_BLUEPRINT["spec"], 5)] == [it["id"] for it in got]
    assert [it["id"] for it in compose(list(reversed(bank)), QUIZ_BLUEPRINT["spec"], 5)] == [it["id"] for it in got]
    # a different seed is a different paper, the same shape
    other = compose(bank, QUIZ_BLUEPRINT["spec"], 6)
    assert sorted(composer._difficulty(it) for it in other) == diffs


def test_nearest_fill_still_raises_when_the_mode_total_is_short():
    bank = _obj(1, 1, "a") + _obj(2, 1, "b")
    with pytest.raises(Unsatisfiable) as exc:
        compose(bank, QUIZ_BLUEPRINT["spec"], 1)
    assert sum(sf["need"] - sf["have"] for sf in exc.value.shortfalls) == 4


def test_fill_mode_defaults_to_exact():
    assert composer.fill_mode(None) == "exact"
    assert composer.fill_mode({"fill": "NEAREST"}) == "nearest"
    assert composer.fill_mode({"fill": "padded"}) == "exact"


def test_questions_json_carries_only_the_objective_items_in_the_players_schema(tmp_path, uploads):
    import json

    bank = _objective_bank([
        ("mcq", 1, {"key": "B"}, {"options": [{"key": k, "text": f"Opt{k}1" if k != "B" else "XQZMCQ"} for k in "ABCD"]}),
        ("true_false", 1, {"value": False}, {}),
        ("fill_blank", 2, {"text": "XQZFILL", "accept": ["XQZALT"]}, {}),
        ("match", 2, {"pairs": []}, {"options": {"pairs": [{"left": "Nucleus", "right": "XQZRIGHT1"}, {"left": "Vacuole", "right": "XQZRIGHT2"}, {"left": "Wall", "right": "XQZRIGHT3"}]}}),
        ("assertion_reason", 1, {"key": "D"}, {"options": [{"key": k, "text": f"AR{k}" if k != "D" else "XQZAR"} for k in "ABCD"]}),
        ("short_answer", 2, {"text": "XQZSHORT"}, {"marks": 2}),
        ("long_answer", 1, {"text": "XQZLONG"}, {"marks": 5}),
    ])
    ids = [it["id"] for it in bank]
    sb = _sb(bank, {**SET, "question_ids": ids}, BLUEPRINT)
    render_question_set(sb, GEN, "gen-ws", tmp_path, "owner/gen-ws", {}, "en")
    data = json.loads((tmp_path / "questions.json").read_text(encoding="utf-8"))
    assert data["title"] == "Cells · Quick check" and data["instructions"]
    qs = data["questions"]
    assert [q["type"] for q in qs] == ["mcq", "true_false", "fill_blank", "match", "mcq"]
    assert [q["id"] for q in qs] == ["q1", "q2", "q3", "q4", "q5"]
    assert qs[0]["options"] == ["OptA1", "XQZMCQ", "OptC1", "OptD1"] and qs[0]["answer"] == "XQZMCQ" and qs[0]["marks"] == 1
    assert qs[1]["answer"] is False
    assert qs[2]["answer"] == "XQZFILL" and qs[2]["accept"] == ["XQZALT"]
    assert qs[3]["pairs"] == [{"left": "Nucleus", "right": "XQZRIGHT1"}, {"left": "Vacuole", "right": "XQZRIGHT2"}, {"left": "Wall", "right": "XQZRIGHT3"}]
    assert qs[3]["marks"] == 3
    assert qs[4]["options"][3] == "XQZAR" and qs[4]["answer"] == "XQZAR"
    raw = (tmp_path / "questions.json").read_text(encoding="utf-8")
    # The subjective items are print-only: neither their stems nor their answers are in the quiz.
    for leak in ("XQZSHORT", "XQZLONG", "Stem 6 short_answer", "Stem 7 long_answer", "XQZEXPL"):
        assert leak not in raw
    # The leak gate holds for the composed set too: no answer in the student sheet,
    # every answer in the key.
    with zipfile.ZipFile(tmp_path / "worksheet.docx") as z:
        student = z.read("word/document.xml").decode("utf-8")
    with zipfile.ZipFile(tmp_path / "worksheet_answer_key.docx") as z:
        key = z.read("word/document.xml").decode("utf-8")
    for sentinel in ("XQZFILL", "XQZSHORT", "XQZLONG"):
        assert sentinel not in student and sentinel in key
    # MCQ option texts print on the sheet (the answer is the letter, which only the key carries).
    assert "XQZMCQ" in student


def test_an_all_subjective_set_writes_documents_but_no_online_quiz(tmp_path, uploads):
    bank = _objective_bank([("short_answer", 1, {"text": "XQZS1"}, {}), ("long_answer", 2, {"text": "XQZL2"}, {"marks": 5})])
    sb = _sb(bank, {**SET, "question_ids": [it["id"] for it in bank]}, BLUEPRINT)
    render_question_set(sb, GEN, "gen-ws", tmp_path, "owner/gen-ws", {}, "en")
    assert not (tmp_path / "questions.json").exists()
    assert [a["kind"] for a in sb.tables["artifacts"]] == ["docx", "answer_key_docx"] and len(uploads) == 2


def test_a_malformed_objective_item_is_left_off_the_quiz_not_marked_free():
    from docgen.questions import bank_item_to_question, bank_questions

    assert bank_item_to_question({"item_type": "mcq", "stem": "S", "options": [{"key": "A", "text": "x"}], "answer": {"key": "B"}}) is None
    assert bank_item_to_question({"item_type": "true_false", "stem": "S", "answer": {"value": "yes"}}) is None
    assert bank_item_to_question({"item_type": "fill_blank", "stem": "S", "answer": {}}) is None
    assert bank_item_to_question({"item_type": "match", "stem": "S", "answer": {"pairs": [{"left": "a", "right": "b"}]}}) is None
    assert bank_item_to_question({"item_type": "numerical", "stem": "S", "answer": {"value": 3}}) is None
    good = {"item_type": "true_false", "stem": "S", "answer": {"value": True}, "marks": "2"}
    assert bank_questions([{"item_type": "mcq", "stem": "bad"}, good]) == [
        {"id": "q1", "type": "true_false", "prompt": "S", "answer": True, "marks": 2,
         "bank_item_id": None, "objective_ref": None, "teaching_objective_id": None}]


def test_a_quiz_question_keeps_its_bank_item_and_objective():
    from docgen.questions import bank_questions

    items = [
        {"id": "b-skip", "item_type": "short_answer", "stem": "Explain.", "answer": {"text": "x"}, "objective_ref": "o9"},
        {"id": "b-1", "item_type": "true_false", "stem": "S1", "answer": {"value": True}, "objective_ref": "o1",
         "teaching_objective_id": "to-77"},
        {"id": "b-2", "item_type": "fill_blank", "stem": "S2", "answer": {"text": "cell"}, "objective_ref": "o2"},
    ]
    qs = bank_questions(items)
    # The player's ids stay q1.. (the skipped subjective item takes none); the bank identity rides along.
    assert [(q["id"], q["bank_item_id"], q["objective_ref"], q["teaching_objective_id"]) for q in qs] == [
        ("q1", "b-1", "o1", "to-77"), ("q2", "b-2", "o2", None)]


def test_the_written_quiz_file_carries_the_bank_identity_but_not_for_a_student(tmp_path, uploads):
    import json

    bank = _objective_bank([("true_false", 1, {"value": False}, {"teaching_objective_id": "to-1"}),
                            ("fill_blank", 2, {"text": "XQZFILL"}, {})])
    sb = _sb(bank, {**SET, "question_ids": [it["id"] for it in bank]}, BLUEPRINT)
    render_question_set(sb, GEN, "gen-ws", tmp_path, "owner/gen-ws", {}, "en")
    qs = json.loads((tmp_path / "questions.json").read_text(encoding="utf-8"))["questions"]
    by_bank = {q["bank_item_id"]: q for q in qs}
    assert set(by_bank) == {"b001", "b002"}, "both gradable items traced to their topic_questions rows"
    assert by_bank["b001"]["id"] == "q1" and by_bank["b001"]["objective_ref"] == "o1"
    assert by_bank["b001"]["teaching_objective_id"] == "to-1"
    assert by_bank["b002"]["id"] == "q2" and by_bank["b002"]["objective_ref"] == "o2" and by_bank["b002"]["teaching_objective_id"] is None
    # The app's stripQuiz builds the student copy field by field (id, type, prompt, marks, options/left, image),
    # so these keys are server-only by construction; the answer fields the file also holds are the proof of the contract.
    assert by_bank["b001"]["answer"] is False


def test_a_quiz_upload_failure_degrades_to_print_only(tmp_path, uploads, monkeypatch):
    real = composer.db.upload_artifact

    def flaky(sb, local_path, dest_path):
        if dest_path.endswith("questions.json"):
            raise RuntimeError("storage 503")
        return real(sb, local_path, dest_path)

    monkeypatch.setattr(composer.db, "upload_artifact", flaky)
    sb = _sb(_bank(n_per=1, difficulties=(1, 2)), SET, BLUEPRINT)
    assert render_question_set(sb, GEN, "gen-ws", tmp_path, "b", {}, "en") == "Cells · Quick check"
    assert [a["kind"] for a in sb.tables["artifacts"]] == ["docx", "answer_key_docx"]
