"""``topic_questions``: an approved article becomes draft question-bank items
— one model call, validated in code, hash-deduplicated, one coverage top-up.

Everything here CALLS things. The model is a fake that returns canned JSON
and records every prompt; the database is the fake Supabase in
tests/catalogue_fakes.py, which honours the unique (topic, language,
content_hash) index on topic_questions and records every write.
"""

from __future__ import annotations

import copy

from catalogue import questions
from catalogue.key import canonical_key
from catalogue.questions import (
    DEFAULT_TARGET, INSERT_CHUNK, MAX_TOKENS, MIN_PER_OBJECTIVE, QUESTIONS_PROMPT, RESPONSE_SCHEMA, SYSTEM_PROMPT,
    TYPE_DEFAULTS, content_hash, mix_for, read_target, run_questions_job, validate_item, validate_items,
)
from tests.catalogue_fakes import FakeSB

JOB = "job-tq"
TOPIC = {"id": "t-cell", "canonical_key": "cell", "title": "Cells", "subject": "Biology", "status": "article_approved"}
ARTICLE = {
    "id": "art-1", "topic_id": "t-cell", "language": "en", "version": 1, "status": "approved",
    "title": "Cells: the basic unit of life",
    "objectives": [{"id": "o1", "text": "State that all organisms are made of cells."},
                   {"id": "o2", "text": "Describe the function of each cell structure."},
                   {"id": "o3", "text": "Compare plant and animal cells."}],
    "sections": [{"id": "s1", "heading": "What a cell is", "body_md": "Every organism is built from cells.", "figure_keys": [], "covers": ["7Bs.01"]},
                 {"id": "s2", "heading": "Structures common to all cells", "body_md": "The nucleus holds the genetic material.", "figure_keys": ["animal_cell"], "covers": ["7Bs.02"]},
                 {"id": "s3", "heading": "Plant cells", "body_md": "Only plant cells have a cell wall and chloroplasts.", "figure_keys": ["plant_cell"], "covers": ["7Bs.02"]}],
    "glossary": [{"term": "cell", "definition": "The smallest unit of life."}],
    "misconceptions": [{"id": "m1", "misconception": "Animal cells have cell walls.", "correction": "Only plant cells do."},
                       {"id": "m2", "misconception": "The nucleus thinks.", "correction": "It stores instructions."}],
    "worked_examples": [],
    "claims": [{"id": "c1", "text": "All organisms are made of cells.", "section_id": "s1"},
               {"id": "c2", "text": "The nucleus holds the genetic material.", "section_id": "s2"},
               {"id": "c3", "text": "Only plant cells have a cell wall.", "section_id": "s3"}],
}
FIGURES = [
    {"id": "fig-1", "article_id": "art-1", "figure_key": "plant_cell", "caption": "A plant cell", "sort": 1, "status": "rendered",
     "labels": [{"group_id": "cell_wall", "label": "cell wall"}, {"group_id": "nucleus", "label": "nucleus"},
                {"group_id": "chloroplast", "label": "chloroplasts"}, {"group_id": "sap_vacuole", "label": "sap vacuole"}]},
    {"id": "fig-0", "article_id": "art-1", "figure_key": "animal_cell", "caption": "An animal cell", "sort": 0, "status": "draft",
     "labels": [{"group_id": "nucleus", "label": "nucleus"}]},
]


def _mcq(stem, obj="o1", claim="c1", answer="B", **over):
    item = {"item_type": "mcq", "objective_ref": obj, "claim_ref": claim, "difficulty": 2, "cognitive_level": "understand",
            "marks": 1, "est_seconds": 60, "stem": stem,
            "options": [{"key": "A", "text": "Nucleus"}, {"key": "B", "text": "Cell membrane"},
                        {"key": "C", "text": "Cytoplasm"}, {"key": "D", "text": "Mitochondrion"}],
            "answer": answer,
            "distractor_rationale": [{"key": "A", "why_wrong": "Confuses control with the boundary.", "misconception_ref": "m2"},
                                     {"key": "C", "why_wrong": "The cytoplasm is the fluid, not the boundary."},
                                     {"key": "D", "why_wrong": "Mitochondria release energy."}],
            "marking_scheme": [{"point": "B", "marks": 1}], "explanation": "The membrane controls entry and exit.", "tags": ["cell membrane"]}
    item.update(over)
    return item


GOOD = {"items": [
    _mcq("Which structure controls what enters and leaves the cell?", "o2", "c2"),
    _mcq("Which structure holds the genetic material?", "o2", "c2", answer="A",
         distractor_rationale=[{"key": "B", "why_wrong": "The membrane is the boundary."}, {"key": "C", "why_wrong": "Fluid."}, {"key": "D", "why_wrong": "Energy."}]),
    {"item_type": "true_false", "objective_ref": "o1", "claim_ref": "c1", "difficulty": 1, "cognitive_level": "recall", "marks": 1, "est_seconds": 30,
     "stem": "Every living organism is made of at least one cell.", "answer": "true", "explanation": "Cell theory.", "tags": ["cells"]},
    {"item_type": "fill_blank", "objective_ref": "o1", "claim_ref": "", "difficulty": 1, "cognitive_level": "recall",
     "stem": "A ____ is the smallest unit that carries out the processes of life.", "answer": "cell", "explanation": "Definition."},
    {"item_type": "match", "objective_ref": "o2", "difficulty": 2, "cognitive_level": "understand", "stem": "Match each structure to its job.",
     "pairs": [{"left": "Nucleus", "right": "Holds the genetic material"}, {"left": "Cell membrane", "right": "Controls what enters and leaves"},
               {"left": "Mitochondrion", "right": "Releases energy"}], "answer": "", "explanation": "Structure and function."},
    {"item_type": "assertion_reason", "objective_ref": "o3", "claim_ref": "c3", "difficulty": 4, "cognitive_level": "analyse",
     "stem": "Assertion (A): Root cells have no chloroplasts. Reason (R): Roots do not receive light.",
     "options": [{"key": "A", "text": "Both A and R are true, and R explains A."}, {"key": "B", "text": "Both A and R are true, but R does not explain A."},
                 {"key": "C", "text": "A is true, but R is false."}, {"key": "D", "text": "A is false, but R is true."}],
     "answer": "A", "explanation": "Chloroplasts are only useful in light."},
    {"item_type": "short_answer", "objective_ref": "o3", "claim_ref": "c3", "difficulty": 3, "cognitive_level": "apply", "marks": 2, "est_seconds": 120,
     "stem": "A cell has a cell wall and chloroplasts. Which kind of cell is it, and how do you know?",
     "answer": "A plant cell: only plant cells have a cell wall and chloroplasts.",
     "marking_scheme": [{"point": "plant cell", "marks": 1}, {"point": "cell wall and chloroplasts named", "marks": 1}], "explanation": "Two plant-only structures."},
    {"item_type": "long_answer", "objective_ref": "o3", "difficulty": 4, "cognitive_level": "evaluate", "marks": 5, "est_seconds": 480,
     "stem": "Compare the structure of a plant cell and an animal cell.", "answer": "Shared: membrane, nucleus, cytoplasm, mitochondria. Plant-only: wall, chloroplasts, vacuole.",
     "marking_scheme": [{"point": "shared structures", "marks": 2}, {"point": "plant-only structures", "marks": 3}], "explanation": "Compare on both sides."},
    {"item_type": "diagram_label", "objective_ref": "o3", "claim_ref": "c3", "difficulty": 2, "cognitive_level": "recall", "figure_key": "plant_cell",
     "stem": "The plant cell figure: part 1 is the rigid outer layer, part 2 is green, part 3 is the large central space.",
     "labels": [{"n": 1, "label": "Cell wall"}, {"n": 2, "label": "chloroplast"}, {"n": 3, "label": "sap vacuole"}], "answer": "", "explanation": "Three plant-only parts."},
    {"item_type": "numerical", "objective_ref": "o1", "difficulty": 3, "cognitive_level": "apply", "marks": 3, "est_seconds": 180,
     "stem": "A cell is 0.02 mm across. How many fit side by side across 1 mm?", "answer": "50", "unit": "cells", "tolerance": 0,
     "marking_scheme": [{"point": "1 / 0.02", "marks": 2}, {"point": "50", "marks": 1}], "explanation": "Divide the width."},
]}


class FakeModel:
    """``analyze`` records every call and answers ``reply`` (a list answers
    call by call; an exception is raised; a callable is called with the
    prompt). Returns the real clients' shape ``{"data", "usage", "truncated"}``."""

    def __init__(self, reply=None):
        self.reply = GOOD if reply is None else reply
        self.calls = []
        self.session_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def analyze(self, prompt, system=None, max_tokens=None, retries=3, cache_prefix=None, response_schema=None):
        self.calls.append({"prompt": prompt, "system": system, "max_tokens": max_tokens, "schema": response_schema})
        reply = self.reply
        if isinstance(reply, list):
            reply = reply[min(len(self.calls) - 1, len(reply) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = reply(prompt)
        self.session_usage["calls"] += 1
        self.session_usage["input_tokens"] += len(prompt) // 4
        self.session_usage["output_tokens"] += 3000
        return {"data": copy.deepcopy(reply), "usage": {"calls": 1}, "truncated": False}


def _job(**params):
    return {"id": JOB, "type": "topic_questions", "status": "processing", "generation_id": None, "book_id": None,
            "params": {"topic_id": "t-cell", **params}}


def _sb(article=ARTICLE, figures=FIGURES, existing=(), job=None):
    sb = FakeSB()
    sb.tables["topics"] = [dict(TOPIC)]
    sb.tables["topic_articles"] = [dict(article)] if article else []
    sb.tables["article_figures"] = [dict(f) for f in figures]
    sb.tables["topic_questions"] = [dict(r) for r in existing]
    sb.tables["jobs"] = [job or _job()]
    sb.tables["generations"] = [{"id": "gen-other", "status": "done"}]
    return sb


def _bank(sb):
    return sb.tables["topic_questions"]


def _job_row(sb):
    return sb.tables["jobs"][0]


def _art_ctx():
    return {**ARTICLE, "figures": questions.labelled_figures(FIGURES)}


# ── the job, end to end ─────────────────────────────────────────────────


def test_a_good_reply_becomes_draft_items_with_the_bank_s_columns_and_a_summary():
    sb, model = _sb(), FakeModel()
    summary = run_questions_job(sb, _job(), client=model)

    assert len(model.calls) == 1, "every objective has ≥2 items, so no top-up call"
    (call,) = model.calls
    assert call["system"] == SYSTEM_PROMPT and call["schema"] is RESPONSE_SCHEMA and call["max_tokens"] == MAX_TOKENS
    rows = _bank(sb)
    assert len(rows) == 10 == summary["written"]
    assert {r["status"] for r in rows} == {"draft"}
    assert all(r["topic_id"] == "t-cell" and r["article_id"] == "art-1" and r["language"] == "en" for r in rows)
    assert [r["item_type"] for r in rows] == ["mcq", "mcq", "true_false", "fill_blank", "match", "assertion_reason",
                                              "short_answer", "long_answer", "diagram_label", "numerical"]
    by_type = {r["item_type"]: r for r in rows}
    mcq = rows[0]
    assert mcq["answer_mode"] == "objective" and mcq["difficulty"] == 2 and mcq["cognitive_level"] == "understand"
    assert mcq["objective_ref"] == "o2" and mcq["claim_ref"] == "c2" and mcq["marks"] == 1 and mcq["est_seconds"] == 60
    assert [o["key"] for o in mcq["options"]] == ["A", "B", "C", "D"] and mcq["answer"] == {"key": "B"}
    assert mcq["distractor_rationale"] == {
        "A": {"why_wrong": "Confuses control with the boundary.", "misconception_ref": "m2"},
        "C": {"why_wrong": "The cytoplasm is the fluid, not the boundary."},
        "D": {"why_wrong": "Mitochondria release energy."}}
    assert mcq["content_hash"] == content_hash("mcq", mcq["stem"])
    assert mcq["tags"] == ["cell membrane"] and mcq["explanation"].startswith("The membrane")
    assert by_type["true_false"]["answer"] == {"value": True}
    assert by_type["fill_blank"]["answer"] == {"text": "cell"} and by_type["fill_blank"]["claim_ref"] is None
    assert by_type["fill_blank"]["marks"] == 1 and by_type["fill_blank"]["est_seconds"] == 45, "type defaults"
    match = by_type["match"]
    assert match["options"] == match["answer"] and len(match["answer"]["pairs"]) == 3 and match["marks"] == 3
    assert by_type["assertion_reason"]["answer"] == {"key": "A"} and by_type["assertion_reason"]["distractor_rationale"] is None
    assert by_type["short_answer"]["answer_mode"] == "subjective" and by_type["short_answer"]["answer"] == {
        "text": "A plant cell: only plant cells have a cell wall and chloroplasts."}
    assert by_type["long_answer"]["marks"] == 5 and by_type["long_answer"]["marking_scheme"][1] == {"point": "plant-only structures", "marks": 3}
    diag = by_type["diagram_label"]
    assert diag["options"] == {"figure_key": "plant_cell", "caption": "A plant cell"}
    # Labels are the FIGURE's labels (case, plural folded) in the item's order, marks = one per label.
    assert diag["answer"] == {"labels": [{"n": 1, "label": "cell wall"}, {"n": 2, "label": "chloroplasts"}, {"n": 3, "label": "sap vacuole"}]}
    assert diag["marks"] == 3
    assert by_type["numerical"]["answer"] == {"value": 50.0, "unit": "cells", "tolerance": 0.0}

    job = _job_row(sb)
    assert job["status"] == "done" and job["progress"] == 100 and job["error"] is None
    assert job["stage"] == summary
    assert summary["step"] == "done" and summary["topic_id"] == "t-cell" and summary["language"] == "en"
    assert summary["article_id"] == "art-1" and summary["requested"] == DEFAULT_TARGET
    assert summary["duplicates"] == 0 and summary["rejected"] == [] and summary["repairs"] == []
    assert summary["coverage"] == {"o1": 3, "o2": 3, "o3": 4} and summary["topup"] is None and summary["still_under"] == []
    assert job["usage"]["calls"] == 1
    assert sb.writes("generations") == [], "an observer job never writes a generation"


def test_the_prompt_carries_the_article_the_ids_the_figures_the_mix_and_the_contract():
    sb, model = _sb(), FakeModel()
    run_questions_job(sb, _job(hints="Fewer recall items.\nAsk about vacuoles."), client=model)
    prompt = model.calls[0]["prompt"]
    assert "Topic: Cells" in prompt and "Subject: Biology" in prompt and "Language of the items: en" in prompt
    assert "o1: State that all organisms are made of cells." in prompt and "m1: Animal cells have cell walls. → Only plant cells do." in prompt
    assert "c3 (s3): Only plant cells have a cell wall." in prompt and "## s2 — Structures common to all cells" in prompt
    assert 'plant_cell — "A plant cell": cell wall, nucleus, chloroplasts, sap vacuole' in prompt
    assert "animal_cell" not in prompt.split("FIGURES")[1], "a draft (unrendered) figure is not offered for labelling"
    # Biology → the numerical slots fold into short answers; labelled figures → diagram_label slots stay.
    assert "REQUESTED ITEMS (30 in total" in prompt and "  10 × short_answer" in prompt and "numerical" not in prompt.split("REQUESTED ITEMS")[1].split("RULES")[0]
    assert "  2 × diagram_label" in prompt and "  8 × mcq" in prompt
    assert "REVIEWER NOTES (address every note):\nFewer recall items.\nAsk about vacuoles." in prompt
    assert prompt.rstrip().endswith(QUESTIONS_PROMPT.format(MIN_PAIRS=3, MIN_LABELS=2).rstrip())
    for phrase in ('"objective_ref"', "why_wrong", "misconception_ref", "at least two items per objective", "British English"):
        assert phrase in prompt, phrase


def test_target_is_honoured_in_range_and_defaults_outside_it():
    assert read_target(12) == 12 and read_target("12") == 12 and read_target(5) == 5 and read_target(60) == 60
    assert read_target(4) == DEFAULT_TARGET and read_target(61) == DEFAULT_TARGET and read_target(None) == DEFAULT_TARGET
    assert read_target(12.5) == DEFAULT_TARGET and read_target(True) == DEFAULT_TARGET
    sb, model = _sb(), FakeModel()
    run_questions_job(sb, _job(target=12), client=model)
    prompt = model.calls[0]["prompt"]
    assert "REQUESTED ITEMS (12 in total" in prompt
    assert sum(mix_for(12, False, True).values()) == 12
    assert _job_row(sb)["stage"]["requested"] == 12
    # The default mix is decision 8's, exactly.
    assert mix_for(30, True, True) == {"mcq": 8, "true_false": 3, "fill_blank": 2, "match": 1, "assertion_reason": 1,
                                       "short_answer": 8, "long_answer": 3, "numerical": 2, "diagram_label": 2}
    assert mix_for(30, False, False)["short_answer"] == 12 and "numerical" not in mix_for(30, False, False)


def test_numerical_slots_open_for_a_physics_topic_or_a_worked_example():
    assert questions.numerical_allowed({"subject": "Physics"}, ARTICLE) is True
    assert questions.numerical_allowed({"subject": "Biology"}, ARTICLE) is False
    assert questions.numerical_allowed({"subject": "Biology"}, {**ARTICLE, "worked_examples": [{"id": "w1", "problem": "p", "solution_md": "s"}]}) is True


def test_no_rendered_figure_means_no_diagram_label_slot():
    sb, model = _sb(figures=[]), FakeModel()
    run_questions_job(sb, _job(), client=model)
    prompt = model.calls[0]["prompt"]
    assert "FIGURES: none are rendered yet" in prompt and "diagram_label" not in prompt.split("REQUESTED ITEMS")[1].split("RULES")[0]
    # The canned reply still carries a diagram_label item: rejected, because no figure is labelled.
    summary = _job_row(sb)["stage"]
    assert summary["written"] == 9 and any("diagram_label" in r and "not a rendered" in r for r in summary["rejected"])


# ── refusals ────────────────────────────────────────────────────────────


def test_an_unapproved_article_is_refused_before_any_model_call():
    sb, model = _sb(article={**ARTICLE, "status": "draft"}), FakeModel()
    assert run_questions_job(sb, _job(), client=model) is None
    assert model.calls == [] and _bank(sb) == []
    job = _job_row(sb)
    assert job["status"] == "error" and "no approved article" in job["error"]
    # Named explicitly: refused with the status in the message.
    sb2, model2 = _sb(article={**ARTICLE, "status": "draft"}), FakeModel()
    run_questions_job(sb2, _job(article_id="art-1"), client=model2)
    assert model2.calls == [] and "is draft, not approved" in _job_row(sb2)["error"]


def test_a_named_article_must_belong_to_the_job_s_topic_and_language():
    sb, model = _sb(article={**ARTICLE, "topic_id": "t-other"}), FakeModel()
    run_questions_job(sb, _job(article_id="art-1"), client=model)
    assert model.calls == [] and "belongs to topic t-other" in _job_row(sb)["error"]
    sb2, model2 = _sb(), FakeModel()
    run_questions_job(sb2, _job(article_id="art-1", language="fr"), client=model2)
    assert model2.calls == [] and "in en, not to this job's topic t-cell in fr" in _job_row(sb2)["error"]


def test_the_highest_approved_version_is_used_when_none_is_named():
    sb = _sb()
    sb.tables["topic_articles"] = [{**ARTICLE, "id": "art-1", "version": 1}, {**ARTICLE, "id": "art-2", "version": 2},
                                   {**ARTICLE, "id": "art-3", "version": 3, "status": "draft"}]
    model = FakeModel()
    run_questions_job(sb, _job(), client=model)
    assert {r["article_id"] for r in _bank(sb)} == {"art-2"}


def test_a_missing_topic_or_topic_id_fails_the_row_without_a_call():
    sb, model = _sb(), FakeModel()
    run_questions_job(sb, {**_job(), "params": {}}, client=model)
    assert model.calls == [] and "params.topic_id" in _job_row(sb)["error"]
    sb2, model2 = _sb(), FakeModel()
    sb2.tables["topics"] = []
    run_questions_job(sb2, _job(), client=model2)
    assert model2.calls == [] and "not found" in _job_row(sb2)["error"]


def test_the_entry_point_never_raises_and_records_the_failure():
    sb = _sb()
    assert run_questions_job(sb, _job(), client=FakeModel(RuntimeError("quota exhausted"))) is None
    job = _job_row(sb)
    assert job["status"] == "error" and job["error"] == "RuntimeError: quota exhausted" and _bank(sb) == []
    # A reply with nothing usable is an error too (never a silent empty bank).
    sb2 = _sb()
    run_questions_job(sb2, _job(), client=FakeModel({"items": [{"item_type": "essay", "stem": "x"}]}))
    assert "no usable item" in _job_row(sb2)["error"] and _bank(sb2) == []
    sb3 = _sb()
    run_questions_job(sb3, _job(), client=FakeModel({"nope": 1}))
    assert "no 'items' list" in _job_row(sb3)["error"]


def test_the_client_is_built_for_the_job_s_language(monkeypatch):
    seen = []

    def fake_client_for(lang, **kw):
        seen.append(lang)
        return FakeModel()

    monkeypatch.setattr(questions, "client_for", fake_client_for)
    sb = _sb(article={**ARTICLE, "language": "fr"})
    run_questions_job(sb, _job(language="FR "))
    assert seen == ["fr"] and {r["language"] for r in _bank(sb)} == {"fr"}


def test_a_finished_earlier_attempt_is_not_re_asked():
    done = {"phase": "questions", "step": "done", "topic_id": "t-cell", "written": 10}
    sb, model = _sb(job={**_job(), "stage": done}), FakeModel()
    summary = run_questions_job(sb, _job(), client=model)  # the dispatcher's copy may lack the stage: read from the row
    assert model.calls == [] and _bank(sb) == []
    assert summary["step"] == "already_done" and summary["written"] == 10
    assert _job_row(sb)["status"] == "done"


# ── the validator ───────────────────────────────────────────────────────


def test_mcq_rules_reject_a_missing_why_wrong_an_alien_answer_and_duplicate_options():
    art = _art_ctx()
    ok, notes = validate_item(_mcq("Which structure controls what enters and leaves the cell?", "o2", "c2"), art)
    assert ok is not None and notes == []

    item, reasons = validate_item(_mcq("Q?", distractor_rationale=[{"key": "A", "why_wrong": "x"}, {"key": "C", "why_wrong": "y"}]), art)
    assert item is None and reasons == ["mcq: distractor D lacks why_wrong"]
    item, reasons = validate_item(_mcq("Q?", distractor_rationale={"A": "x", "C": {"why_wrong": ""}, "D": "z"}), art)
    assert item is None and reasons == ["mcq: distractor C lacks why_wrong"]

    item, reasons = validate_item(_mcq("Q?", answer="E"), art)
    assert item is None and reasons == ["mcq: answer 'E' is not among the options"]
    item, reasons = validate_item(_mcq("Q?", answer={"key": "Z"}), art)
    assert item is None

    dup = _mcq("Q?")
    dup["options"][2]["text"] = "cell membrane"  # case-insensitive duplicate of B
    item, reasons = validate_item(dup, art)
    assert item is None and reasons == ["mcq: duplicate option texts"]

    three = _mcq("Q?")
    three["options"] = three["options"][:3]
    item, reasons = validate_item(three, art)
    assert item is None and reasons == ["mcq: 3 option(s); exactly 4 keyed A-D required"]


def test_mcq_options_and_answers_are_read_in_the_shapes_models_actually_send():
    art = _art_ctx()
    # Bare strings keyed in order; the answer given as the option's text; rationale as an object.
    raw = _mcq("Q?", options=["Nucleus", "Cell membrane", "Cytoplasm", "Mitochondrion"], answer="Cell membrane",
               distractor_rationale={"A": "a", "C": "c", "D": "d"})
    item, notes = validate_item(raw, art)
    assert item["answer"] == {"key": "B"} and [o["key"] for o in item["options"]] == ["A", "B", "C", "D"]
    assert item["distractor_rationale"] == {"A": {"why_wrong": "a"}, "C": {"why_wrong": "c"}, "D": {"why_wrong": "d"}}
    # Keys out of order keep their letters: "B)" still names the membrane.
    raw = _mcq("Q?", options=[{"key": "D", "text": "Mitochondrion"}, {"key": "B", "text": "Cell membrane"},
                              {"key": "A", "text": "Nucleus"}, {"key": "C", "text": "Cytoplasm"}], answer="B)")
    item, _ = validate_item(raw, art)
    assert [o["text"] for o in item["options"]] == ["Nucleus", "Cell membrane", "Cytoplasm", "Mitochondrion"] and item["answer"] == {"key": "B"}
    # A rationale for the answer itself, or an unknown misconception, is dropped with a note — never fatal.
    raw = _mcq("Q?", distractor_rationale=[{"key": "A", "why_wrong": "a", "misconception_ref": "m9"}, {"key": "B", "why_wrong": "?"},
                                           {"key": "C", "why_wrong": "c"}, {"key": "D", "why_wrong": "d"}])
    item, notes = validate_item(raw, art)
    assert "misconception_ref" not in item["distractor_rationale"]["A"]
    assert sorted(notes) == ["misconception reference dropped: m9", "rationale for B ignored (the answer or not an option)"]


def test_enums_and_references_are_enforced_or_repaired():
    art = _art_ctx()
    base = GOOD["items"][2]  # a true_false
    assert validate_item({**base, "difficulty": 6}, art) == (None, ["true_false: difficulty 6 not in 1..5"])
    assert validate_item({**base, "difficulty": "hard"}, art)[0] is None
    assert validate_item({**base, "cognitive_level": "guessing"}, art) == (None, ["true_false: cognitive_level 'guessing' not in the enum"])
    item, notes = validate_item({**base, "cognitive_level": "Analyze", "answer_mode": "subjective"}, art)
    assert item["cognitive_level"] == "analyse" and item["answer_mode"] == "objective"
    assert notes == ["answer_mode subjective corrected to objective (true_false)"]
    assert validate_item({**base, "item_type": "essay"}, art) == (None, ["unknown item_type: essay"])
    assert validate_item({**base, "item_type": "True-False"}, art)[0]["item_type"] == "true_false"
    assert validate_item({**base, "stem": "  "}, art) == (None, ["true_false: empty stem"])
    assert validate_item("not an object", art) == (None, ["item is not an object"])
    # An unknown objective falls back to the nearest by content words (the stem names cells and organisms → o1).
    item, notes = validate_item({**base, "objective_ref": "o9", "claim_ref": "c9"}, art)
    assert item["objective_ref"] == "o1" and item["claim_ref"] is None
    assert notes == ["claim reference dropped: c9", "objective o9 unknown; nearest is o1"]
    # Nothing shared (and no claim to steer by) → None, with the note.
    item, notes = validate_item({**base, "objective_ref": "", "claim_ref": "", "stem": "Zzz qqq?"}, art)
    assert item["objective_ref"] is None and notes == ["objective ? unknown; none near"]
    # The cited claim's section steers the fallback: a stem about "this structure" + claim c3 → o3 (plant/animal).
    item, _ = validate_item({**base, "objective_ref": "x", "claim_ref": "c3", "stem": "Is this only in one kind?"}, art)
    assert item["objective_ref"] == "o3"


def test_marks_and_seconds_default_by_type_and_a_scheme_s_sum_wins():
    art = _art_ctx()
    short = GOOD["items"][6]
    item, notes = validate_item({**short, "marks": None, "est_seconds": None, "marking_scheme": []}, art)
    assert item["marks"] == TYPE_DEFAULTS["short_answer"][0] and item["est_seconds"] == TYPE_DEFAULTS["short_answer"][1]
    assert item["marking_scheme"] == [{"point": item["answer"]["text"], "marks": 2}]
    assert notes == ["marking scheme defaulted to one point (short_answer)"]
    item, notes = validate_item({**short, "marks": 0}, art)
    assert item["marks"] == 2 and "marks 0 replaced by the short_answer default" in notes
    item, notes = validate_item({**short, "marks": 7}, art)  # the scheme sums to 2
    assert item["marks"] == 2 and notes == ["marks 7 set to the marking scheme's 2 (short_answer)"]
    tf, _ = validate_item(GOOD["items"][2], art)
    assert tf["marking_scheme"] == [], "objective items need no scheme"


def test_per_type_answer_shapes():
    art = _art_ctx()
    tf = GOOD["items"][2]
    assert validate_item({**tf, "answer": "FALSE"}, art)[0]["answer"] == {"value": False}
    assert validate_item({**tf, "answer": {"value": True}}, art)[0]["answer"] == {"value": True}
    assert validate_item({**tf, "answer": "maybe"}, art) == (None, ["true_false: answer 'maybe' is not true/false"])
    fill = GOOD["items"][3]
    assert validate_item({**fill, "stem": "No blank here."}, art) == (None, ["fill_blank: stem has no blank"])
    assert validate_item({**fill, "answer": {"text": "cell", "accept": ["cells"]}}, art)[0]["answer"] == {"text": "cell", "accept": ["cells"]}
    assert validate_item({**fill, "answer": ""}, art) == (None, ["fill_blank: empty answer"])
    match = GOOD["items"][4]
    assert validate_item({**match, "pairs": match["pairs"][:2]}, art) == (None, ["match: 2 pair(s); 3-8 required"])
    twin = [dict(p) for p in match["pairs"]]
    twin[2]["right"] = twin[0]["right"]
    assert validate_item({**match, "pairs": twin}, art) == (None, ["match: duplicate pair texts"])
    row_shaped = {**match, "pairs": None, "options": {"pairs": match["pairs"]}, "answer": {"pairs": match["pairs"]}}
    assert len(validate_item(row_shaped, art)[0]["answer"]["pairs"]) == 3
    num = GOOD["items"][9]
    assert validate_item({**num, "answer": "about 50 cells"}, art)[0]["answer"]["value"] == 50.0
    assert validate_item({**num, "answer": {"value": 5e1, "unit": "cells", "tolerance": -2}}, art)[0]["answer"] == {"value": 50.0, "unit": "cells", "tolerance": 2.0}
    assert validate_item({**num, "answer": "fifty"}, art) == (None, ["numerical: answer 'fifty' is not a number"])
    diag = GOOD["items"][8]
    assert validate_item({**diag, "figure_key": "animal_cell"}, art) == (None, ["diagram_label: figure animal_cell is not a rendered, labelled figure"])
    item, notes = validate_item({**diag, "labels": [{"n": 1, "label": "cell wall"}, {"n": 2, "label": "ribosome"}, {"n": 3, "label": "Nucleus"}]}, art)
    assert item["answer"]["labels"] == [{"n": 1, "label": "cell wall"}, {"n": 2, "label": "nucleus"}]
    assert notes == ["diagram label dropped (not on plant_cell): ribosome"]
    # One mark per label is the type's own scheme — derived, not a repair.
    assert item["marks"] == 2 and item["marking_scheme"] == [{"point": "cell wall", "marks": 1}, {"point": "nucleus", "marks": 1}]
    assert validate_item({**diag, "labels": [{"n": 1, "label": "ribosome"}]}, art)[0] is None
    assert validate_item(diag, {**ARTICLE})[0] is None, "no figures known → no diagram_label"
    ar = GOOD["items"][5]
    assert validate_item({**ar, "answer": "E"}, art)[0] is None
    assert validate_item(ar, art)[0]["distractor_rationale"] is None


def test_content_hash_folds_case_punctuation_and_plurals_but_not_the_type():
    a = content_hash("mcq", "What are cells?")
    assert a == content_hash("mcq", "  what ARE cell!! ") == content_hash("mcq", "What-are-Cells")
    assert a != content_hash("short_answer", "What are cells?"), "the same stem as another type is another item"
    assert a != content_hash("mcq", "What are the cells?"), "canonical_key drops only a LEADING article"
    assert content_hash("mcq", "The cells") == content_hash("mcq", "cell")
    assert a == questions.content_hash("mcq", canonical_key("What are cells?").replace("_", " "))
    items, rejected, repairs = validate_items({"items": [GOOD["items"][3], {**GOOD["items"][3], "stem": "A ____ IS the smallest unit that carries out the processes of life?"}]}, _art_ctx())
    assert len(items) == 1 and items[0]["stem"] == GOOD["items"][3]["stem"], "the first of two rewordings is kept"
    (reason,) = rejected
    assert reason.startswith("fill_blank: duplicate in reply (A ____ IS the smallest unit") and repairs == []


# ── writing: chunks, duplicates, the race ──────────────────────────────


def test_hashes_already_in_the_bank_are_duplicates_not_rows_or_failures():
    existing = [{"id": "old-1", "topic_id": "t-cell", "language": "en", "status": "approved", "item_type": "mcq",
                 "content_hash": content_hash("mcq", "Which structure controls what enters and leaves the cell?")},
                {"id": "old-fr", "topic_id": "t-cell", "language": "fr", "status": "approved", "item_type": "mcq",
                 "content_hash": content_hash("mcq", "Which structure holds the genetic material?")}]
    sb, model = _sb(existing=existing), FakeModel()
    summary = run_questions_job(sb, _job(), client=model)
    assert summary["written"] == 9 and summary["duplicates"] == 1
    assert len(_bank(sb)) == 2 + 9
    assert _job_row(sb)["status"] == "done"


def test_a_23505_race_on_a_chunk_is_absorbed_row_by_row(monkeypatch):
    # The pre-check sees an empty bank; the index does not — exactly the race a second replica causes.
    monkeypatch.setattr(questions, "existing_hashes", lambda sb, t, lang: set())
    existing = [{"id": "old-1", "topic_id": "t-cell", "language": "en", "status": "approved", "item_type": "mcq",
                 "content_hash": content_hash("mcq", "Which structure controls what enters and leaves the cell?")}]
    sb, model = _sb(existing=existing), FakeModel()
    summary = run_questions_job(sb, _job(), client=model)
    assert summary["written"] == 9 and summary["duplicates"] == 1 and _job_row(sb)["status"] == "done"
    inserts = [e for e in sb.log if e[0] == "insert" and e[1] == "topic_questions"]
    assert len(inserts[0][2]) == 10, "the chunk was tried whole first"
    assert all(len(e[2]) == 1 for e in inserts[1:]) and len(inserts) == 1 + 10, "then row by row"
    # coverage counts what THIS job landed: the o2 item the other replica wrote is not ours
    assert summary["coverage"] == {"o1": 3, "o2": 2, "o3": 4} and sum(summary["coverage"].values()) == summary["written"]


def test_rows_lost_to_the_race_do_not_hide_an_under_covered_objective(monkeypatch):
    """Both o2 MCQs collide with rows another replica wrote; only the match
    item is ours for o2 → one item → the top-up must run. Counting the
    prepared rows instead of the written ones read o2 as 3 and skipped it."""
    monkeypatch.setattr(questions, "existing_hashes", lambda sb, t, lang: set())
    existing = [{"id": "old-1", "topic_id": "t-cell", "language": "en", "status": "approved", "item_type": "mcq",
                 "content_hash": content_hash("mcq", "Which structure controls what enters and leaves the cell?")},
                {"id": "old-2", "topic_id": "t-cell", "language": "en", "status": "approved", "item_type": "mcq",
                 "content_hash": content_hash("mcq", "Which structure holds the genetic material?")}]
    topup_reply = {"items": [_mcq("Which structure releases energy for the cell?", "o2", "c2")]}
    sb, model = _sb(existing=existing), FakeModel([GOOD, topup_reply])
    summary = run_questions_job(sb, _job(), client=model)
    assert len(model.calls) == 2 and summary["topup"]["objectives"] == ["o2"] and summary["topup"]["written"] == 1
    assert summary["written"] == 8 + 1 and summary["duplicates"] == 2
    assert summary["coverage"] == {"o1": 3, "o2": 2, "o3": 4} and summary["still_under"] == []


# ── the two types whose stem is only an instruction hash their content ──


def test_two_match_items_with_the_same_instruction_but_different_pairs_are_two_items():
    m1 = GOOD["items"][4]                                    # "Match each structure to its job."
    m2 = {**m1, "pairs": [{"left": "Cell wall", "right": "Supports the plant cell"},
                          {"left": "Chloroplast", "right": "Makes food by photosynthesis"},
                          {"left": "Vacuole", "right": "Stores water and salts"}]}
    reordered = {**m1, "pairs": list(reversed(m1["pairs"]))}
    rows, rejected, _ = validate_items({"items": [m1, m2, reordered]}, _art_ctx())
    assert len(rows) == 2 and rows[0]["content_hash"] != rows[1]["content_hash"]
    assert rejected == ["match: duplicate in reply (Match each structure to its job.)"], "the same pairs in another order"
    assert content_hash("match", m1["stem"]) != rows[0]["content_hash"], "a match hashes its pairs too"
    assert content_hash("mcq", "Which?", pairs=m1["pairs"]) == content_hash("mcq", "Which?"), "every other type keeps decision 8's formula"
    # and the bank keeps both across two runs (the top-up call answers empty).
    # The second run is a NEW job row, as the portal's "Generate questions"
    # button inserts one: re-running the SAME row is the already_done path
    # (test_a_finished_earlier_attempt_is_not_re_asked) and writes nothing.
    sb = _sb()
    first = run_questions_job(sb, _job(), client=FakeModel([{"items": [m1]}, {"items": []}]))
    assert first["written"] == 1
    again = {**_job(), "id": "job-tq-again"}
    sb.tables["jobs"].append(again)
    model = FakeModel([{"items": [m2]}, {"items": []}])
    summary = run_questions_job(sb, again, client=model)
    assert len(model.calls) >= 1, "the second job is authored, not read back as already done"
    assert summary["written"] == 1 and summary["duplicates"] == 0, "the second match item is not a 'duplicate' any more"
    assert len([r for r in _bank(sb) if r["item_type"] == "match"]) == 2


def test_two_diagram_label_items_on_one_figure_with_different_labels_are_two_items():
    d1 = GOOD["items"][8]                                    # plant_cell: cell wall, chloroplasts, sap vacuole
    d2 = {**d1, "labels": [{"n": 1, "label": "nucleus"}, {"n": 2, "label": "cell wall"}]}
    reordered = {**d1, "labels": list(reversed(d1["labels"]))}
    rows, rejected, _ = validate_items({"items": [d1, d2, reordered]}, _art_ctx())
    assert len(rows) == 2 and rows[0]["content_hash"] != rows[1]["content_hash"]
    assert len(rejected) == 1 and rejected[0].startswith("diagram_label: duplicate in reply")
    assert content_hash("diagram_label", d1["stem"]) != rows[0]["content_hash"]
    assert content_hash("diagram_label", d1["stem"], figure_key="plant_cell", labels=["Cell wall", "chloroplast", "sap vacuole"]) \
        == content_hash("diagram_label", d1["stem"], figure_key="plant_cell", labels=["sap vacuole", "chloroplasts", "cell wall"]), \
        "order and plurals fold, as in every canonical key"


# ── the article is source material, and a runaway reply is capped ───────


def test_the_article_is_fenced_as_source_material_ahead_of_the_task():
    sb, model = _sb(), FakeModel()
    run_questions_job(sb, _job(hints="Ask about vacuoles."), client=model)
    prompt = model.calls[0]["prompt"]
    assert questions.ARTICLE_FENCE_NOTE in prompt and "<article>\n" in prompt and "\n</article>" in prompt
    body = prompt.split("<article>\n")[1].split("\n</article>")[0]           # the note names the tags too
    assert "## s1 — What a cell is" in body and "Glossary:" in body and "REQUESTED ITEMS" not in body and "RULES" not in body
    # The fence note itself names TASK and RULES (so the model knows what DOES
    # direct it), so the order is checked against the section HEADINGS.
    assert prompt.index(questions.ARTICLE_FENCE_NOTE) < prompt.index("<article>\n") < prompt.index("\n</article>") \
        < prompt.index("REQUESTED ITEMS") < prompt.index("REVIEWER NOTES") < prompt.index("TASK:") < prompt.index("\nRULES\n")


def test_a_reply_that_ignores_the_counts_is_capped_at_twice_the_target():
    flood = {"items": [dict(GOOD["items"][3], stem=f"The ____ number {i} unit of life.") for i in range(300)]}
    sb, model = _sb(), FakeModel(flood)                       # the same flood answers the top-up call too
    summary = run_questions_job(sb, _job(target=10), client=model)
    assert questions.REPLY_CAP_FACTOR == 2
    assert summary["written"] == 20 and len(_bank(sb)) == 20, "2 × the target, not 300"
    assert any(r == "over target: 280 valid item(s) beyond the 20 accepted were dropped" for r in summary["rejected"])
    assert summary["topup"]["written"] == 0 and summary["topup"]["duplicates"] == 2 * summary["topup"]["requested"], \
        "the top-up was capped at 2 × its own request, and every capped row was already in the bank"
    assert _job_row(sb)["status"] == "done"


def test_inserts_go_in_chunks_of_twenty_five():
    many = {"items": [dict(GOOD["items"][3], stem=f"The ____ number {i} unit of life.") for i in range(37)]}
    sb, model = _sb(), FakeModel(many)
    summary = run_questions_job(sb, _job(target=40), client=model)
    inserts = [e for e in sb.log if e[0] == "insert" and e[1] == "topic_questions"]
    assert INSERT_CHUNK == 25 and [len(e[2]) for e in inserts[:2]] == [25, 12]
    assert summary["written"] == 37 + summary["topup"]["written"]


def test_a_non_duplicate_insert_error_fails_the_job():
    class Boom(FakeSB):
        def table(self, name):
            q = super().table(name)
            if name == "topic_questions":
                real_execute = q.execute

                def execute():
                    if q.op == "insert":
                        raise RuntimeError("connection reset")
                    return real_execute()
                q.execute = execute
            return q

    sb = Boom()
    sb.tables.update(_sb().tables)
    run_questions_job(sb, _job(), client=FakeModel())
    assert _job_row(sb)["status"] == "error" and "connection reset" in _job_row(sb)["error"]


# ── coverage top-up ─────────────────────────────────────────────────────


def test_objectives_with_fewer_than_two_items_get_one_top_up_call():
    thin = {"items": [it for it in GOOD["items"] if it["objective_ref"] != "o3"]}  # o3: 0 items; o1: 3; o2: 3
    topup_reply = {"items": [
        _mcq("Which structure is found only in plant cells?", "o3", "c3"),
        {**GOOD["items"][6], "stem": "Explain how a wilting plant relates to its vacuoles."},
    ]}
    sb, model = _sb(), FakeModel([thin, topup_reply])
    summary = run_questions_job(sb, _job(), client=model)
    assert len(model.calls) == 2
    second = model.calls[1]["prompt"]
    assert "COVERAGE TOP-UP" in second and "  o3: Compare plant and animal cells." in second
    assert "  o1:" not in second.split("COVERAGE TOP-UP")[1].split("RULES")[0]
    assert f"REQUESTED ITEMS ({MIN_PER_OBJECTIVE} in total" in second and "  1 × mcq" in second and "  1 × short_answer" in second
    assert summary["written"] == 6 + 2 and summary["coverage"] == {"o1": 3, "o2": 3, "o3": 2}
    assert summary["topup"] == {"objectives": ["o3"], "requested": 2, "written": 2, "duplicates": 0, "rejected": 0}
    assert summary["still_under"] == [] and _job_row(sb)["stage"]["topup"]["objectives"] == ["o3"]
    assert "topup_objectives" not in summary, "the in-flight marker is gone from the final summary"
    assert _job_row(sb)["usage"]["calls"] == 2


def test_a_top_up_that_still_falls_short_is_recorded_not_retried_again():
    thin = {"items": [it for it in GOOD["items"] if it["objective_ref"] == "o2"]}  # o1 and o3 thin
    sb, model = _sb(), FakeModel([thin, {"items": [_mcq("Only plants?", "o3", "c3")]}])
    summary = run_questions_job(sb, _job(), client=model)
    assert len(model.calls) == 2, "ONE top-up, never a loop"
    assert summary["topup"]["objectives"] == ["o1", "o3"] and summary["topup"]["requested"] == 4
    assert summary["coverage"] == {"o1": 0, "o2": 3, "o3": 1} and summary["still_under"] == ["o1", "o3"]
    assert _job_row(sb)["status"] == "done"


def test_a_failing_top_up_call_fails_the_job_but_keeps_the_first_write():
    sb = _sb()
    thin = {"items": [it for it in GOOD["items"] if it["objective_ref"] != "o3"]}
    model = FakeModel([thin, RuntimeError("quota")])
    assert run_questions_job(sb, _job(), client=model) is None
    assert len(_bank(sb)) == 6 and _job_row(sb)["status"] == "error"
