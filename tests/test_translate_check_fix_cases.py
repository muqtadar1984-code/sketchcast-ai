"""The first Fix Agent run (2026-10-22) left 17 translations needing the
founder. Every one traced to the check or the reply reader, with the same
English failing in every language — the strings below are the production
ones:

  * a one-letter conjunction read as a variable — Spanish "y"/"o",
    Portuguese "e": "A(0, 3) y B(4, 11)", "(x + p) e (x + q)", "Grupo 0 (o
    Grupo 18)" (Coordinates, Gradient, Quadratic, Periodic Table);
  * thousands grouped with a point or a space — "300.000", "1 500",
    "1 000 Pascal" (Sound Waves in three languages; Pressure, Portuguese);
  * a "1" for an English article or "another"/"each" — "1 meter",
    "1 lagi", "setiap 1" (Malay) — and for a spelled ordinal, "the first
    power" → "la puissance 1" (French);
  * a reply that is the bare list, not {"items": [...]}, and a batch too
    long for one reply (Hindi).

What is pinned: each passes; the same shapes with a REAL error still block
(a changed coordinate, a changed value, a variable next to an operator, a
"1" that is maths rather than a determiner).

Run: .venv/Scripts/python.exe -m pytest tests/test_translate_check_fix_cases.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json

import pytest

from catalogue import translate as T
from catalogue import translate_check as C


class TestOneLetterConjunctions:
    COORDS = "A line passes through the points A(0, 3) and B(4, 11). Find the equation of this straight line."
    POINT = ("A line passes through the point (1, 2) and has a gradient of 3. Find its y-intercept and write the "
             "full equation of the line.")
    BINOMIALS = "Multiplying two linear binomials of the form (x + p) and (x + q) always yields a quadratic expression."
    GROUP = "Which statement correctly describes the reactivity of noble gases found in Group 0 (or Group 18)?"

    @pytest.mark.parametrize("target", [
        "Una recta pasa por los puntos A(0, 3) y B(4, 11). Halla la ecuación de esta recta.",
        "Uma reta passa pelos pontos A(0, 3) e B(4, 11). Encontra a equação desta reta.",
    ])
    def test_and_between_two_points(self, target):
        ok, lost = C.prose_maths_equivalent(self.COORDS, target)
        assert ok, lost

    @pytest.mark.parametrize("target", [
        "Una recta pasa por el punto (1, 2) y tiene una pendiente de 3. Halla su ordenada al origen y escribe la "
        "ecuación completa de la recta.",
        "Uma reta passa pelo ponto (1, 2) e tem declive 3. Encontre a sua ordenada na origem e escreva a equação "
        "completa da reta.",
    ])
    def test_and_after_a_point(self, target):
        ok, lost = C.prose_maths_equivalent(self.POINT, target)
        assert ok, lost

    @pytest.mark.parametrize("target", [
        "Multiplicar dos binomios lineales de la forma (x + p) y (x + q) siempre da una expresión cuadrática.",
        "Multiplicar dois binómios lineares da forma (x + p) e (x + q) dá sempre uma expressão quadrática.",
    ])
    def test_and_between_two_brackets(self, target):
        ok, lost = C.prose_maths_equivalent(self.BINOMIALS, target)
        assert ok, lost

    def test_or_opening_a_bracket(self):
        ok, lost = C.prose_maths_equivalent(
            self.GROUP, "¿Qué afirmación describe correctamente la reactividad de los gases nobles del Grupo 0 (o Grupo 18)?")
        assert ok, lost

    def test_a_changed_coordinate_still_blocks(self):
        ok, _ = C.prose_maths_equivalent(self.COORDS, "Una recta pasa por los puntos A(0, 3) y B(4, 12). Halla la ecuación.")
        assert not ok

    def test_a_variable_next_to_an_operator_is_still_maths(self):
        assert C.maths_spans("Halla x + y = 7") == ["x + y = 7"]
        assert C.prose_maths_equivalent("If y = 3 and x = 2, find z.", "Si y = 3 y x = 2, halla z.")[0]
        assert not C.prose_maths_equivalent("If y = 3, find x.", "Si y = 4, halla x.")[0]

    def test_an_accented_word_is_not_a_variable(self):
        # "dá" (gives) is a word: no "d" glued onto "(x + q)"
        assert C.maths_spans("(x + q) dá sempre") == ["x + q"]
        assert C.maths_spans("é 3 e não 4") == ["3", "4"]


class TestGroupedThousands:
    @pytest.mark.parametrize("target", ["300.000 kilómetros por segundo", "300 000 kilomètres par seconde",
                                        "300 000 kilomètres par seconde"])
    def test_a_point_or_a_space_groups_thousands(self, target):
        ok, missing, extra = C.numbers_equivalent("300,000 kilometres per second", target)
        assert ok, (missing, extra)

    def test_the_sound_waves_item_in_french(self):
        src = "330 metres per second 343 metres per second 300,000 kilometres per second 1,500 metres per second"
        dst = "330 mètres par seconde 343 mètres par seconde 300 000 kilomètres par seconde 1 500 mètres par seconde"
        ok, missing, extra = C.numbers_equivalent(src, dst)
        assert ok, (missing, extra)

    def test_a_spelled_thousand_written_with_a_space(self):
        ok, missing, extra = C.numbers_equivalent(
            "Wait, is one pascal a lot of pressure? So in real life, we often use kilopascals, where one kilopascal "
            "is equal to one thousand pascals.",
            "Espere, 1 Pascal é muita pressão? Por isso, na vida real, usamos muitas vezes quilopascais, em que "
            "1 quilopascal é igual a 1 000 Pascal.")
        assert ok, (missing, extra)

    def test_a_group_is_read_whole_only_for_a_value_the_english_states(self):
        # "1 500" read whole only because the English says 1,500 — 1,400 never joins
        assert not C.numbers_equivalent("about 1,500 metres per second", "1 400 mètres par seconde")[0]
        assert not C.numbers_equivalent("about 1,500 metres per second", "1.400 metros por segundo")[0]
        # a group the English does not state stays as written: "1 500" for 15 is two numbers
        assert not C.numbers_equivalent("15 metres", "1 500 mètres")[0]
        assert C.numbers_equivalent("0.125 litres", "0.125 litros")[0]


class TestAOneThatIsAWord:
    def test_malay_determiners(self):
        src = ("Imagine a square plot of land of side length a plus b. If we divide it into sub-plots, we get one "
               "square of area a squared, another square of area b squared, and two rectangular gardens, each with "
               "an area of a times b.")
        dst = ("Bayangkan sebidang tanah segi empat sama dengan panjang sisi a tambah b. Jika kita membahagikannya "
               "kepada sub-petak, kita mendapat 1 segi empat sama dengan luas a kuasa dua, 1 lagi segi empat sama "
               "dengan luas b kuasa dua, dan 2 kebun segi empat tepat, setiap 1 dengan luas a darab b.")
        ok, missing, extra = C.numbers_equivalent(src, dst)
        assert ok, (missing, extra)

    def test_a_metre(self):
        ok, missing, extra = C.numbers_equivalent(
            "Is it just dividing by one hundred because a metre has one hundred centimetres? A square metre is a "
            "grid of one hundred centimetres by one hundred centimetres.",
            "Adakah ia hanya dibahagi dengan 100 kerana 1 meter ada 100 sentimeter? 1 meter persegi ialah grid "
            "100 sentimeter darab 100 sentimeter.")
        assert ok, (missing, extra)

    def test_a_spelled_ordinal(self):
        ok, missing, extra = C.numbers_equivalent("Terms that involve different variables raised to the first power",
                                                  "Des termes avec des variables différentes élevées à la puissance 1")
        assert ok, (missing, extra)

    def test_a_one_that_is_maths_still_blocks(self):
        # no word follows the 1: it is a value, and the English has none
        assert not C.numbers_equivalent("Solve a linear equation.", "Résoudre une équation où x = 1.")[0]
        # more determiner 1s than English one-words
        assert not C.numbers_equivalent("Take the cube.", "Ambil 1 kiub dan 1 bola.")[0]
        # a changed number is never absorbed
        assert not C.numbers_equivalent("a metre has one hundred centimetres", "1 meter ada 1000 sentimeter")[0]


class _Client:
    """A model that answers with whatever ``reply`` makes of the payload."""

    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def analyze(self, prompt, **_k):
        payload = json.loads(prompt[prompt.rindex("INPUT:") + len("INPUT:"):].strip())
        self.calls.append(len(payload))
        return {"data": self.reply(payload)}


class TestTheReply:
    def test_a_bare_list_is_the_answer(self):
        c = _Client(lambda p: [f"t:{s}" for s in p])
        assert T.translate_strings(c, ["one", "two"], "hi") == ["t:one", "t:two"]
        assert c.calls == [2]

    def test_a_batch_too_long_for_one_reply_is_asked_in_halves(self):
        # the model cuts off any reply longer than 3 strings
        c = _Client(lambda p: {"items": [f"t:{s}" for s in p]} if len(p) <= 3 else {"raw_text": "…cut"})
        src = [f"s{i}" for i in range(7)]
        assert T.translate_strings(c, src, "hi") == [f"t:{s}" for s in src]
        assert c.calls[0] == 7 and min(c.calls) <= 3, "asked whole first, then in halves"

    def test_a_single_string_that_never_answers_still_raises(self):
        c = _Client(lambda p: {"raw_text": "nothing"})
        with pytest.raises(RuntimeError, match="did not return 1 items"):
            T.translate_strings(c, ["only"], "hi")

    def test_the_rules_say_an_article_is_not_a_number(self):
        c = _Client(lambda p: {"items": list(p)})
        prompts = []
        c.analyze, inner = (lambda prompt, **k: prompts.append(prompt) or inner(prompt, **k)), c.analyze
        T.translate_strings(c, ["a metre"], "ms")
        assert "never as the digit 1" in prompts[0] and "never add a number" in prompts[0]
        note = T.retry_note({"blocking": [{"where": "s006", "what": "numbers differ", "extra": ["1"]}]})
        assert "never turn an English article" in note


class TestJawiOrdinal:
    """Jawi writes the Malay ordinal "ke-50" as "كى-50" (or "کى-50"): the
    hyphen joins the prefix to the number and is no minus (Jawi Sequences
    kit, Fix Agent case 2026-10-23)."""

    MS = "Bagi jujukan dengan sebutan ke-n 6n - 5, cari sebutan ke-50."

    @pytest.mark.parametrize("target", [
        "باݢي جوجوكن دڠن سبوتن كى-n 6n - 5، چاري سبوتن كى-50.",
        "باݢي جوجوکن دڠن سبوتن کى-n 6n - 5، چاري سبوتن کى-50.",
        "باݢي جوجوکن دڠن سبوتن کي-n 6n - 5، چاري سبوتن کی-50.",
    ])
    def test_the_jawi_prefix_is_no_minus(self, target):
        ok, lost = C.prose_maths_equivalent(self.MS, target)
        assert ok, lost
        assert "-50" not in C.number_values(target) and "50" in C.number_values(target)

    def test_the_hundredth_term(self):
        ok, missing, extra = C.numbers_equivalent("Cari sebutan ke-100 bagi jujukan dengan formula am t = 5n - 8",
                                                  "چاري سبوتن كى-100 باݢي جوجوكن دڠن فورمولا ام t = 5n - 8")
        assert ok, (missing, extra)

    def test_a_real_minus_in_jawi_still_counts(self):
        # a minus after a space, or after a number, is still a minus
        assert C.number_values("x = -50") == ["-50"]
        assert not C.numbers_equivalent("t = 5n - 8", "t = 5n - 9")[0]
        assert not C.prose_maths_equivalent(self.MS, "باݢي جوجوكن دڠن سبوتن كى-n 6n - 5، چاري سبوتن كى-60.")[0]
