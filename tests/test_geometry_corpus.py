"""geometry.figure.v1 against its golden corpus — thirty hand-written
questions from Cambridge Primary 5 Unit 2, Lower Secondary 8 Unit 5 and
five deliberate failures (Edtech/geometry-corpus-v1.md).

Every item says what the engine must do with it: verify, or refuse with a
named code. A refusal is the designed outcome for what v1 cannot express
(reflection, tessellation, a curved boundary) and for what is wrong (an
impossible triangle, a claimed relation the construction does not make, a
theorem that does not apply). Verified figures must also RENDER under the
policy their role calls for, with every label placed.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from maths.geometry import GeometryRefusal, parse_question, realise, verify_question
from maths.geometry.render_static import render_figure

CORPUS = json.loads((Path(__file__).parent / "fixtures" / "geometry_corpus_v1.json").read_text(encoding="utf-8"))
ITEMS = {item["question"]["id"]: item for item in CORPUS["items"]}


def _policy_for(role: str) -> str:
    return "assessment_schematic" if role == "reasoning" else "instructional_metric"


@pytest.mark.parametrize("qid", sorted(ITEMS))
def test_corpus_item(qid):
    item = ITEMS[qid]
    rep = verify_question(item["question"])
    want = item["expect"]
    assert rep.status == want["status"], f"{qid}: {rep.refusal} / {[c.detail for c in rep.checks[-2:]]}"
    if want["status"] == "failed":
        assert rep.refusal["code"] == want["code"], f"{qid}: refused with {rep.refusal}"
        return
    # every verified figure draws under its role's policy, labels placed
    q = parse_question(item["question"])
    for ref in q.figures:
        policy = _policy_for(q.figure_role)
        m = realise(ref.figure, policy, metric=rep.models[ref.id])
        r = render_figure(m, ref.figure, role=q.figure_role, policy=policy)
        assert r.png and r.svg.startswith("<svg")
        if q.figure_role == "evidence" and ref.figure.units == "cm":
            assert r.true_scale and r.scale_mm == 10.0


def test_corpus_counts():
    verified = [i for i in CORPUS["items"] if i["expect"]["status"] == "verified"]
    refused = [i for i in CORPUS["items"] if i["expect"]["status"] == "failed"]
    assert len(verified) >= 20 and len(refused) >= 7
    assert {i["expect"]["code"] for i in refused} >= {
        "unsupported_feature", "construction_impossible", "relation_not_implied", "theorem_premise",
        "theorem_unknown", "unknown_construction", "bad_schema"}


# ── the proof is a proof: break it and the engine says so ─────────────────

def _b1():
    return copy.deepcopy(ITEMS["B1"]["question"])


def test_wrong_theorem_is_refused_by_its_premise():
    q = _b1()
    q["steps"][0]["theorem"] = "vertically_opposite"
    rep = verify_question(q)
    assert rep.refusal["code"] == "theorem_premise"


def test_a_line_the_theorem_does_not_give_is_refused():
    q = _b1()
    q["steps"][0]["after"] = ["70 + x = 360"]
    assert verify_question(q).refusal["code"] == "step_not_equivalent"


def test_a_wrong_answer_is_refused():
    q = _b1()
    q["answer"]["value"] = 100
    assert verify_question(q).refusal["code"] == "answer_mismatch"


def test_a_wrong_bind_fails_closure_before_the_proof():
    q = copy.deepcopy(ITEMS["B2"]["question"])
    q["figure"]["bind"]["x"] = 31
    assert verify_question(q).refusal["code"] == "closure_failed"


def test_a_bind_that_closes_but_is_not_the_answer_is_caught():
    q = copy.deepcopy(ITEMS["B7"]["question"])
    # x = 25 is the answer; drawing with x = 25 but claiming 20 mismatches the proof
    q["answer"]["value"] = 20
    assert verify_question(q).refusal["code"] == "answer_mismatch"


def test_the_realisation_is_a_check_not_the_proof():
    """B1 with the construction's own 110° fact: the proof must still cite
    the theorem — a chain with no deduce step proves nothing."""
    q = _b1()
    q["steps"] = [{"kind": "transform", "before": ["x = 110"], "after": ["x = 110"]}]
    rep = verify_question(q)
    assert rep.status == "verified" or rep.refusal["code"] in ("answer_unproved", "step_unverifiable")
    # a transform from thin air is not accepted as establishing x
    assert "angles_on_line" not in rep.reasons_given


def test_reasons_come_from_theorem_ids():
    rep = verify_question(ITEMS["B11"]["question"])
    assert rep.ok
    assert rep.reasons_given == [
        "base angles of an isosceles triangle are equal",
        "angles in a triangle add up to 180°",
        "angles on a straight line add up to 180°",
    ]


# ── evidence figures ──────────────────────────────────────────────────────

def test_evidence_answer_is_computed_not_trusted():
    q = copy.deepcopy(ITEMS["P1"]["question"])
    q["answer"]["value"] = ["A", "B"]
    assert verify_question(q).refusal["code"] == "answer_mismatch"


def test_sides_a_ruler_cannot_tell_apart_are_refused():
    q = copy.deepcopy(ITEMS["P1"]["question"])
    q["figures"][0]["figure"]["objects"][0]["sides"] = [5, 5.3, 6]
    assert verify_question(q).refusal["code"] == "not_discernible"


def test_a_mark_that_answers_the_question_is_refused():
    q = copy.deepcopy(ITEMS["P4"]["question"])
    q["figure"]["angles"] = [{"id": "angle_c0", "rays": [["v1", "v6"], ["v1", "v2"]]}]
    q["figure"]["marks"] = [{"kind": "right_angle_square", "target": "angle_c0"}]
    assert verify_question(q).refusal["code"] == "mark_gives_away"


def test_grid_part_with_no_colour_is_an_impossible_question():
    q = copy.deepcopy(ITEMS["P8"]["question"])
    q["parts"].append({"asks": {"property": "lines_of_symmetry", "over": ["grid"], "fill": "*", "equals": 2},
                       "answer": {"kind": "value_set", "value": ["G"]}})
    rep = verify_question(q)
    assert rep.refusal["code"] == "answer_mismatch"
    assert rep.computed["grid:lines_of_symmetry:by_colour"] == {"R": 4, "Y": 1, "G": 1, "B": 1}


# ── render policies ───────────────────────────────────────────────────────

def test_schematic_hides_the_unknown_and_keeps_the_straight_line():
    q = parse_question(ITEMS["B1"]["question"])
    fig = q.figures[0].figure
    metric = realise(fig, "instructional_metric")
    drawn = realise(fig, "assessment_schematic", metric=metric)
    true = metric.angle_float(metric.angle_ids["angle_dbc"])
    shown = drawn.angle_float(drawn.angle_ids["angle_dbc"])
    assert abs(true - 110.0) < 1e-6
    assert abs(shown - true) >= 8.0
    # the straight line survives: A, B, C stay collinear, and the two
    # drawn angles still make 180°
    a, b, c = (drawn.xy(p) for p in ("p_a", "p_b", "p_c"))
    cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    assert abs(cross) < 1e-9
    assert abs(drawn.angle_float(drawn.angle_ids["angle_abd"]) + shown - 180.0) < 1e-6


def test_schematic_is_deterministic():
    q = parse_question(ITEMS["B9"]["question"])
    fig = q.figures[0].figure
    a = realise(fig, "assessment_schematic")
    b = realise(fig, "assessment_schematic")
    assert {k: (p.x, p.y) for k, p in a.points.items()} == {k: (p.x, p.y) for k, p in b.points.items()}


def test_evidence_prints_at_true_scale():
    q = parse_question(ITEMS["P1"]["question"])
    ref = q.figures[0]
    m = realise(ref.figure, "instructional_metric")
    r = render_figure(m, ref.figure, role="evidence")
    assert r.true_scale and r.scale_mm == 10.0
    # the 4-5-6 triangle is built on its 4 cm side: 40 mm plus the margins
    from maths.geometry.render_static import MARGIN_MM
    assert abs(r.width_mm - (40.0 + 2 * MARGIN_MM)) < 0.5


def test_unknown_schema_version_is_refused():
    q = _b1()
    q["schema_version"] = "geometry.figure.v9"   # v2 is a version now
    assert verify_question(q).refusal["code"] == "bad_schema"


def _blank_uses(theorem: str):
    """Corpus questions citing ``theorem`` with that step's `uses` emptied —
    what the live lesson call sent (2026-10-07)."""
    import copy

    out = []
    for item in CORPUS["items"]:
        q = copy.deepcopy(item["question"])
        hit = False
        for st in q.get("steps") or []:
            if st.get("theorem") == theorem and st.get("uses"):
                st["uses"] = []
                hit = True
        if hit:
            out.append((item["question"]["id"], item["question"], q))
    return out


EQUILATERAL_X = {
    "schema_version": "geometry.figure.v1", "id": "eq", "figure_role": "reasoning", "prompt": "Find x.",
    "figures": [{"id": "f", "figure": {
        "points": [{"id": "p_a", "label": "A"}, {"id": "p_b", "label": "B"}, {"id": "p_c", "label": "C"}],
        "objects": [{"id": "t", "make": "triangle_equilateral", "vertices": ["p_a", "p_b", "p_c"], "side": "4"}],
        "angles": [{"id": "angle_abc", "rays": [["p_b", "p_a"], ["p_b", "p_c"]]}],
        "measures": [{"target": "angle_abc", "value": "x", "unit": "deg", "role": "unknown"}]}}],
    "steps": [{"kind": "deduce", "theorem": "equilateral_angles", "uses": [], "after": ["ang(abc) = 60"]},
              {"kind": "transform", "after": ["x = 60"]}],
    "answer": {"kind": "number", "value": "60", "unit": "deg"},
}


def test_an_equilateral_step_that_cites_nothing_uses_the_angle_its_line_names():
    from maths.geometry import verify_question

    rep = verify_question(EQUILATERAL_X)
    assert rep.ok, rep.refusal
    assert str(rep.proved.get("x")) == "60"


def test_an_angle_named_by_its_points_is_read():
    import copy

    from maths.geometry import verify_question
    from maths.geometry.items import normalise_question

    q = copy.deepcopy(EQUILATERAL_X)
    q["steps"][0]["after"] = ["ang(p_a,p_b,p_c) = 60"]
    spec, _d = normalise_question(q)
    assert spec["steps"][0]["after"] == ["ang(abc) = 60"]
    assert verify_question(spec).ok


@pytest.mark.parametrize("theorem", ["isosceles_base_angles"])
def test_a_deduce_step_that_cites_nothing_takes_the_figures_only_triangle(theorem):
    from maths.geometry import verify_question

    cases = _blank_uses(theorem)
    assert cases, f"the corpus has a {theorem} question"
    for qid, original, blanked in cases:
        want = verify_question(original)
        got = verify_question(blanked)
        if len([pg for m in want.models.values() for pg in m.polygons.values()
                if pg.closed and len(pg.vertices) == 3]) != 1:
            assert not got.ok, f"{qid}: two triangles stay ambiguous"
            continue
        assert got.ok, (qid, got.refusal)
        assert got.proved == want.proved, qid


def test_with_two_triangles_the_angles_a_step_names_say_which_one():
    from maths.geometry import verify_question

    q = {"schema_version": "geometry.figure.v1", "id": "two", "figure_role": "reasoning",
         "prompt": "Find x.",
         "figures": [{"id": "f", "figure": {
             "points": [{"id": "p_a"}, {"id": "p_b"}, {"id": "p_c"}, {"id": "p_d"}, {"id": "p_e"}, {"id": "p_f"}],
             "objects": [{"id": "t1", "make": "triangle_isosceles", "vertices": ["p_a", "p_b", "p_c"], "legs": "5", "apex_angle": "40"},
                         {"id": "t2", "make": "triangle_equilateral", "vertices": ["p_d", "p_e", "p_f"], "side": "3"}],
             "angles": [{"id": "angle_abc", "rays": [["p_b", "p_a"], ["p_b", "p_c"]]},
                        {"id": "angle_acb", "rays": [["p_c", "p_a"], ["p_c", "p_b"]]},
                        {"id": "angle_bac", "rays": [["p_a", "p_b"], ["p_a", "p_c"]]}],
             "measures": [{"target": "angle_bac", "value": "40", "unit": "deg", "role": "given"},
                          {"target": "angle_abc", "value": "x", "unit": "deg", "role": "unknown"}]}}],
         "steps": [{"kind": "deduce", "theorem": "isosceles_base_angles", "uses": [], "after": ["ang(abc) = ang(acb)"]},
                   {"kind": "deduce", "theorem": "triangle_angle_sum", "uses": ["angle_bac", "angle_abc", "angle_acb"],
                    "after": ["40 + 2*ang(acb) = 180"]},
                   {"kind": "transform", "after": ["ang(acb) = 70"]}],
         "answer": {"kind": "number", "value": "70", "unit": "deg"}}
    rep = verify_question(q)
    # `uses` is empty, yet ang(abc) = ang(acb) names the base angles of t1:
    # the equation is the citation, the second triangle is no ambiguity
    assert rep.ok, rep.refusal
    assert str(rep.proved.get("x")) == "70"


def _area_q(objects, segments, measures, steps, answer, extra_pts=()):
    pts = [{"id": pid, "label": pid[-1].upper()} for pid in ("p_a", "p_b", "p_c", "p_d") + tuple(extra_pts)]
    return {"schema_version": "geometry.figure.v1", "id": "area", "figure_role": "reasoning", "prompt": "Find the area.",
            "figures": [{"id": "f", "figure": {"units": "cm", "points": pts, "objects": objects, "segments": segments,
                                               "measures": measures}}],
            "steps": steps, "answer": {"kind": "number", "value": str(answer), "unit": "cm"}}


def test_a_rectangles_area_is_the_questions_unknown():
    from maths.geometry import verify_question

    q = _area_q([{"id": "r", "make": "rectangle", "vertices": ["p_a", "p_b", "p_c", "p_d"], "width": "6", "height": "4"}],
                [{"id": "s_ab", "points": ["p_a", "p_b"]}, {"id": "s_bc", "points": ["p_b", "p_c"]}],
                [{"target": "s_ab", "value": "6", "unit": "cm"}, {"target": "s_bc", "value": "4", "unit": "cm"}],
                [{"kind": "deduce", "theorem": "area_rectangle", "uses": ["r"], "after": ["A = 6 * 4"]},
                 {"kind": "transform", "after": ["A = 24"]}], 24)
    rep = verify_question(q)
    assert rep.ok, rep.refusal
    assert str(rep.proved.get("A")) == "24"


def test_a_parallelograms_area_from_two_sides_and_an_exact_angle():
    from maths.geometry import verify_question

    q = _area_q([{"id": "pg", "make": "parallelogram", "vertices": ["p_a", "p_b", "p_c", "p_d"], "sides": ["8", "5"], "angle": "30"}],
                [{"id": "s_ab", "points": ["p_a", "p_b"]}, {"id": "s_bc", "points": ["p_b", "p_c"]}],
                [{"target": "s_ab", "value": "8", "unit": "cm"}, {"target": "s_bc", "value": "5", "unit": "cm"}],
                [{"kind": "deduce", "theorem": "area_parallelogram", "uses": ["pg"], "after": ["A = 8 * 5 * 1/2"]},
                 {"kind": "transform", "after": ["A = 20"]}], 20)
    rep = verify_question(q)
    assert rep.ok, rep.refusal
    assert str(rep.proved.get("A")) == "20"


def test_a_parallelograms_area_from_a_dropped_height():
    from maths.geometry import verify_question

    q = _area_q([{"id": "pg", "make": "parallelogram", "vertices": ["p_a", "p_b", "p_c", "p_d"], "sides": ["8", "5"], "angle": "30"},
                 {"id": "h", "make": "perpendicular_from", "point": "p_d", "segment": ["p_a", "p_b"], "to": "p_h"}],
                [{"id": "s_ab", "points": ["p_a", "p_b"]}, {"id": "s_dh", "points": ["p_d", "p_h"]}],
                [{"target": "s_ab", "value": "8", "unit": "cm"}, {"target": "s_dh", "value": "2.5", "unit": "cm"}],
                [{"kind": "deduce", "theorem": "area_parallelogram", "uses": ["pg", "s_dh"], "after": ["A = 8 * 2.5"]},
                 {"kind": "transform", "after": ["A = 20"]}], 20, extra_pts=("p_h",))
    rep = verify_question(q)
    assert rep.ok, rep.refusal
    assert float(rep.proved.get("A")) == 20.0
    # a wrong height is refused by the figure, not by the formula
    q["figures"][0]["figure"]["measures"][1]["value"] = "3"
    bad = verify_question(q)
    assert not bad.ok and (bad.refusal or {}).get("code") == "given_not_realised", bad.refusal


def test_a_trapeziums_area_needs_its_height_built_and_cited():
    from maths.geometry import verify_question

    objects = [{"id": "tz", "make": "trapezium", "vertices": ["p_a", "p_b", "p_c", "p_d"], "parallel_sides": ["8", "5"],
                "height": "4", "offset": "1"},
               {"id": "h", "make": "perpendicular_from", "point": "p_d", "segment": ["p_a", "p_b"], "to": "p_h"}]
    segments = [{"id": "s_ab", "points": ["p_a", "p_b"]}, {"id": "s_dc", "points": ["p_d", "p_c"]},
                {"id": "s_dh", "points": ["p_d", "p_h"]}]
    measures = [{"target": "s_ab", "value": "8", "unit": "cm"}, {"target": "s_dc", "value": "5", "unit": "cm"},
                {"target": "s_dh", "value": "4", "unit": "cm"}]
    q = _area_q(objects, segments, measures,
                [{"kind": "deduce", "theorem": "area_trapezium", "uses": ["tz", "s_dh"], "after": ["A = 1/2 * (8 + 5) * 4"]},
                 {"kind": "transform", "after": ["A = 26"]}], 26, extra_pts=("p_h",))
    rep = verify_question(q)
    assert rep.ok, rep.refusal
    assert str(rep.proved.get("A")) == "26"
    # without the height cited the theorem refuses, naming what to build
    q2 = _area_q(objects, segments, measures,
                 [{"kind": "deduce", "theorem": "area_trapezium", "uses": ["tz"], "after": ["A = 1/2 * (8 + 5) * 4"]},
                  {"kind": "transform", "after": ["A = 26"]}], 26, extra_pts=("p_h",))
    bad = verify_question(q2)
    assert not bad.ok and "perpendicular_from" in (bad.refusal or {}).get("message", ""), bad.refusal


def test_the_dropped_height_draws_and_renders():
    from maths.geometry import parse_question, realise, verify_question
    from maths.geometry.render_static import render_figure

    objects = [{"id": "tz", "make": "trapezium", "vertices": ["p_a", "p_b", "p_c", "p_d"], "parallel_sides": ["8", "5"],
                "height": "4", "offset": "1"},
               {"id": "h", "make": "perpendicular_from", "point": "p_d", "segment": ["p_a", "p_b"], "to": "p_h"}]
    q = _area_q(objects, [{"id": "s_dh", "points": ["p_d", "p_h"]}], [{"target": "s_dh", "value": "4", "unit": "cm"}],
                [{"kind": "deduce", "theorem": "area_trapezium", "uses": ["tz", "s_dh"], "after": ["A = 1/2 * (8 + 5) * 4"]}],
                26, extra_pts=("p_h",))
    q["figures"][0]["figure"]["segments"] += [{"id": "s_ab", "points": ["p_a", "p_b"]}, {"id": "s_dc", "points": ["p_d", "p_c"]}]
    q["figures"][0]["figure"]["measures"] += [{"target": "s_ab", "value": "8", "unit": "cm"}, {"target": "s_dc", "value": "5", "unit": "cm"}]
    rep = verify_question(q)
    assert rep.ok, rep.refusal
    spec = parse_question(q).figures[0].figure
    m = rep.models["f"]
    assert m.has_point("p_h") and "p_h" in m.line_through("p_a", "p_b").points
    out = render_figure(m, spec, role="reasoning", policy="instructional_metric")
    assert out.png and len(out.png) > 1000
    sch = realise(spec, "assessment_schematic", metric=m)
    assert sch.has_point("p_h")


def _l_shape_q(after: list[str], answer="68"):
    """An L-shaped garden: 10 by 8 with a 4 by 3 corner taken out of the
    top right, walked as one turtle_polygon (right angles only)."""
    pts = [{"id": f"p_{c}", "label": c.upper()} for c in "abcdef"]
    sides = ["10", "5", "4", "3", "6", "8"]
    segs = [{"id": f"s_{i}", "points": [f"p_{'abcdef'[i]}", f"p_{'abcdef'[(i + 1) % 6]}"]} for i in range(6)]
    return {"schema_version": "geometry.figure.v1", "id": "L", "figure_role": "reasoning",
            "prompt": "Find the area of the garden.",
            "figures": [{"id": "f", "figure": {"units": "m", "points": pts,
                                               "objects": [{"id": "L", "make": "turtle_polygon",
                                                            "vertices": [p["id"] for p in pts],
                                                            "sides": sides, "turns": ["90", "90", "-90", "90", "90", "90"]}],
                                               "segments": segs,
                                               "measures": [{"target": f"s_{i}", "value": sides[i], "unit": "m"} for i in range(6)]}}],
            "steps": [{"kind": "deduce", "theorem": "area_composite", "uses": ["L"], "after": after},
                      {"kind": "transform", "after": [f"A = {answer}"]}],
            "answer": {"kind": "number", "value": answer, "unit": "m"}}


def test_a_compound_shapes_area_is_the_sum_of_its_parts():
    """area_composite was refused in v1; a turtle_polygon's exact corners
    give the exact area, and the step's own decomposition is checked."""
    from maths.geometry import verify_question

    rep = verify_question(_l_shape_q(["A = 10 * 8 - 4 * 3"]))        # a rectangle less the corner
    assert rep.ok, rep.refusal
    assert str(rep.proved.get("A")) == "68"
    rep = verify_question(_l_shape_q(["A = 6 * 8 + 4 * 5"]))         # two rectangles
    assert rep.ok, rep.refusal
    assert "sum of its parts" in " ".join(rep.reasons_given)


def test_a_wrong_decomposition_of_a_compound_shape_is_refused():
    from maths.geometry import verify_question

    rep = verify_question(_l_shape_q(["A = 10 * 8"], answer="80"))   # the corner was never taken out
    assert not rep.ok and rep.refusal["code"] == "step_not_equivalent", rep.refusal


def test_a_compound_shape_not_walked_exactly_is_refused_with_the_fix():
    from maths.geometry import verify_question

    q = _area_q([{"id": "r", "make": "rectangle", "vertices": ["p_a", "p_b", "p_c", "p_d"], "width": "6", "height": "4"}],
                [{"id": "s_ab", "points": ["p_a", "p_b"]}, {"id": "s_bc", "points": ["p_b", "p_c"]}],
                [{"target": "s_ab", "value": "6", "unit": "cm"}, {"target": "s_bc", "value": "4", "unit": "cm"}],
                [{"kind": "deduce", "theorem": "area_composite", "uses": ["r"], "after": ["A = 6 * 4"]},
                 {"kind": "transform", "after": ["A = 24"]}], 24)
    rep = verify_question(q)
    assert not rep.ok and rep.refusal["code"] == "theorem_premise" and "turtle_polygon" in rep.refusal["message"], rep.refusal
