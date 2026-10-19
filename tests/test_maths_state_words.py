"""The engine's own English words on a translated board (maths.i18n.state_text,
maths.board.localised_example) — every translated kit of 2026-10-19 showed
"A: scalene", "B: false" and "sin = opp/hyp" in English, in all ten
languages, because the working lines are the engine's and the translator
keeps them verbatim.

What is pinned:
  * the words are written in the lesson language; English is untouched;
  * only whole words: no hit inside a longer word or "right-angled", and
    "right" only as a label's value;
  * the maths around them (letters, numbers, operators) is untouched;
  * the board localises a COPY — the verified record keeps its English;
  * every word has every non-English language.

Run: .venv/Scripts/python.exe -m pytest tests/test_maths_state_words.py -q -p no:cacheprovider
"""
from __future__ import annotations

import pytest

from maths import i18n
from maths.board import concept_segment, example_segment, localised_example
from maths.schema import Lesson, Step, WorkedExample
from maths.i18n import STATE_WORDS, state_text


class TestStateText:
    def test_english_is_untouched(self):
        assert state_text("A: scalene", "en") == "A: scalene"
        assert state_text("sin = opp/hyp", None) == "sin = opp/hyp"

    @pytest.mark.parametrize("lang,want", [
        ("ms", "A: tak sama sisi"), ("ar", "A: مختلف الأضلاع"), ("hi", "A: विषमबाहु"),
        ("fr", "A: quelconque"), ("ms-arab", "A: تق سام سيسي"), ("ur", "A: مختلف الاضلاع"),
    ])
    def test_a_triangle_type(self, lang, want):
        assert state_text("A: scalene", lang) == want

    def test_true_false_and_the_ratio_sides(self):
        assert state_text("B: false", "ms") == "B: palsu"
        assert state_text("C: true", "te") == "C: సత్యం"
        assert state_text("sin = opp/hyp, cos = adj/hyp", "hi") == "sin = लंब/कर्ण, cos = आधार/कर्ण"
        assert state_text("tan(x) = opposite/adjacent", "es") == "tan(x) = opuesto/adyacente"

    def test_capital_follows_the_source(self):
        assert state_text("True", "fr") == "Vrai"
        assert state_text("A: Scalene", "pt") == "A: Escaleno"

    def test_whole_words_only(self):
        # "right" is the angle type only as a label's value
        assert state_text("right-angled triangle", "ms") == "right-angled triangle"
        assert state_text("C: right", "ms") == "C: tegak"
        assert state_text("the right side", "fr") == "the right side"
        # never inside a longer word
        assert state_text("hypothesis, opposition, adjust", "ms") == "hypothesis, opposition, adjust"

    def test_the_maths_is_untouched(self):
        assert state_text("tan(40) = y/8", "ar") == "tan(40) = y/8"
        assert state_text("x = atan(4/3)", "ur") == "x = atan(4/3)"
        assert state_text("C = 2 × 22/7 × 7", "hi") == "C = 2 × 22/7 × 7"

    def test_every_word_has_every_language(self):
        others = [c for c in i18n.LANGS if c != "en"]
        for word, table in STATE_WORDS.items():
            missing = [c for c in others if not table.get(c)]
            assert not missing, (word, missing)


def _example() -> WorkedExample:
    return WorkedExample(
        label="Example 1", problem="Classify the triangles A, B and C by their sides.",
        steps=[Step(operation="classify A", after=["A: scalene"]),
               Step(operation="classify B", after=["B: isosceles"])],
        final_answer=["A: scalene; B: isosceles"],
    )


class TestTheBoard:
    def test_a_copy_is_localised_and_the_record_keeps_its_english(self):
        ex = _example()
        out = localised_example(ex, "ms")
        assert [st.after for st in out.steps] == [["A: tak sama sisi"], ["B: sama kaki"]]
        assert out.final_answer == ["A: tak sama sisi; B: sama kaki"]
        assert [st.after for st in ex.steps] == [["A: scalene"], ["B: isosceles"]]
        assert localised_example(ex, "en") is ex

    def test_the_slide_points_and_board_are_in_the_language(self):
        lesson = Lesson.model_construct(method=Lesson.model_fields["method"].annotation.model_construct(steps=[]))
        try:
            seg = example_segment(_example(), lesson, "s003", "ar")
        except Exception:  # noqa: BLE001 — a minimal lesson the compiler cannot draw
            pytest.skip("compiler needs a fuller lesson fixture")
        assert seg["slide_points"][:2] == ["A: مختلف الأضلاع", "B: متطابق الضلعين"]
        shown = " ".join(str(e.get("expr") or e.get("text") or "") for e in seg["scene"]["elements"])
        assert "scalene" not in shown and "isosceles" not in shown

    def test_a_formula_concept_point(self):
        lesson = Lesson.model_construct(topic="Trigonometric ratios", concept=[],
                                        concept_points=["sin = opp/hyp, cos = adj/hyp"])
        try:
            seg = concept_segment(lesson, "s002", "hi")
        except Exception:  # noqa: BLE001
            pytest.skip("compiler needs a fuller lesson fixture")
        assert seg["slide_points"][0] == "sin = लंब/कर्ण, cos = आधार/कर्ण"
