"""Degree-mode trigonometry in the algebra verifier (maths/verify.py).

The first English trigonometry lesson (2026-10-18, kit 22754aae, Sonnet 5.5)
dropped five of its eleven worked examples although their mathematics was
right. Two gaps, both pinned here against the examples' own shape:

1. SymPy read sin(30) as thirty RADIANS: "evaluate sin(30): solutions before:
   {10*sin(30)}; after: {5}" — WRONG. An example whose words or working say
   degrees is now read in degrees.
2. A calculator step — "12 / cos 35° = 12 / 0.8192 = 14.65" — can never be
   exactly equivalent. It is accepted ONLY when every new decimal is the
   CORRECT rounding, at the decimals it writes, of a ratio or value the line
   before fixes; the answer likewise. A whole number is never a rounding.
"""
from __future__ import annotations

from maths.schema import Step, WorkedExample
from maths.verify import verify_example


def _ex(problem, givens, steps, answer, task="solve", target="x"):
    return WorkedExample(label="Example", task=task, problem=problem, givens=givens, target=target,
                         steps=[Step(**s) for s in steps], final_answer=answer)


def _status(ex):
    rep = verify_example(ex)
    return rep.status, "; ".join(getattr(rep, "reasons", []) or [])


SIN30 = dict(problem="A right-angled triangle has a hypotenuse of 10 cm and an angle of 30°. Find the opposite side x.",
             givens=["x = 10 * sin(30)"])


def _cos35(after_1, after_2, answer):
    return _ex("In a right-angled triangle the angle is 35° and the adjacent side is 12 cm. Find the hypotenuse x.",
               ["x = 12 / cos(35)"],
               [{"kind": "transform", "operation": "evaluate cos 35°", "before": ["x = 12 / cos(35)"], "after": [after_1]},
                {"kind": "transform", "operation": "divide and round", "before": [after_1], "after": [after_2]}],
               [answer])


# ── 1. degrees ─────────────────────────────────────────────────────────

def test_sin_30_degrees_is_a_half():
    ex = _ex(**SIN30, steps=[{"kind": "transform", "operation": "evaluate sin 30°",
                              "before": ["x = 10 * sin(30)"], "after": ["x = 5"]}], answer=["x = 5"])
    status, why = _status(ex)
    assert status == "verified", why


def test_a_wrong_value_in_degrees_is_still_wrong():
    ex = _ex(**SIN30, steps=[{"kind": "transform", "operation": "evaluate sin 30°",
                              "before": ["x = 10 * sin(30)"], "after": ["x = 6"]}], answer=["x = 6"])
    status, _ = _status(ex)
    assert status != "verified"


def test_without_degrees_trig_is_read_as_before():
    """No degree sign or word: radians, unchanged — sin(30) is not 1/2."""
    ex = _ex("Evaluate x.", ["x = 10 * sin(30)"],
             [{"kind": "transform", "operation": "evaluate", "before": ["x = 10 * sin(30)"], "after": ["x = 5"]}],
             ["x = 5"])
    status, _ = _status(ex)
    assert status != "verified"


# ── 2. correctly rounded evaluation ────────────────────────────────────

def test_a_ratio_rounded_then_the_value_rounded_is_verified():
    # cos 35° = 0.819152…; 12 / cos 35° = 14.6492…; 12 / 0.8192 = 14.6484…
    status, why = _status(_cos35("x = 12 / 0.8192", "x = 14.65", "x = 14.65"))
    assert status == "verified", why


def test_a_wrongly_rounded_ratio_is_refused():
    status, _ = _status(_cos35("x = 12 / 0.8190", "x = 14.65", "x = 14.65"))
    assert status != "verified"


def test_a_wrongly_rounded_value_is_refused():
    status, _ = _status(_cos35("x = 12 / 0.8192", "x = 14.66", "x = 14.66"))
    assert status != "verified"


def test_a_whole_number_is_not_a_rounding():
    status, _ = _status(_cos35("x = 12 / 0.8192", "x = 15", "x = 15"))
    assert status != "verified"
