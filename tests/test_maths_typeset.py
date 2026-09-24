"""The school typesetter: fractions stack, powers rise, roots get a bar,
and a term can be found by name for an arrow."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from agent5_slides.slide_builder import _font
from maths.typeset import PilMeasurer, TokenError, typeset


@pytest.fixture(scope="module")
def m():
    return PilMeasurer(lambda size: _font(False, size, ""))


def _runs(lay):
    return [r.text for kind, i in lay.order() if kind == "run" for r in [lay.runs[i]]]


def test_a_linear_equation_is_one_row_of_runs(m):
    lay = typeset("3x + 5 = 20", 36, m)
    assert _runs(lay) == ["3", "x", "+", "5", "=", "20"]
    assert lay.strokes == []
    assert lay.h < 36 * 1.3 and lay.w > 36 * 3
    xs = [r.x for r in lay.runs]
    assert xs == sorted(xs), "left to right"


def test_a_fraction_stacks_around_a_bar(m):
    lay = typeset("(x + 1)/2", 36, m)
    assert len(lay.strokes) == 1, "the fraction bar"
    bar_y = lay.strokes[0].pts[0][1]
    num = [r for r in lay.runs if r.text in ("x", "+", "1", "(", ")")]
    den = [r for r in lay.runs if r.text == "2"]
    assert all(r.baseline < bar_y for r in num) and all(r.baseline > bar_y for r in den)
    assert lay.h > 36 * 1.6, "taller than one line"


def test_a_power_is_smaller_and_raised(m):
    lay = typeset("x^2 + 5x + 6", 36, m)
    x, two = lay.runs[0], lay.runs[1]
    assert x.text == "x" and two.text == "2"
    assert two.size < x.size and two.baseline < x.baseline and two.x > x.x


def test_a_root_draws_its_sign_and_bar(m):
    lay = typeset("sqrt(x + 1) = 3", 36, m)
    assert len(lay.strokes) == 1
    sign = lay.strokes[0].pts
    assert sign[-1][0] > sign[0][0] + 36 * 1.5, "the vinculum reaches over the radicand"
    assert lay.runs[0].text == "x" and lay.runs[0].x > 36 * 0.5


def test_unicode_and_operators_are_typeset_properly(m):
    lay = typeset("2x − 3 ≤ 7", 36, m)
    assert _runs(lay) == ["2", "x", "−", "3", "≤", "7"]
    lay = typeset("-2x > 4", 36, m)
    assert _runs(lay) == ["−", "2", "x", ">", "4"]
    lay = typeset("2 * 3 = 6", 36, m)
    assert "×" in _runs(lay)
    lay = typeset("(x + 2)(x + 3)", 36, m)
    assert _runs(lay) == ["(", "x", "+", "2", ")", "(", "x", "+", "3", ")"]


def test_a_term_can_be_found_for_an_arrow(m):
    lay = typeset("3x + 5 = 20", 36, m)
    box = lay.find("5")
    assert box is not None
    five = next(r for r in lay.runs if r.text == "5")
    assert abs(box[0] - five.x) < 0.5
    both = lay.find("3x")
    assert both is not None and both[0] < box[0] and both[2] < box[0]
    assert lay.find("7") is None


def test_progressive_reveal_is_left_to_right(m):
    lay = typeset("3x + 5 = 20", 36, m)
    half = lay.visible(0.5)
    full = lay.visible(1.0)
    assert 0 < len(half) < len(full) == len(lay.runs)
    shown = [lay.runs[i].text for kind, i, _p in half if kind == "run"]
    assert shown == _runs(lay)[:len(shown)]


def test_not_notation_raises(m):
    with pytest.raises(TokenError):
        typeset("subtract 5 from both sides", 36, m)


def test_it_draws_without_error(m):
    """A smoke render through PIL: every run and stroke lands inside the layout box."""
    lay = typeset("x = (-b + sqrt(b^2 - 4ac))/(2a)", 40, m)
    img = Image.new("RGB", (int(lay.w) + 20, int(lay.h) + 20), "white")
    d = ImageDraw.Draw(img)
    for r in lay.runs:
        f = _font(False, int(r.size), "")
        asc, _ = f.getmetrics()
        d.text((10 + r.x, 10 + r.baseline - asc), r.text, fill="black", font=f)
        assert 0 <= r.x <= lay.w + 1
    for s in lay.strokes:
        d.line([(10 + x, 10 + y) for x, y in s.pts], fill="black", width=int(s.width))
        assert all(-1 <= x <= lay.w + 1 and -1 <= y <= lay.h + 1 for x, y in s.pts)
    assert img.getbbox() is not None
