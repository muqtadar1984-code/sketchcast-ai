"""The v4 corpus — rotation, trigonometry, circle theorems, compass arcs
(geometry-v4-proposal.md, 2026-10-09). Golden tests."""

from __future__ import annotations

import copy

import pytest

from maths.geometry import parse_question, verify_question
from maths.geometry.render_static import render_figure

V3 = "geometry.figure.v3"


def _pts(*labels):
    return [{"id": f"p_{lab.lower()}", "label": lab} for lab in labels]


def _axes(x=(-6, 6), y=(-6, 6), step=1):
    return {"make": "axes", "x": list(x), "y": list(y), "step": step}


def _at(lab, x, y):
    return {"make": "point_at", "id": f"p_{lab.lower()}", "x": str(x), "y": str(y)}


def _given(lab, x, y):
    return {"target": f"p_{lab.lower()}", "value": f"({x}, {y})", "role": "given"}


def _ang(aid, v, a, b):
    return {"id": aid, "rays": [[v, a], [v, b]]}


R1 = {"schema_version": V3, "id": "R1", "figure_role": "reasoning",
      "prompt": "A is at (3, 1). It is rotated 90° anticlockwise about the origin to A'. Find the coordinates of A'.",
      "figures": [{"id": "f", "figure": {"points": _pts("A") + [{"id": "p_a1", "label": "A'"}],
                   "objects": [_axes(), _at("A", 3, 1), {"make": "rotate_point", "point": "p_a", "by": "90", "to": "p_a1"}],
                   "measures": [_given("A", 3, 1), {"target": "p_a1", "value": "(a, b)", "role": "unknown"}]}}],
      "steps": [{"kind": "deduce", "theorem": "rotation_rule", "uses": ["p_a1"], "after": ["a = -1", "b = 3"]}],
      "answer": {"kind": "values", "value": {"a": "-1", "b": "3"}}}

R2 = {"schema_version": V3, "id": "R2", "figure_role": "reasoning",
      "prompt": "B is at (-2, 4). Rotate B by 180° about the origin.",
      "figures": [{"id": "f", "figure": {"points": _pts("B") + [{"id": "p_b1", "label": "B'"}],
                   "objects": [_axes(), _at("B", -2, 4), {"make": "rotate_point", "point": "p_b", "by": "180", "to": "p_b1"}],
                   "measures": [_given("B", -2, 4), {"target": "p_b1", "value": "(a, b)", "role": "unknown"}]}}],
      "steps": [{"kind": "deduce", "theorem": "rotation_rule", "uses": ["p_b1"], "after": ["a = 2", "b = -4"]}],
      "answer": {"kind": "values", "value": {"a": "2", "b": "-4"}}}

R3 = {"schema_version": V3, "id": "R3", "figure_role": "reasoning",
      "prompt": "C is at (1, 5). Rotate C by 90° clockwise about the origin.",
      "figures": [{"id": "f", "figure": {"points": _pts("C") + [{"id": "p_c1", "label": "C'"}],
                   "objects": [_axes(), _at("C", 1, 5), {"make": "rotate_point", "point": "p_c", "by": "90", "clockwise": True, "to": "p_c1"}],
                   "measures": [_given("C", 1, 5), {"target": "p_c1", "value": "(a, b)", "role": "unknown"}]}}],
      "steps": [{"kind": "deduce", "theorem": "rotation_rule", "uses": ["p_c1"], "after": ["a = 5", "b = -1"]}],
      "answer": {"kind": "values", "value": {"a": "5", "b": "-1"}}}

T1 = {"schema_version": V3, "id": "T1", "figure_role": "reasoning",
      "prompt": "In right-angled triangle ABC, angle C = 30° and AB = 5 cm. Find the hypotenuse AC.",
      "figures": [{"id": "f", "figure": {"units": "cm", "points": _pts("A", "B", "C"),
                   "objects": [{"make": "triangle_rhs", "id": "t", "vertices": ["p_a", "p_b", "p_c"], "hyp": "10", "side": "5"}],
                   "angles": [_ang("angle_acb", "p_c", "p_a", "p_b")],
                   "segments": [{"id": "s_ab", "points": ["p_a", "p_b"]}, {"id": "s_ac", "points": ["p_a", "p_c"]}],
                   "measures": [{"target": "angle_acb", "value": "30", "unit": "deg"}, {"target": "s_ab", "value": "5", "unit": "cm"},
                                {"target": "s_ac", "value": "h", "unit": "cm", "role": "unknown"}]}}],
      "steps": [{"kind": "deduce", "theorem": "sin_ratio", "uses": ["t", "angle_acb"], "after": ["sin(30) = 5/h"]},
                {"kind": "transform", "after": ["h = 10"]}],
      "answer": {"kind": "number", "value": "10", "unit": "cm"}}

T2 = {"schema_version": V3, "id": "T2", "figure_role": "reasoning",
      "prompt": "In right-angled triangle ABC, AB = 4 cm and BC = 3 cm. Find angle A to two decimal places.",
      "figures": [{"id": "f", "figure": {"units": "cm", "points": _pts("A", "B", "C"),
                   "objects": [{"make": "triangle_rhs", "id": "t", "vertices": ["p_a", "p_b", "p_c"], "legs": ["4", "3"]}],
                   "angles": [_ang("angle_bac", "p_a", "p_b", "p_c")],
                   "segments": [{"id": "s_ab", "points": ["p_a", "p_b"]}, {"id": "s_bc", "points": ["p_b", "p_c"]}],
                   "measures": [{"target": "s_ab", "value": "4", "unit": "cm"}, {"target": "s_bc", "value": "3", "unit": "cm"},
                                {"target": "angle_bac", "value": "x", "unit": "deg", "role": "unknown"}]}}],
      "steps": [{"kind": "deduce", "theorem": "tan_ratio", "uses": ["t", "angle_bac"], "after": ["tan(x) = 3/4"]},
                {"kind": "transform", "after": ["x = atan(3/4)"]}],
      "answer": {"kind": "number", "value": "36.87", "unit": "deg"}}

T3 = {"schema_version": V3, "id": "T3", "figure_role": "reasoning",
      "prompt": "In right-angled triangle ABC the hypotenuse AC = 10 cm and angle A = 40°. Find AB to two decimal places.",
      "figures": [{"id": "f", "figure": {"units": "cm", "points": _pts("A", "B", "C"), "bind": {"o": "10*sin(40)"},
                   "objects": [{"make": "triangle_aas", "id": "t", "vertices": ["p_a", "p_b", "p_c"], "angles": ["40", "90"], "side": "o"}],
                   "angles": [_ang("angle_bac", "p_a", "p_b", "p_c")],
                   "segments": [{"id": "s_ab", "points": ["p_a", "p_b"]}, {"id": "s_ac", "points": ["p_a", "p_c"]}],
                   "measures": [{"target": "s_ac", "value": "10", "unit": "cm"}, {"target": "angle_bac", "value": "40", "unit": "deg"},
                                {"target": "s_ab", "value": "a", "unit": "cm", "role": "unknown"}]}}],
      "steps": [{"kind": "deduce", "theorem": "cos_ratio", "uses": ["t", "angle_bac"], "after": ["cos(40) = a/10"]},
                {"kind": "transform", "after": ["a = 10 * cos(40)"]}],
      "answer": {"kind": "number", "value": "7.66", "unit": "cm"}}


def _circle_fig(points, objects, angles, segments, measures, units="cm"):
    return {"id": "f", "figure": {"units": units, "points": points, "objects": objects, "angles": angles,
                                  "segments": segments, "measures": measures}}


K1 = {"schema_version": V3, "id": "K1", "figure_role": "reasoning",
      "prompt": "O is the centre of the circle. Angle AOB = 140°. Find angle ACB.",
      "figures": [_circle_fig(_pts("O", "A", "B", "C"),
                              [{"make": "circle", "id": "c", "centre": "p_o", "radius": "5"},
                               {"make": "point_on_circle", "id": "p_a", "circle": "c", "angle": "200"},
                               {"make": "point_on_circle", "id": "p_b", "circle": "c", "angle": "340"},
                               {"make": "point_on_circle", "id": "p_c", "circle": "c", "angle": "90"}],
                              [_ang("angle_aob", "p_o", "p_a", "p_b"), _ang("angle_acb", "p_c", "p_a", "p_b")],
                              [{"id": "s_ac", "points": ["p_a", "p_c"]}, {"id": "s_bc", "points": ["p_b", "p_c"]},
                               {"id": "s_oa", "points": ["p_o", "p_a"]}, {"id": "s_ob", "points": ["p_o", "p_b"]}],
                              [{"target": "angle_aob", "value": "140", "unit": "deg"},
                               {"target": "angle_acb", "value": "x", "unit": "deg", "role": "unknown"}])],
      "steps": [{"kind": "deduce", "theorem": "angle_at_centre", "uses": ["c", "angle_aob", "angle_acb"], "after": ["140 = 2 * x"]},
                {"kind": "transform", "after": ["x = 70"]}],
      "answer": {"kind": "number", "value": "70", "unit": "deg"}}

K2 = {"schema_version": V3, "id": "K2", "figure_role": "reasoning",
      "prompt": "AB is a diameter of the circle and C lies on the circle. Angle CAB = 30°. Find angles ACB and ABC.",
      "figures": [_circle_fig(_pts("O", "A", "B", "C"),
                              [{"make": "circle", "id": "c", "centre": "p_o", "radius": "4"},
                               {"make": "diameter", "points": ["p_a", "p_b"], "circle": "c", "angle": "180"},
                               {"make": "point_on_circle", "id": "p_c", "circle": "c", "angle": "60"},
                               {"make": "polygon", "id": "t", "vertices": ["p_a", "p_b", "p_c"]}],
                              [_ang("angle_acb", "p_c", "p_a", "p_b"), _ang("angle_cab", "p_a", "p_c", "p_b"), _ang("angle_abc", "p_b", "p_a", "p_c")],
                              [],
                              [{"target": "angle_acb", "value": "x", "unit": "deg", "role": "unknown"},
                               {"target": "angle_cab", "value": "30", "unit": "deg"},
                               {"target": "angle_abc", "value": "y", "unit": "deg", "role": "unknown"}])],
      "steps": [{"kind": "deduce", "theorem": "angle_in_semicircle", "uses": ["c", "angle_acb"], "after": ["x = 90"]},
                {"kind": "deduce", "theorem": "triangle_angle_sum", "uses": ["angle_acb", "angle_cab", "angle_abc"], "after": ["90 + 30 + y = 180"]},
                {"kind": "transform", "after": ["y = 60"]}],
      "answer": {"kind": "values", "value": {"x": "90", "y": "60"}}}

K3 = {"schema_version": V3, "id": "K3", "figure_role": "reasoning",
      "prompt": "ABCD is a cyclic quadrilateral. Angle DAB = 105°. Find angle BCD.",
      "figures": [_circle_fig(_pts("A", "B", "C", "D"),
                              [{"make": "circle", "id": "c", "centre": "p_o", "radius": "5"},
                               {"make": "point_on_circle", "id": "p_a", "circle": "c", "angle": "90"},
                               {"make": "point_on_circle", "id": "p_b", "circle": "c", "angle": "180"},
                               {"make": "point_on_circle", "id": "p_c", "circle": "c", "angle": "300"},
                               {"make": "point_on_circle", "id": "p_d", "circle": "c", "angle": "30"},
                               {"make": "polygon", "id": "q", "vertices": ["p_a", "p_b", "p_c", "p_d"]}],
                              [_ang("angle_dab", "p_a", "p_d", "p_b"), _ang("angle_bcd", "p_c", "p_b", "p_d")],
                              [],
                              [{"target": "angle_dab", "value": "105", "unit": "deg"},
                               {"target": "angle_bcd", "value": "x", "unit": "deg", "role": "unknown"}])],
      "steps": [{"kind": "deduce", "theorem": "cyclic_quadrilateral", "uses": ["c", "q", "angle_dab", "angle_bcd"], "after": ["105 + x = 180"]},
                {"kind": "transform", "after": ["x = 75"]}],
      "answer": {"kind": "number", "value": "75", "unit": "deg"}}

K4 = {"schema_version": V3, "id": "K4", "figure_role": "reasoning",
      "prompt": "PT is a tangent to the circle at P and O is the centre. Find angle OPT.",
      "figures": [_circle_fig(_pts("O", "P", "T"),
                              [{"make": "circle", "id": "c", "centre": "p_o", "radius": "4"},
                               {"make": "point_on_circle", "id": "p_p", "circle": "c", "angle": "90"},
                               {"make": "tangent_at", "id": "s_pt", "circle": "c", "point": "p_p", "length": "6", "side": "right", "to": "p_t"}],
                              [_ang("angle_opt", "p_p", "p_o", "p_t")],
                              [],
                              [{"target": "angle_opt", "value": "x", "unit": "deg", "role": "unknown"}])],
      "steps": [{"kind": "deduce", "theorem": "tangent_radius", "uses": ["c", "angle_opt"], "after": ["x = 90"]}],
      "answer": {"kind": "number", "value": "90", "unit": "deg"}}

K5 = {"schema_version": V3, "id": "K5", "figure_role": "reasoning",
      "prompt": "A, B, C and D lie on the circle. Angle ACB = 40°. Find angle ADB.",
      "figures": [_circle_fig(_pts("A", "B", "C", "D"),
                              [{"make": "circle", "id": "c", "centre": "p_o", "radius": "8"},
                               {"make": "point_on_circle", "id": "p_a", "circle": "c", "angle": "230"},
                               {"make": "point_on_circle", "id": "p_b", "circle": "c", "angle": "310"},
                               {"make": "point_on_circle", "id": "p_c", "circle": "c", "angle": "60"},
                               {"make": "point_on_circle", "id": "p_d", "circle": "c", "angle": "150"}],
                              [_ang("angle_acb", "p_c", "p_a", "p_b"), _ang("angle_adb", "p_d", "p_a", "p_b")],
                              [{"id": "s_ac", "points": ["p_a", "p_c"]}, {"id": "s_bc", "points": ["p_b", "p_c"]},
                               {"id": "s_ad", "points": ["p_a", "p_d"]}, {"id": "s_bd", "points": ["p_b", "p_d"]}],
                              [{"target": "angle_acb", "value": "40", "unit": "deg"},
                               {"target": "angle_adb", "value": "x", "unit": "deg", "role": "unknown"}])],
      "steps": [{"kind": "deduce", "theorem": "angles_same_segment", "uses": ["c", "angle_acb", "angle_adb"], "after": ["x = 40"]}],
      "answer": {"kind": "number", "value": "40", "unit": "deg"}}

C1 = {"schema_version": V3, "id": "C1", "figure_role": "illustration",
      "prompt": "Construct the perpendicular bisector of AB with compasses.",
      "figures": [{"id": "f", "figure": {"units": "cm", "points": _pts("A", "B"),
                   "objects": [{"make": "segment", "points": ["p_a", "p_b"], "length": "8"},
                               {"make": "perpendicular_bisector", "id": "bis", "segment": ["p_a", "p_b"], "hidden": True}],
                   "marks": [{"id": "arc_a", "kind": "construction_arc", "centre": "p_a", "through": "p_b", "hidden": True},
                             {"id": "arc_b", "kind": "construction_arc", "centre": "p_b", "through": "p_a", "hidden": True}]}}]}

CORPUS = {q["id"]: q for q in (R1, R2, R3, T1, T2, T3, K1, K2, K3, K4, K5, C1)}
EXPECT = {"R1": {"a": "-1", "b": "3"}, "R2": {"a": "2", "b": "-4"}, "R3": {"a": "5", "b": "-1"}, "T1": {"h": "10"},
          "K1": {"x": "70"}, "K2": {"x": "90", "y": "60"}, "K3": {"x": "75"}, "K4": {"x": "90"}, "K5": {"x": "40"}}


@pytest.mark.parametrize("qid", sorted(CORPUS))
def test_the_v4_corpus_verifies(qid):
    rep = verify_question(copy.deepcopy(CORPUS[qid]))
    assert rep.ok, (qid, rep.refusal)
    if qid in EXPECT:
        assert {k: str(v) for k, v in rep.proved.items()} == EXPECT[qid], qid


@pytest.mark.parametrize("qid", sorted(CORPUS))
def test_every_v4_figure_draws(qid):
    q = parse_question(copy.deepcopy(CORPUS[qid]))
    rep = verify_question(copy.deepcopy(CORPUS[qid]))
    for ref in q.figures:
        r = render_figure(rep.models[ref.id], ref.figure, role=q.figure_role, policy="assessment_schematic")
        assert len(r.png) > 2000 and r.svg.startswith("<svg")


def test_a_rounded_answer_is_accepted_only_as_the_correct_rounding():
    import sympy as sp
    rep = verify_question(copy.deepcopy(T2))
    assert rep.ok, rep.refusal
    assert abs(float(sp.N(rep.proved["x"])) - 36.8699) < 1e-3
    q = copy.deepcopy(T2)
    q["answer"]["value"] = "36.9"                       # one decimal: still the correct rounding
    assert verify_question(q).ok
    q["answer"]["value"] = "36.88"
    assert verify_question(q).refusal["code"] == "answer_mismatch"
    rep = verify_question(copy.deepcopy(T3))
    assert rep.ok, rep.refusal
    assert abs(float(sp.N(rep.proved["a"])) - 7.6604) < 1e-3


def test_refusals_rotation_about_another_centre_and_wrong_circle_citations():
    q = copy.deepcopy(R1)
    q["figures"][0]["figure"]["objects"][2]["about"] = "(1, 1)"
    assert verify_question(q).refusal["code"] == "unsupported_feature"
    q = copy.deepcopy(R1)
    q["figures"][0]["figure"]["objects"][2]["by"] = "45"
    assert verify_question(q).refusal["code"] == "bad_schema"
    q = copy.deepcopy(K1)
    q["figures"][0]["figure"]["objects"][3]["angle"] = "250"      # C moved onto the minor arc: still on the circle,
    q["figures"][0]["figure"]["measures"][0]["value"] = "140"     # but then angle ACB is not half of 140
    assert verify_question(q).refusal["code"] in ("answer_mismatch", "given_not_realised")
    q = copy.deepcopy(K2)
    q["figures"][0]["figure"]["objects"][1] = {"make": "point_on_circle", "id": "p_a", "circle": "c", "angle": "180"}
    q["figures"][0]["figure"]["objects"].insert(2, {"make": "point_on_circle", "id": "p_b", "circle": "c", "angle": "20"})
    assert verify_question(q).refusal["code"] in ("theorem_premise", "given_not_realised", "answer_mismatch")
    q = copy.deepcopy(K3)
    q["steps"][0]["uses"] = ["c", "q", "angle_dab", "angle_dab"]
    assert verify_question(q).refusal["code"] == "theorem_premise"
    q = copy.deepcopy(T1)
    q["steps"][0]["theorem"] = "tan_ratio"
    assert verify_question(q).refusal["code"] in ("step_not_equivalent", "step_unverifiable")


def test_the_reasons_of_the_v4_theorems_exist_in_every_language():
    from maths.geometry.theorems import REASONS, THEOREMS
    from maths.i18n import LANGS, REASONS as TABLE
    for t in ("rotation_rule", "sin_ratio", "cos_ratio", "tan_ratio", "angle_at_centre", "angle_in_semicircle",
              "angles_same_segment", "cyclic_quadrilateral", "tangent_radius"):
        assert t in THEOREMS and t in REASONS and set(TABLE[t]) == set(LANGS), t


def test_the_prompt_card_teaches_rotation_trig_and_circles():
    from maths.geometry.items import geometry_prompt
    text = geometry_prompt(topic="Circle theorems", level="Grade 9", language="en", n=2, chapter_context="", kind="worksheet")
    assert "rotate_point" in text and "tangent_at" in text and "CIRCLE THEOREMS" in text and "ROUNDED" in text


def test_compass_arcs_wait_hidden_and_a_step_reveals_them_on_the_board():
    from maths import board as B
    from maths.geometry.board_adapter import figure_board, op_actions
    q = parse_question(copy.deepcopy(C1))
    rep = verify_question(copy.deepcopy(C1))
    assert rep.ok, rep.refusal
    m = rep.models["f"]
    fb = figure_board(m, q.figures[0].figure, panel=(60, 118, 470, 452), prefix="fig", text_metric=B._M.text_box)
    assert "arc_a" in fb.hidden and "arc_b" in fb.hidden and "bis" in fb.hidden
    drawn = {a["target"] for a in fb.actions if a["verb"] == "draw"}
    assert not (set(fb.hidden["arc_a"]) & drawn), "a hidden arc is not drawn before its step"
    els, acts = op_actions(fb, m, {"op": "reveal_object", "target": "arc_a"})
    assert [a["verb"] for a in acts] == ["draw"] * len(fb.hidden["arc_a"]) and not els
    els, acts = op_actions(fb, m, {"op": "reveal_object", "target": "bis"})
    assert acts and all(a["verb"] == "draw" for a in acts)
    # the print never shows what waits for a step: the arcs and the bisector are the board's
    r = render_figure(m, q.figures[0].figure, role="illustration", policy="instructional_metric")
    assert r.svg.startswith("<svg") and len(r.png) > 1000
