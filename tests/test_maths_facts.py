"""A geometry chapter's worksheet: naming questions are facts, proved by a
closed table — never "WRONG" by algebra, never printed unproved.

Production, 2026-10-07 (generation 8861d1e6, Cambridge Primary Mathematics
5, "2D shape and pattern"): every question was rejected as
"'triangle' is not equivalent to '3'" and the worksheet failed. No network:
the model client is a stub that answers by the schema it is asked for.
"""

from __future__ import annotations

import copy
import json

import pytest

from docgen import generate_document
from maths.facts import parse_item, verify_item
from maths.questions import FACT_SET_SCHEMA, SET_SCHEMA
from maths.schema import parse_example
from maths.verify import verify_example
from shared.coverage import docx_text

BOOK = {"title": "Cambridge Primary Mathematics Learner's Book 5", "grade": "Stage 5", "subject": "Mathematics"}
CHAPTER = {"title": "2D shape and pattern", "sections": []}

# the two questions the production error quoted, as the model wrote them
Q_TRIANGLE = {"label": "Q", "difficulty": 1, "task": "evaluate",
              "problem": "Identify the number of sides of a triangle", "givens": ["triangle"], "target": "",
              "intro_speech": "", "answer_speech": "",
              "steps": [{"kind": "transform", "operation": "count the sides of a triangle",
                         "before": ["triangle"], "after": ["3"], "speech": "s"}],
              "final_answer": ["3"]}
Q_PENTAGON = {"label": "Q", "difficulty": 2, "task": "evaluate",
              "problem": "Name the regular polygon with 5 sides", "givens": ["5 sides"], "target": "",
              "intro_speech": "", "answer_speech": "",
              "steps": [{"kind": "transform", "operation": "name the polygon",
                         "before": ["5 sides"], "after": ["pentagon"], "speech": "s"}],
              "final_answer": ["pentagon"]}
# a computational geometry question: the perimeter of a regular pentagon
Q_PERIMETER = {"label": "Q", "difficulty": 2, "task": "evaluate",
               "problem": "A regular pentagon has sides of 4 cm. Find its perimeter in cm.", "givens": ["5 * 4"],
               "target": "", "intro_speech": "", "answer_speech": "",
               "steps": [{"kind": "transform", "operation": "multiply the side by the number of sides",
                          "before": ["5 * 4"], "after": ["20"], "speech": "s"}],
               "final_answer": ["20"]}

GOOD_FACTS = [
    {"format": "fill_blank", "difficulty": 1, "q": "A triangle has ____ sides.", "answer": "3",
     "relation": "polygon_sides", "subject": "triangle", "value": "3", "pairs": []},
    {"format": "fill_blank", "difficulty": 1, "q": "A polygon with 5 sides is called a ____.", "answer": "pentagon",
     "relation": "polygon_sides", "subject": "pentagon", "value": "5", "pairs": []},
    {"format": "fill_blank", "difficulty": 3, "q": "A triangle with sides of 6 cm, 6 cm and 6 cm is ____.",
     "answer": "equilateral", "relation": "triangle_by_sides", "subject": "6, 6, 6", "value": "equilateral",
     "pairs": []},
    {"format": "true_false", "difficulty": 1, "q": "A hexagon has 8 sides.", "answer": "false",
     "relation": "polygon_sides", "subject": "hexagon", "value": "8", "pairs": []},
    {"format": "true_false", "difficulty": 2, "q": "A regular hexagon has 6 lines of symmetry.", "answer": "true",
     "relation": "symmetry_lines", "subject": "regular hexagon", "value": "6", "pairs": []},
    {"format": "match", "difficulty": 1, "q": "", "answer": "", "relation": "polygon_sides", "subject": "",
     "value": "", "pairs": [{"left": "octagon", "right": "8 sides", "subject": "octagon", "value": "8"},
                            {"left": "heptagon", "right": "7 sides", "subject": "heptagon", "value": "7"},
                            {"left": "decagon", "right": "10 sides", "subject": "decagon", "value": "10"}]},
]
BAD_FACTS = [
    # a false fact in a blank: must never reach the key
    {"format": "fill_blank", "difficulty": 1, "q": "An octagon has ____ sides.", "answer": "6",
     "relation": "polygon_sides", "subject": "octagon", "value": "6", "pairs": []},
    # the answer key would say "true" to a false statement
    {"format": "true_false", "difficulty": 1, "q": "A nonagon has 7 sides.", "answer": "true",
     "relation": "polygon_sides", "subject": "nonagon", "value": "7", "pairs": []},
    # a blank with more than one right answer (square, kite, rhombus...)
    {"format": "fill_blank", "difficulty": 1, "q": "A shape with 4 sides is a ____.", "answer": "rhombus",
     "relation": "polygon_sides", "subject": "rhombus", "value": "4", "pairs": []},
    # a negation the checker cannot follow
    {"format": "true_false", "difficulty": 1, "q": "A dodecagon does not have 12 sides.", "answer": "false",
     "relation": "polygon_sides", "subject": "dodecagon", "value": "12", "pairs": []},
    # a solid the table deliberately does not hold
    {"format": "fill_blank", "difficulty": 1, "q": "A cylinder has ____ faces.", "answer": "3",
     "relation": "solid_faces", "subject": "cylinder", "value": "3", "pairs": []},
]


class GeometryClient:
    """Answers the ladder call with what production got (naming questions,
    optionally one computational one) and the fact call with items."""
    model = "stub"

    def __init__(self, ladder: list[dict], facts: list[dict]):
        self.ladder, self.facts = ladder, facts
        self.calls: list[str] = []

    def analyze(self, prompt, system="", max_tokens=0, retries=3, cache_prefix=None, response_schema=None, **kw):
        if response_schema is FACT_SET_SCHEMA:
            self.calls.append("facts")
            return {"data": {"items": copy.deepcopy(self.facts)}, "usage": {}, "truncated": False}
        assert response_schema is SET_SCHEMA
        self.calls.append("ladder")
        return {"data": {"questions": copy.deepcopy(self.ladder)}, "usage": {}, "truncated": False}


# ── the verifier no longer "proves" a word wrong ─────────────────────────────

@pytest.mark.parametrize("q, word", [(Q_TRIANGLE, "triangle"), (Q_PENTAGON, "sides")])
def test_a_naming_step_is_unverifiable_not_wrong(q, word):
    rep = verify_example(parse_example(copy.deepcopy(q)))
    assert rep.status == "failed", "unproven is still never printed"
    text = " ".join(rep.reasons)
    assert "WRONG" not in text, text
    assert repr(word) in text and "not a quantity" in text


def test_a_word_on_both_sides_still_compares():
    """A quantity named by a word ("speed") is algebra when both sides carry it."""
    q = {"label": "Q", "difficulty": 1, "task": "simplify", "problem": "2speed + 3speed",
         "givens": ["2speed + 3speed"], "target": "", "intro_speech": "", "answer_speech": "",
         "steps": [{"kind": "transform", "operation": "collect like terms", "before": ["2speed + 3speed"],
                    "after": ["5speed"], "speech": "s"}], "final_answer": ["5speed"]}
    assert verify_example(parse_example(q)).status == "verified"
    q["steps"][0]["after"] = ["6speed"]
    q["final_answer"] = ["6speed"]
    rep = verify_example(parse_example(q))
    assert any("WRONG" in r for r in rep.reasons), "a real algebra slip is still WRONG"


def test_the_computational_geometry_question_verifies():
    assert verify_example(parse_example(copy.deepcopy(Q_PERIMETER))).status == "verified"


# ── the table ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("item", GOOD_FACTS)
def test_a_true_fact_item_verifies(item):
    check = verify_item(parse_item(item))
    assert check.ok, check.detail


@pytest.mark.parametrize("item", BAD_FACTS)
def test_an_unprovable_fact_item_is_refused(item):
    assert not verify_item(parse_item(item)).ok


@pytest.mark.parametrize("item", [
    {"format": "fill_blank", "q": "A triangular prism has ____ edges.", "answer": "9",
     "relation": "solid_edges", "subject": "triangular prism", "value": "9"},
    {"format": "fill_blank", "q": "A square-based pyramid has ____ vertices.", "answer": "5",
     "relation": "solid_vertices", "subject": "square-based pyramid", "value": "5"},
    {"format": "fill_blank", "q": "An angle of 135° is an ____ angle.", "answer": "obtuse",
     "relation": "angle_type", "subject": "135", "value": "obtuse"},
    {"format": "true_false", "q": "A triangle with angles of 30°, 60° and 90° is right-angled.", "answer": "true",
     "relation": "triangle_by_angles", "subject": "30, 60, 90", "value": "right-angled"},
    {"format": "fill_blank", "q": "Face east and turn 135° anticlockwise. You now face ____.",
     "answer": "north-west", "relation": "compass_turn", "subject": "east, 135, anticlockwise",
     "value": "north-west"},
    {"format": "fill_blank", "q": "An event with a probability of 1/2 has an ____.", "answer": "even chance",
     "relation": "probability_word", "subject": "1/2", "value": "even chance"},
    {"format": "fill_blank", "q": "3:45 pm on the 24-hour clock is ____.", "answer": "15:45",
     "relation": "time_24h", "subject": "3:45 pm", "value": "15:45"},
])
def test_the_other_chapters_of_the_book_have_facts(item):
    """3D shapes, angles, position and direction, probability and time —
    the chapters the same failure would have hit."""
    check = verify_item(parse_item(item))
    assert check.ok, check.detail


def test_a_match_with_two_rows_sharing_a_partner_is_refused():
    item = {"format": "match", "relation": "polygon_sides", "pairs": [
        {"left": "square", "right": "4 sides", "subject": "square", "value": "4"},
        {"left": "kite", "right": "4 sides", "subject": "kite", "value": "4"},
        {"left": "octagon", "right": "8 sides", "subject": "octagon", "value": "8"}]}
    assert not verify_item(parse_item(item)).ok


def test_midnight_and_noon_are_not_guessed():
    assert not verify_item(parse_item({"format": "fill_blank", "q": "12:10 am on the 24-hour clock is ____.",
                                       "answer": "12:10", "relation": "time_24h", "subject": "12:10 am",
                                       "value": "12:10"})).ok
    assert verify_item(parse_item({"format": "fill_blank", "q": "12:10 am on the 24-hour clock is ____.",
                                   "answer": "00:10", "relation": "time_24h", "subject": "12:10 am",
                                   "value": "00:10"})).ok


# ── the worksheet, end to end ────────────────────────────────────────────────

def test_the_production_geometry_worksheet_now_builds_from_proved_facts(tmp_path):
    client = GeometryClient([Q_TRIANGLE, Q_PENTAGON], GOOD_FACTS + BAD_FACTS)
    paths = generate_document("worksheet", BOOK, CHAPTER, {}, client, {"num_questions": 10}, tmp_path,
                              language="en", maths=True)
    assert client.calls == ["ladder", "facts"], \
        "a round of nothing but naming questions does not buy a second round"
    sheet, key = docx_text(paths[0]), docx_text(paths[1])
    for good in ("A triangle has ____ sides.", "A regular hexagon has 6 lines of symmetry.", "heptagon"):
        assert good in sheet
    for bad in ("An octagon has ____ sides.", "A nonagon has 7 sides.", "A shape with 4 sides",
                "does not have", "cylinder"):
        assert bad not in sheet, f"an unproved item was printed: {bad!r}"
    assert "Identify the number of sides" not in sheet
    assert "equilateral" in key and "pentagon" in key and "False" in key
    assert "table of mathematical facts" in key
    assert "computer algebra" not in key, "no question in this key was checked by algebra"
    qs = json.loads((tmp_path / "questions.json").read_text())["questions"]
    kinds = [q["type"] for q in qs]
    assert kinds.count("fill_blank") == 3 and kinds.count("true_false") == 2 and kinds.count("match") == 1
    assert next(q for q in qs if q["prompt"] == "A hexagon has 8 sides.")["answer"] is False


def test_computational_questions_come_first_and_facts_fill_the_rest(tmp_path):
    client = GeometryClient([Q_PERIMETER, Q_TRIANGLE], GOOD_FACTS)
    paths = generate_document("worksheet", BOOK, CHAPTER, {}, client, {"num_questions": 4}, tmp_path,
                              language="en", maths=True)
    sheet, key = docx_text(paths[0]), docx_text(paths[1])
    assert "A regular pentagon has sides of 4 cm" in sheet and "20" in key
    assert "computer algebra" in key and "table of mathematical facts" in key
    qs = json.loads((tmp_path / "questions.json").read_text())["questions"]
    assert len(qs) == 4, "the fact items fill the shortfall, not more"


def test_a_geometry_test_paper_counts_the_fact_marks(tmp_path):
    client = GeometryClient([Q_TRIANGLE], GOOD_FACTS)
    paths = generate_document("exam_paper", BOOK, CHAPTER, {}, client,
                              {"objective": {"fill_blank": 3, "true_false": 2, "match_column": 1}, "subjective": 0},
                              tmp_path, language="en", maths=True)
    sheet = docx_text(paths[0])
    # 3 blanks + 2 statements at 1 mark, the match at 1 per pair (3)
    assert "[1 mark]" in sheet and "[3 marks]" in sheet and "Total: 8" in sheet


def test_a_non_english_geometry_worksheet_still_fails_rather_than_print_unchecked_facts(tmp_path):
    client = GeometryClient([Q_TRIANGLE, Q_PENTAGON], GOOD_FACTS)
    with pytest.raises(RuntimeError) as err:
        generate_document("worksheet", BOOK, CHAPTER, {}, client, {"num_questions": 6}, tmp_path,
                          language="ms", maths=True)
    assert "English only" in str(err.value) and "not a quantity" in str(err.value)
    assert "facts" not in client.calls, "no model call is spent on items that could not be checked"
