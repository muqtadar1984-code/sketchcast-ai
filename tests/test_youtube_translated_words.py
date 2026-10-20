"""The YouTube title and description of a TRANSLATED video follow the English
structure, in the video's language (founder, 2026-10-17, after the first
Arabic post went up with an Arabic title over an English description):

  * youtube_meta.WORDS — the fixed words (block labels, the ask, the
    SketchCast line, the link prefix, the part pointer) per language, English
    the fallback; the audience tag is never translated;
  * the model writes the title, intro, key terms and hashtags in the kit's
    language, headed by the translated article's title, guided by the
    translator's glossary; the words are stamped with their language;
  * the publish uses stored words only when they are in the kit's language
    (words written in English before the stamp existed are left off);
  * ``action: rewords`` on a topic_publish job rewrites the words of the
    parts already on the channel — the Cells video, posted before this.
"""

from __future__ import annotations

import json

import pytest

from catalogue import publish as P
from catalogue import youtube_backfill as B
from catalogue import youtube_enrich as E
from catalogue import youtube_meta as Y
from shared.outro import DESCRIPTION_CTA
from tests.catalogue_fakes import FakeYouTube
from tests.test_catalogue_publish import GEN, KIT, OWNER, _arabic, _job, _pubs, _sb
from tests.test_youtube_meta import BOARDS, FakeClient

AUDIENCE = "CBSE Class 9 & Cambridge Stage 7 Science"


@pytest.fixture(autouse=True)
def _publish_on(monkeypatch):
    """The publish suite's own autouse fixtures do not reach this module:
    Phase 4 on, no credentials in the environment, an empty builder queue."""
    monkeypatch.setenv(P.FEATURE_FLAG, "1")
    monkeypatch.delenv(P.AUDIT_FLAG, raising=False)
    for name in (P.CLIENT_ID_ENV, P.CLIENT_SECRET_ENV, "YOUTUBE_REFRESH_TOKEN_EN", "YOUTUBE_PLAYLISTS_EN",
                 "YOUTUBE_PLAYLISTS_AR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(P, "builders_are_queued", lambda sb: False)
AR_TITLE = "شرح الخلايا | النواة, الميتوكوندريا | " + AUDIENCE


# ── the words table ─────────────────────────────────────────────────────────


class TestWords:
    def test_every_language_row_carries_every_fixed_word_and_english_is_the_fallback(self):
        for lang, row in Y.WORDS.items():
            words = Y.words_for(lang)
            for key in ("explained", "aligned", "chapters", "key_terms", "part", "next", "full_kit", "sketchcast",
                        "cta", "fallback_intro", "more_lessons"):
                assert words.get(key), (lang, key)
            assert "{topic}" in row["explained"] and "{discipline}" in row["more_lessons"]
            assert "{part}" in row["part"] and "{total}" in row["part"]
        assert Y.words_for("en")["cta"] == DESCRIPTION_CTA
        assert Y.words_for("xx") == Y.words_for("en") == Y.words_for(None)
        assert Y.cta_line("ar") != DESCRIPTION_CTA and Y.sketchcast_line("ar") != Y.SKETCHCAST_LINE

    def test_the_anchors_recognise_every_languages_lines(self):
        for lang in Y.WORDS:
            assert Y.is_sketchcast_line("  " + Y.sketchcast_line(lang) + " extra")
            assert Y.is_kit_link_line(Y.kit_link_line("https://x", lang))
        assert not Y.is_sketchcast_line("A paragraph about cells.") and not Y.is_kit_link_line("Chapters")

    def test_hashtags_in_any_script_survive_cleaning(self):
        assert Y.clean_hashtags(["الخلايا", "#Class9Science", "cell wall", "123", "خلية نباتية"]) == \
            ["الخلايا", "Class9Science", "CellWall", "خليةنباتية"]
        assert Y.clean_hashtags(["कोशिका", "SketchCast"]) == ["कोशिका", "SketchCast"]


# ── the composers in another language ───────────────────────────────────────


class TestComposers:
    def test_the_headline_uses_the_languages_template(self):
        assert Y.headline("الخلايا", "ar") == "شرح الخلايا"
        assert Y.headline("Les cellules", "fr") == "Les cellules expliqué"
        assert Y.headline("Cells", "en") == "Cells Explained"
        assert Y.headline("Cells Compared", "en") == "Cells Compared"

    def test_the_title_keeps_the_latin_audience_block_and_a_localised_part_label(self):
        assert Y.compose_title("الخلايا", key_terms=["النواة", "الميتوكوندريا"], audience=AUDIENCE, language="ar") == AR_TITLE
        two = Y.compose_title("الخلايا", audience=AUDIENCE, part=2, total=3, language="ar")
        assert two.endswith(" — الجزء 2 من 3") and AUDIENCE in two
        assert Y.compose_title("Cells", audience=AUDIENCE, part=2, total=3, language="en").endswith(" — Part 2 of 3")

    def test_the_description_reads_in_the_language_with_no_english_fixed_line(self):
        d = Y.compose_description(topic_title="الخلايا", summary=None, meta={"intro": "ما هي الخلية؟"},
                                  header_lines=["Cambridge Lower Secondary Science 0893 · 7Bs.01"],
                                  chapter_lines_=["0:00 مقدمة", "1:00 النواة", "3:00 خلاصة"],
                                  key_terms=["النواة", "الغشاء"], hashtags=["الخلايا", "SketchCast"],
                                  part=1, total=2, next_title="شرح الخلايا — الجزء 2 من 2",
                                  link="https://sketchcast.app/topics/cells", language="ar")
        words = Y.words_for("ar")
        for key in ("aligned", "chapters", "sketchcast", "cta"):
            assert words[key] in d, key
        assert words["key_terms"] + "النواة, الغشاء." in d
        assert "الجزء 1 من 2. التالي: شرح الخلايا — الجزء 2 من 2" in d
        assert d.rstrip().endswith("#الخلايا #SketchCast")
        assert words["full_kit"] + "https://sketchcast.app/topics/cells" in d
        for english in ("Aligned to", "Chapters\n", "Key terms:", Y.SKETCHCAST_LINE, DESCRIPTION_CTA, "Full kit:"):
            assert english not in d, english

    def test_the_fallback_intro_is_in_the_language_when_there_is_no_summary_and_no_words(self):
        d = Y.compose_description(topic_title="الخلايا", summary=None, meta=None, header_lines=[], chapter_lines_=[],
                                  key_terms=[], hashtags=[], part=1, total=1, next_title=None, link="", language="ar")
        assert d.startswith("الخلايا — درس من سكتش كاست.")


# ── the model call ──────────────────────────────────────────────────────────


TOPIC_AR = {"id": "t1", "title": "الخلايا", "subject": "Science", "summary": None}


class TestGenerateInLanguage:
    def test_the_prompt_asks_for_the_language_the_shape_and_the_glossary(self):
        prompt = Y.build_prompt(TOPIC_AR, [], AUDIENCE, ["مقدمة"], "الخلية هي وحدة الحياة.", dialogue=True,
                                language="ar", glossary=[("Cell", "الخلية"), ("Nucleus", "النواة")])
        assert "Language: Arabic" in prompt and "شرح <Topic> |" in prompt
        assert "Cell = الخلية" in prompt and "Nucleus = النواة" in prompt
        assert "audience tag stays EXACTLY as given" in prompt
        english = Y.build_prompt({**TOPIC_AR, "title": "Cells"}, [], AUDIENCE, [], "n", dialogue=True)
        assert "Language:" not in english and "Plant vs Animal Cells Explained" in english

    def test_the_words_are_stamped_with_their_language_and_the_budget_grows_for_a_non_latin_script(self):
        client = FakeClient({"title": AR_TITLE, "intro": "ما هي الخلية؟", "key_terms": ["النواة"], "hashtags": ["الخلايا"]})
        seen = {}
        real = client.analyze

        def analyze(prompt, system=None, max_tokens=None, response_schema=None):
            seen["max_tokens"] = max_tokens
            return real(prompt, system=system, max_tokens=max_tokens, response_schema=response_schema)
        client.analyze = analyze
        meta = Y.generate_meta(client, TOPIC_AR, [], BOARDS, [], "n", language="ar")
        assert meta["language"] == "ar" and meta["title"] == AR_TITLE and meta["hashtags"] == ["الخلايا"]
        assert seen["max_tokens"] == 2400
        assert Y.meta_in_language(meta, "ar") and not Y.meta_in_language(meta, "en")
        assert not Y.meta_in_language({"title": "Cells Explained | …"}, "ar"), "an unstamped meta is English"
        assert Y.meta_in_language({"title": "Cells Explained | …"}, "en")

    def test_a_title_without_the_audience_is_recomposed_in_the_language(self):
        client = FakeClient({"title": "الخلايا!!!", "intro": "i", "key_terms": ["النواة"], "hashtags": ["A"]})
        meta = Y.generate_meta(client, TOPIC_AR, [], BOARDS, [], "n", language="ar")
        assert meta["title"] == "شرح الخلايا | النواة | " + AUDIENCE


class TestWriteForTranslatedKit:
    def test_the_prompt_is_headed_by_the_translated_article_title_and_carries_the_glossary(self):
        sb = _arabic(_sb(parts=(1,)))
        kit = sb.tables["topic_kits"][0]
        kit["translation"] = {"glossary": {"pairs": [["Cell", "الخلية"], ["Nucleus", "النواة"]]}}
        client = FakeClient({"title": AR_TITLE, "intro": "ما هي الخلية؟", "key_terms": ["النواة"], "hashtags": ["الخلايا"]})
        meta = Y.write_for_kit(sb, kit, [{"part": 1, "chapters": [{"t": 0, "label": "مقدمة"}], "narration": "الخلية هي وحدة الحياة."}],
                               client_factory=lambda: client)
        assert meta["language"] == "ar" and sb.tables["topic_kits"][0]["youtube_meta"]["language"] == "ar"
        prompt = client.prompts[0]
        assert "Topic: الخلايا" in prompt and "Language: Arabic" in prompt and "Nucleus = النواة" in prompt
        assert "Every living thing" not in prompt, "the English summary stays off the prompt"


# ── the publish ─────────────────────────────────────────────────────────────


def _arabic_meta() -> dict:
    return {"title": AR_TITLE, "intro": "ما هي الخلية؟ يعمل معلم وطالبة على ذلك.", "key_terms": ["النواة", "الميتوكوندريا"],
            "hashtags": ["الخلايا", "Class9Science", "SketchCast"], "source": "generated", "language": "ar"}


class TestTranslatedPublish:
    def test_words_written_in_the_kits_language_are_used(self, monkeypatch):
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN_AR", "token-present")
        sb = _arabic(_sb(parts=(1,)))
        sb.tables["topic_kits"][0]["youtube_meta"] = _arabic_meta()
        yt = FakeYouTube()
        summary = P.run_publish_job(sb, _job(), transport=yt)
        assert summary["published"] == [1]
        up = yt.uploads[0]
        assert up["title"] == AR_TITLE
        assert up["description"].startswith("ما هي الخلية؟")
        assert Y.words_for("ar")["chapters"] in up["description"] and "0:00" in up["description"]
        assert Y.words_for("ar")["sketchcast"] in up["description"]
        assert "#الخلايا" in up["description"] and "Aligned to" not in up["description"]
        assert "النواة" in up["tags"], "the key terms reach the tags field"

    def test_words_written_in_english_before_the_stamp_are_left_off_an_arabic_video(self, monkeypatch):
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN_AR", "token-present")
        sb = _arabic(_sb(parts=(1,)))
        sb.tables["topic_kits"][0]["youtube_meta"] = {"title": "Cells Explained | Nucleus | Cambridge Stage 7 Biology",
                                                      "intro": "What is a cell?", "key_terms": ["nucleus"],
                                                      "hashtags": ["Cells"], "source": "generated"}
        yt = FakeYouTube()
        P.run_publish_job(sb, _job(), transport=yt)
        up = yt.uploads[0]
        assert up["title"] == "شرح الخلايا | Cambridge Stage 7 Biology", "the Arabic headline and the Latin audience, no English words"
        assert "What is a cell?" not in up["description"] and "Cells Explained" not in up["description"]
        assert up["description"].startswith("الخلايا — درس من سكتش كاست.")


def _staged_script(sb, language="ar"):
    """A translated kit's staged script in storage, named by the presentation's params."""
    path = f"{OWNER}/{KIT}/translated/script.json"
    sb.files[("artifacts", path)] = json.dumps({"part": 1, "of": 1, "language": language, "script": {"segments": [
        {"segment_id": "s001", "text": "الخلية هي الوحدة الأساسية للحياة."},
        {"segment_id": "s002", "text": "للنباتات جدار خلوي أيضًا."}]}}, ensure_ascii=False).encode("utf-8")
    sb.tables["generations"] = [{"id": GEN, "status": "done",
                                 "params": {"translation": {"kit_id": KIT, "scripts": [path]}}}]
    return path


class TestRewords:
    def test_the_published_parts_words_are_written_in_the_language_and_pushed_to_the_channel(self, monkeypatch):
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN_AR", "token-present")
        sb = _arabic(_sb(parts=(1,), publications=[{"topic_kit_id": KIT, "part": 1, "channel_language": "ar",
                                                    "youtube_video_id": "AZ_Y", "privacy": "public"}]))
        _staged_script(sb)
        # the words the Cells kit carried: English, unstamped
        sb.tables["topic_kits"][0]["youtube_meta"] = {"title": "Cells Explained | Prokaryotes | " + AUDIENCE,
                                                      "intro": "What is the basic unit of life?", "key_terms": ["cell"],
                                                      "hashtags": ["Cells"], "source": "generated"}
        # the fixture's one board gives the audience "Cambridge Stage 7 Biology"
        fixture_title = "شرح الخلايا | النواة, الميتوكوندريا | Cambridge Stage 7 Biology"
        client = FakeClient({"title": fixture_title, "intro": "ما هي الوحدة الأساسية للحياة؟", "key_terms": ["النواة", "الميتوكوندريا"],
                             "hashtags": ["الخلايا", "SketchCast"]})
        yt = FakeYouTube()
        summary = P.run_publish_job(sb, _job(action="rewords"), transport=yt, client_factory=lambda: client)
        assert summary["step"] == "done" and summary["reworded"] == [1] and summary["failed"] == []
        assert summary["meta"] == "written" and summary["channel"] == "ar"
        assert yt.uploads == [], "a re-word never uploads"
        rw = yt.rewords[0]
        assert rw["video_id"] == "AZ_Y" and rw["language"] == "ar" and rw["title"] == fixture_title
        assert rw["description"].startswith("ما هي الوحدة الأساسية للحياة؟")
        assert Y.words_for("ar")["cta"] in rw["description"] and "#الخلايا" in rw["description"]
        assert "<narration>\nالخلية هي الوحدة الأساسية للحياة. للنباتات جدار خلوي أيضًا.\n</narration>" in client.prompts[0]
        stored = sb.tables["topic_kits"][0]["youtube_meta"]
        assert stored["language"] == "ar" and stored["title"] == fixture_title
        assert _pubs(sb)[1]["youtube_video_id"] == "AZ_Y", "the publication row is untouched"
        assert sb.tables["jobs"][0]["status"] == "done"

    def test_words_already_in_the_language_are_kept_and_no_model_is_called(self, monkeypatch):
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN_AR", "token-present")
        sb = _arabic(_sb(parts=(1,), publications=[{"topic_kit_id": KIT, "part": 1, "channel_language": "ar",
                                                    "youtube_video_id": "AZ_Y", "privacy": "public"}]))
        sb.tables["topic_kits"][0]["youtube_meta"] = _arabic_meta()

        class Never:
            def analyze(self, *a, **k):
                raise AssertionError("no model call when the words are already in the language")
        yt = FakeYouTube()
        summary = P.run_publish_job(sb, _job(action="rewords"), transport=yt, client_factory=lambda: Never())
        assert summary["reworded"] == [1] and summary["meta"] == "kept" and yt.rewords[0]["title"] == AR_TITLE

    def test_a_kit_with_nothing_on_the_channel_rewords_nothing(self, monkeypatch):
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN_AR", "token-present")
        sb = _arabic(_sb(parts=(1,)))
        yt = FakeYouTube()
        summary = P.run_publish_job(sb, _job(action="rewords"), transport=yt)
        assert summary["reworded"] == [] and yt.rewords == [] and "nothing to re-word" in summary["note"]

    def test_a_failed_update_is_recorded_on_the_job_and_the_others_still_run(self, monkeypatch):
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN_AR", "token-present")
        sb = _arabic(_sb(parts=(1, 2), publications=[
            {"topic_kit_id": KIT, "part": 1, "channel_language": "ar", "youtube_video_id": "A1", "privacy": "public"},
            {"topic_kit_id": KIT, "part": 2, "channel_language": "ar", "youtube_video_id": "A2", "privacy": "public"}]))
        sb.tables["topic_kits"][0]["youtube_meta"] = _arabic_meta()

        class Flaky(FakeYouTube):
            def update_words(self, video_id, **kw):
                if video_id == "A1":
                    raise RuntimeError("quota")
                return super().update_words(video_id, **kw)
        yt = Flaky()
        summary = P.run_publish_job(sb, _job(action="rewords"), transport=yt)
        assert summary["failed"] == [1] and summary["reworded"] == [2]
        assert "quota" in sb.tables["jobs"][0]["error"] and yt.rewords[0]["title"].endswith("— الجزء 2 من 2")

    def test_an_english_kit_rewords_from_its_script_artifacts(self):
        sb = _sb(parts=(1,), publications=[{"topic_kit_id": KIT, "part": 1, "channel_language": "en",
                                            "youtube_video_id": "E1", "privacy": "public"}])
        sb.tables["generations"] = [{"id": GEN, "status": "done", "params": {}}]
        client = FakeClient({"title": "Cells Explained | Nucleus, Membrane | Cambridge Stage 7 Biology", "intro": "What is a cell?",
                             "key_terms": ["nucleus", "membrane"], "hashtags": ["Cells", "SketchCast"]})
        yt = FakeYouTube()
        summary = P.run_publish_job(sb, _job(action="rewords"), transport=yt, client_factory=lambda: client)
        assert summary["reworded"] == [1] and summary["meta"] == "written"
        assert "A cell is the basic unit." in client.prompts[0]
        assert yt.rewords[0]["title"] == "Cells Explained | Nucleus, Membrane | Cambridge Stage 7 Biology"
        assert sb.tables["topic_kits"][0]["youtube_meta"]["language"] == "en"


# ── the enrich and backfill passes on a video in another language ───────────


class TestPassesInAnotherLanguage:
    def _arabic_description(self):
        return Y.compose_description(topic_title="الخلايا", summary=None, meta={"intro": "ما هي الخلية؟"},
                                     header_lines=["Cambridge Lower Secondary Science 0893 · 7Bs.01"],
                                     chapter_lines_=["0:00 مقدمة", "1:00 النواة", "3:00 خلاصة"], key_terms=["النواة"],
                                     hashtags=["الخلايا", "SketchCast"], part=1, total=1, next_title=None,
                                     link="https://sketchcast.app/topics/cells?utm_source=youtube", language="ar")

    def test_the_playlist_line_is_in_the_language_and_sits_above_the_arabic_sketchcast_line(self):
        d = self._arabic_description()
        out = E.enrich_description(d, discipline="biology", subject="Science", boards=BOARDS, playlist_id="PL1",
                                   link="https://sketchcast.app/topics/cells?utm_source=youtube", language="ar")
        paras = out.split("\n\n")
        line = next(p for p in paras if E.PLAYLIST_URL in p)
        assert line.startswith("المزيد من دروس Biology: ") and E.is_playlist_line(line)
        assert paras.index(line) + 1 == next(i for i, p in enumerate(paras) if Y.is_sketchcast_line(p))
        assert out.count(E.PLAYLIST_URL) == 1
        again = E.enrich_description(out, discipline="biology", subject="Science", boards=BOARDS, playlist_id="PL2",
                                     link="https://sketchcast.app/topics/cells?utm_source=youtube", language="ar")
        assert again.count(E.PLAYLIST_URL) == 1 and "PL2" in again and "PL1" not in again, "replaced in place"
        assert again.count(Y.kit_link_prefix("ar")) == 1 and "Full kit:" not in again

    def test_the_english_line_is_still_found_and_replaced_on_an_english_video(self):
        d = Y.compose_description(topic_title="Cells", summary="The cell.", meta=None, header_lines=[],
                                  chapter_lines_=[], key_terms=[], hashtags=["Cells"], part=1, total=1,
                                  next_title=None, link="https://sketchcast.app/?utm_source=youtube")
        out = E.enrich_description(d, discipline="biology", subject="Science", boards=BOARDS, playlist_id="PL1")
        assert "More Biology lessons: " + E.PLAYLIST_URL + "PL1" in out and E.is_playlist_line("More Biology lessons: " + E.PLAYLIST_URL + "PL1")

    def test_the_backfill_asks_in_the_channels_language(self):
        d = self._arabic_description().replace(Y.cta_line("ar") + "\n\n", "")
        assert Y.cta_line("ar") not in d
        out = B.with_cta(d, Y.cta_line("ar"))
        paras = out.split("\n\n")
        assert paras.index(Y.cta_line("ar")) + 1 == next(i for i, p in enumerate(paras) if Y.is_sketchcast_line(p))
        assert DESCRIPTION_CTA not in out


@pytest.mark.parametrize("lang", sorted(Y.WORDS))
def test_every_language_composes_a_full_description_without_an_english_fixed_line(lang):
    d = Y.compose_description(topic_title="T", summary=None, meta={"intro": "I"}, header_lines=["H"],
                              chapter_lines_=["0:00 a", "1:00 b", "2:00 c"], key_terms=["k"], hashtags=["Tag"],
                              part=2, total=3, next_title="N", link="https://x", language=lang)
    words = Y.words_for(lang)
    assert words["aligned"] in d and words["chapters"] in d and words["sketchcast"] in d and words["cta"] in d
    if lang != "en":
        for english in ("Aligned to", "Key terms:", "Part 2 of 3.", Y.SKETCHCAST_LINE, DESCRIPTION_CTA, "Full kit:"):
            assert english not in d, (lang, english)
