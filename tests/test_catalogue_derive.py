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

from pathlib import Path

import pytest

from catalogue import derive
from catalogue.derive import (
    DEFAULT_RATIONALE, MAX_TOPICS_PER_CLUSTER, RESPONSE_SCHEMA, SYSTEM_PROMPT, Cluster, build_clusters,
    build_prompt, clean_name, content_words, leaf_name, plan_writes, run_derive_job, validate_proposals,
)
from catalogue.harvest import is_heading
from catalogue.key import canonical_key
from catalogue.seeds.loader import load_seed
from tests.catalogue_fakes import FakeSB

SEEDS = Path(__file__).resolve().parents[1] / "catalogue" / "seeds"

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
        # A chapter list is its topics: no cap, and no invitation to reword.
        assert "never more than 8" not in text and "8 topics" not in text
        assert "do not shorten or reword it" in text and "2-5 words" not in text

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
        ("[Cells]", "Cells"), ("{Cells}", "Cells"), ("Plant [and] animal {cells}", "Plant and animal cells"),
    ])
    def test_tidied(self, raw, clean):
        assert clean_name(raw) == clean

    @pytest.mark.parametrize("raw", [
        "All living things are made of cells.",                # a sentence
        "The cell is the basic unit of life",                  # a determiner opening 6+ words
        "7Bs.01",                                              # an LO code
        "", None, "   ", "12", "الخلية",                       # nothing, or no key material
        "Fig. 3 shows the cell",
        "{}", "[]", "[{}]",                                    # JSON punctuation and nothing else
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
                                     [{"name": "All living things are made of cells.", "objective_codes": ["7Bs.01"]}],
                                     [{"name": 42, "objective_codes": ["7Bs.01"]}], [{"name": None, "objective_codes": ["7Bs.01"]}],
                                     [{"name": ["Cells"], "objective_codes": ["7Bs.01"]}], ["Cells", 7, None]])
    def test_a_reply_that_names_nothing_usable_is_empty(self, raw):
        assert validate_proposals(raw, _bs_cluster()) == []

    def test_a_name_that_is_not_a_string_is_skipped_and_its_codes_repaired(self):
        reply = [{"name": {"text": "Cells"}, "objective_codes": ["7Bs.01", "7Bs.02"]},
                 {"name": "Specialised cells", "objective_codes": ["7Bs.03", "7Bs.04", "7Bs.05"]}]
        props = validate_proposals(reply, _bs_cluster())
        assert [p.name for p in props] == ["Specialised cells"]
        assert props[0].codes == [c for _, c, _ in BS], "the skipped item's codes were repaired, not lost"

    @pytest.mark.parametrize("matches,kept", [
        ("Cell - Basic Unit of life", []),                     # a string is not a list of strings
        ({"title": "Cells"}, []),
        (["Cell - Basic Unit of life", 7, None, ["x"], "  "], ["Cell - Basic Unit of life"]),
        ([f"M{i}" for i in range(20)], [f"M{i}" for i in range(10)]),
    ])
    def test_matches_must_be_a_list_of_strings(self, matches, kept):
        (p,) = validate_proposals([{"name": "Cells", "objective_codes": [c for _, c, _ in BS], "matches": matches}],
                                  _bs_cluster())
        assert p.matches == kept

    @pytest.mark.parametrize("rationale", [None, 7, {"why": "x"}, ["x"]])
    def test_a_rationale_that_is_not_a_string_is_empty(self, rationale):
        (p,) = validate_proposals([{"name": "Cells", "objective_codes": [c for _, c, _ in BS], "rationale": rationale}],
                                  _bs_cluster())
        assert p.rationale == ""


# ── names mode validation (pure): a chapter list is its topics ──────────

# The real CBSE grade 8 chapter list (catalogue/seeds/cbse_science_086.json):
# thirteen chapters, two of which the harvest's gate refuses as names.
GRADE8 = [
    "Exploring the Investigative World of Science",
    "The Invisible Living World: Beyond Our Naked Eye",           # is_heading: False (determiner run)
    "Health: The Ultimate Treasure",
    "Electricity: Magnetic and Heating Effects",
    "Exploring Forces",
    "Pressure, Winds, Storms, and Cyclones",
    "Particulate Nature of Matter",
    "Nature of Matter: Elements, Compounds, and Mixtures",
    "The Amazing World of Solutes, Solvents, and Solutions",      # is_heading: False (determiner run)
    "Light: Mirrors and Lenses",
    "Keeping Time with the Skies",
    "How Nature Works in Harmony",
    "Our Home: Earth, a Unique Life-Sustaining Planet",
]


def _grade8_cluster():
    nodes = [_node(f"g8-{i + 1:02d}", f"cbse:8:ch{i + 1:02d}", t, grade="8", sort=i) for i, t in enumerate(GRADE8)]
    (c,) = build_clusters(nodes)
    assert c.mode == "names" and c.parent_id == "g8-01" and len(c.leaves) == 13
    return c


def _identity_reply(cluster):
    return [{"name": cluster.statement(n), "objective_codes": [code]} for code, n in zip(cluster.codes, cluster.leaves)]


class TestValidateNames:
    def test_the_gate_refuses_two_real_chapter_titles(self):
        assert [t for t in GRADE8 if not is_heading(t)] == [GRADE8[1], GRADE8[8]]

    def test_an_identity_reply_keeps_all_thirteen_chapters_as_their_own_topics(self):
        c = _grade8_cluster()
        props = validate_proposals(_identity_reply(c), c)
        assert [p.name for p in props] == GRADE8, "no cap of eight, no grade-label bucket"
        assert [p.codes for p in props] == [[code] for code in c.codes]
        assert all(p.key == canonical_key(leaf_name(n)) for p, n in zip(props, c.leaves)), (
            "each proposal keys exactly as the loader's row for that leaf")

    def test_a_merge_of_two_spellings_survives_with_the_leaf_s_own_title(self):
        c = _grade8_cluster()
        reply = _identity_reply(c)
        # The model merges ch03 into ch02 and names the pair by ch02's title -
        # a title the gate refuses. It is the leaf's own name: accepted as is.
        reply[1]["objective_codes"] = ["cbse:8:ch02", "cbse:8:ch03"]
        del reply[2]
        props = validate_proposals(reply, c)
        assert len(props) == 12
        assert (props[1].name, props[1].codes) == (GRADE8[1], ["cbse:8:ch02", "cbse:8:ch03"])

    def test_a_merge_under_a_new_name_is_kept_and_the_rest_stay_their_own(self):
        c = _grade8_cluster()
        reply = _identity_reply(c)
        reply[6] = {"name": "Matter and its nature", "objective_codes": ["cbse:8:ch07", "cbse:8:ch08"]}
        del reply[7]
        props = {p.name: p.codes for p in validate_proposals(reply, c)}
        assert props["Matter and its nature"] == ["cbse:8:ch07", "cbse:8:ch08"]
        assert len(props) == 12 and "Grade 8 chapters" not in props

    def test_an_orphaned_leaf_is_its_own_topic_never_a_neighbour_or_the_grade_label(self):
        c = _grade8_cluster()
        # The model names three chapters and forgets ten - among them ch02,
        # whose title shares "World" with ch01's; ch09 shares "Solutions"
        # with nothing. Before: word overlap or "Grade 8 chapters".
        reply = [{"name": GRADE8[0], "objective_codes": ["cbse:8:ch01"]},
                 {"name": "Exploring Forces", "objective_codes": ["cbse:8:ch05"]},
                 {"name": "Light: Mirrors and Lenses", "objective_codes": ["cbse:8:ch10"]}]
        props = validate_proposals(reply, c)
        by_code = {code: p for p in props for code in p.codes}
        assert len(props) == 13 and "Grade 8 chapters" not in {p.name for p in props}
        assert by_code["cbse:8:ch02"].name == GRADE8[1] and by_code["cbse:8:ch09"].name == GRADE8[8]
        assert by_code["cbse:8:ch02"].rationale == DEFAULT_RATIONALE
        assert sorted(code for p in props for code in p.codes) == sorted(c.codes)

    def test_an_invented_sentence_is_dropped_and_the_leaf_falls_back_to_its_own_title(self):
        c = _grade8_cluster()
        reply = _identity_reply(c)
        reply[4] = {"name": "Forces make things move.", "objective_codes": ["cbse:8:ch05"]}   # a sentence
        reply[9] = {"name": "cbse:8:ch10", "objective_codes": ["cbse:8:ch10"]}              # a code
        props = {code: p.name for p in validate_proposals(reply, c) for code in p.codes}
        assert props["cbse:8:ch05"] == "Exploring Forces" and props["cbse:8:ch10"] == "Light: Mirrors and Lenses"

    def test_a_tidied_spelling_of_a_refused_title_still_resolves_to_the_leaf_s_own_form(self):
        c = _grade8_cluster()
        reply = _identity_reply(c)
        reply[8]["name"] = '"9. The amazing world of solutes, solvents, and solutions."'
        props = {code: p.name for p in validate_proposals(reply, c) for code in p.codes}
        assert props["cbse:8:ch09"] == GRADE8[8], "same key as the leaf: the curriculum's spelling wins"

    def test_default_proposals_carry_the_refused_titles_too(self):
        c = _grade8_cluster()
        assert [p.name for p in derive.default_proposals(c)] == GRADE8
        assert validate_proposals({"raw_text": "nope"}.get("topics"), c) == []

    def test_objectives_mode_keeps_its_cap_and_repair(self):
        leaves = [_node(f"n-pf{i}", f"7Pf.{i:02d}", f"Statement about item{i} alpha{i}.", parent="n-pf",
                        strand="Physics", sub_strand="Forces", sort=i) for i in range(1, 14)]
        parent = _node("n-pf", "7/Pf", "Forces", parent="n-phy", strand="Physics", sub_strand="Forces")
        (cluster,) = build_clusters(leaves + [parent, _node("n-phy", "7/Physics", "Physics", strand="Physics")])
        props = validate_proposals([{"name": f"Topic item{i}", "objective_codes": [f"7Pf.{i:02d}"]} for i in range(1, 14)],
                                   cluster)
        assert cluster.mode == "objectives" and len(props) == MAX_TOPICS_PER_CLUSTER
        assert sorted(code for p in props for code in p.codes) == sorted(cluster.codes)


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


def test_a_row_the_loader_filed_under_the_parent_does_not_count_as_done_and_is_updated():
    """A key already under the parent WITHOUT node_ids is not this job's work:
    the model is still asked — and the proposal with that key UPDATES the old
    row (node_ids, rationale) rather than vanishing with the objectives it
    covered (review 2026-09-06). The unique index is never hit."""
    sb = _sb(candidates=[{"source_kind": "curriculum", "node_id": "n-bs", "raw_title": "Cells", "normalized": "cell",
                          "status": "open"}])
    model = _good_model()
    summary = run_derive_job(sb, _job(), client=model)
    assert len(model.calls) == 3 and summary["skipped"] == 0
    bs_rows = _rows(sb, "n-bs")
    assert sorted(r["raw_title"] for r in bs_rows) == ["Cells", "Plant and animal cells", "Specialised cells",
                                                       "Tissues, organs and organ systems"]
    (cells,) = [r for r in bs_rows if r["raw_title"] == "Cells"]
    assert cells["node_ids"] == ["n-bs1", "n-bs2"] and cells["rationale"] == "What a cell is and its parts."
    assert cells["status"] == "open", "status is the curator's; only coverage and rationale are ours"
    assert summary["proposed"] == 5 and summary["updated"] == 1 and summary["dismissed"] == 0
    placed = [nid for r in sb.tables["topic_candidates"] for nid in (r.get("node_ids") or [])]
    assert sorted(placed) == sorted(id_ for id_, _, _ in BS + BP + TWS), "7Bs.01 and 7Bs.02 are covered"
    assert [e for e in sb.writes("topic_candidates") if e[0] == "update"] == [
        ("update", "topic_candidates", {"node_ids": ["n-bs1", "n-bs2"], "rationale": "What a cell is and its parts."},
         [("eq", "source_kind", "curriculum"), ("eq", "node_id", "n-bs"), ("eq", "normalized", "cell")])]

    # The run is now done: nothing is asked again.
    model2 = FakeModel(default=GOOD_BS)
    assert run_derive_job(sb, _job(), client=model2)["skipped"] == 3 and model2.calls == []


def test_a_curator_s_dismissed_row_under_the_parent_gains_coverage_but_keeps_its_status():
    sb = _sb(candidates=[{"source_kind": "curriculum", "node_id": "n-bs", "raw_title": "Cells", "normalized": "cell",
                          "status": "dismissed", "suggested_topic_id": "t-human"}])
    run_derive_job(sb, _job(), client=_good_model())
    (cells,) = [r for r in _rows(sb, "n-bs") if r["raw_title"] == "Cells"]
    assert cells["status"] == "dismissed" and cells["node_ids"] == ["n-bs1", "n-bs2"]
    assert cells["suggested_topic_id"] == "t-human", "a suggestion already there is not overwritten"


def test_an_existing_row_s_node_ids_are_unioned_not_replaced():
    sb = _sb(candidates=[{"source_kind": "curriculum", "node_id": "n-bs", "raw_title": "Cells", "normalized": "cell",
                          "node_ids": ["n-elsewhere"], "status": "open"}])
    # node_ids naming a node outside the cluster: not done, so the model is asked.
    run_derive_job(sb, _job(), client=_good_model())
    (cells,) = [r for r in _rows(sb, "n-bs") if r["raw_title"] == "Cells"]
    assert cells["node_ids"] == ["n-elsewhere", "n-bs1", "n-bs2"]


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


def test_names_mode_files_a_merge_under_a_new_name_under_the_unit_and_dismisses_the_swallowed_rows():
    sb = _sb(nodes=_cbse_nodes(), curriculum=CBSE, jobs=[_cbse_job()], candidates=LOADER_ROWS)
    reply = {"topics": [{"name": "Matter around us", "objective_codes": ["cbse:9:U1:01", "cbse:9:U1:02"],
                         "rationale": "Two chapters on one topic."}]}
    summary = run_derive_job(sb, _cbse_job(), client=FakeModel(default=reply))
    new = _rows(sb, "u1")
    assert [(r["raw_title"], r["node_ids"]) for r in new] == [("Matter around us", ["t1", "t2"])]
    assert summary["proposed"] == 1 and summary["updated"] == 0 and summary["dismissed"] == 2
    # One candidate per topic: the two leaves' own rows leave the queue, saying where they went.
    for nid in ("t1", "t2"):
        (own,) = _rows(sb, nid)
        assert own["status"] == "dismissed" and own["rationale"] == "Merged into Matter around us."
    assert len(sb.tables["topic_candidates"]) == 3

    # Done: the merged row under the unit names the leaves.
    model2 = FakeModel(default=reply)
    assert run_derive_job(sb, _cbse_job(), client=model2)["skipped"] == 1 and model2.calls == []


def test_names_mode_a_merge_named_after_a_leaf_updates_that_leaf_s_row_and_dismisses_the_other():
    """Two spellings of one topic: the merged row IS the first leaf's own row
    (node_ids = both); the second leaf's open row is dismissed."""
    sb = _sb(nodes=_cbse_nodes(), curriculum=CBSE, jobs=[_cbse_job()], candidates=LOADER_ROWS)
    reply = {"topics": [{"name": "Matter in Our Surroundings", "objective_codes": ["cbse:9:U1:01", "cbse:9:U1:02"],
                         "rationale": "Same topic, two spellings."}]}
    summary = run_derive_job(sb, _cbse_job(), client=FakeModel(default=reply))
    assert _rows(sb, "u1") == [], "nothing new under the unit"
    (t1,) = _rows(sb, "t1")
    assert t1["node_ids"] == ["t1", "t2"] and t1["rationale"] == "Same topic, two spellings." and t1["status"] == "open"
    (t2,) = _rows(sb, "t2")
    assert t2["status"] == "dismissed" and t2["rationale"] == "Merged into Matter in Our Surroundings."
    assert summary["proposed"] == 0 and summary["updated"] == 1 and summary["dismissed"] == 1
    assert len(sb.tables["topic_candidates"]) == 2

    # Done, by the rows alone: the leaf's row names both leaves.
    model2 = FakeModel(default=reply)
    assert run_derive_job(sb, _cbse_job(), client=model2)["skipped"] == 1 and model2.calls == []


def test_a_merge_never_dismisses_a_row_a_curator_has_already_decided():
    rows = [dict(r) for r in LOADER_ROWS]
    rows[1]["status"] = "merged"
    sb = _sb(nodes=_cbse_nodes(), curriculum=CBSE, jobs=[_cbse_job()], candidates=rows)
    reply = {"topics": [{"name": "Matter in Our Surroundings", "objective_codes": ["cbse:9:U1:01", "cbse:9:U1:02"]}]}
    summary = run_derive_job(sb, _cbse_job(), client=FakeModel(default=reply))
    (t2,) = _rows(sb, "t2")
    assert t2["status"] == "merged" and "rationale" not in t2 and summary["dismissed"] == 0
    assert _rows(sb, "t1")[0]["node_ids"] == ["t1", "t2"]


def test_a_one_leaf_rename_dismisses_nothing():
    """A rename is not a merge: the loader's row and the renamed one both
    stay open, for the curator to choose."""
    sb = _sb(nodes=_cbse_nodes(), curriculum=CBSE, jobs=[_cbse_job()], candidates=LOADER_ROWS)
    reply = {"topics": [{"name": "Matter in Our Surroundings", "objective_codes": ["cbse:9:U1:01"]},
                        {"name": "Pure substances and mixtures", "objective_codes": ["cbse:9:U1:02"]}]}
    summary = run_derive_job(sb, _cbse_job(), client=FakeModel(default=reply))
    assert [(r["raw_title"], r["node_ids"]) for r in _rows(sb, "u1")] == [("Pure substances and mixtures", ["t2"])]
    assert _rows(sb, "t2")[0]["status"] == "open" and summary["dismissed"] == 0
    assert summary["proposed"] == 1 and summary["updated"] == 0


class TestPlanWrites:
    """The pure planner, on the grade-8 cluster with the loader's rows."""

    def _existing(self, cluster, **extra_rows):
        ex = {n["id"]: {canonical_key(leaf_name(n)): {"node_id": n["id"], "normalized": canonical_key(leaf_name(n)),
                                                     "status": "open", "node_ids": None}}
              for n in cluster.leaves}
        for nid, rows in extra_rows.items():
            ex.setdefault(nid, {}).update(rows)
        return ex

    def test_identity_writes_nothing(self):
        c = _grade8_cluster()
        props = validate_proposals(_identity_reply(c), c)
        rows = derive.rows_for_cluster(c, props, {})
        assert plan_writes(c, props, rows, self._existing(c)) == ([], [], [])

    def test_a_merge_named_after_the_first_leaf(self):
        c = _grade8_cluster()
        reply = _identity_reply(c)
        reply[0]["objective_codes"] = ["cbse:8:ch01", "cbse:8:ch02"]
        del reply[1]
        props = validate_proposals(reply, c)
        rows = derive.rows_for_cluster(c, props, {})
        inserts, updates, dismissals = plan_writes(c, props, rows, self._existing(c))
        k1, k2 = canonical_key(GRADE8[0]), canonical_key(GRADE8[1])
        assert inserts == []
        assert updates == [("g8-01", k1, {"node_ids": ["g8-01", "g8-02"]})]
        assert dismissals == [("g8-02", k2, {"status": "dismissed", "rationale": f"Merged into {GRADE8[0]}."})]

    def test_a_new_key_is_inserted_under_the_parent_with_no_loader_rows_touched_for_one_leaf(self):
        c = _grade8_cluster()
        reply = _identity_reply(c)
        reply[4]["name"] = "Forces"
        props = validate_proposals(reply, c)
        rows = derive.rows_for_cluster(c, props, {})
        inserts, updates, dismissals = plan_writes(c, props, rows, self._existing(c))
        assert [(r["node_id"], r["raw_title"], r["node_ids"]) for r in inserts] == [("g8-01", "Forces", ["g8-05"])]
        assert updates == [] and dismissals == []

    def test_a_suggested_topic_fills_an_empty_slot_only(self):
        c = _grade8_cluster()
        reply = _identity_reply(c)
        reply[0]["objective_codes"] = ["cbse:8:ch01", "cbse:8:ch02"]
        del reply[1]
        props = validate_proposals(reply, c)
        k1 = canonical_key(GRADE8[0])
        rows = derive.rows_for_cluster(c, props, {k1: "t-new"})
        _, updates, _ = plan_writes(c, props, rows, self._existing(c))
        assert updates[0][2]["suggested_topic_id"] == "t-new"
        ex = self._existing(c)
        ex["g8-01"][k1]["suggested_topic_id"] = "t-curator"
        _, updates, _ = plan_writes(c, props, rows, ex)
        assert "suggested_topic_id" not in updates[0][2]


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
    assert _rows(sb, "c2")[0]["status"] == "open", "a rename dismisses nothing"
    assert summary["done_clusters"] == [["c1", "names"]]


# ── idempotency: (parent_id, mode) and the record in jobs.stage ─────────


def test_the_skip_set_is_keyed_on_parent_and_mode():
    """One parent, two clusters (a names leaf and four objective leaves). A
    row covering the names leaf says the NAMES cluster is done; the objectives
    cluster under the same parent is still asked."""
    nodes = _nodes()
    for n in nodes:
        if n["id"] == "n-bs1":
            n["kind"], n["title"] = "topic", "Cells"
    sb = _sb(nodes=nodes, candidates=[{"source_kind": "curriculum", "node_id": "n-bs", "raw_title": "Cells",
                                       "normalized": "cell", "node_ids": ["n-bs1"], "status": "open"}])
    objectives_reply = {"topics": [{"name": "Cell structure", "objective_codes": ["7Bs.02", "7Bs.03", "7Bs.04", "7Bs.05"]}]}
    model = FakeModel({"bs": {"topics": [{"name": "Cells", "objective_codes": ["7Bs.01"]}]},   # "bs" = the names cluster (7Bs.01)
                       "bp": GOOD_BP, "tws": GOOD_TWS}, default=objectives_reply)
    summary = run_derive_job(sb, _job(), client=model)
    prompts = [c["prompt"] for c in model.calls]
    assert not any("7Bs.01:" in p for p in prompts), "the names cluster is done"
    assert sum("7Bs.02:" in p for p in prompts) == 1, "the objectives cluster under the same parent is asked"
    assert summary["skipped"] == 1 and summary["calls"] == 3
    assert ["n-bs", "names"] in summary["done_clusters"] and ["n-bs", "objectives"] in summary["done_clusters"]
    assert {r["raw_title"]: r["node_ids"] for r in _rows(sb, "n-bs")} == {
        "Cells": ["n-bs1"], "Cell structure": ["n-bs2", "n-bs3", "n-bs4", "n-bs5"]}


def test_done_clusters_recorded_by_an_earlier_job_are_skipped_without_a_row():
    """The names-mode case in miniature: an earlier run finished a cluster
    but wrote nothing (every proposal was the loader's row). Its jobs.stage
    record is what says so."""
    old = _job(id="job-old", status="done", stage={"step": "done", "done_clusters": [["n-bs", "objectives"]]})
    sb = _sb(jobs=[old, _job()])
    model = _good_model()
    summary = run_derive_job(sb, _job(), client=model)
    assert [_cluster_of(c["prompt"]) for c in model.calls] == ["bp", "tws"]
    assert summary["skipped"] == 1 and _rows(sb, "n-bs") == []
    assert sorted(summary["done_clusters"]) == [["n-bp", "objectives"], ["n-bs", "objectives"], ["n-twsm", "objectives"]], (
        "the record carries every cluster done as of this run, skipped ones included")
    assert sb.tables["jobs"][1]["stage"]["done_clusters"] == summary["done_clusters"]


@pytest.mark.parametrize("old", [
    dict(id="job-other", status="done", params={"curriculum_id": "cur-other"},
         stage={"done_clusters": [["n-bs", "objectives"]]}),                       # another curriculum
    dict(id="job-harvest", type="topic_harvest", status="done", stage={"done_clusters": [["n-bs", "objectives"]]}),
    dict(id="job-junk", status="done", stage={"done_clusters": "n-bs"}),
    dict(id="job-junk2", status="done", stage={"done_clusters": [["n-bs"], ["n-bs", "objectives", "x"], [1, 2], None]}),
    dict(id="job-junk3", status="done", stage="clusters"),
    dict(id="job-junk4", status="done", stage=None),
])
def test_a_record_that_is_not_this_curriculum_s_or_not_a_pair_is_ignored(old):
    sb = _sb(jobs=[_job(**old), _job()])
    model = _good_model()
    run_derive_job(sb, _job(), client=model)
    assert len(model.calls) == 3


def test_a_processing_job_s_partial_record_counts():
    """A run that is still going (or a runner re-using this row) lists the
    clusters it has finished; those ARE done."""
    sb = _sb(jobs=[_job(id="job-running", status="processing", stage={"done_clusters": [["n-bp", "objectives"]]}), _job()])
    model = _good_model()
    summary = run_derive_job(sb, _job(), client=model)
    assert [_cluster_of(c["prompt"]) for c in model.calls] == ["bs", "tws"] and summary["skipped"] == 1


def test_a_reused_job_row_keeps_its_record_because_it_is_read_before_the_first_stage_write():
    sb = _sb()
    run_derive_job(sb, _job(), client=_good_model())
    sb.tables["topic_candidates"] = []          # the rows are gone; only the record remains
    sb.tables["jobs"][0]["status"] = "processing"
    model = FakeModel(default=GOOD_BS)
    summary = run_derive_job(sb, _job(), client=model)
    assert model.calls == [] and summary["skipped"] == 3


def test_an_unreadable_jobs_record_costs_calls_not_the_job(monkeypatch):
    """The record is an optimisation: a PostgREST that refuses the JSON-path
    filter means clusters are re-asked, never that the job fails."""
    from tests.catalogue_fakes import _Query
    real = _Query.execute

    def execute(self):
        if self.table == "jobs" and self.op == "select":
            raise RuntimeError("PGRST100: malformed filter")
        return real(self)

    monkeypatch.setattr(_Query, "execute", execute)
    sb = _sb()
    run_derive_job(sb, _job(), client=_good_model())            # writes the rows
    sb.tables["jobs"][0]["status"] = "processing"
    model = _good_model()
    summary = run_derive_job(sb, _job(), client=model)
    assert model.calls == [] and summary["skipped"] == 3, "the rows still say done"
    sb.tables["topic_candidates"] = []
    sb.tables["jobs"][0]["status"] = "processing"
    model = _good_model()
    summary = run_derive_job(sb, _job(), client=model)
    assert len(model.calls) == 3 and summary["failed"] == 0 and _job_row(sb)["status"] == "done"


# ── the real seeds through the real loader ──────────────────────────────


def _loaded(which):
    """The REAL loader against the fake DB: exactly what prod has after
    ``load_seed`` (nodes with parent_id and kind; the loader's per-leaf
    candidates for CBSE, none for Cambridge)."""
    sb = FakeSB()
    out = load_seed(sb, SEEDS / f"{which}.json")
    sb.tables["jobs"] = []
    sb.tables.setdefault("topics", [])
    return sb, out["curriculum_id"]


def _run(sb, cur_id, model, job_id):
    """A fresh job row per run, as the queue gives it."""
    job = {"id": job_id, "type": "topic_derive", "status": "processing", "generation_id": None, "book_id": None,
           "params": {"curriculum_id": cur_id}}
    sb.tables["jobs"].append(dict(job))
    return run_derive_job(sb, job, client=model)


def _lines(prompt):
    """The CODE: STATEMENT lines of a prompt."""
    block = prompt.split("(one per line, CODE: ", 1)[1].split("\n\nTASK:", 1)[0]
    return [tuple(line.split(": ", 1)) for line in block.split("\n")[1:] if ": " in line]


def _identity(prompt):
    return {"topics": [{"name": s, "objective_codes": [c]} for c, s in _lines(prompt)]}


def _merge_first_two(prompt):
    tops = [{"name": s, "objective_codes": [c]} for c, s in _lines(prompt)]
    if len(tops) >= 2:
        tops[0]["objective_codes"] += tops[1]["objective_codes"]
        del tops[1]
    return {"topics": tops}


def _grouper(prompt):
    """A plausible objectives model: one topic per two objectives, named from words of the first."""
    lines = _lines(prompt)
    topics = []
    for i in range(0, len(lines), 2):
        chunk = lines[i:i + 2]
        words = [w for w in chunk[0][1].split() if w.isalpha()][2:5]
        topics.append({"name": " ".join(words).title() or "Things", "objective_codes": [c for c, _ in chunk]})
    return {"topics": topics}


def test_the_real_cbse_seed_with_an_identity_model_is_asked_once_and_never_again():
    sb, cur_id = _loaded("cbse_science_086")
    assert len(sb.tables["topic_candidates"]) == 67, "the loader's per-leaf rows"
    before = sb.snapshot()["topic_candidates"]

    m1 = FakeModel(default=_identity)
    s1 = _run(sb, cur_id, m1, "job-1")
    assert len(m1.calls) == 12 and s1["clusters_total"] == 12
    assert (s1["proposed"], s1["updated"], s1["dismissed"], s1["failed"]) == (0, 0, 0, 0)
    assert sb.tables["topic_candidates"] == before, "every leaf keeps its own candidate; nothing new under any parent"
    assert not any(r.get("raw_title", "").startswith("Grade ") for r in sb.tables["topic_candidates"])
    assert len(s1["done_clusters"]) == 12 and all(mode == "names" for _, mode in s1["done_clusters"])

    m2 = FakeModel(default=_identity)
    s2 = _run(sb, cur_id, m2, "job-2")
    assert m2.calls == [] and s2["skipped"] == 12 and s2["calls"] == 0
    assert sb.tables["topic_candidates"] == before


def test_the_real_cbse_grade_8_list_keeps_all_thirteen_chapters():
    sb, cur_id = _loaded("cbse_science_086")
    by_code = {n["code"]: n for n in sb.tables["curriculum_nodes"]}
    _run(sb, cur_id, FakeModel(default=_identity), "job-1")
    for i, title in enumerate(GRADE8, start=1):
        node = by_code[f"cbse:8:ch{i:02d}"]
        (own,) = _rows(sb, node["id"])
        # The loader writes no status (the column defaults to 'open'); the fake keeps it absent.
        assert own["raw_title"] == title and own.get("status", "open") == "open" and not own.get("node_ids")
    assert [r for r in sb.tables["topic_candidates"] if r.get("node_ids")] == []


def test_the_real_cbse_seed_with_a_merging_model_files_one_merged_row_per_cluster():
    sb, cur_id = _loaded("cbse_science_086")
    by_code = {n["code"]: n for n in sb.tables["curriculum_nodes"]}
    m1 = FakeModel(default=_merge_first_two)
    s1 = _run(sb, cur_id, m1, "job-1")
    # 12 clusters; the two one-leaf units have nothing to merge.
    assert len(m1.calls) == 12 and (s1["proposed"], s1["updated"], s1["dismissed"]) == (0, 10, 10)
    assert len(sb.tables["topic_candidates"]) == 67, "no new row: every merge lives in the first leaf's own row"
    for first, second in (("cbse:8:ch01", "cbse:8:ch02"), ("cbse:6:ch01", "cbse:6:ch02"), ("cbse:9:U1:01", "cbse:9:U1:02")):
        (a,), (b,) = _rows(sb, by_code[first]["id"]), _rows(sb, by_code[second]["id"])
        assert a["node_ids"] == [by_code[first]["id"], by_code[second]["id"]] and a.get("status", "open") == "open"
        assert b["status"] == "dismissed" and b["rationale"] == f"Merged into {by_code[first]['title']}."
    merged = [r for r in sb.tables["topic_candidates"] if r.get("node_ids")]
    assert len(merged) == 10 and all(len(r["node_ids"]) == 2 for r in merged)

    m2 = FakeModel(default=_merge_first_two)
    s2 = _run(sb, cur_id, m2, "job-2")
    assert m2.calls == [] and s2["skipped"] == 12


def test_the_real_cambridge_seed_is_asked_once_per_sub_strand_and_never_again():
    sb, cur_id = _loaded("cambridge_ls_science_0893")
    assert sb.tables["topic_candidates"] == [], "its seed says candidates: none"
    m1 = FakeModel(default=_grouper)
    s1 = _run(sb, cur_id, m1, "job-1")
    assert len(m1.calls) == 48 and s1["clusters_total"] == 48 and s1["failed"] == 0
    placed = [nid for r in sb.tables["topic_candidates"] for nid in r["node_ids"]]
    objectives = [n["id"] for n in sb.tables["curriculum_nodes"] if n.get("kind") == "objective"]
    assert len(objectives) == 200 and sorted(placed) == sorted(objectives), "every objective in exactly one row"
    assert all(mode == "objectives" for _, mode in s1["done_clusters"]) and len(s1["done_clusters"]) == 48

    m2 = FakeModel(default=_grouper)
    s2 = _run(sb, cur_id, m2, "job-2")
    assert m2.calls == [] and s2["skipped"] == 48 and s2["proposed"] == 0


# ── prompt hygiene ──────────────────────────────────────────────────────


def test_only_headings_reach_the_prompt_from_existing_topics_and_other_curricula():
    sb = _sb(topics=[
        {"id": "t1", "canonical_key": "photosynthesi", "title": "Photosynthesis", "status": "approved"},
        {"id": "t2", "canonical_key": "x", "title": "The cell is the basic unit of life", "status": "approved"},  # a sentence
        {"id": "t3", "canonical_key": "y", "title": "Understand that all organisms are made of cells.", "status": "approved"},
        {"id": "t4", "canonical_key": "z", "title": "T" * 130, "status": "approved"},                             # too long
    ])
    sb.tables["curricula"].append({"id": "cur-cbse", "code": "cbse_science_086", "name": "CBSE Science"})
    sb.tables["curriculum_nodes"] += [
        {"id": "cb-1", "curriculum_id": "cur-cbse", "code": "cbse:9:U2:01", "title": "Cell - Basic Unit of life", "sort": 1},
        {"id": "cb-2", "curriculum_id": "cur-cbse", "code": "cbse:9:U2:02", "title": "x", "sort": 2},
    ]
    sb.tables["topic_candidates"] += [
        {"source_kind": "curriculum", "node_id": "cb-1", "raw_title": "Cell - Basic Unit of life",
         "normalized": "cell_basic_unit_of_life", "status": "open"},
        # Filed from the portal off an objective node: a sentence, not a name.
        {"source_kind": "curriculum", "node_id": "cb-2", "raw_title": "Describe the process of digestion, including the role of enzymes.",
         "normalized": "describe_the_process_of_digestion_including_the_role_of_enzyme", "status": "open"},
    ]
    model = _good_model()
    run_derive_job(sb, _job(), client=model)
    prompt = model.calls[0]["prompt"]
    # The two context sections, exactly (the cluster's OWN objective lines
    # above them are sentences by design — they are what is being named).
    existing = prompt.split("EXISTING TOPICS (titles already in the catalogue):\n", 1)[1].split("\n\nOPEN CANDIDATES", 1)[0]
    others = prompt.split("OPEN CANDIDATES FROM OTHER CURRICULA (title | curriculum code):\n", 1)[1].split("\n\nReply with", 1)[0]
    assert existing == "- Photosynthesis"
    assert others == "- Cell - Basic Unit of life | cbse_science_086"
    assert derive.load_existing_titles(sb) == ["Photosynthesis"]
    assert derive.load_other_candidates(sb, CUR["id"]) == [("Cell - Basic Unit of life", "cbse_science_086")]


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
    monkeypatch.setattr(derive, "existing_rows_by_node",
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
