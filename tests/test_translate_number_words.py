"""Four Arabic kits failed in a row on 2026-10-20 (the Auto Translate Agent
paused itself): Sonnet 5.5 writes natural Arabic — "ثلاثي الأبعاد" for "3D",
"بروتون واحد" for "1 proton", an ordinal word for "the 50th term" — and
pre-check A read each as a lost number. The second ask was blind and wrote
the same thing.

What is pinned:
  * a dimension ("2D", "3D", "3-D") is a word, not a value, on both sides;
  * every other number still counts — a lost 7 still blocks;
  * the translator is told to write 1, 2 and ordinals as digits;
  * the second ask is told what the first one dropped (retry_note).

Run: .venv/Scripts/python.exe -m pytest tests/test_translate_number_words.py -q -p no:cacheprovider
"""
from __future__ import annotations

from catalogue import translate as T
from catalogue import translate_check as C


class TestDimensionsAreWords:
    def test_3d_and_2d_are_not_values(self):
        assert C.number_values("complex 3D surfaces and flat 2D shapes, a 3-D solid") == []

    def test_a_translation_that_writes_the_word_is_equivalent(self):
        ok, missing, extra = C.numbers_equivalent("all these 3D shapes", "كل هذه الأشكال ثلاثية الأبعاد")
        assert ok, (missing, extra)

    def test_real_numbers_still_count(self):
        assert C.number_values("a 3D box 4 cm wide") == ["4"]
        ok, missing, _ = C.numbers_equivalent("7 valence electrons", "إلكترونات تكافؤ")
        assert not ok and missing == ["7"]
        # a code or a word with a digit is not a dimension
        assert C.number_values("12D") == ["12"]


class TestTheTranslatorIsTold:
    def test_the_rule_names_1_2_and_ordinals(self):
        import inspect
        src = inspect.getsource(T.translate_strings)
        assert "1 and 2 included" in src and "ordinal keeps its digits" in src

    def test_retry_note_names_the_fields_and_the_numbers(self):
        report = {"ok": False, "blocking": [
            {"where": "hook[0].line", "what": "numbers differ", "missing": ["1"]},
            {"where": "s011", "what": "numbers differ", "missing": ["8", "7"], "extra": ["9"]},
        ]}
        note = T.retry_note(report)
        assert "hook[0].line: numbers differ (dropped 1)" in note
        assert "s011: numbers differ (dropped 8, 7; added 9)" in note
        assert "DIGITS" in note

    def test_no_findings_no_note(self):
        assert T.retry_note({"ok": True, "blocking": []}) == ""
