"""Notation is spoken the way a teacher says it."""

from __future__ import annotations

import pytest

from maths.speech import has_notation, speakable_maths, spoken


@pytest.mark.parametrize("notation, words", [
    ("3x + 5 = 20", "3 x plus 5 equals 20"),
    ("x^2", "x squared"),
    ("x²", "x squared"),
    ("x^3 - 1", "x cubed minus 1"),
    ("x^n", "x to the power of n"),
    ("(x + 1)^2", "x plus 1, all squared"),
    ("x/2", "x over 2"),
    ("1/2", "a half"),
    ("3/4", "three quarters"),
    ("(x + 1)/2", "x plus 1, over 2"),
    ("(x + 1)/(x - 1)", "x plus 1, over x minus 1"),
    ("sqrt(x + 1) = 3", "the square root of x plus 1, equals 3"),
    ("√(x+1)", "the square root of x plus 1"),
    ("2(x + 1)", "2 times x plus 1"),
    ("(x + 2)(x + 3)", "x plus 2 times x plus 3"),
    ("2 * 3", "2 times 3"),
    ("-2x > 4", "negative 2 x is greater than 4"),
    ("x <= -2", "x is less than or equal to negative 2"),
    ("2x − 3 ≤ 7", "2 x minus 3 is less than or equal to 7"),
    ("xy", "x y"),
    ("b^2 - 4ac", "b squared minus 4 a c"),
    ("x y", "x y"),
    ("3.5x", "3.5 x"),
])
def test_spoken(notation, words):
    assert spoken(notation) == words


def test_prose_keeps_its_words_and_speaks_the_notation():
    line = "Now subtract 5 from both sides: 3x + 5 = 20 becomes 3x = 15, and then x = 5."
    out = speakable_maths(line)
    assert out == ("Now subtract 5 from both sides: 3 x plus 5 equals 20 becomes 3 x equals 15, "
                   "and then x equals 5.")
    assert has_notation(line) and not has_notation(out)


def test_plain_numbers_and_lone_letters_are_not_notation():
    for s in ["We have 20 tickets and I bought 5.", "Take a look at option A.", "In 2026 the class read 3 books."]:
        assert speakable_maths(s) == s
        assert not has_notation(s)


def test_a_power_written_with_a_caret_inside_prose():
    assert speakable_maths("Remember that x^2 means x times x.") == "Remember that x squared means x times x."
    assert speakable_maths("So (x+2)(x+3) = 0.") == "So x plus 2 times x plus 3 equals 0."


def test_notation_is_spoken_in_the_lesson_language():
    from maths.speech import speakable_maths, spoken
    assert spoken("x^2 + 3/4 = 1", "ar") == "x تربيع زائد ثلاثة أرباع يساوي 1"
    assert spoken("2x - 1 = 7", "hi") == "2 x घटा 1 बराबर 7"
    assert spoken("(x + 1)^2", "fr") == "x plus 1, le tout au carré"
    assert spoken("(x + 1)^2", "hi") == "x जमा 1, का वर्ग"
    assert spoken("x < 5", "mr") == "x, 5 पेक्षा लहान आहे"
    assert spoken("sqrt(x + 1)", "es") == "raíz cuadrada de x plus 1".replace("plus", "más")
    assert speakable_maths("अब 2x + 1 = 7 को हल करें।", "hi") == "अब 2 x जमा 1 बराबर 7 को हल करें।"
    assert spoken("x^2 + 1", "xx") == "x squared plus 1", "unknown language -> English"


def test_every_language_has_every_word():
    from maths.i18n import LANGS, WORDS, BOARD, words_for
    for lang in LANGS:
        w = words_for(lang)
        for key in ("plus", "minus", "times", "neg", "frac", "sq", "cube", "pow", "rel_eq", "rel_lt",
                    "sqrt", "func", "lhs", "rhs"):
            assert key in w and "{" not in w[key].replace("{x}", "").replace("{b}", "").replace("{e}", "") \
                .replace("{n}", "").replace("{d}", "").replace("{l}", "").replace("{r}", "").replace("{f}", ""), (lang, key)
        for key, table in BOARD.items():
            assert table.get(lang), (lang, key)
    assert set(WORDS) == set(LANGS)
