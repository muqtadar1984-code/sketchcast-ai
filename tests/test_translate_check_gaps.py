"""Four translations failed pre-check A overnight 2026-10-20 — every one a
correct translation the check misread:

  * Pythagoras (ar): "the top of a 15-meter pole" — the English ARTICLE "a"
    was read as a variable, the span "a 15", which no translation keeps;
  * Sequences (ms): "sebutan ke-50" — the Malay ordinal prefix read as a
    minus, -50;
  * Linear Equations (ar): "instead of negative 2" written "-2";
  * Reflection (ms): "three hundred million" written "300 juta".

What is pinned: each now passes; the same shapes with a REAL error still
block (a sign flip, a changed value, a wrong scale, a minus in maths).

Run: .venv/Scripts/python.exe -m pytest tests/test_translate_check_gaps.py -q -p no:cacheprovider
"""
from __future__ import annotations

import pytest

from catalogue import translate_check as C


class TestTheArticle:
    SRC = ("A high-wire walker stretches a cable from the top of a 15-meter pole to a point on the ground. "
           "The cable is 17 meters long.")

    def test_a_is_not_a_variable(self):
        assert C.maths_spans(self.SRC) == ["15", "17"]
        assert C.maths_spans("I 3 times") == ["3"]

    def test_the_arabic_passes(self):
        ok, lost = C.prose_maths_equivalent(self.SRC, "يمد لاعب كابلاً من قمة عمود طوله 15 مترًا. طول الكابل 17 مترًا.")
        assert ok, lost

    def test_a_variable_with_an_operator_is_still_maths(self):
        assert C.maths_spans("Let a = 15") == ["a = 15"]
        assert not C.prose_maths_equivalent("Let a = 15", "Biar a = 16")[0]


class TestMalayOrdinals:
    def test_ke_50_is_not_minus_50(self):
        assert C.number_values("cari sebutan ke-50") == ["50"]
        ok, lost = C.prose_maths_equivalent("For the sequence with nth term 6n - 5, find the 50th term.",
                                           "Bagi jujukan dengan sebutan ke-n 6n - 5, cari sebutan ke-50.")
        assert ok, lost

    def test_a_minus_after_a_variable_stays_a_minus(self):
        assert C.number_values("6n-5") == ["6", "-5"]
        assert C.maths_spans("x-2 = 5") == ["x-2 = 5"]


class TestSignWords:
    def test_negative_2_is_minus_2(self):
        assert C.numbers_equivalent("Dividing by positive 2 instead of negative 2.",
                                    "القسمة على 2 الموجب بدلاً من -2 السالب.")[0]

    @pytest.mark.parametrize("src,tgt", [("x is 2", "x هو -2"), ("negative 2.5 here", "هنا -2")])
    def test_a_real_sign_or_value_change_still_blocks(self, src, tgt):
        assert not C.numbers_equivalent(src, tgt)[0]


class TestScaleWords:
    @pytest.mark.parametrize("tgt", ["kira-kira 300 juta meter sesaat", "حوالي 300 مليون متر في الثانية",
                                     "environ 300 millions de mètres par seconde", "unos 300 millones de metros"])
    def test_300_million_in_words_or_digits_and_a_scale_word(self, tgt):
        ok, missing, extra = C.numbers_equivalent("about three hundred million metres per second", tgt)
        assert ok, (missing, extra)

    def test_a_wrong_value_with_a_scale_word_still_blocks(self):
        assert not C.numbers_equivalent("about three hundred million", "kira-kira 400 juta")[0]

    def test_mil_does_not_shadow_millones(self):
        assert C._scaled_values("300 millones") == [("300", "300000000")]
        assert C._scaled_values("3 mil") == [("3", "3000")]
