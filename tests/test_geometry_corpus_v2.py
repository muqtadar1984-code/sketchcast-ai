"""geometry.figure.v2 — coordinate geometry (rollout 2): the corpus, each
item verified by the chain and drawn; the grid is the ruler."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from maths.geometry import parse_question, realise, verify_question
from maths.geometry.render_static import render_figure

V2 = "geometry.figure.v2"


def _pts(*labels):
    return [{"id": f"p_{lab.lower()}", "label": lab} for lab in labels]


def _axes(x=(-6, 6), y=(-6, 6), step=1):
    return {"make": "axes", "x": list(x), "y": list(y), "step": step}


def _at(lab, x, y):
    return {"make": "point_at", "id": f"p_{lab.lower()}", "x": str(x), "y": str(y)}


def _given(lab, x, y):
    return {"target": f"p_{lab.lower()}", "value": f"({x}, {y})", "role": "given"}


C1 = {"schema_version": V2, "id": "C1", "figure_role": "evidence", "prompt": "Write the coordinates of A, B, C and D.",
      "figures": [{"id": "f", "figure": {"points": _pts("A", "B", "C", "D"),
                   "objects": [_axes(), _at("A", 3, 2), _at("B", -4, 1), _at("C", 0, -3), _at("D", -2, -5)]}}],
      "asks": {"property": "coordinates_of", "over": ["f"]},
      "answer": {"kind": "label_map", "value": {"A": "(3, 2)", "B": "(-4, 1)", "C": "(0, -3)", "D": "(-2, -5)"}}}

C2 = {"schema_version": V2, "id": "C2", "figure_role": "evidence", "prompt": "Which points lie in the third quadrant?",
      "figures": [{"id": "f", "figure": {"points": _pts("A", "B", "C", "D"),
                   "objects": [_axes(), _at("A", 3, 2), _at("B", -4, -1), _at("C", 2, -3), _at("D", -2, -5)]}}],
      "asks": {"property": "quadrant", "over": ["f"], "select": "3"},
      "answer": {"kind": "label_set", "value": ["B", "D"]}}

C3 = {"schema_version": V2, "id": "C3", "figure_role": "reasoning", "prompt": "M is the midpoint of AB. Find the coordinates of M.",
      "figures": [{"id": "f", "figure": {"points": _pts("A", "B", "M"),
                   "objects": [_axes((-2, 8), (-2, 8)), _at("A", 1, 2), _at("B", 7, 6),
                               {"make": "midpoint", "id": "p_m", "segment": ["p_a", "p_b"]}],
                   "segments": [{"id": "s_ab", "points": ["p_a", "p_b"]}],
                   "measures": [_given("A", 1, 2), _given("B", 7, 6), {"target": "p_m", "value": "(a, b)", "role": "unknown"}]}}],
      "steps": [{"kind": "deduce", "theorem": "midpoint_formula", "uses": ["s_ab", "p_m"], "after": ["a = (1 + 7)/2", "b = (2 + 6)/2"]},
                {"kind": "transform", "after": ["a = 4", "b = 4"]}],
      "answer": {"kind": "values", "value": {"a": "4", "b": "4"}}}

C4 = {"schema_version": V2, "id": "C4", "figure_role": "reasoning", "prompt": "Find the length of AB.",
      "figures": [{"id": "f", "figure": {"points": _pts("A", "B"), "units": "units",
                   "objects": [_axes((-1, 6), (-1, 6)), _at("A", 1, 1), _at("B", 4, 5)],
                   "segments": [{"id": "s_ab", "points": ["p_a", "p_b"]}],
                   "measures": [_given("A", 1, 1), _given("B", 4, 5), {"target": "s_ab", "value": "d", "role": "unknown", "unit": "units"}]}}],
      "steps": [{"kind": "deduce", "theorem": "distance_formula", "uses": ["s_ab"], "after": ["d = sqrt(3^2 + 4^2)"]},
                {"kind": "transform", "after": ["d = 5"]}],
      "answer": {"kind": "number", "value": "5", "unit": "units"}}

C5 = {"schema_version": V2, "id": "C5", "figure_role": "reasoning", "prompt": "Find the gradient of the line through A and B.",
      "figures": [{"id": "f", "figure": {"points": _pts("A", "B"),
                   "objects": [_axes((-2, 6), (-2, 8)), _at("A", 1, 1), _at("B", 3, 5), {"make": "line_through", "id": "l_ab", "points": ["p_a", "p_b"]}],
                   "measures": [_given("A", 1, 1), _given("B", 3, 5)]}}],
      "steps": [{"kind": "deduce", "theorem": "gradient", "uses": ["l_ab"], "after": ["m = (5 - 1)/(3 - 1)"]},
                {"kind": "transform", "after": ["m = 2"]}],
      "answer": {"kind": "number", "value": "2"}}

C6 = {"schema_version": V2, "id": "C6", "figure_role": "evidence", "prompt": "Is triangle ABC right-angled?",
      "figures": [{"id": "f", "figure": {"points": _pts("A", "B", "C"),
                   "objects": [_axes((-1, 7), (-1, 7)), _at("A", 1, 1), _at("B", 5, 1), _at("C", 5, 4),
                               {"make": "polygon", "id": "t", "vertices": ["p_a", "p_b", "p_c"]}]}}],
      "asks": {"property": "triangle_class_by_angles", "over": ["f"]},
      "answer": {"kind": "label_map", "value": {"f": "right"}}}

C7 = {"schema_version": V2, "id": "C7", "figure_role": "reasoning", "prompt": "Find the equation of the line through A and B in the form y = mx + c.",
      "figures": [{"id": "f", "figure": {"points": _pts("A", "B"),
                   "objects": [_axes((-2, 6), (-4, 8)), _at("A", 0, -1), _at("B", 2, 3), {"make": "line_through", "id": "l_ab", "points": ["p_a", "p_b"]}],
                   "measures": [_given("A", 0, -1), _given("B", 2, 3)]}}],
      "steps": [{"kind": "deduce", "theorem": "line_equation", "uses": ["l_ab"], "after": ["m = (3 - (-1))/(2 - 0)", "c = -1 - m*0"]},
                {"kind": "transform", "after": ["m = 2", "c = -1"]}],
      "answer": {"kind": "values", "value": {"m": "2", "c": "-1"}}}

C8 = {"schema_version": V2, "id": "C8", "figure_role": "reasoning", "prompt": "P is reflected in the y-axis to P'. Find the coordinates of P'.",
      "figures": [{"id": "f", "figure": {"points": [{"id": "p_p", "label": "P"}, {"id": "p_q", "label": "P'"}],
                   "objects": [_axes((-5, 5), (-2, 6)), _at("P", 3, 2), {"make": "reflect_point", "point": "p_p", "in": "y_axis", "to": "p_q"}],
                   "measures": [_given("P", 3, 2), {"target": "p_q", "value": "(a, b)", "role": "unknown"}]}}],
      "steps": [{"kind": "deduce", "theorem": "reflection_rule", "uses": ["p_q"], "after": ["a = -3", "b = 2"]}],
      "answer": {"kind": "values", "value": {"a": "-3", "b": "2"}}}

C9 = {"schema_version": V2, "id": "C9", "figure_role": "evidence", "prompt": "Name the shape ABCD.",
      "figures": [{"id": "f", "figure": {"points": _pts("A", "B", "C", "D"),
                   "objects": [_axes((-1, 7), (-1, 7)), _at("A", 1, 1), _at("B", 5, 1), _at("C", 5, 5), _at("D", 1, 5),
                               {"make": "polygon", "id": "q", "vertices": ["p_a", "p_b", "p_c", "p_d"]}]}}],
      "asks": {"property": "polygon_name", "over": ["f"]},
      "answer": {"kind": "label_map", "value": {"f": "square"}}}

C10 = {"schema_version": V2, "id": "C10", "figure_role": "reasoning", "prompt": "A is (k, 2) and B is (3, 6). The gradient of AB is 2. Find k.",
       "figures": [{"id": "f", "figure": {"points": _pts("A", "B"), "bind": {"k": "1"},
                    "objects": [_axes((-2, 6), (-2, 8)), {"make": "point_at", "id": "p_a", "x": "k", "y": "2"}, _at("B", 3, 6),
                                {"make": "line_through", "id": "l_ab", "points": ["p_a", "p_b"]}],
                    "measures": [{"target": "p_a", "value": "(k, 2)", "role": "given"}, _given("B", 3, 6)]}}],
       "steps": [{"kind": "setup", "after": ["m = 2"]},
                 {"kind": "deduce", "theorem": "gradient", "uses": ["l_ab"], "after": ["m = (6 - 2)/(3 - k)"]},
                 {"kind": "transform", "before": ["m = 2", "m = (6 - 2)/(3 - k)"], "after": ["2 = 4/(3 - k)", "m = 2"]},
                 {"kind": "transform", "after": ["k = 1", "m = 2"]}],
       "answer": {"kind": "values", "value": {"k": "1", "m": "2"}}}

CORPUS = {"C1": C1, "C2": C2, "C3": C3, "C4": C4, "C5": C5, "C6": C6, "C7": C7, "C8": C8, "C9": C9, "C10": C10}
EXPECT = {"C1": None, "C2": None, "C3": {"a": "4", "b": "4"}, "C4": {"d": "5"}, "C5": {"m": "2"}, "C6": None,
          "C7": {"m": "2", "c": "-1"}, "C8": {"a": "-3", "b": "2"}, "C9": None, "C10": {"k": "1", "m": "2"}}


@pytest.mark.parametrize("qid", list(CORPUS))
def test_the_v2_corpus_verifies(qid):
    rep = verify_question(copy.deepcopy(CORPUS[qid]))
    assert rep.ok, (qid, rep.refusal)
    if EXPECT[qid]:
        assert {k: str(v) for k, v in rep.proved.items()} == EXPECT[qid], (qid, rep.proved)


@pytest.mark.parametrize("qid", list(CORPUS))
def test_the_v2_corpus_draws_on_a_grid_metric_under_every_policy(qid, tmp_path: Path):
    q = parse_question(copy.deepcopy(CORPUS[qid]))
    rep = verify_question(copy.deepcopy(CORPUS[qid]))
    for ref in q.figures:
        metric = rep.models[ref.id]
        assert metric.axes is not None
        schematic = realise(ref.figure, "assessment_schematic", metric=metric)
        assert schematic is metric, "a coordinate figure is never distorted"
        out = render_figure(metric, ref.figure, role=q.figure_role, policy="assessment_schematic")
        assert out.png and len(out.png) > 2000
        (tmp_path / f"{qid}.png").write_bytes(out.png)


def test_a_point_off_the_grid_is_not_readable():
    q = copy.deepcopy(C1)
    q["figures"][0]["figure"]["objects"][1] = _at("A", "7/2", 2)
    rep = verify_question(q)
    assert not rep.ok and (rep.refusal or {}).get("code") == "not_discernible", rep.refusal


def test_a_wrong_midpoint_claim_and_a_wrong_given_are_refused():
    q = copy.deepcopy(C3)
    q["answer"]["value"] = {"a": "4", "b": "5"}
    q["steps"][1]["after"] = ["a = 4", "b = 5"]
    rep = verify_question(q)
    assert not rep.ok and (rep.refusal or {}).get("code") in ("step_not_equivalent", "answer_mismatch"), rep.refusal
    q2 = copy.deepcopy(C4)
    q2["figures"][0]["figure"]["measures"][0] = _given("A", 2, 1)   # says A is at (2, 1); it is plotted at (1, 1)
    rep2 = verify_question(q2)
    assert not rep2.ok and (rep2.refusal or {}).get("code") == "given_not_realised", rep2.refusal


def test_a_v1_record_is_still_a_valid_record():
    import json

    corpus = json.loads((Path(__file__).parent / "fixtures" / "geometry_corpus_v1.json").read_text(encoding="utf-8"))
    b1 = copy.deepcopy(next(i["question"] for i in corpus["items"] if i["question"]["id"] == "B1"))
    b1["schema_version"] = "geometry.figure.v1"
    assert verify_question(b1).ok


def test_the_v2_theorems_have_their_reasons_everywhere():
    from maths.geometry.theorems import REASONS, THEOREMS
    from maths.i18n import LANGS, REASONS as TABLE

    for t in ("distance_formula", "midpoint_formula", "gradient", "line_equation", "parallel_gradients",
              "perpendicular_gradients", "reflection_rule"):
        assert t in THEOREMS and t in REASONS and set(TABLE[t]) == set(LANGS), t


def test_a_coordinate_example_reaches_the_board_with_a_faint_fast_grid():
    from maths import board as B
    from maths import lesson as L
    from maths.geometry.items import GeometryItem
    from maths.schema import Lesson, MethodCard
    from spike.scene_engine.schema import Scene

    q = copy.deepcopy(C3)
    for st in q["steps"]:
        st["speech"] = "Average the x-coordinates, then the y-coordinates."
    rep = verify_question(copy.deepcopy(q))
    item = GeometryItem("C3", q["prompt"], "reasoning", 2, q, rep,
                        speech={"intro": "A is at one, two and B at seven, six. M is the midpoint.",
                                "answer": "So M is at four, four.", "observations": {}})
    ex = L.figure_example(item)
    scene, _ = B.example_scene(ex, MethodCard(), "s003", has_card=False)
    Scene.model_validate(scene)
    grid = [e for e in scene["elements"] if e["type"] == "shape" and e.get("color") == "muted" and e.get("width") == 1.0]
    assert len(grid) >= 10, "the grid is on the board, faint"
    acts = {a["target"]: a for a in scene["actions"] if a["verb"] == "draw"}
    assert all(acts[e["id"]]["duration"] <= 0.06 for e in grid if e["id"] in acts), "and quick"
    # the try-it pause of a coordinate figure is metric and carries no 'not to scale'
    t = L.figure_try_it(ex, "en")
    pause = B.try_it_segment(Lesson(try_it=t), "s007", "en")
    assert not any("not drawn to scale" in (e.get("text") or "") for e in pause["scene"]["elements"])
    assert [e for e in pause["scene"]["elements"] if e["type"] == "shape" and e.get("exact")]
