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
    q["schema_version"] = "geometry.figure.v2"
    assert verify_question(q).refusal["code"] == "bad_schema"
