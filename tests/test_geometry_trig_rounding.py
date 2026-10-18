"""Trigonometry the way a textbook writes it (2026-10-18). The first English
trigonometry kit, on Sonnet 5.5, had every figure refused although its
mathematics was right:

1. a ROUNDED bind — "the figure was drawn with x = 53.13, the proof gives
   180*atan(4/3)/pi": the rounding rule read the parsed value
   (53.1300000000000, thirteen decimals) instead of the bind as written;
2. an inverse-trig step — x = asin(3/5) refused as "solutions before:
   Union(ImageSet(...))": the whole periodic set, where an acute angle of a
   right triangle has exactly one value;
3. a ROUNDED derived given — 8.66 written for 5√3.

The rule for all three is the one an ANSWER already follows: a value written
WITH its decimals is accepted when it is the correct rounding of the exact
one. A whole number still reads as exact ("53" on a 3-4-5 triangle stays
refused), and the figure keeps its exact value.
"""
from __future__ import annotations

import copy

from maths.geometry import verify_question
from tests.test_geometry_corpus_v4 import V3, _ang, _pts


def _q(objects, angles, segments, measures, steps, answer, bind=None, prompt="Find x."):
    fig = {"units": "cm", "points": _pts("A", "B", "C"), "objects": objects, "angles": angles,
           "segments": segments, "measures": measures}
    if bind:
        fig["bind"] = bind
    return {"schema_version": V3, "id": "q", "figure_role": "reasoning", "prompt": prompt,
            "figures": [{"id": "f", "figure": fig}], "steps": steps, "answer": answer}


SEGS = [{"id": "s_ab", "points": ["p_a", "p_b"]}, {"id": "s_bc", "points": ["p_b", "p_c"]},
        {"id": "s_ac", "points": ["p_a", "p_c"]}]
LEGS_3_4 = [{"make": "triangle_rhs", "id": "t", "vertices": ["p_a", "p_b", "p_c"], "legs": ["3", "4"]}]


def _ok(q):
    rep = verify_question(copy.deepcopy(q))
    return rep.ok, (rep.refusal or {})


# ── 1. a rounded bind ──────────────────────────────────────────────────

def test_a_correctly_rounded_bind_is_the_proved_angle():
    q = _q(LEGS_3_4, [_ang("angle_bac", "p_a", "p_b", "p_c")], SEGS,
           [{"target": "s_ab", "value": "3", "unit": "cm"}, {"target": "s_bc", "value": "4", "unit": "cm"},
            {"target": "angle_bac", "value": "x", "unit": "deg", "role": "unknown"}],
           [{"kind": "deduce", "theorem": "tan_ratio", "uses": ["t", "angle_bac"], "after": ["tan(x) = 4/3"]},
            {"kind": "transform", "after": ["x = atan(4/3)"]}],
           {"kind": "number", "value": "53.13", "unit": "deg"}, bind={"x": "53.13"})
    ok, why = _ok(q)
    assert ok, why


def test_a_wrongly_rounded_bind_is_still_refused():
    q = _q(LEGS_3_4, [_ang("angle_bac", "p_a", "p_b", "p_c")], SEGS,
           [{"target": "s_ab", "value": "3", "unit": "cm"}, {"target": "s_bc", "value": "4", "unit": "cm"},
            {"target": "angle_bac", "value": "x", "unit": "deg", "role": "unknown"}],
           [{"kind": "deduce", "theorem": "tan_ratio", "uses": ["t", "angle_bac"], "after": ["tan(x) = 4/3"]},
            {"kind": "transform", "after": ["x = atan(4/3)"]}],
           {"kind": "number", "value": "53.13", "unit": "deg"}, bind={"x": "53.2"})
    ok, why = _ok(q)
    assert not ok and why.get("code") == "bind_mismatch", why


# ── 2. an inverse-trig step on the acute range ─────────────────────────

def _sin_question(after_x):
    hyp = [{"make": "triangle_rhs", "id": "t", "vertices": ["p_a", "p_b", "p_c"], "hyp": "5", "side": "4"}]
    return _q(hyp, [_ang("angle_bac", "p_a", "p_b", "p_c")], SEGS,
              [{"target": "s_ac", "value": "5", "unit": "cm"}, {"target": "s_bc", "value": "3", "unit": "cm"},
               {"target": "angle_bac", "value": "x", "unit": "deg", "role": "unknown"}],
              [{"kind": "deduce", "theorem": "sin_ratio", "uses": ["t", "angle_bac"], "after": ["sin(x) = 3/5"]},
               {"kind": "transform", "after": [after_x]}],
              {"kind": "number", "value": "36.87", "unit": "deg"})


def test_x_equals_asin_is_the_acute_angle():
    ok, why = _ok(_sin_question("x = asin(3/5)"))
    assert ok, why


def test_the_obtuse_solution_is_still_refused():
    """sin(x) = 3/5 also holds at 180° − 36.87°: not an angle this triangle has."""
    ok, why = _ok(_sin_question("x = 180 - asin(3/5)"))
    assert not ok, "an obtuse 'solution' of a right triangle's acute angle must not pass"


def test_a_wrong_inverse_is_still_refused():
    ok, why = _ok(_sin_question("x = acos(3/5)"))
    assert not ok, "acos(3/5) is the OTHER acute angle, not this one"


# ── 3. a rounded derived given ─────────────────────────────────────────

def _thirty(given_side: str):
    """Hypotenuse 10 and angle C = 30° (asa-built): BC is 5√3 = 8.66025…"""
    obj = [{"make": "triangle_rhs", "id": "t", "vertices": ["p_a", "p_b", "p_c"], "hyp": "10", "side": "5"}]
    return _q(obj, [_ang("angle_acb", "p_c", "p_a", "p_b")], SEGS,
              [{"target": "s_ac", "value": "10", "unit": "cm"}, {"target": "s_ab", "value": "5", "unit": "cm"},
               {"target": "s_bc", "value": given_side, "unit": "cm"},
               {"target": "angle_acb", "value": "x", "unit": "deg", "role": "unknown"}],
              [{"kind": "deduce", "theorem": "sin_ratio", "uses": ["t", "angle_acb"], "after": ["sin(x) = 5/10"]},
               {"kind": "transform", "after": ["x = 30"]}],
              {"kind": "number", "value": "30", "unit": "deg"})


def test_a_side_given_correctly_rounded_is_accepted():
    ok, why = _ok(_thirty("8.66"))
    assert ok, why


def test_a_side_given_wrongly_rounded_is_refused():
    ok, why = _ok(_thirty("8.67"))
    assert not ok and why.get("code") == "given_not_realised", why


def test_a_whole_number_still_reads_as_exact():
    """'9' for 8.66 has no decimals to be a rounding of: refused, as '53' is."""
    ok, why = _ok(_thirty("9"))
    assert not ok and why.get("code") == "given_not_realised", why
