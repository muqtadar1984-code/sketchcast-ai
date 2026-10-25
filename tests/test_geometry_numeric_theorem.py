"""A theorem whose sides are all numbers on the figure (live, 2026-10-25).

On a coordinate figure every gradient is a number, so parallel_gradients gives
Eq(2, 2) and perpendicular_gradients Eq(-1, -1): sympy folds those to True, and
the chain read `.lhs` off it — one generated question crashed the whole
coordinates-and-plotting-linear-graphs video. The theorem holds and states
nothing new; the step that writes it out still checks.
"""
from __future__ import annotations

from maths.geometry import verify_question

V2 = "geometry.figure.v2"


def _pts(*labels):
    return [{"id": f"p_{lab.lower()}", "label": lab} for lab in labels]


def _at(lab, x, y):
    return {"make": "point_at", "id": f"p_{lab.lower()}", "x": str(x), "y": str(y)}


def _given(lab, x, y):
    return {"target": f"p_{lab.lower()}", "value": f"({x}, {y})", "role": "given"}


def _question(theorem: str, c: tuple, d: tuple, after: str) -> dict:
    return {"schema_version": V2, "id": "N1", "figure_role": "reasoning",
            "prompt": "Show how the lines AB and CD meet.",
            "figures": [{"id": "f", "figure": {
                "points": _pts("A", "B", "C", "D"),
                "objects": [{"make": "axes", "x": [-2, 8], "y": [-2, 8], "step": 1},
                            _at("A", 1, 1), _at("B", 3, 5), _at("C", *c), _at("D", *d)],
                "segments": [{"id": "s_ab", "points": ["p_a", "p_b"]}, {"id": "s_cd", "points": ["p_c", "p_d"]}],
                "measures": [_given("A", 1, 1), _given("B", 3, 5), _given("C", *c), _given("D", *d)]}}],
            "steps": [{"kind": "deduce", "theorem": theorem, "uses": ["s_ab", "s_cd"], "after": [after]}],
            "answer": {"kind": "number", "value": "2"}}


def _step_refusal(rep):
    return rep.refusal if rep.refusal and (rep.refusal.get("where") or "").startswith("step") else None


def test_perpendicular_gradients_on_numbers_checks_the_step():
    rep = verify_question(_question("perpendicular_gradients", (6, 1), (2, 3), "2 * (-1/2) = -1"))
    assert _step_refusal(rep) is None, rep.refusal


def test_parallel_gradients_on_numbers_checks_the_step():
    rep = verify_question(_question("parallel_gradients", (4, 1), (6, 5), "2 = 2"))
    assert _step_refusal(rep) is None, rep.refusal


def test_a_wrong_line_under_a_numeric_theorem_is_refused():
    rep = verify_question(_question("perpendicular_gradients", (6, 1), (2, 3), "2 * (-1/2) = 5"))
    assert (rep.refusal or {}).get("code") == "step_not_equivalent"
