"""``topic_article``: a topic becomes a DRAFT knowledge article — one model
call, validated in code, written as a new version with its figure specs.

Everything here CALLS things (see tests/test_worker_entrypoint_runs.py for why
a source-substring test is worth nothing). The model is a fake that returns
canned JSON and records every prompt; the database is the fake Supabase in
tests/catalogue_fakes.py, which honours the unique index on topic_articles and
records every write. No network, no model, no live Supabase.
"""

from __future__ import annotations

import copy

import pytest

from catalogue import article
from catalogue.article import (
    ARTICLE_PROMPT, MAX_FIGURES, MAX_TOKENS, MIN_SECTIONS, RESPONSE_SCHEMA, SYSTEM_PROMPT, ArticleInvalid,
    build_article_prompt, figure_labels, run_article_job, validate_article, word_count,
)
from catalogue.key import canonical_key
from tests.catalogue_fakes import FakeSB

JOB = "job-ta"
TOPIC = {"id": "t-cell", "canonical_key": "cell", "title": "Cells", "subject": "Biology",
         "summary": "The basic unit of life.", "depth_node_id": None, "prerequisites": ["t-living"],
         "status": "approved"}
PREREQ = {"id": "t-living", "canonical_key": "living_thing", "title": "Living things", "subject": "Biology"}
CAM = {"id": "cur-cam", "code": "cambridge_ls_science_0893", "name": "Cambridge Lower Secondary Science 0893"}
CBSE = {"id": "cur-cbse", "code": "cbse_science_086", "name": "CBSE Science"}

NODES = [
    {"id": "n-bs1", "curriculum_id": "cur-cam", "code": "7Bs.01", "grade": "7", "strand": "Biology",
     "sub_strand": "Structure and function", "title": "Understand that all organisms are made of cells and microorganisms are typically single celled.",
     "description": "Understand that all organisms are made of cells and microorganisms are typically single celled.", "kind": "objective"},
    {"id": "n-bs2", "curriculum_id": "cur-cam", "code": "7Bs.02", "grade": "7", "strand": "Biology",
     "sub_strand": "Structure and function", "title": "Identify and describe the functions of cell structures.",
     "description": "Identify and describe the functions of cell structures (limited to cell membrane, cytoplasm, nucleus, cell wall, chloroplast, mitochondria and sap vacuole).", "kind": "objective"},
    {"id": "n-cb1", "curriculum_id": "cur-cbse", "code": "cbse:9:U2:01", "grade": "9", "strand": "Organisation in the Living World",
     "sub_strand": None, "title": "Cell - Basic Unit of life", "description": None, "kind": "topic"},
]
MAPS = [{"topic_id": "t-cell", "node_id": "n-bs1", "coverage": "full"},
        {"topic_id": "t-cell", "node_id": "n-bs2", "coverage": "full"},
        {"topic_id": "t-cell", "node_id": "n-cb1", "coverage": "partial"}]
CODES = ["7Bs.01", "7Bs.02", "cbse:9:U2:01"]


def _job(**extra):
    return {"id": JOB, "type": "topic_article", "status": "processing", "generation_id": None, "book_id": None,
            "params": {"topic_id": "t-cell"}, **extra}


def _sb(topic=TOPIC, nodes=NODES, maps=MAPS, jobs=None, articles=(), figures=()):
    sb = FakeSB()
    sb.tables["topics"] = [dict(t) for t in ([topic] if topic else []) + [PREREQ]]
    sb.tables["curricula"] = [dict(CAM), dict(CBSE)]
    sb.tables["curriculum_nodes"] = [dict(n) for n in nodes]
    sb.tables["topic_curriculum_map"] = [dict(m) for m in maps]
    sb.tables["jobs"] = jobs if jobs is not None else [_job()]
    sb.tables["generations"] = [{"id": "gen-other", "status": "done"}]
    sb.tables["topic_articles"] = [dict(a) for a in articles]
    sb.tables["article_figures"] = [dict(f) for f in figures]
    return sb


# ── the fake model ──────────────────────────────────────────────────────

BODY_1 = ("Every living organism is built from **cells**. A cell is the smallest unit that carries out the "
          "processes of life: it takes in nutrients, releases energy and responds to its surroundings. Some "
          "organisms, such as bacteria, consist of a single cell; a human body contains trillions.")
BODY_2 = ("The **cell membrane** controls what enters and leaves the cell. The **nucleus** holds the genetic "
          "material and directs the cell's activities. The **cytoplasm** is the jelly-like fluid in which "
          "chemical reactions take place. **Mitochondria** release energy from glucose by respiration.")
BODY_3 = ("Plant cells have three structures animal cells lack: a rigid **cell wall** of cellulose, "
          "**chloroplasts** that carry out photosynthesis, and a large **sap vacuole** that stores water and "
          "keeps the cell firm.")
BODY_4 = ("Cells are organised into tissues, tissues into organs, and organs into organ systems. Comparing "
          "cell types shows how structure suits function.")

GOOD = {
    "title": "Cells: the basic unit of life",
    "objectives": [{"id": "o1", "text": "State that all organisms are made of cells."},
                   {"id": "o2", "text": "Describe the function of each cell structure."}],
    "sections": [
        {"id": "s1", "heading": "What a cell is", "body_md": BODY_1, "figure_keys": [], "covers": ["7Bs.01", "cbse:9:U2:01"]},
        {"id": "s2", "heading": "Structures common to all cells", "body_md": BODY_2, "figure_keys": ["animal_cell"], "covers": ["7Bs.02"]},
        {"id": "s3", "heading": "Plant cells", "body_md": BODY_3, "figure_keys": ["plant_cell", "animal_cell"], "covers": ["7Bs.02"]},
        {"id": "s4", "heading": "From cells to organisms", "body_md": BODY_4, "figure_keys": [], "covers": []},
    ],
    "glossary": [{"term": "cell", "definition": "The smallest unit of a living thing that can carry out the processes of life."},
                 {"term": "organelle", "definition": "A structure inside a cell with a particular job."}],
    "misconceptions": [
        {"id": "m1", "misconception": "Plants are not made of cells because they do not move.",
         "correction": "Every plant is made of cells; movement is not what defines a cell."},
        {"id": "m2", "misconception": "The nucleus is the brain of the cell and thinks.",
         "correction": "The nucleus stores the instructions; it does not think or decide."},
        {"id": "m3", "misconception": "Animal cells have cell walls.", "correction": "Only plant cells have a cell wall."},
    ],
    "worked_examples": [{"id": "w1", "problem": "A cell has a cell wall and chloroplasts. Is it a plant or an animal cell?",
                         "solution_md": "Only plant cells have a **cell wall** and **chloroplasts**, so it is a plant cell."}],
    "claims": [{"id": "c1", "text": "All organisms are made of cells.", "section_id": "s1"},
               {"id": "c2", "text": "The nucleus holds the genetic material.", "section_id": "s2"},
               {"id": "c3", "text": "Only plant cells have a cell wall.", "section_id": "s3"}],
    "figures": [
        {"figure_key": "animal_cell", "caption": "An animal cell", "spec": {
            "subject": "an animal cell in cross-section", "parts": ["cell membrane", "cytoplasm", "nucleus", "mitochondria"],
            "style": "whiteboard diagram", "notes": "Round outline, no cell wall."}},
        {"figure_key": "plant_cell", "caption": "A plant cell", "spec": {
            "subject": "a plant cell in cross-section", "parts": ["cell wall", "cell membrane", "nucleus", "chloroplasts", "sap vacuole"],
            "style": "whiteboard diagram", "notes": "Rectangular outline with a large central vacuole."}},
    ],
    "depth_rationale": "Taught at CBSE Class 9 depth, the deepest curriculum mapped to this topic.",
}


class FakeModel:
    """``analyze`` records every call and answers ``reply``; an exception is
    raised, a callable is called with the prompt. Returns the real clients'
    shape: ``{"data", "usage", "truncated"}``."""

    def __init__(self, reply=None):
        self.reply = GOOD if reply is None else reply
        self.calls = []
        self.session_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def analyze(self, prompt, system=None, max_tokens=None, retries=3, cache_prefix=None, response_schema=None):
        self.calls.append({"prompt": prompt, "system": system, "max_tokens": max_tokens, "schema": response_schema})
        reply = self.reply
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = reply(prompt)
        self.session_usage["calls"] += 1
        self.session_usage["input_tokens"] += len(prompt) // 4
        self.session_usage["output_tokens"] += 900
        return {"data": copy.deepcopy(reply), "usage": {"calls": 1}, "truncated": False}


def _articles(sb):
    return sb.tables["topic_articles"]


def _figures(sb):
    return sb.tables["article_figures"]


def _job_row(sb):
    return sb.tables["jobs"][0]


def _good_words():
    return word_count([{"body_md": b} for b in (BODY_1, BODY_2, BODY_3, BODY_4)])


# ── the job, end to end ─────────────────────────────────────────────────


def test_a_good_reply_becomes_a_draft_article_with_its_figures():
    sb, model = _sb(), FakeModel()
    summary = run_article_job(sb, _job(), client=model)

    assert len(model.calls) == 1, "one model call per job"
    (row,) = _articles(sb)
    assert row["topic_id"] == "t-cell" and row["language"] == "en" and row["version"] == 1
    assert row["status"] == "draft" and row["author"] == "model" and row["notes"] is None
    assert row["source_article_id"] is None
    assert row["title"] == GOOD["title"]
    assert [s["id"] for s in row["sections"]] == ["s1", "s2", "s3", "s4"]
    assert row["sections"][2]["figure_keys"] == ["plant_cell", "animal_cell"]
    assert row["sections"][0]["covers"] == ["7Bs.01", "cbse:9:U2:01"]
    assert [o["id"] for o in row["objectives"]] == ["o1", "o2"]
    assert [g["term"] for g in row["glossary"]] == ["cell", "organelle"]
    assert [m["id"] for m in row["misconceptions"]] == ["m1", "m2", "m3"]
    assert row["worked_examples"] == [{"id": "w1", **{k: GOOD["worked_examples"][0][k] for k in ("problem", "solution_md")}}]
    assert [(c["id"], c["section_id"]) for c in row["claims"]] == [("c1", "s1"), ("c2", "s2"), ("c3", "s3")]
    assert row["word_count"] == _good_words() > 0
    assert row["depth_rationale"] == GOOD["depth_rationale"]
    # The depth: the deepest mapped grade is CBSE Class 9, chosen and recorded.
    assert row["depth_node_id"] == "n-cb1"
    assert sb.tables["topics"][0]["depth_node_id"] == "n-cb1"

    figs = _figures(sb)
    assert [(f["figure_key"], f["sort"], f["status"]) for f in figs] == [("animal_cell", 0, "draft"), ("plant_cell", 1, "draft")]
    assert all(f["article_id"] == row["id"] for f in figs)
    plant = figs[1]
    assert plant["caption"] == "A plant cell"
    assert plant["spec"] == GOOD["figures"][1]["spec"]
    assert plant["labels"] == [
        {"group_id": "cell_wall", "label": "cell wall"}, {"group_id": "cell_membrane", "label": "cell membrane"},
        {"group_id": "nucleus", "label": "nucleus"}, {"group_id": "chloroplast", "label": "chloroplasts"},
        {"group_id": "sap_vacuole", "label": "sap vacuole"}]

    job = _job_row(sb)
    assert job["status"] == "done" and job["progress"] == 100 and job["error"] is None
    assert job["stage"] == summary
    assert summary["step"] == "done" and summary["article_id"] == row["id"] and summary["version"] == 1
    assert summary["figures"] == 2 and summary["sections"] == 4 and summary["claims"] == 3
    assert summary["uncovered"] == [] and summary["repairs"] == []
    assert job["usage"]["calls"] == 1
    # The call is the client's JSON contract: the system prompt, the closed schema, the token cap.
    (call,) = model.calls
    assert call["system"] == SYSTEM_PROMPT and call["schema"] is RESPONSE_SCHEMA and call["max_tokens"] == MAX_TOKENS
    assert sb.writes("generations") == []


def test_the_prompt_carries_the_depth_the_coverage_target_verbatim_the_prerequisites_and_the_contract():
    sb, model = _sb(), FakeModel()
    run_article_job(sb, _job(), client=model)
    prompt = model.calls[0]["prompt"]
    assert "Topic: Cells" in prompt and "Subject: Biology" in prompt
    assert "DEPTH CURRICULUM (teach at this depth): CBSE Science (code cbse_science_086) - grade/stage 9" in prompt
    assert "Prerequisite topics (assume these are known): Living things" in prompt
    # Every mapped statement, verbatim, under its curriculum and grade.
    for n in NODES:
        assert f"{n['code']}: {n['description'] or n['title']}" in prompt
    assert "Cambridge Lower Secondary Science 0893 (code cambridge_ls_science_0893) - grade/stage 7" in prompt
    assert "cbse:9:U2:01: Cell - Basic Unit of life (partial coverage)" in prompt
    assert prompt.rstrip().endswith(ARTICLE_PROMPT.rstrip())
    for phrase in ("900-1600 words", "4-8 sections", "3-6 common misconceptions", "1-3 worked examples", "2-5 figures",
                   "Never quote or paraphrase a textbook", "British English", "No first person", '"covers"',
                   "Name the layer" if False else '"figure_keys"'):
        assert phrase in prompt, phrase
    assert "REVIEWER NOTES" not in prompt and "PREVIOUS VERSION" not in prompt


def test_an_existing_depth_node_on_the_topic_is_respected_not_overwritten():
    sb = _sb(topic={**TOPIC, "depth_node_id": "n-bs2"})
    model = FakeModel()
    run_article_job(sb, _job(), client=model)
    assert _articles(sb)[0]["depth_node_id"] == "n-bs2"
    assert sb.tables["topics"][0]["depth_node_id"] == "n-bs2"
    assert [w for w in sb.writes("topics")] == [], "nothing to record"
    assert "grade/stage 7 (7Bs.02:" in model.calls[0]["prompt"]


def test_the_client_is_built_for_the_article_s_language(monkeypatch):
    seen = []

    def fake_client_for(lang, **kw):
        seen.append(lang)
        return FakeModel()

    monkeypatch.setattr(article, "client_for", fake_client_for)
    sb = _sb(jobs=[_job(params={"topic_id": "t-cell", "language": "ar"})])
    run_article_job(sb, _job(params={"topic_id": "t-cell", "language": "ar"}))
    assert seen == ["ar"] and _articles(sb)[0]["language"] == "ar"
    assert "Language of the article: ar" in _job_row(sb)["stage"]["language"] or _job_row(sb)["stage"]["language"] == "ar"


# ── validation, through the job ─────────────────────────────────────────


def test_a_reply_without_sections_errors_the_job_and_writes_nothing():
    bad = {k: v for k, v in GOOD.items() if k != "sections"}
    sb = _sb()
    assert run_article_job(sb, _job(), client=FakeModel(bad)) is None
    assert _articles(sb) == [] and _figures(sb) == []
    job = _job_row(sb)
    assert job["status"] == "error" and "no 'sections' list" in job["error"]


def test_too_few_usable_sections_errors_the_job():
    bad = copy.deepcopy(GOOD)
    bad["sections"] = bad["sections"][:2] + [{"id": "s3", "heading": "Empty", "body_md": "   "}]
    sb = _sb()
    run_article_job(sb, _job(), client=FakeModel(bad))
    assert _articles(sb) == []
    assert _job_row(sb)["status"] == "error" and f"at least {MIN_SECTIONS}" in _job_row(sb)["error"]


def test_a_dangling_figure_key_is_dropped_and_recorded():
    bad = copy.deepcopy(GOOD)
    bad["sections"][1]["figure_keys"] = ["animal_cell", "Animal Cell", "mitochondrion_detail"]
    sb = _sb()
    summary = run_article_job(sb, _job(), client=FakeModel(bad))
    (row,) = _articles(sb)
    assert row["sections"][1]["figure_keys"] == ["animal_cell"], "canonicalised, deduplicated, pruned"
    assert summary["repairs"] == ["dangling figure key dropped from s2: mitochondrion_detail"]
    assert len(_figures(sb)) == 2 and _job_row(sb)["status"] == "done"


def test_a_claim_naming_an_unknown_section_keeps_the_fact_and_loses_the_pointer():
    bad = copy.deepcopy(GOOD)
    bad["claims"].append({"id": "c9", "text": "Mitochondria release energy by respiration.", "section_id": "s99"})
    sb = _sb()
    summary = run_article_job(sb, _job(), client=FakeModel(bad))
    claims = _articles(sb)[0]["claims"]
    assert claims[-1] == {"id": "c4", "text": "Mitochondria release energy by respiration.", "section_id": None}
    assert "claim section reference dropped: s99" in summary["repairs"]


def test_word_count_is_computed_from_the_bodies_not_taken_from_the_reply():
    bad = copy.deepcopy(GOOD)
    bad["word_count"] = 5000
    bad["sections"] = [{"id": f"s{i}", "heading": f"H{i}", "body_md": "**one** two, three-four (five)!"} for i in range(1, 4)]
    sb = _sb()
    run_article_job(sb, _job(), client=FakeModel(bad))
    assert _articles(sb)[0]["word_count"] == 15
    assert word_count([{"body_md": "a b\n\n- c\n- d"}]) == 4 and word_count([]) == 0


def test_a_dropped_section_re_issues_ids_and_claims_follow_the_model_s_ids():
    bad = copy.deepcopy(GOOD)
    bad["sections"].insert(1, {"id": "sx", "heading": "Nothing here", "body_md": ""})
    bad["claims"] = [{"id": "c1", "text": "A fact in the model's s2.", "section_id": "s2"},
                     {"id": "c2", "text": "A fact in the dropped section.", "section_id": "sx"}]
    sb = _sb()
    summary = run_article_job(sb, _job(), client=FakeModel(bad))
    (row,) = _articles(sb)
    assert [s["id"] for s in row["sections"]] == ["s1", "s2", "s3", "s4"]
    assert row["sections"][1]["heading"] == "Structures common to all cells", "the model's s2 is still second"
    assert row["claims"][0]["section_id"] == "s2" and row["claims"][1]["section_id"] is None
    assert "section dropped: empty body (Nothing here)" in summary["repairs"]


def test_figures_are_deduplicated_keyed_and_capped_at_eight():
    bad = copy.deepcopy(GOOD)
    extra = [{"figure_key": f"figure_{i}", "caption": f"Figure {i}", "spec": {"subject": f"thing {i}", "parts": ["a", "b"]}}
             for i in range(10)]
    bad["figures"] = bad["figures"] + [{"figure_key": "Plant Cell", "caption": "dup", "spec": {"subject": "x", "parts": []}},
                                       {"caption": "No key", "spec": {"subject": "the digestive system", "parts": ["mouth"]}},
                                       {"figure_key": "", "caption": "", "spec": {"subject": "", "parts": []}}] + extra
    sb = _sb()
    summary = run_article_job(sb, _job(), client=FakeModel(bad))
    keys = [f["figure_key"] for f in _figures(sb)]
    assert len(keys) == MAX_FIGURES == 8
    assert keys[:3] == ["animal_cell", "plant_cell", "digestive_system"], "subject supplies a missing key"
    assert "duplicate figure merged: plant_cell" in summary["repairs"]
    assert "figure dropped: no key material" in summary["repairs"]
    assert any("over the cap of 8" in r for r in summary["repairs"])
    assert summary["figures"] == 8


def test_uncovered_codes_are_reported_for_the_reviewer():
    bad = copy.deepcopy(GOOD)
    for s in bad["sections"]:
        s["covers"] = ["7bs.01", "9Zz.99"]     # case-insensitive match; an unknown code is dropped
    sb = _sb()
    summary = run_article_job(sb, _job(), client=FakeModel(bad))
    assert _articles(sb)[0]["sections"][0]["covers"] == ["7Bs.01"]
    assert summary["uncovered"] == ["7Bs.02", "cbse:9:U2:01"]


def test_worked_examples_may_be_absent_but_the_other_arrays_may_not():
    no_examples = {k: v for k, v in GOOD.items() if k != "worked_examples"}
    a = validate_article(no_examples, CODES, "Cells")
    assert a.worked_examples == [] and a.repairs == []
    for name in ("objectives", "glossary", "misconceptions", "claims", "figures"):
        with pytest.raises(ArticleInvalid, match=name):
            validate_article({k: v for k, v in GOOD.items() if k != name}, CODES, "Cells")
    for raw in (None, [], "text", {"raw_text": "```json {"}, 7):
        with pytest.raises(ArticleInvalid):
            validate_article(raw, CODES, "Cells")


def test_incomplete_apparatus_items_are_dropped_and_the_title_falls_back():
    bad = copy.deepcopy(GOOD)
    bad["title"] = "   "
    bad["glossary"].append({"term": "vacuole"})                    # no definition
    bad["misconceptions"].append("Plants breathe in oxygen.")       # not an object
    bad["objectives"].append({"id": "o3", "text": 7})
    a = validate_article(bad, CODES, "Cells")
    assert a.title == "Cells"
    assert [g["term"] for g in a.glossary] == ["cell", "organelle"]
    assert len(a.misconceptions) == 3 and len(a.objectives) == 2
    assert sorted(a.repairs) == ["glossary entry dropped: incomplete", "misconception dropped: incomplete",
                                 "objective dropped: incomplete"]


def test_figure_labels_are_the_catalogue_key_of_each_part():
    assert figure_labels(["cell wall", "Chloroplasts", "the nucleus"]) == [
        {"group_id": "cell_wall", "label": "cell wall"}, {"group_id": "chloroplast", "label": "Chloroplasts"},
        {"group_id": "nucleus", "label": "the nucleus"}]
    assert all(canonical_key(l["label"]) == l["group_id"] for l in figure_labels(["sap vacuole"]))


# ── versions, hints, regeneration ───────────────────────────────────────


def _v1(**extra):
    return {"id": "art-1", "topic_id": "t-cell", "version": 1, "language": "en", "title": "Cells v1",
            "sections": [{"id": "s1", "heading": "Old heading one", "body_md": "Old body one."},
                         {"id": "s2", "heading": "Old heading two", "body_md": "Old body two."}],
            "glossary": [{"term": "cell", "definition": "old"}], "status": "rejected", **extra}


def test_the_version_increments_and_the_previous_version_gives_continuity():
    sb = _sb(articles=[_v1(), {**_v1(), "id": "art-2", "version": 2, "status": "approved", "title": "Cells v2"}])
    model = FakeModel()
    summary = run_article_job(sb, _job(), client=model)
    assert [a["version"] for a in _articles(sb)] == [1, 2, 3] and summary["version"] == 3
    prompt = model.calls[0]["prompt"]
    assert "PREVIOUS VERSION (version 2, for continuity of terms and structure)" in prompt
    assert "Headings: Old heading one; Old heading two" in prompt and "Glossary terms: cell" in prompt
    assert "Old body one." not in prompt, "continuity carries headings and terms, not the old text"


def test_another_language_starts_its_own_version_sequence():
    sb = _sb(articles=[_v1()], jobs=[_job(params={"topic_id": "t-cell", "language": "fr"})])
    run_article_job(sb, _job(params={"topic_id": "t-cell", "language": "fr"}), client=FakeModel())
    fr = [a for a in _articles(sb) if a["language"] == "fr"]
    assert [a["version"] for a in fr] == [1]


def test_hints_are_recorded_and_a_source_version_is_revised_in_full():
    params = {"topic_id": "t-cell", "hints": "Section two overstates the size of a nucleus; fix the vacuole claim.",
              "source_article_id": "art-1"}
    sb = _sb(articles=[_v1()], jobs=[_job(params=params)])
    model = FakeModel()
    summary = run_article_job(sb, _job(params=params), client=model)
    new = [a for a in _articles(sb) if a["version"] == 2]
    assert len(new) == 1
    assert new[0]["notes"] == params["hints"] and new[0]["source_article_id"] == "art-1"
    assert summary["source_article_id"] == "art-1"
    prompt = model.calls[0]["prompt"]
    assert "REVIEWER NOTES (a regeneration; address every note):\n" + params["hints"] in prompt
    assert "PREVIOUS VERSION (version 1, to be REVISED" in prompt
    assert "## Old heading one\nOld body one." in prompt, "the source version's full text is offered"


def test_a_missing_source_version_errors_the_job():
    params = {"topic_id": "t-cell", "source_article_id": "art-missing"}
    sb = _sb(jobs=[_job(params=params)])
    run_article_job(sb, _job(params=params), client=FakeModel())
    assert _articles(sb) == [] and "art-missing" in _job_row(sb)["error"]


# ── idempotency and failure paths ───────────────────────────────────────


def test_a_job_that_already_produced_an_article_does_nothing_but_finish():
    done = _job(status="processing", stage={"phase": "article", "step": "done", "article_id": "art-1"})
    sb = _sb(articles=[_v1()], jobs=[done])
    model = FakeModel()
    summary = run_article_job(sb, done, client=model)
    assert model.calls == [] and len(_articles(sb)) == 1
    assert summary["step"] == "already_done" and summary["article_id"] == "art-1"
    job = _job_row(sb)
    assert job["status"] == "done" and job["stage"]["article_id"] == "art-1"


def test_the_record_is_read_from_the_database_when_the_claimed_row_lacks_it():
    """The reaper requeued the row after the write; the claim hands run.py
    the row with its stage, but a caller passing a bare dict must still see
    the database's record."""
    sb = _sb(articles=[_v1()], jobs=[_job(stage={"article_id": "art-1"})])
    model = FakeModel()
    run_article_job(sb, _job(), client=model)
    assert model.calls == [] and len(_articles(sb)) == 1 and _job_row(sb)["status"] == "done"


def test_a_client_that_raises_errors_the_job_and_writes_nothing():
    sb = _sb()
    assert run_article_job(sb, _job(), client=FakeModel(RuntimeError("503 model unavailable"))) is None
    assert _articles(sb) == [] and _figures(sb) == []
    job = _job_row(sb)
    assert job["status"] == "error" and "503 model unavailable" in job["error"]
    assert sb.writes("generations") == []
    assert sb.tables["topics"][0]["depth_node_id"] == "n-cb1", "the depth was recorded before the call; that is fine"


def test_a_client_that_cannot_be_built_errors_the_job(monkeypatch):
    def boom(lang, **kw):
        raise RuntimeError("VERTEX_PROJECT_ID unset")
    monkeypatch.setattr(article, "client_for", boom)
    sb = _sb()
    run_article_job(sb, _job())
    assert _job_row(sb)["status"] == "error" and "VERTEX_PROJECT_ID" in _job_row(sb)["error"]


@pytest.mark.parametrize("params", [None, {}, {"topic_id": ""}, {"topic_id": 7}, "t-cell"])
def test_a_job_without_a_topic_id_finishes_with_error(params):
    sb = _sb(jobs=[_job(params=params)])
    run_article_job(sb, _job(params=params), client=FakeModel())
    assert _job_row(sb)["status"] == "error" and "topic_id" in _job_row(sb)["error"]


def test_a_missing_topic_finishes_the_job_with_error():
    sb = _sb(topic=None)
    assert run_article_job(sb, _job(), client=FakeModel()) is None
    assert _job_row(sb)["status"] == "error" and "not found" in _job_row(sb)["error"]


def test_a_topic_with_no_mappings_is_still_authored_at_a_default_depth():
    sb = _sb(maps=[])
    model = FakeModel()
    summary = run_article_job(sb, _job(), client=model)
    prompt = model.calls[0]["prompt"]
    assert "no curriculum is mapped yet; teach at lower-secondary depth" in prompt
    assert "(no curriculum statements are mapped to this topic yet)" in prompt
    assert _articles(sb)[0]["depth_node_id"] is None and summary["uncovered"] == []
    assert _job_row(sb)["status"] == "done"


def test_a_database_failure_after_the_call_is_recorded(monkeypatch):
    sb = _sb()
    monkeypatch.setattr(article, "write_article", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503 upstream")))
    assert run_article_job(sb, _job(), client=FakeModel()) is None
    assert _job_row(sb)["status"] == "error" and "503" in _job_row(sb)["error"]
    assert _figures(sb) == []


def test_progress_and_stage_are_reported_step_by_step():
    sb = _sb()
    run_article_job(sb, _job(), client=FakeModel())
    steps = [e[2]["stage"]["step"] for e in sb.writes("jobs") if e[0] == "update" and "stage" in e[2]]
    assert steps == ["load", "author", "validate", "write", "done"]
    progress = [e[2]["progress"] for e in sb.writes("jobs") if e[0] == "update" and "progress" in e[2]]
    assert progress == [5, 20, 80, 95, 100]


def test_the_generation_table_is_never_written():
    sb = _sb(jobs=[_job(generation_id="gen-other")])
    run_article_job(sb, _job(generation_id="gen-other"), client=FakeModel())
    assert sb.writes("generations") == [] and sb.tables["generations"][0]["status"] == "done"


# ── the prompt builder, pure ────────────────────────────────────────────


def test_build_article_prompt_is_pure_and_names_the_depth_curriculum():
    mappings = article.load_mappings(_sb(), "t-cell")
    depth = article.pick_depth_node(TOPIC, mappings)
    assert depth["id"] == "n-cb1"
    text = build_article_prompt(TOPIC, "en", mappings, depth, CBSE, ["Living things"])
    assert text.count(ARTICLE_PROMPT) == 1 and "CBSE Science" in text.split("COVERAGE TARGET")[0]
    assert article.pick_depth_node({**TOPIC, "depth_node_id": "n-bs1"}, mappings)["id"] == "n-bs1"
    assert article.pick_depth_node(TOPIC, []) is None
    # A non-numeric grade never beats a number for the depth.
    odd = article.Mapping(node={"id": "n-x", "grade": "Upper Secondary", "code": "X"}, curriculum=CAM)
    assert article.pick_depth_node(TOPIC, mappings + [odd])["id"] == "n-cb1"
    assert article.pick_depth_node(TOPIC, [odd])["id"] == "n-x"
