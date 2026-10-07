"""A geometry chapter's worksheet prints verified DIAGRAMS: the model
describes each as a construction, the engine builds, proves and draws it,
and a question it refuses never reaches the page.

The client is a stub that answers each call by the schema it is asked for
(the ladder, the geometry set, the facts), with no network. The geometry
reply is in the closed REPLY shape (lists for binds and answer maps, every
value a string) that maths.geometry.items normalises to the spec.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from docx import Document

from docgen import generate_document
from maths.geometry.items import GEOMETRY_SET_SCHEMA, geometry_prompt, normalise_question
from maths.geometry.verify import verify_question
from maths.questions import FACT_SET_SCHEMA, SET_SCHEMA
from shared.coverage import docx_text

BOOK = {"title": "Cambridge Primary Mathematics Learner's Book 5", "grade": "Stage 5", "subject": "Mathematics"}
CHAPTER = {"title": "2D shape and pattern", "sections": []}

# the reply shape: a closed schema makes a model emit every field, empties included
Q_SCALENE = {
    "id": "q1", "difficulty": 1, "figure_role": "evidence", "prompt": "Which of these triangles are scalene?",
    "figures": [
        {"id": "fig_a", "label": "A", "figure": {"units": "cm", "orientation": 0, "bind": [], "points": [],
                                                  "objects": [{"make": "triangle_sss", "id": "t", "sides": ["4", "5", "6"], "vertices": [], "angle": ""}],
                                                  "angles": [], "segments": [], "measures": [], "relations": [], "marks": []}},
        {"id": "fig_b", "label": "B", "figure": {"units": "cm", "orientation": 30, "bind": [], "points": [],
                                                  "objects": [{"make": "triangle_isosceles", "id": "t", "legs": "5", "base": "3"}],
                                                  "angles": [], "segments": [], "measures": [], "relations": [], "marks": []}},
        {"id": "fig_c", "label": "C", "figure": {"units": "cm", "objects": [{"make": "triangle_equilateral", "id": "t", "side": "4"}]}}],
    "asks": {"property": "triangle_class_by_sides", "over": ["fig_a", "fig_b", "fig_c"], "select": "scalene", "fill": ""},
    "parts": [], "steps": [],
    "answer": {"kind": "label_set", "value": "", "labels": ["A"], "values": [], "map": []},
}
Q_STRAIGHT_LINE = {
    "id": "q2", "difficulty": 2, "figure_role": "reasoning", "prompt": "ABC is a straight line. Find x.",
    "figures": [{"id": "fig", "label": "", "figure": {
        "points": [{"id": "p_a", "label": "A"}, {"id": "p_b", "label": "B"}, {"id": "p_c", "label": "C"}, {"id": "p_d", "label": "D"}],
        "objects": [{"id": "l_abc", "make": "line_through", "points": ["p_a", "p_b", "p_c"]},
                    {"id": "r_bd", "make": "ray_at_angle", "vertex": "p_b", "from_ray": ["p_b", "p_a"], "angle": "angle_abd", "side": "left", "to": "p_d"}],
        "angles": [{"id": "angle_abd", "rays": [["p_b", "p_a"], ["p_b", "p_d"]], "region": "interior"},
                   {"id": "angle_dbc", "rays": [["p_b", "p_d"], ["p_b", "p_c"]], "region": "interior"}],
        "measures": [{"target": "angle_abd", "value": "70", "unit": "deg", "role": "given"},
                     {"target": "angle_dbc", "value": "x", "unit": "deg", "role": "unknown"}],
        "relations": [{"id": "r1", "kind": "collinear", "given": True, "points": ["p_a", "p_b", "p_c"]}],
        "bind": [], "marks": []}}],
    "steps": [{"kind": "deduce", "theorem": "angles_on_line", "uses": ["r1", "angle_abd", "angle_dbc"], "before": [], "after": ["70 + x = 180"], "speech": "", "figure_ops": []},
              {"kind": "transform", "theorem": "", "uses": [], "before": [], "after": ["x = 110"], "speech": "", "figure_ops": []}],
    "answer": {"kind": "number", "value": "110", "unit": "deg", "values": [], "labels": [], "map": []},
}
Q_WITH_BIND = {
    "id": "q3", "difficulty": 3, "figure_role": "reasoning", "prompt": "Find x.",
    "figures": [{"id": "fig", "figure": {"bind": [{"name": "x", "value": "30"}],
        "points": [{"id": "p_o", "label": "O"}, {"id": "p_a", "label": "A"}, {"id": "p_b", "label": "B"}, {"id": "p_c", "label": "C"}, {"id": "p_d", "label": "D"}],
        "objects": [{"id": "fan", "make": "angles_at_point", "vertex": "p_o", "points": ["p_a", "p_b", "p_c", "p_d"]}],
        "angles": [{"id": "angle_aob", "rays": [["p_o", "p_a"], ["p_o", "p_b"]]}, {"id": "angle_boc", "rays": [["p_o", "p_b"], ["p_o", "p_c"]]},
                   {"id": "angle_cod", "rays": [["p_o", "p_c"], ["p_o", "p_d"]]}, {"id": "angle_doa", "rays": [["p_o", "p_d"], ["p_o", "p_a"]]}],
        "measures": [{"target": "angle_aob", "value": "90", "role": "given"}, {"target": "angle_boc", "value": "2x", "role": "given"},
                     {"target": "angle_cod", "value": "3x", "role": "given"}, {"target": "angle_doa", "value": "120", "role": "given"}]}}],
    "steps": [{"kind": "deduce", "theorem": "angles_at_point", "uses": ["angle_aob", "angle_boc", "angle_cod", "angle_doa"], "after": ["90 + 2x + 3x + 120 = 360"]},
              {"kind": "transform", "after": ["5x = 150"]}, {"kind": "transform", "after": ["x = 30"]}],
    "answer": {"kind": "number", "value": "30"},
}
# the model's slips: an impossible triangle, and a claimed answer the figures contradict
Q_IMPOSSIBLE = {
    "id": "q4", "difficulty": 1, "figure_role": "evidence", "prompt": "Which triangle is obtuse?",
    "figures": [{"id": "fig_a", "label": "A", "figure": {"objects": [{"make": "triangle_asa", "id": "t", "angles": ["100", "90"], "side": "5"}]}}],
    "asks": {"property": "triangle_class_by_angles", "over": ["fig_a"], "select": "obtuse"},
    "answer": {"kind": "label_set", "labels": ["A"]},
}
Q_WRONG_ANSWER = copy.deepcopy(Q_SCALENE)
Q_WRONG_ANSWER["id"] = "q5"
Q_WRONG_ANSWER["answer"]["labels"] = ["A", "B"]


class Client:
    model = "stub"

    def __init__(self, ladder, geometry, facts):
        self.ladder, self.geometry, self.facts = ladder, geometry, facts
        self.calls: list[str] = []
        self.prompts: dict[str, str] = {}

    def analyze(self, prompt, system="", max_tokens=0, retries=3, cache_prefix=None, response_schema=None, **kw):
        if response_schema is GEOMETRY_SET_SCHEMA:
            self.calls.append("geometry")
            self.prompts["geometry"] = prompt
            # constrained decoding is a switch (off by default: measured to make
            # gemini-3.5-flash omit construction parameters); the call names it
            assert "strict_schema" in kw
            return {"data": {"questions": copy.deepcopy(self.geometry)}, "usage": {}, "truncated": False}
        if response_schema is FACT_SET_SCHEMA:
            self.calls.append("facts")
            return {"data": {"items": copy.deepcopy(self.facts)}, "usage": {}, "truncated": False}
        assert response_schema is SET_SCHEMA
        self.calls.append("ladder")
        return {"data": {"questions": copy.deepcopy(self.ladder)}, "usage": {}, "truncated": False}


def _pictures(path: Path) -> int:
    return len(Document(str(path)).inline_shapes)


# ── normalisation ─────────────────────────────────────────────────────────────

def test_the_reply_shape_normalises_to_the_spec_and_verifies():
    spec, difficulty = normalise_question(copy.deepcopy(Q_SCALENE))
    assert difficulty == 1 and spec["schema_version"] == "geometry.figure.v1"
    assert spec["answer"] == {"kind": "label_set", "value": ["A"]}
    assert "bind" not in spec["figures"][0]["figure"] and "vertices" not in spec["figures"][0]["figure"]["objects"][0]
    assert verify_question(spec).ok
    spec, _ = normalise_question(copy.deepcopy(Q_WITH_BIND))
    assert spec["figures"][0]["figure"]["bind"] == {"x": "30"}
    rep = verify_question(spec)
    assert rep.ok and str(rep.proved["x"]) == "30"


def test_the_prompt_names_only_what_exists():
    text = geometry_prompt(topic="Angles", level="Stage 8", language="en", n=4, chapter_context="", kind="worksheet")
    for must in ("parallels_transversal", "alternate_angles", "triangle_class_by_sides", "never takes a coordinate",
                 "figure_role 'evidence'", "figure_role 'reasoning'"):
        assert must in text
    assert "triangle_ssa" not in text


# ── the worksheet ─────────────────────────────────────────────────────────────

def test_a_geometry_worksheet_prints_verified_diagrams_and_refuses_the_rest(tmp_path):
    client = Client([], [Q_SCALENE, Q_IMPOSSIBLE, Q_STRAIGHT_LINE, Q_WRONG_ANSWER, Q_WITH_BIND], [])
    paths = generate_document("worksheet", BOOK, CHAPTER, {}, client, {"num_questions": 6}, tmp_path,
                              language="en", maths=True)
    # the ladder's two rounds (an empty reply buys a second), the geometry
    # call plus its repair round (two of five were refused and the set is
    # short), then the facts fill what is left
    assert client.calls.count("geometry") == 2 and client.calls[-3:] == ["geometry", "geometry", "facts"]
    sheet, key = docx_text(paths[0]), docx_text(paths[1])
    assert "Diagrams" in sheet
    assert "Which of these triangles are scalene?" in sheet and "ABC is a straight line. Find x." in sheet
    assert "Which triangle is obtuse?" not in sheet, "an impossible figure was printed"
    # three labelled triangles + the straight line + the fan = 5 pictures on the sheet
    assert _pictures(paths[0]) == 5
    assert _pictures(paths[1]) == 0, "the key is text"
    assert "Answer: A" in key, "the evidence answer the engine computed"
    assert "70 + x = 180   (angles on a straight line add up to 180°)" in key
    assert "Answer: x = 110°" in key and "Answer: x = 30" in key
    assert "geometry engine" in key
    # the wrong-answer twin of q1 is refused, not printed twice
    assert sheet.count("Which of these triangles are scalene?") == 1
    qs = json.loads((tmp_path / "questions.json").read_text(encoding="utf-8"))["questions"]
    assert qs == [], "figure questions stay out of the quiz player (no picture in its schema)"


def test_the_diagram_is_printed_at_true_size_for_an_evidence_question(tmp_path):
    client = Client([], [Q_SCALENE], [])
    paths = generate_document("worksheet", BOOK, CHAPTER, {}, client, {"num_questions": 1}, tmp_path,
                              language="en", maths=True)
    doc = Document(str(paths[0]))
    widths_mm = sorted(round(s.width / 36000, 1) for s in doc.inline_shapes)
    # the 4-5-6 triangle is 4 cm wide plus 2 × 3 mm margins = 46 mm; the
    # 3-5-5 isosceles rotated 30° and the 4 cm equilateral come out as the
    # renderer measured them — all under the column, none shrunk
    assert 46.0 in widths_mm and max(widths_mm) < 170.0


def test_a_geometry_test_paper_carries_marks(tmp_path):
    client = Client([], [Q_SCALENE, Q_STRAIGHT_LINE], [])
    paths = generate_document("exam_paper", BOOK, CHAPTER, {}, client,
                              {"objective": {"fill_blank": 2}, "subjective": 2}, tmp_path, language="en", maths=True)
    sheet, key = docx_text(paths[0]), docx_text(paths[1])
    assert "[2 marks]" in sheet and "[3 marks]" in sheet and "Total: 5" in sheet
    assert "method" in key.lower()


def test_a_non_english_paper_draws_the_note_in_its_language(tmp_path):
    q = copy.deepcopy(Q_STRAIGHT_LINE)
    q["prompt"] = "ABC es una línea recta. Halla x."
    client = Client([], [q], [])
    paths = generate_document("worksheet", BOOK, CHAPTER, {}, client, {"num_questions": 1}, tmp_path,
                              language="es", maths=True)
    sheet, key = docx_text(paths[0]), docx_text(paths[1])
    assert "Figuras" in sheet and "Halla x" in sheet
    assert "motor de geometría" in key
    assert "angles on a straight line" not in key, "English reasons are not printed on a Spanish key"
    assert _pictures(paths[0]) == 1


def test_nothing_verified_fails_loudly_with_the_geometry_reasons(tmp_path):
    client = Client([], [Q_IMPOSSIBLE], [])
    with pytest.raises(RuntimeError) as err:
        generate_document("worksheet", BOOK, CHAPTER, {}, client, {"num_questions": 2}, tmp_path,
                          language="en", maths=True)
    assert "construction_impossible" in str(err.value)
