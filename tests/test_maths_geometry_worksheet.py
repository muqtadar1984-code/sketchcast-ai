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
    assert difficulty == 1 and spec["schema_version"] == "geometry.figure.v3"   # the current version (v2 is a superset of v1)
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
    # figures FIRST: the share of six is three, the stub's three verified
    # questions fill it in one call (no repair round), then the ladder's
    # two rounds (an empty reply buys a second), then the facts fill the rest
    assert client.calls[0] == "geometry" and client.calls.count("geometry") == 1
    assert client.calls[-1] == "facts" and "ladder" in client.calls
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
    figure_qs = [q for q in qs if q.get("image")]
    assert figure_qs, "figure questions reach the quiz player, each with its picture"
    assert all(q["image"].startswith("data:image/png;base64,") for q in figure_qs)
    assert {q["type"] for q in figure_qs} <= {"fill_blank", "true_false", "match"}
    # find x -> fill_blank on the number the engine proved
    find_x = [q for q in figure_qs if q["type"] == "fill_blank" and "straight line" in q["prompt"]]
    assert find_x and find_x[0]["answer"] == "110" and "number only" in find_x[0]["prompt"]
    # "which of these triangles are scalene?" -> one true/false per figure, A true
    tf = [q for q in figure_qs if q["type"] == "true_false" and "scalene" in q["prompt"]]
    assert [q["prompt"].rsplit(" — ", 1)[1] for q in tf] == ["A", "B", "C"] and [q["answer"] for q in tf] == [True, False, False]
    assert len({q["image"] for q in tf}) == 1, "the three figures share one labelled picture"
    assert [q["id"] for q in qs] == [f"q{i}" for i in range(1, len(qs) + 1)]


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


def test_the_coverage_gate_counts_what_the_diagrams_show(tmp_path):
    """A sheet of drawn triangles teaches scalene, isosceles and
    equilateral triangles; the gate reads text, so the builder writes the
    figures' transcript beside the document and the gate reads both."""
    from shared import coverage

    client = Client([], [Q_SCALENE, Q_STRAIGHT_LINE], [])
    paths = generate_document("worksheet", BOOK, CHAPTER, {}, client, {"num_questions": 4}, tmp_path,
                              language="en", maths=True)
    sidecar = tmp_path / coverage.FIGURE_TRANSCRIPT
    assert sidecar.exists()
    transcript = sidecar.read_text(encoding="utf-8")
    for word in ("scalene triangle", "isosceles triangle", "equilateral triangle", "lines of symmetry",
                 "Which of these triangles are scalene?", "angle abd = 70°", "straight line"):
        assert word in transcript, transcript
    assert "Answer: A" in transcript and "x = 110" in transcript
    # the document's own text says none of the class names...
    plain = coverage.docx_text(paths[0])
    assert "isosceles" not in plain.lower() and "equilateral" not in plain.lower()
    # ...the gate's text does, and the topics come out addressed
    full = coverage.document_text(paths[0])
    assert "isosceles" in full.lower()
    # the analyzer's own shape (shared.coverage.chapter_topics reads it)
    analysis = {"concepts": {"concepts": [{"name": n, "concept_id": f"c{i}"} for i, n in enumerate((
        "Equilateral Triangle", "Isosceles Triangle", "Scalene Triangle", "Lines of symmetry",
        "Angles on a straight line", "Tessellation"))]}}
    before = coverage.measure(analysis, None, plain)
    after = coverage.measure(analysis, None, full)
    # the sheet's own words reach "scalene" and "straight line"; the figures
    # B and C are what teach isosceles and equilateral, and only the
    # transcript says so
    assert {"Isosceles Triangle", "Equilateral Triangle"} <= set(before["missed"])
    assert after["addressed"] > before["addressed"]
    assert not {"Isosceles Triangle", "Equilateral Triangle"} & set(after["missed"])
    assert "Tessellation" in after["missed"], "a concept the figures do not show stays missed"


def test_nothing_verified_fails_loudly_with_the_geometry_reasons(tmp_path):
    client = Client([], [Q_IMPOSSIBLE], [])
    with pytest.raises(RuntimeError) as err:
        generate_document("worksheet", BOOK, CHAPTER, {}, client, {"num_questions": 2}, tmp_path,
                          language="en", maths=True)
    assert "construction_impossible" in str(err.value)


def test_an_algebra_worksheet_makes_no_figure_call(tmp_path):
    # the same gate as the lesson's: a chapter named by no geometry is never
    # asked for diagrams, and the ladder (plus the facts) fills the sheet
    client = Client([], [Q_SCALENE, Q_STRAIGHT_LINE], [])
    chapter = {"title": "Variables, Constants and Algebraic Expressions",
               "sections": [{"section_title": "Translating Words into Algebra", "section_type": "body",
                             "content": "The perimeter of a rectangle with sides l and w is 2l + 2w."}]}
    analysis = {"concepts": {"concepts": [{"name": "Variable", "concept_id": "c1"},
                                          {"name": "Algebraic expression", "concept_id": "c2"}]}}
    try:
        paths = generate_document("worksheet", BOOK, chapter, analysis, client, {"num_questions": 4}, tmp_path,
                                  language="en", maths=True)
    except RuntimeError as err:
        # the stub ladder may verify nothing — the point is the call that was never made
        assert "figure questions" not in str(err)
        paths = []
    assert "geometry" not in client.calls, client.calls
    if paths:
        assert "Which of these triangles are scalene?" not in docx_text(paths[0])


def test_an_area_unit_written_as_a_square_folds_to_the_length_unit():
    # "Area of Trapeziums and Compound Shapes" (2026-10-14): 4 of 4 figures refused on `unit: cm^2`
    from maths.geometry.items import _unit, normalise_question
    assert _unit("cm^2") == "cm" and _unit("cm²") == "cm" and _unit("sq cm") == "cm" and _unit("square metres") == "m"
    assert _unit("m^3") == "m" and _unit("cubic cm") == "cm" and _unit("mm") == "mm" and _unit("degrees") == "deg"
    assert _unit("square units") == "units" and _unit("furlongs") is None and _unit("") is None
    spec, _d = normalise_question({"figures": [{"id": "f", "figure": {
        "objects": [{"id": "r", "make": "rectangle", "vertices": ["p_a", "p_b", "p_c", "p_d"], "width": "6", "height": "4"}],
        "measures": [{"target": "r", "value": "24", "unit": "cm^2", "kind": "area", "role": "given"},
                     {"target": "s_ab", "value": "6", "unit": "furlongs"}]}}]})
    ms = spec["figures"][0]["figure"]["measures"]
    assert ms[0]["unit"] == "cm" and "unit" not in ms[1]


def test_a_shape_written_the_way_the_model_writes_it_is_read_not_refused():
    # the trapeziums kit (2026-10-14): parallel_sides as the side NAMES, the numbers in the measures;
    # an "id" on every measure; the area given as a measure of the shape with no kind
    from maths.geometry import verify_question
    from maths.geometry.items import normalise_question
    raw = {"id": "t", "figure_role": "reasoning", "prompt": "Find the height of the trapezium.", "difficulty": 2,
           "figures": [{"id": "f", "figure": {"units": "cm",
               "points": [{"id": "p_a", "label": "A"}, {"id": "p_b", "label": "B"}, {"id": "p_c", "label": "C"},
                          {"id": "p_d", "label": "D"}, {"id": "p_h", "label": "H"}],
               "objects": [{"id": "tz", "make": "trapezium", "vertices": ["p_a", "p_b", "p_c", "p_d"],
                            "parallel_sides": ["AB", "DC"], "height": "h", "offset": "2"},
                           {"id": "hh", "make": "perpendicular_from", "point": "p_d", "segment": ["p_a", "p_b"], "to": "p_h"}],
               "segments": [{"id": "s_ab", "points": ["p_a", "p_b"]}, {"id": "s_dc", "points": ["p_d", "p_c"]},
                            {"id": "s_dh", "points": ["p_d", "p_h"]}],
               "measures": [{"id": "m1", "target": "s_ab", "value": "14", "unit": "cm", "role": "given"},
                            {"id": "m2", "target": "s_dc", "value": "10", "unit": "cm", "role": "given"},
                            {"id": "m3", "target": "s_dh", "value": "h", "unit": "cm", "role": "unknown"},
                            {"id": "m4", "target": "tz", "value": "72", "unit": "cm^2"}],
               "bind": {"h": 6}}}],
           "steps": [{"kind": "deduce", "theorem": "area_trapezium", "uses": ["tz", "s_dh"], "after": ["72 = 1/2 * (14 + 10) * h"]},
                     {"kind": "transform", "after": ["h = 6"]}],
           "answer": {"kind": "number", "value": "6", "unit": "cm"}}
    spec, _d = normalise_question(raw)
    fig = spec["figures"][0]["figure"]
    assert fig["objects"][0]["parallel_sides"] == ["14", "10"]
    assert all("id" not in ms for ms in fig["measures"])
    assert fig["measures"][3] == {"target": "tz", "value": "72", "unit": "cm", "kind": "area", "role": "given"}
    rep = verify_question(spec)
    assert rep.ok, rep.refusal
    assert str(rep.proved.get("h")) == "6"
    # a comma string, and a name with no given length left alone for the engine's own message
    raw2 = {"figures": [{"id": "f", "figure": {"objects": [{"id": "k", "make": "kite", "vertices": ["p_a", "p_b", "p_c", "p_d"],
                                                              "sides": "5, 7", "angle": "100"}]}}]}
    assert normalise_question(raw2)[0]["figures"][0]["figure"]["objects"][0]["sides"] == ["5", "7"]
    raw3 = {"figures": [{"id": "f", "figure": {"objects": [{"id": "tz", "make": "trapezium", "vertices": ["p_a", "p_b", "p_c", "p_d"],
                                                              "parallel_sides": ["AB", "DC"], "height": "4"}]}}]}
    assert normalise_question(raw3)[0]["figures"][0]["figure"]["objects"][0]["parallel_sides"] == ["AB", "DC"]


def test_the_models_extra_keys_and_an_unknown_area_measure_are_set_aside():
    # live trial 2026-10-14 after the first fix: "segments.2.style: Extra inputs are not permitted" and
    # "measure of 'trap': an area is given; the unknown is a side" (the model wrote the area asked for as a measure)
    from maths.geometry.items import normalise_question
    raw = {"figures": [{"id": "f", "figure": {
        "points": [{"id": "p_a", "label": "A", "note": "top"}],
        "segments": [{"id": "s_ab", "points": ["p_a", "p_b"], "style": "dashed"}],
        "angles": [{"id": "angle_abc", "rays": [["p_b", "p_a"], ["p_b", "p_c"]], "mark": "arc"}],
        "objects": [{"id": "tz", "make": "trapezium", "vertices": ["p_a", "p_b", "p_c", "p_d"], "parallel_sides": ["8", "5"], "height": "4"}],
        "measures": [{"target": "tz", "value": "A", "role": "unknown"}, {"target": "s_ab", "value": "8", "unit": "cm"}]}}]}
    fig = normalise_question(raw)[0]["figures"][0]["figure"]
    assert fig["points"] == [{"id": "p_a", "label": "A"}] and fig["segments"] == [{"id": "s_ab", "points": ["p_a", "p_b"]}]
    assert "mark" not in fig["angles"][0]
    assert [ms["target"] for ms in fig["measures"]] == ["s_ab"], "the unknown area measure is dropped; the theorem supplies A"


def test_the_question_text_is_read_under_the_key_the_model_chose():
    from maths.geometry.items import normalise_question
    for key in ("question", "text", "stem", "problem"):
        spec, _d = normalise_question({key: "Find the area.", "figures": []})
        assert spec["prompt"] == "Find the area.", key
    assert normalise_question({"prompt": "P", "question": "Q", "figures": []})[0]["prompt"] == "P"


def test_a_question_without_a_sentence_gets_one_from_its_figure():
    # the trapeziums video (2026-10-14): two examples with an empty problem line, the
    # fifth figure refused as the try-it for the same reason
    from maths.geometry.items import normalise_question
    fig = {"units": "cm",
           "points": [{"id": "p_a", "label": "A"}, {"id": "p_b", "label": "B"}, {"id": "p_c", "label": "C"},
                      {"id": "p_d", "label": "D"}, {"id": "p_h", "label": "H"}],
           "objects": [{"id": "trap", "make": "trapezium", "vertices": ["p_a", "p_b", "p_c", "p_d"],
                        "parallel_sides": ["10", "14"], "height": "h", "offset": "2"},
                       {"id": "hh", "make": "perpendicular_from", "point": "p_a", "segment": ["p_d", "p_c"], "to": "p_h"}],
           "segments": [{"id": "s_ab", "points": ["p_a", "p_b"]}, {"id": "s_dc", "points": ["p_d", "p_c"]},
                        {"id": "s_ah", "points": ["p_a", "p_h"]}],
           "measures": [{"target": "s_ab", "value": "10", "unit": "cm", "role": "given"},
                        {"target": "s_dc", "value": "14", "unit": "cm", "role": "given"},
                        {"target": "s_ah", "value": "h", "unit": "cm", "role": "unknown"},
                        {"target": "trap", "value": "72", "unit": "cm^2"}],
           "bind": {"h": 6}}
    raw = {"id": "g2", "figure_role": "reasoning", "figures": [{"id": "f", "figure": fig}],
           "steps": [{"kind": "deduce", "theorem": "area_trapezium", "uses": ["trap", "s_ah"], "after": ["72 = 1/2 * (10 + 14) * h"]},
                     {"kind": "transform", "after": ["h = 6"]}],
           "answer": {"kind": "number", "value": "6", "unit": "cm"}}
    spec, _d = normalise_question(raw)
    assert spec["prompt"] == "The area of the trapezium ABCD is 72 cm². Find h."
    # an area to find: the theorem names it
    import copy
    raw2 = copy.deepcopy(raw)
    raw2["figures"][0]["figure"]["measures"] = [{"target": "s_ab", "value": "8", "unit": "cm", "role": "given"},
                                                 {"target": "s_dc", "value": "12", "unit": "cm", "role": "given"},
                                                 {"target": "s_ah", "value": "5", "unit": "cm", "role": "given"}]
    raw2["figures"][0]["figure"]["objects"][0].update({"parallel_sides": ["8", "12"], "height": "5"})
    raw2["figures"][0]["figure"].pop("bind")
    raw2["steps"] = [{"kind": "deduce", "theorem": "area_trapezium", "uses": ["trap", "s_ah"], "after": ["A = 1/2 * (8 + 12) * 5"]},
                     {"kind": "transform", "after": ["A = 50"]}]
    raw2["answer"] = {"kind": "number", "value": "50", "unit": "cm"}
    assert normalise_question(raw2)[0]["prompt"] == "Find the area of the trapezium ABCD."
    # the model's own sentence is never replaced; an evidence question is left alone
    raw3 = copy.deepcopy(raw); raw3["prompt"] = "Work out h."
    assert normalise_question(raw3)[0]["prompt"] == "Work out h."
    raw4 = {"id": "e", "figure_role": "evidence", "figures": [{"id": "f", "figure": fig}],
            "asks": {"property": "polygon_name", "over": ["f"]}, "answer": {"kind": "selection", "value": ["f"]}}
    assert normalise_question(raw4)[0]["prompt"] == ""


def test_a_trapezium_written_with_a_stray_area_key_and_an_unplaced_foot_is_read():
    # live trial 2026-10-14: the area under a figure-level "target" key, the foot p_e listed with
    # its height segment but no perpendicular construction
    from maths.geometry import verify_question
    from maths.geometry.items import normalise_question
    raw = {"id": "q", "figure_role": "reasoning", "prompt": "The area of the trapezium is 72 cm². Find h.",
           "figures": [{"id": "f", "figure": {"units": "cm",
               "points": [{"id": "p_a", "label": "A"}, {"id": "p_b", "label": "B"}, {"id": "p_c", "label": "C"},
                          {"id": "p_d", "label": "D"}, {"id": "p_e", "label": "E"}],
               "objects": [{"id": "trap", "make": "trapezium", "vertices": ["p_a", "p_b", "p_c", "p_d"],
                            "parallel_sides": ["10", "14"], "height": "6", "offset": "2"}],
               "segments": [{"id": "s_ab", "points": ["p_a", "p_b"]}, {"id": "s_dc", "points": ["p_d", "p_c"]},
                            {"id": "s_ae", "points": ["p_a", "p_e"]}],
               "measures": [{"target": "s_ab", "value": "10", "unit": "cm", "role": "given"},
                            {"target": "s_dc", "value": "14", "unit": "cm", "role": "given"},
                            {"target": "p_e", "value": "h", "unit": "cm", "role": "unknown"}],
               "target": {"target": "trap", "value": "72", "unit": "cm", "kind": "area", "role": "given"}}}],
           "steps": [{"kind": "deduce", "theorem": "area_trapezium", "uses": ["trap", "s_ae"], "after": ["72 = 1/2 * (10 + 14) * h"]},
                     {"kind": "transform", "after": ["h = 6"]}],
           "answer": {"kind": "number", "value": "6", "unit": "cm"}}
    spec, _d = normalise_question(raw)
    fig = spec["figures"][0]["figure"]
    assert "target" not in fig
    assert {"target": "trap", "value": "72", "unit": "cm", "kind": "area", "role": "given"} in fig["measures"]
    foot = [o for o in fig["objects"] if o["make"] == "perpendicular_from"]
    assert foot == [{"id": "h_p_e", "make": "perpendicular_from", "point": "p_a", "segment": ["p_d", "p_c"], "to": "p_e"}]
    assert [m for m in fig["measures"] if m["value"] == "h"][0]["target"] == "s_ae"
    rep = verify_question(spec)
    assert rep.ok, rep.refusal
    assert str(rep.proved.get("h")) == "6"


def test_a_trapeziums_bases_written_the_other_way_round_follow_the_given_ab():
    from maths.geometry.items import normalise_question
    fig = {"units": "cm",
           "points": [{"id": "p_a", "label": "A"}, {"id": "p_b", "label": "B"}, {"id": "p_c", "label": "C"}, {"id": "p_d", "label": "D"}],
           "objects": [{"id": "trap", "make": "trapezium", "vertices": ["p_a", "p_b", "p_c", "p_d"],
                        "parallel_sides": ["8", "12"], "height": "5", "offset": "2"}],
           "segments": [{"id": "s_ab", "points": ["p_a", "p_b"]}, {"id": "s_dc", "points": ["p_d", "p_c"]}],
           "measures": [{"target": "s_ab", "value": "12", "unit": "cm", "role": "given"},
                        {"target": "s_dc", "value": "8", "unit": "cm", "role": "given"}]}
    spec, _d = normalise_question({"figures": [{"id": "f", "figure": fig}]})
    assert spec["figures"][0]["figure"]["objects"][0]["parallel_sides"] == ["12", "8"]
    # consistent already: untouched
    fig["measures"][0]["value"], fig["measures"][1]["value"] = "8", "12"
    fig["objects"][0]["parallel_sides"] = ["8", "12"]
    spec, _d = normalise_question({"figures": [{"id": "f", "figure": fig}]})
    assert spec["figures"][0]["figure"]["objects"][0]["parallel_sides"] == ["8", "12"]


def test_a_turtle_polygon_whose_turn_signs_do_not_close_is_re_signed():
    # live trials 2026-10-14: every L-shape came as a staircase — the magnitudes right, the signs wrong
    import math
    from maths.geometry import verify_question
    from maths.geometry.items import normalise_question

    def walk(sides, turns):
        pts, heading = [(0.0, 0.0)], 0.0
        for s, t in zip(sides, turns):
            x, y = pts[-1]
            pts.append((x + s * math.cos(math.radians(heading)), y + s * math.sin(math.radians(heading))))
            heading += t
        return pts

    sides = ["10", "4", "4", "6", "6", "10"]
    bad = ["-90", "90", "-90", "90", "-90", "-90"]
    raw = {"figures": [{"id": "f", "figure": {"units": "cm", "points": [{"id": f"p_{c}", "label": c.upper()} for c in "abcdef"],
                                                "objects": [{"id": "shape", "make": "turtle_polygon", "vertices": [f"p_{c}" for c in "abcdef"],
                                                             "sides": sides, "turns": bad}]}}]}
    spec, _d = normalise_question(raw)
    turns = [float(t) for t in spec["figures"][0]["figure"]["objects"][0]["turns"]]
    pts = walk([float(s) for s in sides], turns)
    assert math.dist(pts[-1], pts[0]) < 1e-6 and abs(abs(sum(turns)) - 360) < 1e-6
    assert [abs(t) for t in turns] == [90.0] * 6
    # the shape the engine now draws has the area the shoelace gives
    xs, ys = zip(*pts[:-1])
    area = abs(sum(xs[i] * ys[(i + 1) % 6] - xs[(i + 1) % 6] * ys[i] for i in range(6))) / 2
    q = {"id": "q", "figure_role": "reasoning", "prompt": "Find the area of the L-shaped floor plan.", "figures": raw["figures"],
         "steps": [{"kind": "deduce", "theorem": "area_composite", "uses": ["shape"], "after": [f"A = {int(area)}"]}],
         "answer": {"kind": "number", "value": str(int(area)), "unit": "cm"}}
    rep = verify_question(normalise_question(q)[0])
    assert rep.ok, rep.refusal
    # a walk the side lengths cannot close is left for the engine's own refusal
    raw["figures"][0]["figure"]["objects"][0]["sides"] = ["10", "4", "4", "6", "6", "3"]
    raw["figures"][0]["figure"]["objects"][0]["turns"] = list(bad)
    assert normalise_question(raw)[0]["figures"][0]["figure"]["objects"][0]["turns"] == bad


def test_an_unnamed_compound_shape_takes_the_name_its_measure_and_steps_use():
    # live trial 2026-10-14: the turtle polygon had no id; the measure and the step called it "shape"
    from maths.geometry import verify_question
    from maths.geometry.items import normalise_question
    raw = {"id": "q", "figure_role": "reasoning", "prompt": "Find the area of the L-shaped floor plan.",
           "figures": [{"id": "f", "figure": {"units": "cm", "points": [{"id": f"p_{c}", "label": c.upper()} for c in "abcdef"],
                                                "objects": [{"make": "turtle_polygon", "sides": ["8", "4", "4", "2", "4", "6"],
                                                             "turns": ["-90", "90", "-90", "90", "-90", "90"]}],
                                                "measures": [{"target": "shape", "value": "A", "role": "unknown"}]}}],
           "steps": [{"kind": "deduce", "theorem": "area_composite", "uses": ["shape"], "after": ["A = 8 * 6 - 4 * 2"]},
                     {"kind": "transform", "after": ["A = 40"]}],
           "answer": {"kind": "number", "value": "40", "unit": "cm"}}
    spec, _d = normalise_question(raw)
    fig = spec["figures"][0]["figure"]
    assert fig["objects"][0]["id"] == "shape" and fig.get("measures", []) == []     # the unknown area is the theorem's
    rep = verify_question(spec)
    assert rep.ok, rep.refusal
    assert str(rep.proved.get("A")) == "40"


def test_a_flat_sort_asked_with_the_solids_property_sorts_polygons():
    from maths.geometry.items import normalise_question
    raw = {"id": "e", "figure_role": "evidence", "prompt": "Which of these shapes are trapezia?",
           "figures": [{"id": "a", "figure": {"objects": [{"id": "t", "make": "trapezium", "vertices": ["p_a", "p_b", "p_c", "p_d"],
                                                              "parallel_sides": ["6", "10"], "height": "4", "offset": "1"}]}},
                       {"id": "b", "figure": {"objects": [{"id": "r", "make": "rectangle", "vertices": ["p_a", "p_b", "p_c", "p_d"],
                                                              "width": "6", "height": "4"}]}}],
           "asks": {"property": "solid_name", "over": ["a", "b"], "select": "trapezium"},
           "answer": {"kind": "selection", "value": ["a"]}}
    spec, _d = normalise_question(raw)
    assert spec["asks"]["property"] == "polygon_name"
    raw["figures"] = [{"id": "a", "figure": {"objects": [{"id": "c", "make": "cuboid", "length": "6", "width": "4", "height": "3"}]}}]
    assert normalise_question(raw)[0]["asks"]["property"] == "solid_name"


def test_a_listed_point_nothing_refers_to_is_dropped_not_fatal():
    from maths.geometry import verify_question
    from maths.geometry.items import normalise_question
    raw = {"id": "q", "figure_role": "reasoning", "prompt": "Find the area of the trapezium.",
           "figures": [{"id": "f", "figure": {"units": "cm",
               "points": [{"id": f"p_{c}", "label": c.upper()} for c in "abcdef"],
               "objects": [{"id": "s", "make": "trapezium", "vertices": ["p_a", "p_b", "p_c", "p_d"],
                            "parallel_sides": ["8", "12"], "height": "5", "offset": "2"},
                           {"id": "s_h", "make": "perpendicular_from", "point": "p_a", "segment": ["p_d", "p_c"], "to": "p_e"}],
               "segments": [{"id": "s_ab", "points": ["p_a", "p_b"]}, {"id": "s_dc", "points": ["p_d", "p_c"]},
                            {"id": "s_ae", "points": ["p_a", "p_e"]}],
               "measures": [{"target": "s_ab", "value": "8", "unit": "cm", "role": "given"},
                            {"target": "s_dc", "value": "12", "unit": "cm", "role": "given"},
                            {"target": "s_ae", "value": "5", "unit": "cm", "role": "given"},
                            {"target": "s", "value": "A", "role": "unknown"}]}}],
           "steps": [{"kind": "deduce", "theorem": "area_trapezium", "uses": ["s", "s_ae"], "after": ["A = 1/2 * (8 + 12) * 5"]},
                     {"kind": "transform", "after": ["A = 50"]}],
           "answer": {"kind": "number", "value": "50", "unit": "cm"}}
    spec, _d = normalise_question(raw)
    assert [p["id"] for p in spec["figures"][0]["figure"]["points"]] == ["p_a", "p_b", "p_c", "p_d", "p_e"]
    rep = verify_question(spec)
    assert rep.ok, rep.refusal
    # a polygon written without vertices keeps its listed points: they ARE its corners
    raw2 = {"figures": [{"id": "f", "figure": {"points": [{"id": f"p_{c}", "label": c.upper()} for c in "abcd"],
                                                 "objects": [{"id": "r", "make": "turtle_polygon", "sides": ["4", "3", "4", "3"],
                                                              "turns": ["90", "90", "90", "90"]}]}}]}
    assert len(normalise_question(raw2)[0]["figures"][0]["figure"]["points"]) == 4


def test_a_superscript_the_model_wrote_as_mojibake_is_read_back():
    # two boards of the trapeziums video read "54 cmÂ²" (2026-10-15)
    from maths.geometry.items import _demojibake, normalise_question
    assert _demojibake("The area is 54 cmÂ². Find h.") == "The area is 54 cm². Find h."
    assert _demojibake("an angle of 90Â° â€” not 45Â°") == "an angle of 90° — not 45°"
    assert _demojibake("plain 54 cm². Find h.") == "plain 54 cm². Find h."
    spec, _d = normalise_question({"prompt": "Area 72 cmÂ².", "intro_speech": "It is 72 cmÂ²", "figures": [],
                                   "steps": [{"kind": "transform", "after": ["h = 6"], "narration": "so 12 cmÂ² each"}]})
    assert spec["prompt"] == "Area 72 cm²."
    from maths.geometry.items import _speech
    assert _speech({"intro_speech": "It is 72 cmÂ²"})["intro"] == "It is 72 cm²"
