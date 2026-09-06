"""``topic_derive``: a curriculum's objective clusters become GROUPED topic
candidates — one model call per cluster, every reply validated in code, and no
call at all for a cluster already done.

Everything here CALLS things (see tests/test_worker_entrypoint_runs.py for why
a source-substring test is worth nothing). The model is a fake that returns
canned JSON and records every prompt; the database is the fake Supabase in
tests/catalogue_fakes.py, which honours the unique index on topic_candidates
and records every write. No network, no model, no live Supabase.
"""

from __future__ import annotations

import pytest

from catalogue import derive
from catalogue.derive import (
    MAX_TOPICS_PER_CLUSTER, RESPONSE_SCHEMA, SYSTEM_PROMPT, Cluster, build_clusters, build_prompt,
    clean_name, content_words, run_derive_job, validate_proposals,
)
from catalogue.harvest import is_heading
from catalogue.key import canonical_key
from tests.catalogue_fakes import FakeSB

CUR = {"id": "cur-cam", "code": "cambridge_ls_science_0893", "name": "Cambridge Lower Secondary Science 0893"}
JOB = "job-td"

# One stage of Cambridge: two Biology sub-strands and one TWS sub-strand. No
# ``kind`` stored — the codes must say what each node is.
BS = [
    ("n-bs1", "7Bs.01", "Understand that all organisms are made of cells and microorganisms are typically single celled."),
    ("n-bs2", "7Bs.02", "Identify and describe the functions of cell structures (limited to cell membrane, cytoplasm, nucleus, cell wall, chloroplast, mitochondria and sap vacuole)."),
    ("n-bs3", "7Bs.03", "Explain how the structures of some specialised cells are related to their functions (including red blood cells, neurones, ciliated cells, root hair cells and palisade cells)."),
    ("n-bs4", "7Bs.04", "Describe the similarities and differences between the structures of plant and animal cells."),
    ("n-bs5", "7Bs.05", "Understand that cells can be grouped together to form tissues, organs and organ systems."),
]
BP = [
    ("n-bp1", "7Bp.01", "Describe the human digestive system and the functions of its organs."),
    ("n-bp2", "7Bp.02", "Describe the process of digestion, including the role of enzymes."),
]
TWS = [("n-twsm1", "7TWSm.01", "Describe the strengths and limitations of a model.")]


def _node(id_, code, title, parent=None, grade="7", strand=None, sub_strand=None, sort=0, **extra):
    return {"id": id_, "curriculum_id": CUR["id"], "code": code, "grade": grade, "strand": strand,
            "sub_strand": sub_strand, "title": title, "description": None, "parent_id": parent,
            "sort": sort, **extra}


def _nodes():
    out = [
        _node("n-bio", "7/Biology", "Biology", strand="Biology", sort=1),
        _node("n-bs", "7/Bs", "Structure and function", parent="n-bio", strand="Biology",
              sub_strand="Structure and function", sort=2),
        _node("n-bp", "7/Bp", "Life processes", parent="n-bio", strand="Biology",
              sub_strand="Life processes", sort=10),
        _node("n-tws", "7/Thinking and Working Scientifically", "Thinking and Working Scientifically",
              strand="Thinking and Working Scientifically", sort=20),
        _node("n-twsm", "7/TWSm", "Models and representations", parent="n-tws",
              strand="Thinking and Working Scientifically", sub_strand="Models and representations", sort=21),
    ]
    for i, (id_, code, text) in enumerate(BS):
        out.append(_node(id_, code, text, parent="n-bs", strand="Biology",
                         sub_strand="Structure and function", sort=3 + i))
    for i, (id_, code, text) in enumerate(BP):
        out.append(_node(id_, code, text, parent="n-bp", strand="Biology", sub_strand="Life processes", sort=11 + i))
    for i, (id_, code, text) in enumerate(TWS):
        out.append(_node(id_, code, text, parent="n-twsm", strand="Thinking and Working Scientifically",
                         sub_strand="Models and representations", sort=22 + i))
    return out


def _job(**extra):
    return {"id": JOB, "type": "topic_derive", "status": "processing", "generation_id": None,
            "book_id": None, "params": {"curriculum_id": CUR["id"]}, **extra}


def _sb(nodes=None, curriculum=CUR, jobs=None, topics=(), aliases=(), candidates=()):
    sb = FakeSB()
    sb.tables["curricula"] = [dict(curriculum)] if curriculum else []
    sb.tables["curriculum_nodes"] = [dict(n) for n in (nodes if nodes is not None else _nodes())]
    sb.tables["jobs"] = jobs if jobs is not None else [_job()]
    sb.tables["generations"] = [{"id": "gen-other", "status": "done"}]
    sb.tables["topics"] = [dict(t) for t in topics]
    sb.tables["topic_aliases"] = [dict(a) for a in aliases]
    sb.tables["topic_candidates"] = [dict(c) for c in candidates]
    return sb


# ── the fake model ──────────────────────────────────────────────────────

GOOD_BS = {"topics": [
    {"name": "Cells", "objective_codes": ["7Bs.01", "7Bs.02"], "rationale": "What a cell is and its parts.",
     "matches": ["Cell - Basic Unit of life"]},
    {"name": "Specialised cells", "objective_codes": ["7Bs.03"], "rationale": "Structure suits function."},
    {"name": "Plant and animal cells", "objective_codes": ["7Bs.04"]},
    {"name": "Tissues, organs and organ systems", "objective_codes": ["7Bs.05"]},
]}
GOOD_BP = {"topics": [{"name": "The digestive system", "objective_codes": ["7Bp.01", "7Bp.02"],
                       "rationale": "Digestion in the human body."}]}
GOOD_TWS = {"topics": [{"name": "Models in science", "objective_codes": ["7TWSm.01"]}]}


def _cluster_of(prompt: str) -> str:
    for tag, code in (("bs", "7Bs.01:"), ("bp", "7Bp.01:"), ("tws", "7TWSm.01:")):
        if code in prompt:
            return tag
    return "?"


class FakeModel:
    """``analyze`` records every prompt and answers from ``replies`` keyed by
    cluster ("bs" / "bp" / "tws" / "*"); a reply that is an exception is raised,
    a callable is called with the prompt. The shape returned is the real
    clients': ``{"data", "usage", "truncated"}``."""

    def __init__(self, replies=None, default=None):
        self.replies = dict(replies or {})
        self.default = default
        self.calls = []
        self.session_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def analyze(self, prompt, system=None, max_tokens=None, retries=3, cache_prefix=None,
                response_schema=None):
        self.calls.append({"prompt": prompt, "system": system, "max_tokens": max_tokens,
                           "schema": response_schema})
        tag = _cluster_of(prompt)
        reply = self.replies.get(tag, self.replies.get("*", self.default))
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = reply(prompt)
        self.session_usage["calls"] += 1
        self.session_usage["input_tokens"] += len(prompt) // 4
        return {"data": reply, "usage": {"calls": 1}, "truncated": False}


def _good_model():
    return FakeModel({"bs": GOOD_BS, "bp": GOOD_BP, "tws": GOOD_TWS})


def _rows(sb, node_id=None):
    return [r for r in sb.tables["topic_candidates"] if node_id is None or r["node_id"] == node_id]


def _by_title(sb):
    return {r["raw_title"]: r for r in sb.tables["topic_candidates"]}


def _job_row(sb):
    return sb.tables["jobs"][0]


# ── clusters ────────────────────────────────────────────────────────────


class TestClusters:
    def test_one_cluster_per_sub_strand_with_kind_inferred_from_the_codes(self):
        clusters = build_clusters(_nodes())
        assert [(c.parent_id, c.mode, len(c.leaves)) for c in clusters] == [
            ("n-bs", "objectives", 5), ("n-bp", "objectives", 2), ("n-twsm", "objectives", 1)]
        bs = clusters[0]
        assert bs.codes == [c for _, c, _ in BS]
        assert bs.label == "Structure and function" and bs.grade == "7" and bs.strand == "Biology"
        assert bs.statement(bs.leaves[0]) == BS[0][2]

    def test_a_stored_kind_wins_over_the_code(self):
        nodes = _nodes()
        for n in nodes:
            if n["id"] == "n-bs1":
                n["kind"] = "topic"          # says it is a NAME even though the code is an LO code
                n["title"] = "Cells"
        clusters = build_clusters(nodes)
        modes = {(c.parent_id, c.mode): len(c.leaves) for c in clusters}
        assert modes[("n-bs", "names")] == 1 and modes[("n-bs", "objectives")] == 4

    def test_a_childless_grouping_node_is_not_a_cluster(self):
        nodes = _nodes() + [_node("n-empty", "7/Pe", "Electricity", parent="n-bio", sort=99)]
        assert [c.parent_id for c in build_clusters(nodes)] == ["n-bs", "n-bp", "n-twsm"]

    def test_parentless_chapters_cluster_per_grade_under_the_first_chapter(self):
        nodes = [
            _node("c7-1", "cbse:7:ch01", "Nutrition in Plants", grade="7", sort=1),
            _node("c7-2", "cbse:7:ch02", "Heat", grade="7", sort=2),
            _node("c6-1", "cbse:6:ch01", "The Wonderful World of Science", grade="6", sort=3),
        ]
        clusters = build_clusters(nodes)
        assert [(c.parent_id, c.mode, c.codes) for c in clusters] == [
            ("c6-1", "names", ["cbse:6:ch01"]), ("c7-1", "names", ["cbse:7:ch01", "cbse:7:ch02"])]
        assert clusters[1].label == "Grade 7 chapters"

    def test_a_unit_of_topic_names_is_one_names_cluster(self):
        nodes = [
            _node("u1", "cbse:9:U1", "Matter-Nature and Behaviour", grade="9", strand="Materials", sort=1),
            _node("t1", "cbse:9:U1:01", "Matter in Our Surroundings", parent="u1", grade="9", sort=2),
            _node("t2", "cbse:9:U1:02", "Is Matter Around Us Pure?", parent="u1", grade="9", sort=3),
        ]
        (c,) = build_clusters(nodes)
        assert c.mode == "names" and c.parent_id == "u1" and c.codes == ["cbse:9:U1:01", "cbse:9:U1:02"]

    def test_an_unknown_scheme_reads_the_title(self):
        nodes = [_node("x1", "X-1", "Cells", sort=1), _node("x2", "X-2", "Understand that cells divide.", sort=2)]
        assert sorted((c.mode, c.codes[0]) for c in build_clusters(nodes)) == [("names", "X-1"), ("objectives", "X-2")]


# ── the prompt ──────────────────────────────────────────────────────────


class TestPrompt:
    def test_the_prompt_carries_the_cluster_the_rules_and_the_context(self):
        (bs, _, _) = build_clusters(_nodes())
        text = build_prompt(CUR, bs, ["Photosynthesis", "Cells"], [("Cell - Basic Unit of life", "cbse_science_086")])
        assert "Cambridge Lower Secondary Science 0893" in text and "Structure and function" in text
        for _, code, statement in BS:
            assert f"{code}: {statement}" in text
        assert "exactly ONE topic" in text and "2-5 words" in text and "never more than 8" in text
        assert "SKILLS" in text and "Thinking and Working Scientifically" in text
        assert "- Photosynthesis\n- Cells" in text
        assert "- Cell - Basic Unit of life | cbse_science_086" in text
        assert '"objective_codes"' in text and text.rstrip().endswith("}]}")

    def test_an_empty_context_says_so(self):
        (_, bp, _) = build_clusters(_nodes())
        text = build_prompt(CUR, bp, [], [])
        assert "(none yet)" in text and "(none)" in text

    def test_a_names_cluster_asks_only_to_normalise_and_merge(self):
        (c,) = build_clusters([
            _node("u1", "cbse:9:U1", "Matter", grade="9", sort=1),
            _node("t1", "cbse:9:U1:01", "Matter in Our Surroundings", parent="u1", grade="9", sort=2)])
        text = build_prompt(CUR, c, [], [])
        assert "TOPIC NAMES in this unit" in text and "normalise these names and suggest merges" in text
        assert "SKILLS" not in text

    def test_the_context_lists_are_capped(self):
        (bs, _, _) = build_clusters(_nodes())
        text = build_prompt(CUR, bs, [f"T{i:04d}" for i in range(1000)], [(f"C{i:04d}", "x") for i in range(1000)])
        assert "- T0299" in text and "- T0300" not in text
        assert "- C0299 | x" in text and "- C0300" not in text


# ── validation and repair (pure) ────────────────────────────────────────


class TestCleanName:
    @pytest.mark.parametrize("raw,clean", [
        ("Cells", "Cells"), ("Cells.", "Cells"), ('"Cells"', "Cells"), ("  Plant and  animal cells ", "Plant and animal cells"),
        ("3.2 Cells", "Cells"), ("The digestive system", "The digestive system"),
        ("What is matter?", "What is matter?"),
    ])
    def test_tidied(self, raw, clean):
        assert clean_name(raw) == clean

    @pytest.mark.parametrize("raw", [
        "All living things are made of cells.",                # a sentence
        "The cell is the basic unit of life",                  # a determiner opening 6+ words
        "7Bs.01",                                              # an LO code
        "", None, "   ", "12", "الخلية",                       # nothing, or no key material
        "Fig. 3 shows the cell",
    ])
    def test_dropped(self, raw):
        assert clean_name(raw) == ""

    def test_a_cluster_code_is_never_a_name(self):
        assert clean_name("cbse:9:U1:01", ["cbse:9:U1:01"]) == ""
        assert clean_name("Matter", ["cbse:9:U1:01"]) == "Matter"

    def test_content_words_drop_function_words_and_lo_verbs_and_singularise(self):
        assert content_words("Describe the similarities and differences between plant and animal cells.") == {
            "similaritie", "difference", "plant", "animal", "cell"}
        assert content_words("Cells") == {"cell"}


def _bs_cluster():
    return build_clusters(_nodes())[0]


class TestValidate:
    def test_a_good_reply_passes_through_in_code_order(self):
        props = validate_proposals(GOOD_BS["topics"], _bs_cluster())
        assert [(p.name, p.codes) for p in props] == [
            ("Cells", ["7Bs.01", "7Bs.02"]), ("Specialised cells", ["7Bs.03"]),
            ("Plant and animal cells", ["7Bs.04"]), ("Tissues, organs and organ systems", ["7Bs.05"])]
        assert props[0].key == "cell" and props[0].matches == ["Cell - Basic Unit of life"]
        assert props[0].rationale == "What a cell is and its parts."

    def test_a_missing_code_goes_to_the_topic_sharing_most_words(self):
        reply = [t for t in GOOD_BS["topics"] if t["name"] != "Plant and animal cells"]
        props = {p.name: p.codes for p in validate_proposals(reply, _bs_cluster())}
        # "…structures of plant and animal cells" shares "cell" with "Cells" and
        # with "Specialised cells" alike; the first listed wins the tie.
        assert props["Cells"] == ["7Bs.01", "7Bs.02", "7Bs.04"]
        assert props["Specialised cells"] == ["7Bs.03"]

    def test_a_missing_code_with_no_shared_word_gets_the_sub_strand_topic(self):
        (_, bp, _) = build_clusters(_nodes())
        props = validate_proposals([{"name": "Digestion", "objective_codes": ["7Bp.02"]}], bp)
        assert [(p.name, p.codes) for p in props] == [("Digestion", ["7Bp.02"]), ("Life processes", ["7Bp.01"])]
        assert "unassigned" in props[1].rationale

    def test_a_duplicated_code_stays_with_the_first_topic(self):
        reply = [{"name": "Cells", "objective_codes": ["7Bs.01", "7Bs.02"]},
                 {"name": "Specialised cells", "objective_codes": ["7Bs.02", "7Bs.03"]},
                 {"name": "Plant and animal cells", "objective_codes": ["7Bs.04", "7Bs.05"]}]
        props = {p.name: p.codes for p in validate_proposals(reply, _bs_cluster())}
        assert props["Cells"] == ["7Bs.01", "7Bs.02"] and props["Specialised cells"] == ["7Bs.03"]

    def test_unknown_codes_are_dropped_and_a_topic_of_only_unknown_codes_vanishes(self):
        reply = GOOD_BS["topics"] + [{"name": "Photosynthesis", "objective_codes": ["9Bs.99", "nonsense"]}]
        props = validate_proposals(reply, _bs_cluster())
        assert "Photosynthesis" not in {p.name for p in props}
        assert sorted(c for p in props for c in p.codes) == sorted(c for _, c, _ in BS)

    def test_a_sentence_as_a_name_is_dropped_and_its_codes_repaired(self):
        reply = [{"name": "All living things are made of cells.", "objective_codes": ["7Bs.01", "7Bs.02"]},
                 {"name": "7Bs.03", "objective_codes": ["7Bs.03"]},
                 {"name": "Cells", "objective_codes": ["7Bs.04"]},
                 {"name": "Tissues, organs and organ systems", "objective_codes": ["7Bs.05"]}]
        props = validate_proposals(reply, _bs_cluster())
        names = [p.name for p in props]
        assert "All living things are made of cells." not in names and "7Bs.03" not in names
        assert dict((p.name, p.codes) for p in props)["Cells"] == ["7Bs.01", "7Bs.02", "7Bs.03", "7Bs.04"]

    def test_two_topics_with_one_key_merge(self):
        reply = [{"name": "Cells", "objective_codes": ["7Bs.01"]}, {"name": "The Cell", "objective_codes": ["7Bs.02"]},
                 {"name": "Specialised cells", "objective_codes": ["7Bs.03", "7Bs.04", "7Bs.05"]}]
        props = validate_proposals(reply, _bs_cluster())
        assert [(p.name, p.codes) for p in props] == [("Cells", ["7Bs.01", "7Bs.02"]),
                                                      ("Specialised cells", ["7Bs.03", "7Bs.04", "7Bs.05"])]

    def test_a_trailing_period_is_stripped_before_keying(self):
        (p,) = validate_proposals([{"name": "Cells.", "objective_codes": [c for _, c, _ in BS]}], _bs_cluster())
        assert p.name == "Cells" and p.key == "cell" == canonical_key(p.name)

    def test_the_cap_is_eight_and_every_code_is_still_placed_once(self):
        leaves = [_node(f"n-pf{i}", f"7Pf.{i:02d}", f"Statement about item{i} alpha{i}.", parent="n-pf",
                        strand="Physics", sub_strand="Forces", sort=i) for i in range(1, 11)]
        parent = _node("n-pf", "7/Pf", "Forces", parent="n-phy", strand="Physics", sub_strand="Forces")
        (cluster,) = build_clusters(leaves + [parent, _node("n-phy", "7/Physics", "Physics", strand="Physics")])
        reply = [{"name": f"Topic item{i}", "objective_codes": [f"7Pf.{i:02d}"]} for i in range(1, 11)]
        props = validate_proposals(reply, cluster)
        assert len(props) == MAX_TOPICS_PER_CLUSTER == 8
        placed = [c for p in props for c in p.codes]
        assert sorted(placed) == sorted(cluster.codes) and len(placed) == len(set(placed)) == 10
        assert "Forces" in {p.name for p in props}, "the orphans landed under the sub-strand"

    @pytest.mark.parametrize("raw", [None, [], "not a list", [{"name": "Cells"}], [{"name": "Cells", "objective_codes": []}],
                                     [{"name": "All living things are made of cells.", "objective_codes": ["7Bs.01"]}]])
    def test_a_reply_that_names_nothing_usable_is_empty(self, raw):
        assert validate_proposals(raw, _bs_cluster()) == []


# ── the job ─────────────────────────────────────────────────────────────


def test_the_job_files_one_candidate_per_proposed_topic():
    sb, model = _sb(), _good_model()
    summary = run_derive_job(sb, _job(), client=model)

    assert len(model.calls) == 3, "one call per cluster"
    rows = _by_title(sb)
    assert set(rows) == {"Cells", "Specialised cells", "Plant and animal cells", "Tissues, organs and organ systems",
                         "The digestive system", "Models in science"}
    cells = rows["Cells"]
    assert cells["source_kind"] == "curriculum" and cells["node_id"] == "n-bs"
    assert cells["node_ids"] == ["n-bs1", "n-bs2"] and cells["normalized"] == "cell"
    assert cells["rationale"] == "What a cell is and its parts." and cells["suggested_topic_id"] is None
    assert cells.get("book_id") is None
    assert rows["The digestive system"]["node_id"] == "n-bp" and rows["The digestive system"]["node_ids"] == ["n-bp1", "n-bp2"]
    assert rows["Models in science"]["node_ids"] == ["n-twsm1"]
    for r in sb.tables["topic_candidates"]:
        assert is_heading(r["raw_title"]) and r["normalized"] == canonical_key(r["raw_title"])
        assert len(r["raw_title"]) <= 120 and not r["raw_title"].endswith(".")

    job = _job_row(sb)
    assert job["status"] == "done" and job["progress"] == 100 and job["error"] is None
    assert job["stage"] == summary
    assert {k: summary[k] for k in ("clusters_total", "clusters_done", "proposed", "skipped", "failed", "calls")} == {
        "clusters_total": 3, "clusters_done": 3, "proposed": 6, "skipped": 0, "failed": 0, "calls": 3}
    assert summary["step"] == "done" and summary["curriculum"] == CUR["code"]
    # The call is the client's JSON contract: the system prompt and the closed schema.
    assert all(c["system"] == SYSTEM_PROMPT and c["schema"] is RESPONSE_SCHEMA for c in model.calls)
    assert job["usage"]["calls"] == 3


def test_every_objective_of_the_curriculum_lands_in_exactly_one_candidate():
    sb = _sb()
    run_derive_job(sb, _job(), client=_good_model())
    placed = [nid for r in sb.tables["topic_candidates"] for nid in r["node_ids"]]
    assert sorted(placed) == sorted(id_ for id_, _, _ in BS + BP + TWS)


def test_a_second_run_makes_no_model_call_and_writes_nothing():
    sb = _sb()
    run_derive_job(sb, _job(), client=_good_model())
    before, n_log = sb.snapshot(), len(sb.log)

    model = FakeModel(default=GOOD_BS)
    summary = run_derive_job(sb, _job(), client=model)

    assert model.calls == [], "a cluster whose candidates exist costs no call"
    assert sb.tables["topic_candidates"] == before["topic_candidates"]
    assert [e for e in sb.log[n_log:] if e[1] == "topic_candidates"] == []
    assert summary["skipped"] == 3 and summary["proposed"] == 0 and summary["calls"] == 0
    assert _job_row(sb)["status"] == "done"


def test_a_partly_done_curriculum_calls_only_for_the_missing_clusters():
    sb = _sb(candidates=[{"source_kind": "curriculum", "node_id": "n-bs", "node_ids": ["n-bs1", "n-bs2", "n-bs3", "n-bs4", "n-bs5"],
                          "raw_title": "Cells", "normalized": "cell", "status": "merged"}])
    model = _good_model()
    summary = run_derive_job(sb, _job(), client=model)
    assert [_cluster_of(c["prompt"]) for c in model.calls] == ["bp", "tws"]
    assert summary["skipped"] == 1 and summary["proposed"] == 2
    assert len(_rows(sb, "n-bs")) == 1, "the curator's merged row is left alone"


def test_a_row_the_loader_filed_under_the_parent_does_not_count_as_done():
    """A key already under the parent WITHOUT node_ids is not this job's work:
    the model is still asked, and the select-then-insert keeps that one key
    from being filed twice."""
    sb = _sb(candidates=[{"source_kind": "curriculum", "node_id": "n-bs", "raw_title": "Cells", "normalized": "cell"}])
    model = _good_model()
    summary = run_derive_job(sb, _job(), client=model)
    assert len(model.calls) == 3 and summary["skipped"] == 0
    bs_rows = _rows(sb, "n-bs")
    assert sorted(r["raw_title"] for r in bs_rows) == ["Cells", "Plant and animal cells", "Specialised cells",
                                                       "Tissues, organs and organ systems"]
    assert [r for r in bs_rows if r["raw_title"] == "Cells"][0].get("node_ids") is None, "the old row, not a new one"
    assert summary["proposed"] == 5


def test_the_repairs_reach_the_written_rows():
    """Missing code, duplicated code, a sentence and a code as names — one
    reply with all four, asked of what was WRITTEN."""
    bad_bs = {"topics": [
        {"name": "All living things are made of cells.", "objective_codes": ["7Bs.01"]},   # sentence → dropped, 01 orphaned
        {"name": "Cells", "objective_codes": ["7Bs.02", "9Bs.99"]},                       # unknown code dropped
        {"name": "Specialised cells", "objective_codes": ["7Bs.02", "7Bs.03"]},            # 02 duplicated → stays with Cells
        {"name": "7Bs.05", "objective_codes": ["7Bs.05"]},                                 # a code as a name → dropped
        # 7Bs.04 named by nobody → word overlap
    ]}
    sb = _sb()
    run_derive_job(sb, _job(), client=FakeModel({"bs": bad_bs, "bp": GOOD_BP, "tws": GOOD_TWS}))
    bs = {r["raw_title"]: r["node_ids"] for r in _rows(sb, "n-bs")}
    assert set(bs) == {"Cells", "Specialised cells"}
    assert bs["Cells"] == ["n-bs1", "n-bs2", "n-bs4", "n-bs5"] and bs["Specialised cells"] == ["n-bs3"]
    assert _job_row(sb)["status"] == "done"


def test_suggested_topic_id_from_the_name_key_and_from_an_alias():
    sb = _sb(topics=[{"id": "t-dig", "canonical_key": "digestive_system", "title": "Digestive system", "status": "approved"}],
             aliases=[{"topic_id": "t-cell", "alias": "The Cell", "normalized": "cell"}])
    run_derive_job(sb, _job(), client=_good_model())
    rows = _by_title(sb)
    assert rows["Cells"]["suggested_topic_id"] == "t-cell", "an alias with the same key"
    assert rows["The digestive system"]["suggested_topic_id"] == "t-dig", "topics.canonical_key with the same key"
    assert rows["Models in science"]["suggested_topic_id"] is None


def test_cross_link_via_matches_to_an_existing_topic_title():
    sb = _sb(topics=[{"id": "t-cbse-cell", "canonical_key": "cell_basic_unit_of_life",
                      "title": "Cell - Basic Unit of life", "status": "candidate"}])
    model = _good_model()
    run_derive_job(sb, _job(), client=model)
    assert _by_title(sb)["Cells"]["suggested_topic_id"] == "t-cbse-cell"
    assert "- Cell - Basic Unit of life" in model.calls[0]["prompt"], "existing titles are offered to the model"


def test_open_candidates_from_other_curricula_are_offered_and_a_match_is_recorded():
    sb = _sb()
    sb.tables["curricula"].append({"id": "cur-cbse", "code": "cbse_science_086", "name": "CBSE Science"})
    sb.tables["curriculum_nodes"] += [
        {"id": "cb-1", "curriculum_id": "cur-cbse", "code": "cbse:9:U2:01", "title": "Cell - Basic Unit of life", "sort": 1},
        {"id": "cb-2", "curriculum_id": "cur-cbse", "code": "cbse:9:U2:02", "title": "Tissues", "sort": 2},
    ]
    sb.tables["topic_candidates"] += [
        {"source_kind": "curriculum", "node_id": "cb-1", "raw_title": "Cell - Basic Unit of life",
         "normalized": "cell_basic_unit_of_life", "status": "open"},
        {"source_kind": "curriculum", "node_id": "cb-2", "raw_title": "Tissues", "normalized": "tissue", "status": "dismissed"},
        {"source_kind": "book", "book_id": "book-1", "raw_title": "Photosynthesis", "normalized": "photosynthesi", "status": "open"},
    ]
    model = _good_model()
    run_derive_job(sb, _job(), client=model)
    prompt = model.calls[0]["prompt"]
    assert "- Cell - Basic Unit of life | cbse_science_086" in prompt
    assert "Tissues |" not in prompt and "Photosynthesis" not in prompt, "open curriculum candidates only"
    cells = _by_title(sb)["Cells"]
    assert cells["suggested_topic_id"] is None, "a candidate is not a topic"
    assert 'Also proposed by cbse_science_086 as "Cell - Basic Unit of life".' in cells["rationale"]


def test_this_curriculum_s_own_candidates_are_not_offered_as_other_curricula():
    sb = _sb(candidates=[{"source_kind": "curriculum", "node_id": "n-bp1", "raw_title": "Digestion",
                          "normalized": "digestion", "status": "open"}])
    model = _good_model()
    run_derive_job(sb, _job(), client=model)
    assert "Digestion |" not in model.calls[0]["prompt"]


def test_the_generation_table_is_never_written():
    sb = _sb()
    run_derive_job(sb, _job(), client=_good_model())
    assert sb.writes("generations") == []
    # Even a derive row that somehow carries a generation_id leaves it alone.
    sb = _sb(jobs=[_job(generation_id="gen-other")])
    run_derive_job(sb, _job(generation_id="gen-other"), client=_good_model())
    assert sb.writes("generations") == [] and sb.tables["generations"][0]["status"] == "done"


# ── names mode ──────────────────────────────────────────────────────────


def _cbse_nodes():
    return [
        {"id": "u1", "curriculum_id": "cur-cbse", "code": "cbse:9:U1", "grade": "9", "strand": "Materials",
         "sub_strand": None, "title": "Matter-Nature and Behaviour", "description": None, "parent_id": None, "sort": 1},
        {"id": "t1", "curriculum_id": "cur-cbse", "code": "cbse:9:U1:01", "grade": "9", "strand": "Materials",
         "sub_strand": None, "title": "Matter in Our Surroundings", "description": None, "parent_id": "u1", "sort": 2},
        {"id": "t2", "curriculum_id": "cur-cbse", "code": "cbse:9:U1:02", "grade": "9", "strand": "Materials",
         "sub_strand": None, "title": "Is Matter Around Us Pure?", "description": None, "parent_id": "u1", "sort": 3},
    ]


CBSE = {"id": "cur-cbse", "code": "cbse_science_086", "name": "CBSE Science"}
LOADER_ROWS = [  # what seeds.loader queues for CBSE leaves: node_id = the leaf, no node_ids
    {"source_kind": "curriculum", "node_id": "t1", "raw_title": "Matter in Our Surroundings",
     "normalized": "matter_in_our_surrounding", "status": "open"},
    {"source_kind": "curriculum", "node_id": "t2", "raw_title": "Is Matter Around Us Pure?",
     "normalized": "is_matter_around_us_pure", "status": "open"},
]


def _cbse_job():
    return _job(params={"curriculum_id": "cur-cbse"})


def test_names_mode_with_an_unusable_reply_defaults_to_each_leaf_and_skips_what_the_loader_filed():
    sb = _sb(nodes=_cbse_nodes(), curriculum=CBSE, jobs=[_cbse_job()], candidates=LOADER_ROWS)
    model = FakeModel(default={"raw_text": "I cannot help with that."})
    summary = run_derive_job(sb, _cbse_job(), client=model)
    assert len(model.calls) == 1 and "TOPIC NAMES in this unit" in model.calls[0]["prompt"]
    assert summary["proposed"] == 0 and summary["failed"] == 0 and _job_row(sb)["status"] == "done"
    assert len(sb.tables["topic_candidates"]) == 2, "the loader's rows, and nothing new"


def test_names_mode_files_a_merge_under_the_unit():
    sb = _sb(nodes=_cbse_nodes(), curriculum=CBSE, jobs=[_cbse_job()], candidates=LOADER_ROWS)
    reply = {"topics": [{"name": "Matter around us", "objective_codes": ["cbse:9:U1:01", "cbse:9:U1:02"],
                         "rationale": "Two chapters on one topic."}]}
    summary = run_derive_job(sb, _cbse_job(), client=FakeModel(default=reply))
    new = _rows(sb, "u1")
    assert [(r["raw_title"], r["node_ids"]) for r in new] == [("Matter around us", ["t1", "t2"])]
    assert summary["proposed"] == 1


def test_names_mode_default_without_loader_rows_files_each_leaf_under_the_unit():
    sb = _sb(nodes=_cbse_nodes(), curriculum=CBSE, jobs=[_cbse_job()])
    summary = run_derive_job(sb, _cbse_job(), client=FakeModel(default=[]))
    assert sorted((r["raw_title"], r["node_ids"]) for r in _rows(sb, "u1")) == [
        ("Is Matter Around Us Pure?", ["t2"]), ("Matter in Our Surroundings", ["t1"])]
    assert summary["proposed"] == 2 and summary["failed"] == 0


def test_a_chapter_list_is_not_mistaken_for_done_because_the_loader_filed_its_first_chapter():
    nodes = [
        {"id": "c1", "curriculum_id": "cur-cbse", "code": "cbse:6:ch01", "grade": "6", "strand": None, "sub_strand": None,
         "title": "The Wonderful World of Science", "description": None, "parent_id": None, "sort": 1},
        {"id": "c2", "curriculum_id": "cur-cbse", "code": "cbse:6:ch02", "grade": "6", "strand": None, "sub_strand": None,
         "title": "Diversity in the Living World", "description": None, "parent_id": None, "sort": 2},
    ]
    loader_rows = [{"source_kind": "curriculum", "node_id": "c1", "raw_title": "The Wonderful World of Science",
                    "normalized": "wonderful_world_of_science", "status": "open"},
                   {"source_kind": "curriculum", "node_id": "c2", "raw_title": "Diversity in the Living World",
                    "normalized": "diversity_in_the_living_world", "status": "open"}]
    sb = _sb(nodes=nodes, curriculum=CBSE, jobs=[_cbse_job()], candidates=loader_rows)
    reply = {"topics": [{"name": "Living things around us", "objective_codes": ["cbse:6:ch02"]},
                        {"name": "The Wonderful World of Science", "objective_codes": ["cbse:6:ch01"]}]}
    model = FakeModel(default=reply)
    summary = run_derive_job(sb, _cbse_job(), client=model)
    assert len(model.calls) == 1 and summary["skipped"] == 0
    new = [r for r in _rows(sb, "c1") if r.get("node_ids")]
    assert [(r["raw_title"], r["node_ids"]) for r in new] == [("Living things around us", ["c2"])], (
        "the renamed chapter is filed under the grade's first chapter node; the unchanged one is not filed twice")


# ── failure paths ───────────────────────────────────────────────────────


def test_a_failing_cluster_is_counted_the_rest_are_written_and_the_job_says_error():
    sb = _sb()
    model = FakeModel({"bs": GOOD_BS, "bp": RuntimeError("503 model unavailable"), "tws": GOOD_TWS})
    summary = run_derive_job(sb, _job(), client=model)
    assert len(model.calls) == 3, "the next cluster still runs"
    assert summary["failed"] == 1 and summary["proposed"] == 5 and summary["clusters_done"] == 3
    assert summary["errors"] == ["Life processes: RuntimeError: 503 model unavailable"]
    job = _job_row(sb)
    assert job["status"] == "error" and "1 of 3 clusters failed" in job["error"] and "503" in job["error"]
    assert _rows(sb, "n-bp") == []

    # The re-run asks only for the cluster that failed.
    model2 = _good_model()
    summary2 = run_derive_job(sb, _job(), client=model2)
    assert [_cluster_of(c["prompt"]) for c in model2.calls] == ["bp"]
    assert summary2["skipped"] == 2 and summary2["proposed"] == 1 and _job_row(sb)["status"] == "done"


def test_an_objectives_reply_that_names_nothing_is_a_failure_not_a_row():
    sb = _sb()
    model = FakeModel({"bs": {"raw_text": "```json\n{broken"}, "bp": GOOD_BP, "tws": GOOD_TWS})
    summary = run_derive_job(sb, _job(), client=model)
    assert summary["failed"] == 1 and _rows(sb, "n-bs") == []
    assert "no usable topics" in summary["errors"][0]
    assert _job_row(sb)["status"] == "error"


def test_a_client_that_always_raises_errors_the_job_and_writes_no_candidate():
    sb = _sb()
    summary = run_derive_job(sb, _job(), client=FakeModel(default=RuntimeError("quota")))
    assert summary["failed"] == 3 and sb.tables["topic_candidates"] == []
    assert _job_row(sb)["status"] == "error" and "3 of 3 clusters failed" in _job_row(sb)["error"]
    assert sb.writes("generations") == []


def test_a_client_that_cannot_be_built_errors_the_job(monkeypatch):
    """Production builds client_for("en") lazily; if that raises (no
    credentials) every cluster fails and the row says why."""
    def boom(lang, **kw):
        raise RuntimeError("VERTEX_PROJECT_ID unset")
    monkeypatch.setattr(derive, "client_for", boom)
    sb = _sb()
    run_derive_job(sb, _job())
    assert _job_row(sb)["status"] == "error" and "VERTEX_PROJECT_ID" in _job_row(sb)["error"]


def test_no_client_is_built_when_every_cluster_is_already_done(monkeypatch):
    sb = _sb()
    run_derive_job(sb, _job(), client=_good_model())
    monkeypatch.setattr(derive, "client_for", lambda *a, **k: pytest.fail("no cluster needed a call"))
    summary = run_derive_job(sb, _job())
    assert summary["skipped"] == 3 and _job_row(sb)["status"] == "done"


def test_a_missing_curriculum_finishes_the_job_with_error():
    sb = _sb(curriculum=None)
    assert run_derive_job(sb, _job(), client=_good_model()) is None
    assert _job_row(sb)["status"] == "error" and "not found" in _job_row(sb)["error"]
    assert sb.tables["topic_candidates"] == []


@pytest.mark.parametrize("params", [None, {}, {"curriculum_id": ""}, "cur-cam"])
def test_a_job_without_a_curriculum_id_finishes_with_error(params):
    sb = _sb(jobs=[_job(params=params)])
    run_derive_job(sb, _job(params=params), client=_good_model())
    assert _job_row(sb)["status"] == "error" and "curriculum_id" in _job_row(sb)["error"]


def test_a_curriculum_without_nodes_finishes_done_with_zero_clusters():
    sb = _sb(nodes=[])
    model = _good_model()
    summary = run_derive_job(sb, _job(), client=model)
    assert model.calls == [] and summary["clusters_total"] == 0 and _job_row(sb)["status"] == "done"


def test_a_database_failure_after_the_read_is_recorded(monkeypatch):
    sb = _sb()
    monkeypatch.setattr(derive, "existing_keys_by_node",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503 upstream")))
    assert run_derive_job(sb, _job(), client=_good_model()) is None
    assert _job_row(sb)["status"] == "error" and "503" in _job_row(sb)["error"]


def test_progress_is_reported_per_cluster():
    sb = _sb()
    run_derive_job(sb, _job(), client=_good_model())
    stages = [e[2]["stage"] for e in sb.writes("jobs") if e[0] == "update" and "stage" in e[2]]
    assert [s["clusters_done"] for s in stages] == [0, 1, 2, 3, 3]
    assert stages[-1]["step"] == "done" and stages[1]["step"] == "clusters"
    progress = [e[2]["progress"] for e in sb.writes("jobs") if e[0] == "update" and "progress" in e[2]]
    assert progress == [5, 35, 65, 95, 100]
