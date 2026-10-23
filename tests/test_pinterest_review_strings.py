"""Regression for review finding 5: a card's fixed words exist in the pin's
language or the card is refused, and the copy rules also run over the words
drawn ON the card (title, points, labels)."""

from __future__ import annotations

import pytest
from PIL import Image

from catalogue.pinterest import common as C
from catalogue.pinterest import draft as D
from catalogue.pinterest import precheck as PC
from catalogue.pinterest import templates as T
from tests.pinterest_fakes import TOPIC, FakeCopyClient, good_copy, world

PREVIEW = Image.new("RGB", (640, 360), "white")
ALL_LANGUAGES = ("en", "ar", "ms", "ms-arab", "hi", "mr", "te", "ur", "fr", "es", "pt")


@pytest.mark.parametrize("language", ALL_LANGUAGES)
def test_every_template_string_exists_for_every_lesson_language(language):
    for template in T.TEMPLATES:
        assert T.missing_strings(template, language) == [], (template, language)


def test_the_default_settings_languages_are_covered():
    for language in C.languages(C.DEFAULTS):
        assert all(not T.missing_strings(t, language) for t in T.TEMPLATES)


@pytest.mark.parametrize("language,script", [("fr", "latin"), ("ar", "arabic"), ("ms-arab", "arabic"),
                                             ("te", "telugu"), ("ur", "arabic")])
def test_the_worksheet_band_is_in_the_pins_language_never_english(language, script, monkeypatch):
    monkeypatch.setenv("TEXT_SHAPING_REQUIRED", "0")
    band = T.string("free_worksheet", language)
    assert band != T.STRINGS["free_worksheet"]["en"]
    assert PC.script_share(band).get(script, 0) > 0.6
    _img, meta = T.render("worksheet", T.Inputs("Titre", subject="Biology", preview=PREVIEW), language)
    assert not any(b["name"] == "subject" for b in meta["text_boxes"]), "the English subject name is not drawn"


def test_a_language_without_the_words_is_refused_not_drawn_in_english():
    with pytest.raises(T.StringMissing):
        T.render("worksheet", T.Inputs("Zellen", preview=PREVIEW), "de")
    assert T.missing_strings("worksheet", "de") == ["free_worksheet"]
    assert any("no de text" in b for b in PC.image_text_checks([], template="worksheet", language="de",
                                                                source_text=""))
    # a layout with no fixed words draws in any language
    T.render("explainer", T.Inputs("Zellen", ["Eins zwei", "Drei vier", "Funf sechs"], subject="biology"), "de")


SOURCE = PC.article_text({"title": "Cells", "sections": [{"body_md": "A cell has 3 parts."}]})


@pytest.mark.parametrize("texts,needle", [
    (["Cells for kids", "The 3 parts"], "children as the audience"),
    (["Cells", "Over 40 parts"], "number(s) 40"),
    (["الخلايا", "أجزاء الخلية"], "not in the pin's language"),
])
def test_the_copy_rules_run_over_the_image_text(texts, needle):
    assert any(needle in b for b in PC.image_text_checks(texts, template="explainer", language="en",
                                                         source_text=SOURCE))


def test_clean_image_text_passes():
    assert PC.image_text_checks(["Plant cells", "The 3 parts of a cell"], template="explainer", language="en",
                                source_text=SOURCE) == []


def test_a_draft_whose_card_breaks_a_rule_fails_and_no_copy_retry_can_cure_it(monkeypatch):
    monkeypatch.setattr(C, "builders_queued", lambda sb, exclude_job_id=None: False)
    monkeypatch.setenv("CATALOGUE_WINDOW_UTC", "always")
    sb = world()
    sb.tables["topic_articles"][0]["objectives"][0]["text"] = "Teach kids the parts of a plant cell"
    client = FakeCopyClient(good_copy)
    D.draft_topic(sb, "job-1", {"topic_id": TOPIC, "language": "en"}, client=client, link_check=lambda u: True)
    explainer = next(p for p in sb.pins() if p["template"] == "explainer")
    assert explainer["status"] == "failed" and any("card mentions children" in b
                                                   for b in explainer["precheck"]["blocking"])
    assert len(client.prompts) == 1, "an image problem is not sent back to the copy model"
    assert next(p for p in sb.pins() if p["template"] == "diagram")["status"] == "in_review"
