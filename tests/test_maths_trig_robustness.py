"""Two failures of the first English trigonometry kit (2026-10-18, catalogue
on Claude via Bedrock), pinned:

1. One worked example with an off-list ``task`` ("find missing side") made
   ``parse_example`` raise, and the whole worksheet failed. An unreadable
   example is now EMPTY — every caller already treats that as "not given".
2. A right triangle standing on its short leg under a two-line question did
   not fit the try-it panel ("the figure (5.0×12.0 units) does not fit the
   board panel"). An ungridded figure that is too tall is now laid on its
   side, in place, once.
"""
from __future__ import annotations

import copy

import pytest

from maths import board as B
from maths.geometry import parse_question, verify_question
from maths.geometry.board_adapter import figure_board
from maths.geometry.errors import GeometryRefusal
from maths.schema import Lesson, WorkedExample, parse_example, parse_lesson
from tests.test_geometry_corpus_v4 import V3

GOOD = {"label": "Example 1", "task": "evaluate", "problem": "Work out 3 + 4.", "givens": ["3 + 4"],
        "target": "expression", "steps": [{"to": "7", "operation": "add"}], "final_answer": ["7"]}
OFF_LIST = {**GOOD, "label": "Example 2", "task": "find missing side"}


# ── 1. the parser ──────────────────────────────────────────────────────

def test_an_off_list_task_is_an_empty_example_not_an_exception():
    with pytest.raises(Exception):
        WorkedExample.model_validate(OFF_LIST)          # the fixture really is invalid
    ex = parse_example(OFF_LIST)
    assert isinstance(ex, WorkedExample) and ex.steps == []


def test_a_readable_example_is_unchanged():
    ex = parse_example(GOOD)
    assert ex.task == "evaluate" and ex.problem == "Work out 3 + 4." and ex.steps


def test_a_lesson_keeps_its_readable_examples_and_drops_the_unreadable_one():
    lesson = parse_lesson({"topic": "Trigonometry", "examples": [GOOD, OFF_LIST, {**GOOD, "label": "Example 3"}]})
    assert isinstance(lesson, Lesson)
    assert [e.label for e in lesson.examples] == ["Example 1", "Example 3"]


# ── 2. the tall figure ─────────────────────────────────────────────────

def _right_triangle(legs):
    pts = [{"id": f"p_{c}", "label": c.upper()} for c in "abc"]
    return {"schema_version": V3, "id": "T", "figure_role": "reasoning", "prompt": "Find angle A.",
            "figures": [{"id": "f", "figure": {"units": "cm", "points": pts,
                         "objects": [{"make": "triangle_rhs", "id": "t", "vertices": ["p_a", "p_b", "p_c"], "legs": legs}],
                         "angles": [{"id": "angle_bac", "rays": [["p_a", "p_b"], ["p_a", "p_c"]]}],
                         "segments": [{"id": "s_ab", "points": ["p_a", "p_b"]}, {"id": "s_bc", "points": ["p_b", "p_c"]}],
                         "measures": [{"target": "s_ab", "value": legs[0], "unit": "cm"},
                                      {"target": "s_bc", "value": legs[1], "unit": "cm"},
                                      {"target": "angle_bac", "value": "x", "unit": "deg", "role": "unknown"}]}}],
            "steps": [{"kind": "deduce", "theorem": "tan_ratio", "uses": ["t", "angle_bac"],
                       "after": [f"tan(x) = {legs[1]}/{legs[0]}"]},
                      {"kind": "transform", "after": [f"x = atan({legs[1]}/{legs[0]})"]}],
            "answer": {"kind": "number", "value": "x", "unit": "deg"}}


def _board(legs, top):
    q = _right_triangle(legs)
    rep = verify_question(copy.deepcopy(q))
    m = next(iter(rep.models.values()))
    ref = parse_question(q).figures[0]
    panel = (B.Q_AT[0], top, B.FIG_RIGHT, B.WORK_BOTTOM)
    return m, figure_board(m, ref.figure, panel=panel, prefix="fig", text_metric=B._M.text_box)


def test_a_tall_triangle_under_a_long_question_is_laid_on_its_side():
    m, fb = _board(["5", "12"], top=200.0)
    assert fb.scale >= 18.0, "drawn at a legible scale, not shrunk below the floor"
    x0, y0, x1, y1 = m.bbox()
    assert (x1 - x0) > (y1 - y0), "the model itself was turned (later reveals read it)"
    assert getattr(m, "_turned_to_fit", False)


def test_a_figure_that_fits_is_never_turned():
    m, _fb = _board(["5", "12"], top=120.0)
    assert not getattr(m, "_turned_to_fit", False)
    m, _fb = _board(["12", "5"], top=200.0)
    assert not getattr(m, "_turned_to_fit", False)


def test_a_model_is_turned_at_most_once():
    m, _fb = _board(["5", "12"], top=200.0)
    q = _right_triangle(["5", "12"])
    ref = parse_question(q).figures[0]
    panel = (B.Q_AT[0], 200.0, B.FIG_RIGHT, B.WORK_BOTTOM)
    before = m.bbox()
    figure_board(m, ref.figure, panel=panel, prefix="fig", text_metric=B._M.text_box)   # the second call (evidence row)
    assert m.bbox() == before


def test_a_figure_with_a_chosen_orientation_is_not_turned():
    q = _right_triangle(["5", "12"])
    q["figures"][0]["figure"]["orientation"] = 180
    rep = verify_question(copy.deepcopy(q))
    m = next(iter(rep.models.values()))
    ref = parse_question(q).figures[0]
    with pytest.raises(GeometryRefusal):
        figure_board(m, ref.figure, panel=(B.Q_AT[0], 200.0, B.FIG_RIGHT, B.WORK_BOTTOM), prefix="fig",
                     text_metric=B._M.text_box)


# ── 3. the drawn-size floor (ungridded figures) ────────────────────────
# The rerun of the same kit (2026-10-18, after #276) failed on "the figure
# (27.5×12.5 units) does not fit the board panel": a 7-24-25 right triangle
# needed 495 px at 18 px/unit in a 410 px panel, whichever way it was turned.

from maths.geometry.board_adapter import MIN_FIGURE_PX, MIN_PX_PER_UNIT, _min_px  # noqa: E402


@pytest.mark.parametrize("legs", [["24", "7"], ["7", "24"], ["25", "12.5"]])
def test_a_triangle_with_big_side_lengths_scales_down_to_fit(legs):
    for top in (120.0, 200.0):
        m, fb = _board(legs, top=top)
        x0, y0, x1, y1 = m.bbox()
        drawn = max(x1 - x0, y1 - y0) * fb.scale
        assert drawn >= MIN_FIGURE_PX - 1, f"still legible: its longest side draws {drawn:.0f} px"


def test_a_small_figure_keeps_the_per_unit_floor():
    m, fb = _board(["4", "3"], top=120.0)
    assert _min_px(m) == MIN_PX_PER_UNIT and fb.scale == 70.0, "unchanged: the floor only ever lowers"


def test_a_gridded_figure_keeps_the_per_unit_floor():
    from tests.test_geometry_corpus_v4 import R1
    rep = verify_question(copy.deepcopy(R1))
    m = next(iter(rep.models.values()))
    assert m.axes is not None
    assert _min_px(m) == MIN_PX_PER_UNIT, "on a grid the units are numbers the eye reads"
