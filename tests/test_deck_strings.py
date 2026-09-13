"""The deck's furniture speaks the lesson's language.

The first Arabic, Hindi and Telugu decks carried every heading the storyboard
writes itself in English. These pin the table and the two routes into it.
"""

from __future__ import annotations

from agent5_slides import deck_generator as dg
from agent5_slides.deck_strings import LANGS, T, _STRINGS
from agent5_slides.deck_storyboard import storyboard
from tests.test_deck_routes import _video_script

JAWI_HOUSE = set("ڤݢڠچڽ")


class TestTheTable:
    def test_every_key_has_every_language(self):
        for key, row in _STRINGS.items():
            missing = [l for l in LANGS if not row.get(l)]
            assert not missing, f"{key}: {missing}"

    def test_english_is_the_fallback_for_an_unknown_code(self):
        assert T("xx", "by_end") == T("en", "by_end")
        assert T(None, "ready") == "Ready to teach."

    def test_the_document_keys_are_reused_not_retranslated(self):
        from docgen.strings import _t
        assert T("ar", "objectives") == _t("learning_objectives", "ar")
        assert T("hi", "term") == _t("term", "hi")

    def test_jawi_is_arabic_script_not_latin(self):
        for key, row in _STRINGS.items():
            text = row["ms-arab"].replace("{n}", "")      # the format placeholder is not Jawi
            letters = [c for c in text if c.isalpha()]
            assert letters and all(ord(c) > 0x0600 for c in letters), f"{key}: {text}"

    def test_digits_stay_western_in_every_language(self):
        for lang in LANGS:
            assert T(lang, "worked_example_n", n=3).count("3") == 1

    def test_arabic_and_jawi_differ(self):
        """Jawi is MALAY in Arabic script, never Arabic vocabulary."""
        assert _STRINGS["check"]["ar"] != _STRINGS["check"]["ms-arab"]


class TestItReachesTheSlides:
    def test_an_arabic_lesson_gets_arabic_chrome(self):
        model = dg.model_from_script({}, _video_script(), {"objectives": ["هدف"]}, language="ar")
        slides = storyboard(model)
        obj = next(s for s in slides if s.kind == "objectives")
        assert obj.heading == T("ar", "by_end") and obj.kicker == T("ar", "objectives")
        quiz = next(s for s in slides if s.kind == "quiz")
        assert quiz.kicker == T("ar", "check") and quiz.notes.startswith(T("ar", "answer"))
        closing = slides[-1]
        assert closing.heading == T("ar", "ready") and closing.label == T("ar", "notes_line")

    def test_the_language_comes_from_the_script_when_not_given(self):
        script = {**_video_script(), "language": "hi"}
        model = dg.model_from_script({}, script)
        assert model.language == "hi"
        assert next(s for s in storyboard(model) if s.kind == "takeaways").kicker == T("hi", "remember")

    def test_an_article_carries_its_own_language(self):
        model = dg.from_article({"id": "a", "title": "t", "language": "te", "sections": [], "claims": []}, [])
        assert model.language == "te"

    def test_a_continued_page_says_so_in_the_lesson_language(self):
        # Prose is distilled to points now; a long LIST still paginates.
        model = dg.model_from_script({}, _video_script(), language="hi")
        model.sections[0].body_md = "\n".join("- इस पाठ में एक बात" for _ in range(60))
        secs = [s for s in storyboard(model) if s.kind == "section" and s.section_id == "s001"]
        assert len(secs) > 1
        assert secs[1].heading.endswith(T("hi", "continued"))
        assert not secs[0].heading.endswith(T("hi", "continued"))
