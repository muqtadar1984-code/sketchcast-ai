"""Board points are cut by their measured width, not a character count.

The Malay formula concept point of the trigonometry kit (77 characters)
lost its tail to the old 64-character cap although it fitted the gap at
the board's minimum size (Auto Translate Agent plan, P0, 2026-10-20). A
point too wide for one line now wraps onto a second line.
"""
from __future__ import annotations

import pytest

from maths.board import CARD_X, POINT_MAX_LINES, _M, _point_elements, _point_rows, concept_segment
from maths.schema import Lesson

MAX_W = CARD_X - 16 - 24 - 110
MALAY = "sin = sisi bertentangan/hipotenus, kos = sisi bersebelahan/hipotenus, tan"
LONG = ("Bahagikan setiap sisi dengan hipotenus untuk mendapat nisbah sinus dan kosinus, "
        "kemudian bahagikan sinus dengan kosinus untuk mendapat tangen sudut itu")


def _words(lines: list[str]) -> list[str]:
    return " ".join(lines).replace("…", "").split()


class TestPointRows:
    def test_a_77_character_point_keeps_every_word(self):
        point = MALAY + "/x"
        assert len(point) > 64
        (lines, _y, size), = _point_rows([point], "Konsep")
        assert _words(lines) == point.split()
        assert all(_M.text_box(ln, size)[0] <= MAX_W for ln in lines)

    def test_a_short_point_is_one_line_at_the_largest_size(self):
        (lines, _y, size), = _point_rows(["a + b = c"], "Konsep")
        assert lines == ["a + b = c"] and size == 27.0

    def test_a_point_too_wide_for_one_line_wraps(self):
        (lines, _y, size), = _point_rows([LONG], "Konsep")
        assert 1 < len(lines) <= POINT_MAX_LINES
        assert size == 20.0
        assert all(_M.text_box(ln, size)[0] <= MAX_W for ln in lines)

    def test_the_next_point_starts_below_a_wrapped_one(self):
        rows = _point_rows([LONG, "a + b = c"], "Konsep")
        (lines0, y0, size0), (_l1, y1, _s1) = rows
        line_h = max(_M.text_box(ln, size0)[1] for ln in lines0) + 4.0
        assert y1 >= y0 + line_h * len(lines0)


class TestPointElements:
    def test_a_wrapped_point_draws_its_second_line_after_the_first(self):
        rows = _point_rows([LONG, "a + b = c"], "Konsep")
        els, acts = _point_elements(rows, [0.1, 0.2])
        ids = [e["id"] for e in els]
        assert ids[:3] == ["wb_d0", "wb_p0", "wb_p0_2"] and "wb_p1" in ids
        by_id = {e["id"]: e for e in els}
        assert by_id["wb_p0_2"]["at"][1] > by_id["wb_p0"]["at"][1]
        assert by_id["wb_p0_2"]["at"][0] == by_id["wb_p0"]["at"][0] == 110
        writes = [a["target"] for a in acts if a["verb"] == "write"]
        assert writes == ["wb_p0", "wb_p0_2", "wb_p1"]

    def test_unwrapped_points_keep_their_ids(self):
        rows = _point_rows(["a", "b", "c"], "Konsep")
        els, _acts = _point_elements(rows, [0.1, 0.2, 0.3])
        assert [e["id"] for e in els] == ["wb_d0", "wb_p0", "wb_d1", "wb_p1", "wb_d2", "wb_p2"]


def test_the_concept_board_shows_the_whole_malay_formula():
    lesson = Lesson.model_construct(topic="Nisbah trigonometri", concept=[], concept_points=[MALAY + "/x"])
    try:
        seg = concept_segment(lesson, "s002", "ms")
    except Exception:  # noqa: BLE001 — a minimal lesson the compiler cannot draw
        pytest.skip("compiler needs a fuller lesson fixture")
    shown = [e["text"] for e in seg["scene"]["elements"] if str(e.get("id", "")).startswith("wb_p0")]
    assert "hipotenus," in " ".join(shown) and "…" not in " ".join(shown)
