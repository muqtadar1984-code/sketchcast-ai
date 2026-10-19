"""Urdu as a lesson language (Kit Translator plan §5.10, 2026-10-19).

What is pinned:
  * the registry: ``ur`` is right-to-left, Arabic script, routed to Claude
    like Arabic, and has its own prompt directive naming the Urdu letters;
  * the voices: a free Edge pair (ur-PK) and the premium Chirp 3 HD pair
    plus the student pair (ur-IN, read from the live voices.list), so a
    translated Urdu dialogue has two premium voices and no Edge fallback;
  * the Urdu full stop ۔ ends a sentence for per-sentence synthesis (Chirp
    word timing interpolates within a sentence — an unsplit paragraph would
    smear every word);
  * every localised string table carries Urdu for every key, and Urdu
    papers enumerate (الف) (ب) (ج) (د);
  * documents treat Urdu as right-to-left with Urdu fonts.

Run: .venv/Scripts/python.exe -m pytest tests/test_urdu_language.py -q -p no:cacheprovider
"""
from __future__ import annotations

import re

import agent5_slides.deck_strings as deck
import docgen.docx_builder as dx
import docgen.strings as ds
import maths.i18n as mi
import shared.outro as outro
from shared.languages import LANGUAGES, get_language, is_rtl, prompt_directive, script_of
from shared.model_routing import _ARABIC_SCRIPT
from shared.tts.chunks import _SENT_RE
from shared.tts.registry import VOICES, default_voice_id_for, get_voice, student_voice_id_for

# Arabic letters an Urdu text must not use in place of the Urdu ones
_ARABIC_SUBSTITUTES = re.compile("[كيه]")


class TestRegistry:
    def test_urdu_is_an_rtl_arabic_script_language(self):
        ur = get_language("ur")
        assert ur.code == "ur" and ur.native == "اردو" and ur.direction == "rtl"
        assert is_rtl("ur") and script_of("ur") == "arab"
        assert "ur" in _ARABIC_SCRIPT, "routed to the Arabic-script model, as Arabic is"

    def test_its_prompt_names_the_urdu_letters_and_the_urdu_full_stop(self):
        p = prompt_directive("ur")
        assert "Urdu" in p and "ک (not ك)" in p and "۔" in p
        assert prompt_directive("ar") != p, "Arabic keeps its own directive"


class TestVoices:
    def test_the_free_pair_is_pakistani_urdu(self):
        assert LANGUAGES["ur"].default_voice == "edge-uzma"
        assert default_voice_id_for("ur") == "edge-uzma"
        assert default_voice_id_for("ur", gender="m") == "edge-asad"
        assert get_voice("edge-uzma").ref == "ur-PK-UzmaNeural"

    def test_the_premium_teacher_and_student_pairs_are_chirp_3_hd(self):
        assert get_voice("g-ur-f").ref == "ur-IN-Chirp3-HD-Achernar"
        assert get_voice("g-ur-m").ref == "ur-IN-Chirp3-HD-Achird" and get_voice("g-ur-m").gender == "m"
        assert student_voice_id_for("ur", "f") == "g-ur-student-f"
        assert student_voice_id_for("ur", "m") == "g-ur-student-m"
        assert all(v.role == "student" for v in VOICES if v.voice_id.startswith("g-ur-student"))

    def test_the_kit_voice_pair_names_exist(self):
        # app voicePairFor: teacher g-<lang>-<f|m>, student the other gender's g-<lang>-student-<m|f>
        for teacher, student in (("g-ur-f", "g-ur-student-m"), ("g-ur-m", "g-ur-student-f")):
            assert get_voice(teacher) and get_voice(student)


class TestSentences:
    def test_the_urdu_full_stop_ends_a_sentence(self):
        parts = _SENT_RE.split("یہ پہلا جملہ ہے۔ یہ دوسرا ہے۔ کیا یہ تیسرا ہے؟ ہاں")
        assert parts == ["یہ پہلا جملہ ہے۔", "یہ دوسرا ہے۔", "کیا یہ تیسرا ہے؟", "ہاں"]


class TestStrings:
    def test_every_table_has_urdu_for_every_key(self):
        for name, table in (("maths BOARD", mi.BOARD), ("maths REASONS", mi.REASONS),
                            ("docgen", ds._STRINGS), ("deck", deck._STRINGS), ("outro", outro.STRINGS)):
            missing = [k for k, row in table.items() if not (row.get("ur") or "").strip()]
            assert not missing, (name, missing)
        assert set(mi.WORDS["ur"]) >= set(mi.WORDS["ar"]), "every spoken-notation key Arabic has"

    def test_urdu_strings_use_urdu_letters_and_keep_their_placeholders(self):
        for table in (mi.BOARD, mi.REASONS, ds._STRINGS, deck._STRINGS, outro.STRINGS):
            for key, row in table.items():
                ur, en = row["ur"], row["en"]
                assert not _ARABIC_SUBSTITUTES.search(ur), (key, ur)
                assert set(re.findall(r"{\w+}", ur)) == set(re.findall(r"{\w+}", en)), (key, ur)

    def test_lookups_return_urdu(self):
        assert mi.board_text("today", "ur", topic="خلیہ") == "آج ہم خلیہ سیکھیں گے۔"
        assert mi.norm_lang("ur") == "ur" and "ur" in mi.LANGS and "ur" in deck.LANGS
        assert deck.T("ur", "worked_example_n", n=2) == "حل شدہ مثال 2"
        assert ds._t("answer_key", "ur") == "جوابی کلید"
        assert "sketchcast.app" in outro.STRINGS["cta_2"]["ur"]


class TestDocuments:
    def test_urdu_documents_are_rtl_and_enumerate_alif_be_jim_dal(self):
        assert "ur" in ds.RTL_LANGS
        assert ds.letters("ur")[:4] == ["الف", "ب", "ج", "د"]
        assert len(ds.letters("ur")) >= 26, "enough for the longest match table"
        assert ds.letters("ar")[0] == "أ", "Arabic keeps its own sequence"

    def test_urdu_has_complex_script_fonts(self):
        assert dx._CS_FONTS["_default"]["ur"] == "Arial"
        assert dx._CS_FONTS["exam_board"]["ur"] == "Urdu Typesetting"
