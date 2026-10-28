"""Coordinates and Plotting Linear Graphs (te) failed three times, 2026-10-25:

  * "layout_collision: the figure (10.1×16.5 units) does not fit the board
    panel" — a two-line Telugu question left a 288 px panel for a coordinate
    figure 16.5 units tall (17.45 px/unit, floor 18); the English one-line
    question leaves 334. A gridded figure had no fallback floor at all.
  * "maths in the stem altered" for "The point (3, 7) lies on the graph of
    y = 2x + 1." — a translation wrote "(3,7)", read as the decimal-comma
    3.7; and set the pair straight before the equation, "(3, 7) y = 2x + 1",
    one span whose ")" paired with nothing.

What is pinned: the grid may tighten to GRIDDED_RELAX of its floor in the
refusal fallback only (never below; normal layout unchanged); "(3,7)" is a
pair; juxtaposed expressions are two spans; real changes still block.

Run: .venv/Scripts/python.exe -m pytest tests/test_te_coordinates_fix.py -q -p no:cacheprovider
"""
from __future__ import annotations

import copy

import pytest

from catalogue import translate_check as C
from maths.geometry.board_adapter import GRIDDED_RELAX, MIN_PX_PER_UNIT, _min_px, figure_board
from maths.geometry import parse_question, verify_question
from maths.geometry.errors import GeometryRefusal


def _axes_model():
    from tests.test_geometry_corpus_v4 import R1
    q = copy.deepcopy(R1)
    rep = verify_question(copy.deepcopy(q))
    m = next(iter(rep.models.values()))
    assert m.axes is not None
    return m, parse_question(q).figures[0]


class TestGriddedFallbackFloor:
    def test_the_floor_without_a_panel_is_unchanged(self):
        m, _ = _axes_model()
        assert _min_px(m) == MIN_PX_PER_UNIT

    def test_the_fallback_floor_is_the_relaxed_share(self):
        m, _ = _axes_model()
        assert _min_px(m, (0, 0, 410, 288)) == pytest.approx(MIN_PX_PER_UNIT * GRIDDED_RELAX)

    def test_a_grid_a_little_too_tall_now_fits_at_the_relaxed_scale(self):
        m, ref = _axes_model()
        x0, y0, x1, y1 = m.bbox()
        h = y1 - y0
        # a panel that holds the grid at ~16.5 px/unit: under the 18 floor,
        # over the relaxed one (labels widen the drawing a little)
        panel = (0.0, 0.0, 2000.0, 16.9 * (h + 1.0))
        fb = figure_board(m, ref.figure, panel=panel, prefix="g")
        assert MIN_PX_PER_UNIT * GRIDDED_RELAX - 0.01 <= fb.scale < MIN_PX_PER_UNIT

    def test_a_grid_far_too_tall_is_still_refused(self):
        m, ref = _axes_model()
        x0, y0, x1, y1 = m.bbox()
        with pytest.raises(GeometryRefusal):
            figure_board(m, ref.figure, panel=(0.0, 0.0, 2000.0, 12.0 * (y1 - y0)), prefix="g")


class TestCoordinatePairs:
    SRC = "The point (3, 7) lies on the graph of y = 2x + 1."

    @pytest.mark.parametrize("tgt", [
        "బిందువు (3,7) y=2x+1 గ్రాఫ్‌పై ఉంటుంది.",
        "బిందువు (3, 7) y = 2x + 1 గ్రాఫ్‌పై ఉంటుంది.",
        "బిందువు (3, 7) అనేది y = 2x + 1 యొక్క గ్రాఫ్‌పై ఉంటుంది.",
    ])
    def test_the_telugu_stems_pass(self, tgt):
        ok, lost = C.prose_maths_equivalent(self.SRC, tgt)
        assert ok, lost

    def test_a_pair_without_its_space_is_two_numbers(self):
        assert C.number_values("(3,7)") == ["3", "7"]
        assert C.number_values("(-1.5,2)") == ["-1.5", "2"]

    def test_a_decimal_comma_is_still_a_decimal(self):
        assert C.number_values("soustraire 273,15") == ["273.15"]
        assert C.number_values("x = 1,5") == ["1.5"]

    @pytest.mark.parametrize("tgt", ["బిందువు (3,8) y=2x+1 గ్రాఫ్‌పై", "బిందువు (3, 7) y = 2x + 3 గ్రాఫ్‌పై"])
    def test_a_changed_coordinate_or_equation_still_blocks(self, tgt):
        assert not C.prose_maths_equivalent(self.SRC, tgt)[0]

    def test_brackets_that_belong_to_the_maths_stay_one_span(self):
        assert C.maths_spans("2(x + 3) = 10") == ["2(x + 3) = 10"]
        assert C.maths_spans("(x + 1)(x + 2)") == ["(x + 1)(x + 2)"]
        # a bracket genuinely lost is still caught (Malay Substitution, #287)
        assert not C.prose_maths_equivalent("Evaluate 4x^2 - 3(y - 1) when x = -2",
                                            "Nilaikan 4x^2 - 3(y - 1 apabila x = -2")[0]
