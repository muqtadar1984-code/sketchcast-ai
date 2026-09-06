"""A catalogue KIT's generations, worker side (catalogue.kit + the catalogue
branch of worker.process, Phase 3).

Everything CALLS things: the database is tests/catalogue_fakes.FakeSB, the
model and every heavy stage (analysis, script, slides, compose, render,
documents, the composer) are fakes that record what they were handed. No
network, no model, no live Supabase, no book download — that last point is
the whole design: a kit has no book, and the build must never ask for one."""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent1_ingestion.models import ChapterContent
from agent2_analysis.analyzer import MAX_ANALYSIS_CHARS, MAX_PART_WORDS, _hard_split, build_chapter_parts
from agent3_scripts.models import ScriptSegment
from catalogue import kit
from catalogue.article import Mapping
from tests.catalogue_fakes import FakeSB
from tests.test_catalogue_loader import ARTICLE as _ARTICLE, FIGURES

TOPIC = {"id": "t-cell", "title": "Cells", "subject": "Biology", "status": "generating", "depth_node_id": None,
         "language": "en", "canonical_key": "cell"}
CAM = {"id": "cur-cam", "code": "cambridge_ls_science_0893", "name": "Cambridge Lower Secondary Science 0893"}
CBSE = {"id": "cur-cbse", "code": "cbse_science_086", "name": "CBSE Science (Class 6-10)"}
NODES = [
    {"id": "n-bs1", "curriculum_id": "cur-cam", "code": "7Bs.01", "grade": "7", "kind": "objective",
     "title": "Understand that all organisms are made of cells.", "description": "Understand that all organisms are made of cells."},
    {"id": "n-bs2", "curriculum_id": "cur-cam", "code": "7Bs.02", "grade": "7", "kind": "objective",
     "title": "Identify and describe the functions of cell structures.", "description": "Identify and describe the functions of cell structures."},
    {"id": "n-cb1", "curriculum_id": "cur-cbse", "code": "cbse:9:U2:01", "grade": "9", "kind": "topic",
     "title": "Cell - Basic Unit of life", "description": None},
]
MAPS = [{"topic_id": "t-cell", "node_id": "n-bs1", "coverage": "full"},
        {"topic_id": "t-cell", "node_id": "n-bs2", "coverage": "full"},
        {"topic_id": "t-cell", "node_id": "n-cb1", "coverage": "partial"}]
ARTICLE = {**_ARTICLE, "status": "approved", "depth_node_id": "n-cb1"}
HEADER = ["Cambridge Lower Secondary Science 0893 · 7Bs.01, 7Bs.02", "CBSE Science (Class 6-10) · Class 9 · Cell - Basic Unit of life"]
KIT = {"id": "kit-1", "topic_id": "t-cell", "article_id": "art-1", "language": "en", "status": "generating",
       "teacher_avatar": "female", "presentation_generation_id": "gen-p",
       "doc_generation_ids": {"activity": "gen-a", "case_study": "gen-c", "worksheet": "gen-w", "deck": "gen-d"},
       "chapters": [], "clips": [], "part_plan": [], "notes": None}
PARAMS = {"catalogue": True, "topic_id": "t-cell", "kit_id": "kit-1", "article_id": "art-1", "language": "en",
          "narration_style": "dialogue", "teacher_avatar": "female", "tts_voice": "g-en-f",
          "student_voice": "g-en-student-m", "curriculum_header": HEADER}
KINDS = {"gen-p": "presentation", "gen-a": "activity", "gen-c": "case_study", "gen-w": "worksheet", "gen-d": "deck"}


def _gen(gen_id="gen-p", status="processing", **extra):
    return {"id": gen_id, "kind": KINDS.get(gen_id, "presentation"), "status": status, "owner_id": "sys",
            "book_id": None, "chapter_ref": None, "params": {**PARAMS, **extra}}


def _sb(*, article=ARTICLE, kit_row=KIT, topic=TOPIC, statuses=None, maps=MAPS):
    """Every kit generation exists; ``statuses`` overrides their status (default done)."""
    sb = FakeSB()
    sb.tables["topics"] = [dict(topic)] if topic else []
    sb.tables["curricula"] = [dict(CAM), dict(CBSE)]
    sb.tables["curriculum_nodes"] = [dict(n) for n in NODES]
    sb.tables["topic_curriculum_map"] = [dict(m) for m in maps]
    sb.tables["topic_articles"] = [dict(article)] if article else []
    sb.tables["article_figures"] = [dict(f) for f in FIGURES]
    sb.tables["topic_kits"] = [dict(kit_row)] if kit_row else []
    statuses = statuses or {}
    sb.tables["generations"] = [_gen(g, statuses.get(g, "done")) for g in KINDS]
    sb.tables["jobs"] = []
    return sb


def _kit(sb):
    return sb.tables["topic_kits"][0]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("TTS_PREMIUM_PROVIDER", "ELEVENLABS_ENABLED", "ELEVENLABS_API_KEY", "GOOGLE_TTS_ENABLED",
              "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS_JSON", "VERTEX_PROJECT_ID",
              "VIDEO_ENGINE", "SEMANTIC_PLAN", "FEATURE_TEXTBOOK_FIGURES", "FEATURE_CHAPTER_HEAL",
              "DECK_IN_PRESENTATION", "CATALOGUE_PART_TARGET_MIN", "CATALOGUE_WINDOW_UTC",
              "SUPPORT_AGENT_ENABLED", "TTS_PREMIUM_CANARY_OWNERS"):
        monkeypatch.delenv(k, raising=False)


# ── prepare ─────────────────────────────────────────────────────────────


class TestPrepare:
    def test_the_happy_path_reads_the_article_not_a_book(self):
        sb = _sb()
        p = kit.prepare(sb, _gen())
        assert ("select", "books") not in sb.calls
        assert p.kit_id == "kit-1" and p.article_id == "art-1" and p.topic_id == "t-cell"
        # decision 14: level from the DEPTH node (the article's, grade 9)
        assert p.grade == "9" and p.level == "high_school"
        assert p.book == {"id": None, "title": "Cells", "grade": "9", "subject": "Biology",
                          "curriculum": "CBSE Science (Class 6-10)", "language": "en", "author": "SketchCast"}
        ChapterContent(**p.chapter)                       # the loader's contract, end to end
        assert p.chapter["chapter_num"] == -1 and p.chapter["title"] == ARTICLE["title"]
        assert p.chunks and all(c["words"] <= kit.part_words_budget() for c in p.chunks)
        assert p.section_ids == {"what a cell is": "s1", "structures common to all cells": "s2"}
        assert p.narration_style == "conversational", "the ONE label two_voice_dialogue() recognises"
        assert (p.tts_voice, p.student_voice, p.student_voice_fallback) == ("g-en-f", "g-en-student-m", False)
        assert p.curriculum_header == HEADER, "the portal's header lines are taken as sent"
        assert p.synthetic_book_id == "catalogue-t-cell"
        assert p.band is not None

    def test_header_lines_are_composed_when_the_params_carry_none(self):
        sb = _sb()
        p = kit.prepare(sb, _gen(curriculum_header=None))
        assert p.curriculum_header == HEADER

    def test_the_teacher_voice_defaults_from_the_avatar_and_the_student_is_the_other_gender(self):
        sb = _sb()
        p = kit.prepare(sb, _gen(tts_voice=None, student_voice=None, teacher_avatar="male"))
        assert p.tts_voice == "g-en-m" and p.student_voice == "g-en-student-f" and p.student_voice_fallback is False

    def test_a_language_without_a_student_voice_records_the_fallback(self):
        sb = _sb()
        p = kit.prepare(sb, _gen(language="hi", tts_voice="g-hi-f", student_voice=None))
        assert p.student_voice is None and p.student_voice_fallback is True

    def test_a_composed_worksheet_needs_no_kit_and_no_article(self):
        sb = _sb(kit_row=None)
        gen = {"id": "gen-q", "kind": "worksheet", "status": "processing", "owner_id": "sys", "book_id": None,
               "params": {"catalogue": True, "topic_id": "t-cell", "question_set_id": "qs-1", "language": "en"}}
        p = kit.prepare(sb, gen)
        assert p.question_set_id == "qs-1" and p.kit_id is None and p.article is None and p.chapter is None
        assert p.chunks == [] and p.curriculum_header == HEADER
        assert p.level == "high_school", "the depth node still sets the level (deepest mapped grade)"

    # decision 13 — refusals, before any model call
    def test_an_unapproved_article_is_refused_and_the_kit_marked_failed(self):
        sb = _sb(article={**ARTICLE, "status": "draft"})
        with pytest.raises(kit.CatalogueRefused, match="not approved"):
            kit.prepare(sb, _gen())
        assert _kit(sb)["status"] == "failed" and "not approved" in _kit(sb)["notes"]

    def test_a_missing_kit_is_refused(self):
        with pytest.raises(kit.CatalogueRefused, match="not found"):
            kit.prepare(_sb(kit_row=None), _gen())

    def test_a_rejected_kit_is_refused_and_stays_rejected(self):
        sb = _sb(kit_row={**KIT, "status": "rejected"})
        with pytest.raises(kit.CatalogueRefused, match="rejected"):
            kit.prepare(sb, _gen())
        assert _kit(sb)["status"] == "rejected"

    def test_a_kit_generation_without_a_kit_id_is_refused(self):
        with pytest.raises(kit.CatalogueRefused, match="kit_id"):
            kit.prepare(_sb(), _gen(kit_id=None))

    def test_a_missing_article_is_refused_and_the_kit_marked_failed(self):
        sb = _sb(article=None)
        with pytest.raises(kit.CatalogueRefused, match="not found"):
            kit.prepare(sb, _gen())
        assert _kit(sb)["status"] == "failed"

    def test_a_non_catalogue_generation_is_refused(self):
        with pytest.raises(kit.CatalogueRefused):
            kit.prepare(_sb(), {"id": "g", "params": {"part": 1}})

    def test_an_already_failed_kit_is_not_relabelled_by_a_late_refusal(self):
        sb = _sb(article={**ARTICLE, "status": "draft"}, kit_row={**KIT, "status": "in_review", "notes": "keep"})
        with pytest.raises(kit.CatalogueRefused):
            kit.prepare(sb, _gen())
        assert _kit(sb)["status"] == "in_review" and _kit(sb)["notes"] == "keep", "guarded from generating only"

    def test_a_question_set_id_is_refused_on_any_kind_but_a_worksheet(self):
        """A set id switches the kit/article gates OFF; it must not travel on a
        kind the composer does not render (the model would run on an empty
        chapter with every gate of decision 13 skipped)."""
        sb = _sb(kit_row=None)
        gen = {"id": "gen-q", "kind": "presentation", "status": "processing", "owner_id": "sys", "book_id": None,
               "params": {"catalogue": True, "topic_id": "t-cell", "question_set_id": "qs-1", "language": "en"}}
        with pytest.raises(kit.CatalogueRefused, match="only rendered as a worksheet, not a presentation"):
            kit.prepare(sb, gen)
        assert ("select", "topic_articles") not in sb.calls, "refused before any further read"

    def test_a_question_set_id_together_with_a_kit_id_is_refused(self):
        with pytest.raises(kit.CatalogueRefused, match="question_set_id and kit_id together"):
            kit.prepare(_sb(), _gen("gen-w", question_set_id="qs-1"))

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(kit.CatalogueRefused, match="not a catalogue kind"):
            kit.prepare(_sb(), {**_gen(), "kind": "exam"})


# ── the pure helpers ────────────────────────────────────────────────────


class TestPureHelpers:
    @pytest.mark.parametrize("grade,level", [
        ("3", "primary_school"), ("5", "primary_school"), ("6", "middle_school"), ("Stage 8", "middle_school"),
        ("9", "high_school"), ("Class 10", "high_school"), ("11", "high_school"), ("12", "high_school"),
        (None, "middle_school"), ("Upper Secondary", "middle_school"),
    ])
    def test_level_for_grade(self, grade, level):
        assert kit.level_for_grade(grade) == level

    def test_part_words_budget_defaults_to_17_minutes_and_never_exceeds_20(self, monkeypatch):
        assert kit.part_words_budget() == 17 * 130 == 2210
        monkeypatch.setenv("CATALOGUE_PART_TARGET_MIN", "25")
        assert kit.part_words_budget() == 20 * 130 == 2600, "the 20-minute ceiling"
        monkeypatch.setenv("CATALOGUE_PART_TARGET_MIN", "10")
        assert kit.part_words_budget() == 1300
        monkeypatch.setenv("CATALOGUE_PART_TARGET_MIN", "lots")
        assert kit.part_words_budget() == 2210

    @pytest.mark.parametrize("hhmm,expected", [
        ("21:00", True), ("04:59", True), ("20:00", True), ("05:00", False), ("12:00", False), ("19:59", False),
    ])
    def test_the_default_window_wraps_midnight(self, hhmm, expected):
        h, m = map(int, hhmm.split(":"))
        assert kit.catalogue_window_open(datetime(2026, 9, 6, h, m, tzinfo=timezone.utc)) is expected

    def test_window_env_always_and_a_daytime_window_and_a_typo(self, monkeypatch):
        noon = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
        monkeypatch.setenv("CATALOGUE_WINDOW_UTC", "always")
        assert kit.catalogue_window_open(noon) is True
        monkeypatch.setenv("CATALOGUE_WINDOW_UTC", "09:00-17:00")
        assert kit.catalogue_window_open(noon) is True
        assert kit.catalogue_window_open(datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)) is False
        monkeypatch.setenv("CATALOGUE_WINDOW_UTC", "whenever")
        assert kit.catalogue_window_open(noon) is False, "a typo fails CLOSED (the default window)"
        assert kit.catalogue_window_open(datetime(2026, 9, 6, 22, 0, tzinfo=timezone.utc)) is True
        # a zero-width window is a typo of the default, not "all day"
        monkeypatch.setenv("CATALOGUE_WINDOW_UTC", "05:00-05:00")
        assert kit.catalogue_window_open(noon) is False, "start == end fails CLOSED too"
        assert kit.catalogue_window_open(datetime(2026, 9, 6, 22, 0, tzinfo=timezone.utc)) is True

    def test_the_presentation_margin_needs_the_window_to_stay_open(self, monkeypatch):
        tz = timezone.utc
        assert kit.catalogue_window_open(datetime(2026, 9, 6, 3, 59, tzinfo=tz), margin_minutes=60) is True
        assert kit.catalogue_window_open(datetime(2026, 9, 6, 4, 1, tzinfo=tz), margin_minutes=60) is False
        assert kit.catalogue_window_open(datetime(2026, 9, 6, 4, 1, tzinfo=tz)) is True, "a document may still be claimed"
        assert kit.catalogue_window_open(datetime(2026, 9, 6, 21, 0, tzinfo=tz), margin_minutes=60) is True, "wraps midnight"
        assert kit.catalogue_window_open(datetime(2026, 9, 6, 21, 0, tzinfo=tz), margin_minutes=9 * 60) is False, \
            "a margin longer than the window is never honoured"
        monkeypatch.setenv("CATALOGUE_WINDOW_UTC", "always")
        assert kit.catalogue_window_open(datetime(2026, 9, 6, 12, 0, tzinfo=tz), margin_minutes=600) is True
        assert kit.presentation_margin_minutes() == 60
        monkeypatch.setenv("CATALOGUE_PRESENTATION_MARGIN_MIN", "45")
        assert kit.presentation_margin_minutes() == 45
        monkeypatch.setenv("CATALOGUE_PRESENTATION_MARGIN_MIN", "-5")
        assert kit.presentation_margin_minutes() == 0
        monkeypatch.setenv("CATALOGUE_PRESENTATION_MARGIN_MIN", "soon")
        assert kit.presentation_margin_minutes() == 60

    def test_yield_to_users_polls_until_the_queue_clears_and_gives_up_at_the_cap(self):
        answers = iter([True, True, False])
        waits, t, waited_for = [], {"now": 100.0}, []

        def sleep(s):
            waits.append(s)
            t["now"] += s

        clock = lambda: t["now"]  # noqa: E731
        assert kit.yield_to_users(lambda: next(answers), poll_seconds=5, max_seconds=60, on_wait=waited_for.append,
                                  sleep=sleep, clock=clock) is True
        assert waits == [5, 5] and waited_for == [0.0, 5.0]
        waits.clear()
        assert kit.yield_to_users(lambda: False, poll_seconds=5, max_seconds=60, sleep=sleep, clock=clock) is True
        assert waits == [], "never contended: no wait at all"
        assert kit.yield_to_users(lambda: True, poll_seconds=10, max_seconds=25, sleep=sleep, clock=clock) is False
        assert waits == [10, 10, 10], "polled at 0, 10 and 20 s; at 30 s the cap had passed"
        assert (kit.yield_poll_seconds(), kit.yield_max_seconds()) == (20.0, 1800.0)

    def test_the_contention_probe_caches_one_read_and_treats_an_unreadable_queue_as_contended(self, monkeypatch):
        reads = []
        probe = kit.ContentionProbe(object(), ttl=100, reader=lambda sb: (reads.append(1), True)[1])
        assert probe() is True and probe() is True and len(reads) == 1, "eight render threads cost one query"
        probe.invalidate()
        assert probe() is True and len(reads) == 2
        assert kit.ContentionProbe(object(), ttl=0, reader=lambda sb: False)() is False

        def broken(sb):
            raise RuntimeError("db down")

        assert kit.ContentionProbe(object(), ttl=0, reader=broken)() is True, "cannot read the queue → a kit waits"
        monkeypatch.setattr("catalogue.figures.builder_queued", lambda sb: sb == "the client")
        assert kit.ContentionProbe("the client", ttl=0)() is True, "the default reader is builder_queued"
        monkeypatch.setattr(kit, "PROBE_TTL_S", 0.0)
        assert kit.ContentionProbe("x")._ttl == 0.0, "the default TTL is read at construction"

    def test_header_lines_one_per_curriculum(self):
        by_id = {n["id"]: n for n in NODES}
        mappings = [Mapping(node=by_id[m["node_id"]], curriculum=CAM if by_id[m["node_id"]]["curriculum_id"] == "cur-cam" else CBSE,
                            coverage=m["coverage"]) for m in MAPS]
        assert kit.header_lines(mappings) == HEADER
        assert kit.header_lines([]) == []

    def test_student_voice_for(self):
        assert kit.student_voice_for("g-en-f", "en") == ("g-en-student-m", False)
        assert kit.student_voice_for("g-en-m", "en") == ("g-en-student-f", False)
        assert kit.student_voice_for("g-ar-f", "ar") == ("g-ar-student-m", False)
        assert kit.student_voice_for("g-en-f", "en", requested="g-en-student-f") == ("g-en-student-f", False), "an explicit student id wins"
        assert kit.student_voice_for("g-en-f", "en", requested="g-en-m") == ("g-en-student-m", False), "a narrator id is not a student voice"
        assert kit.student_voice_for("g-hi-f", "hi") == (None, True)

    def test_is_catalogue(self):
        assert kit.is_catalogue({"catalogue": True}) and kit.is_catalogue({"catalogue": "true"})
        assert not kit.is_catalogue({"catalogue": False}) and not kit.is_catalogue({}) and not kit.is_catalogue(None)


# ── recap / outro (decision 3) ──────────────────────────────────────────


class TestSegments:
    @pytest.mark.parametrize("lang", ["en", "ar", "fr", "es", "ms"])
    def test_recap_and_outro_validate_as_script_segments(self, lang):
        recap = kit.recap_segment(2, 3, ["What a cell is", "Plant cells"], lang)
        outro = kit.outro_segment(2, 3, ["From cells to organisms"], lang)
        for seg in (recap, outro):
            model = ScriptSegment(**seg)
            assert model.text and model.elevenlabs_text == model.text and model.dialogue is None, "teacher-only, one voice"
            assert model.scene is None and model.estimated_duration_seconds >= 4
        assert recap["segment_id"] == "recap" and recap["type"] == "activate"
        assert outro["segment_id"] == "outro" and outro["type"] == "preview"
        assert "2" in recap["text"] and "3" in recap["text"] and "What a cell is" in recap["text"]
        assert "3" in outro["text"] and "From cells to organisms" in outro["text"]
        assert recap["slide_points"] == ["What a cell is", "Plant cells"]

    def test_english_wording_and_the_plain_variants(self):
        assert kit.recap_segment(2, 3, ["A", "B", "C", "D"], "en")["text"] == \
            "Welcome back to part 2 of 3. Last time we covered A, B and C. Let us build on that."
        assert kit.recap_segment(3, 3, [], "en")["text"] == "Welcome back to part 3 of 3. Let us pick up where we left off."
        assert kit.outro_segment(1, 2, ["Plant cells"], "en")["text"] == \
            "That brings part 1 of 2 to a close. In part 2 we continue with Plant cells. See you there."
        assert kit.outro_segment(1, 2, [], "en")["slide_heading"] == "Next · Part 2 of 2"

    def test_an_unknown_language_falls_back_to_english(self):
        assert kit.recap_segment(2, 2, ["A"], "xx")["text"].startswith("Welcome back")


# ── the parts budget (analyzer max_words) ──────────────────────────────


def _long_chapter(n_sections=6, words_each=600):
    body = " ".join(f"word{i}" for i in range(words_each))
    return {"chapter_num": -1, "title": "T", "start_page": 0, "end_page": 0, "images": [], "key_boxes": [],
            "sections": [{"section_title": f"Section {k}", "section_type": "body", "content": body, "page_num": 0,
                          "subsections": []} for k in range(1, n_sections + 1)]}


class TestPartsBudget:
    def test_the_default_is_byte_identical(self):
        ch = _long_chapter()
        assert build_chapter_parts(ch) == build_chapter_parts(ch, max_words=MAX_PART_WORDS)

    def test_a_longer_budget_makes_fewer_parts_and_never_splits_a_section(self):
        ch = _long_chapter()                       # 3,600 words in 6 whole sections
        default = build_chapter_parts(ch)
        longer = build_chapter_parts(ch, max_words=kit.part_words_budget())
        assert len(longer) <= len(default) and len(longer) == 2
        for chunk in longer:
            assert chunk["words"] <= kit.part_words_budget()
        seen = [t for c in longer for t in c["section_titles"]]
        assert seen == [f"Section {k}" for k in range(1, 7)], "every section once, in order, whole"
        for k in range(1, 7):
            assert sum(c["text"].count(f"## Section {k}\n") for c in longer) == 1

    def test_hard_split_honours_the_budget_it_is_given(self):
        block = " ".join(f"w{i}" for i in range(5000))
        for budget in (MAX_PART_WORDS, 2210, 2600):
            pieces = _hard_split(block, budget)
            assert all(len(p.split()) <= budget for p in pieces) and "".join(pieces) == block
            assert all(len(p) <= MAX_ANALYSIS_CHARS for p in pieces), "the model's char bound is never widened"
        assert len(_hard_split(block, 2600)) < len(_hard_split(block, MAX_PART_WORDS))


# ── after_generation (decision 6) ──────────────────────────────────────


PARTS = [
    {"part": 1, "chapters": [{"t": 0, "label": "What a cell is", "section_id": "s1"}, {"t": 140, "label": "Structures"}],
     "clips": [{"part": 1, "start": 0, "end": 140, "label": "What a cell is", "purpose": "introduce"}],
     "plan": {"part": 1, "sections": ["What a cell is", "Structures"], "minutes": 4.7}},
    {"part": 2, "chapters": [{"t": 0, "label": "Recap"}, {"t": 30, "label": "Plant cells"}],
     "clips": [{"part": 2, "start": 30, "end": 200, "label": "Plant cells", "purpose": "consolidate"}],
     "plan": {"part": 2, "sections": ["Plant cells"], "minutes": 3.3}},
]


class TestAfterGeneration:
    def test_a_finished_presentation_writes_timestamps_and_spawns_the_lesson_plan(self):
        sb = _sb(statuses={"gen-a": "queued"})
        res = kit.after_generation(sb, _gen("gen-p", "done"), "kit-1", {"status": "done", "kind": "presentation", "parts": PARTS})
        k = _kit(sb)
        assert res["timestamps"] is True and k["chapters"] == [{"part": 1, "chapters": PARTS[0]["chapters"]},
                                                              {"part": 2, "chapters": PARTS[1]["chapters"]}]
        assert k["clips"] == PARTS[0]["clips"] + PARTS[1]["clips"]
        assert k["part_plan"] == [PARTS[0]["plan"], PARTS[1]["plan"]]
        lp = next(g for g in sb.tables["generations"] if g["kind"] == "lesson_plan")
        assert res["lesson_plan"] == lp["id"] and k["doc_generation_ids"]["lesson_plan"] == lp["id"]
        assert (lp["status"], lp["owner_id"], lp["book_id"], lp["chapter_ref"]) == ("queued", "sys", None, None)
        assert lp["params"]["clips"] == k["clips"] and lp["params"]["lesson_modes"] is True
        assert lp["params"]["curriculum_header"] == HEADER and lp["params"]["catalogue"] is True
        assert lp["params"]["kit_id"] == "kit-1" and lp["params"]["student_voice"] == "g-en-student-m"
        assert "coverage" not in lp["params"], "only the inherited keys travel"
        # the activity is still queued and the plan is new: not in review yet
        assert res["in_review"] is False and k["status"] == "generating"
        assert sb.tables["topics"][0]["status"] == "generating"

    def test_the_kit_and_topic_go_to_review_when_every_referenced_generation_is_done(self):
        sb = _sb()
        kit.after_generation(sb, _gen("gen-p", "done"), "kit-1", {"status": "done", "kind": "presentation", "parts": PARTS})
        assert _kit(sb)["status"] == "generating", "the lesson_plan it just spawned is queued"
        lp = next(g for g in sb.tables["generations"] if g["kind"] == "lesson_plan")
        lp["status"] = "done"
        res = kit.after_generation(sb, {**lp, "params": {**PARAMS, "lesson_modes": True}}, "kit-1",
                                   {"status": "done", "kind": "lesson_plan"})
        assert res["in_review"] is True
        assert _kit(sb)["status"] == "in_review" and sb.tables["topics"][0]["status"] == "in_review"

    def test_a_document_finishing_early_does_not_flip_the_kit(self):
        sb = _sb(statuses={"gen-p": "processing"})
        res = kit.after_generation(sb, _gen("gen-a", "done"), "kit-1", {"status": "done", "kind": "activity"})
        assert res["in_review"] is False and _kit(sb)["status"] == "generating"
        assert not [g for g in sb.tables["generations"] if g["kind"] == "lesson_plan"], "only the presentation spawns it"

    def test_the_review_flip_is_guarded_from_generating_only(self):
        sb = _sb(kit_row={**KIT, "status": "rejected", "doc_generation_ids": {**KIT["doc_generation_ids"], "lesson_plan": "gen-l"}})
        sb.tables["generations"].append(_gen("gen-l", "done"))
        sb.tables["generations"][-1]["kind"] = "lesson_plan"
        res = kit.after_generation(sb, _gen("gen-a", "done"), "kit-1", {"status": "done", "kind": "activity"})
        assert res["in_review"] is False and _kit(sb)["status"] == "rejected"
        assert sb.tables["topics"][0]["status"] == "generating", "the topic follows the kit, and the kit did not move"

    def test_the_topic_flip_is_guarded_too(self):
        sb = _sb(topic={**TOPIC, "status": "video_approved"},
                 kit_row={**KIT, "doc_generation_ids": {**KIT["doc_generation_ids"], "lesson_plan": "gen-l"}})
        sb.tables["generations"].append({**_gen("gen-l", "done"), "kind": "lesson_plan"})
        res = kit.after_generation(sb, _gen("gen-a", "done"), "kit-1", {"status": "done", "kind": "activity"})
        assert res["in_review"] is True and _kit(sb)["status"] == "in_review"
        assert sb.tables["topics"][0]["status"] == "video_approved", "never relabelled from another state"

    def test_a_failure_marks_the_kit_failed_and_leaves_the_topic(self):
        sb = _sb()
        res = kit.after_generation(sb, _gen("gen-w"), "kit-1", {"status": "failed", "kind": "worksheet", "error": "docx exploded"})
        assert res["failed"] is True
        assert _kit(sb)["status"] == "failed" and _kit(sb)["notes"] == "worksheet failed: docx exploded"
        assert sb.tables["topics"][0]["status"] == "generating"
        assert not [g for g in sb.tables["generations"] if g["kind"] == "lesson_plan"]

    def test_a_failure_never_relabels_a_reviewed_kit(self):
        sb = _sb(kit_row={**KIT, "status": "in_review"})
        res = kit.after_generation(sb, _gen("gen-w"), "kit-1", {"status": "failed", "kind": "worksheet", "error": "late"})
        assert res["failed"] is False and _kit(sb)["status"] == "in_review"

    def test_a_lifecycle_fault_is_recorded_on_the_kit_and_never_raised(self, monkeypatch):
        sb = _sb()
        monkeypatch.setattr(kit, "insert_lesson_plan", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("insert refused")))
        res = kit.after_generation(sb, _gen("gen-p", "done"), "kit-1", {"status": "done", "kind": "presentation", "parts": PARTS})
        assert res["failed"] is True and "insert refused" in res["error"]
        assert _kit(sb)["status"] == "failed" and "insert refused" in _kit(sb)["notes"]
        assert _kit(sb)["chapters"], "the timestamps written before the fault are kept"

    def test_a_retry_keeps_a_queued_lesson_plan_and_refreshes_its_clips(self):
        # Behaviour changed on purpose (review finding): the plan is still kept
        # — nothing has read it — but its clips now follow the re-rendered video.
        stale = [{"part": 1, "start": 0, "end": 150, "label": "old", "purpose": "introduce"}]
        sb = _sb(kit_row={**KIT, "doc_generation_ids": {**KIT["doc_generation_ids"], "lesson_plan": "gen-l"}})
        sb.tables["generations"].append({**_gen("gen-l", "queued", clips=stale), "kind": "lesson_plan"})
        res = kit.after_generation(sb, _gen("gen-p", "done"), "kit-1", {"status": "done", "kind": "presentation", "parts": PARTS})
        assert res["lesson_plan"] == "gen-l"
        plans = [g for g in sb.tables["generations"] if g["kind"] == "lesson_plan"]
        assert len(plans) == 1 and plans[0]["params"]["clips"] == _kit(sb)["clips"] != stale

    @pytest.mark.parametrize("status", ["done", "processing"])
    def test_a_built_lesson_plan_citing_the_same_clips_is_kept(self, status):
        clips = PARTS[0]["clips"] + PARTS[1]["clips"]
        reworded = [dict(c, label="reworded", purpose="other") for c in clips]   # prose differs, boundaries agree
        sb = _sb(kit_row={**KIT, "doc_generation_ids": {**KIT["doc_generation_ids"], "lesson_plan": "gen-l"}})
        sb.tables["generations"].append({**_gen("gen-l", status, clips=reworded), "kind": "lesson_plan"})
        res = kit.after_generation(sb, _gen("gen-p", "done"), "kit-1", {"status": "done", "kind": "presentation", "parts": PARTS})
        assert res["lesson_plan"] == "gen-l"
        assert len([g for g in sb.tables["generations"] if g["kind"] == "lesson_plan"]) == 1
        assert res["in_review"] is (status == "done")

    @pytest.mark.parametrize("status", ["done", "processing"])
    def test_a_built_lesson_plan_citing_old_clips_is_replaced(self, status):
        """Mode B cites [mm:ss–mm:ss] from params.clips; a re-rendered video
        has new boundaries (TTS durations vary), so a finished plan would
        point into a video that no longer has them."""
        old = [{"part": 1, "start": 0, "end": 130, "label": "What a cell is", "purpose": "introduce"}]
        sb = _sb(kit_row={**KIT, "doc_generation_ids": {**KIT["doc_generation_ids"], "lesson_plan": "gen-l"}})
        sb.tables["generations"].append({**_gen("gen-l", status, clips=old), "kind": "lesson_plan"})
        res = kit.after_generation(sb, _gen("gen-p", "done"), "kit-1", {"status": "done", "kind": "presentation", "parts": PARTS})
        assert res["lesson_plan"] != "gen-l"
        new = next(g for g in sb.tables["generations"] if g["id"] == res["lesson_plan"])
        assert new["status"] == "queued" and new["params"]["clips"] == _kit(sb)["clips"]
        assert _kit(sb)["doc_generation_ids"]["lesson_plan"] == res["lesson_plan"]
        assert res["in_review"] is False and _kit(sb)["status"] == "generating", "the new plan is queued"
        assert next(g for g in sb.tables["generations"] if g["id"] == "gen-l")["status"] == status, "the old row is left as it is"

    def test_a_kit_with_a_presentation_but_no_lesson_plan_is_never_complete(self):
        sb = _sb()                                    # every referenced generation done, no plan referenced
        assert kit.sync_kit_completion(sb, dict(_kit(sb))) is False
        assert _kit(sb)["status"] == "generating" and sb.tables["topics"][0]["status"] == "generating"
        # a kit without a presentation is decided by its documents alone
        assert kit.sync_kit_completion(sb, {**KIT, "presentation_generation_id": None}) is True
        assert _kit(sb)["status"] == "in_review"

    def test_the_two_halves_close_the_sibling_window(self):
        """Thread A records the presentation (timestamps + the plan) while its
        row still reads processing; thread B, finishing the deck in between,
        sees a kit that references a queued plan and does not review it."""
        sb = _sb(statuses={"gen-p": "processing", "gen-d": "processing"})
        gen_p = _gen("gen-p", "processing")
        recorded = kit.record_presentation(sb, gen_p, "kit-1", {"kind": "presentation", "parts": PARTS})
        assert recorded["timestamps"] is True and recorded["lesson_plan"] and recorded["failed"] is False
        assert _kit(sb)["doc_generation_ids"]["lesson_plan"] == recorded["lesson_plan"]
        deck = next(g for g in sb.tables["generations"] if g["id"] == "gen-d")
        deck["status"] = "done"
        assert kit.after_generation(sb, deck, "kit-1", {"status": "done", "kind": "deck"})["in_review"] is False
        assert _kit(sb)["status"] == "generating"
        next(g for g in sb.tables["generations"] if g["id"] == "gen-p")["status"] = "done"
        res = kit.after_generation(sb, gen_p, "kit-1", {"status": "done", "kind": "presentation", "parts": PARTS},
                                   recorded=recorded)
        assert res["lesson_plan"] == recorded["lesson_plan"] and res["timestamps"] is True and res["in_review"] is False
        assert len([g for g in sb.tables["generations"] if g["kind"] == "lesson_plan"]) == 1, "recorded once, not again"
        # a recording that failed is carried, and completion is not attempted
        sb2 = _sb()
        failed = {"kit": "kit-1", "timestamps": True, "lesson_plan": None, "in_review": False, "failed": True, "error": "boom"}
        res2 = kit.after_generation(sb2, _gen("gen-p", "done"), "kit-1", {"status": "done", "kind": "presentation"}, recorded=failed)
        assert res2["failed"] is True and res2["error"] == "boom" and res2["in_review"] is False
        assert sb2.writes("topic_kits") == []

    def test_a_recording_fault_marks_the_kit_failed_without_raising(self, monkeypatch):
        sb = _sb()
        monkeypatch.setattr(kit, "insert_lesson_plan", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("insert refused")))
        res = kit.record_presentation(sb, _gen("gen-p", "processing"), "kit-1", {"kind": "presentation", "parts": PARTS})
        assert res["failed"] is True and "insert refused" in res["error"] and _kit(sb)["status"] == "failed"
        assert kit.record_presentation(sb, _gen("gen-a"), "kit-1", {"kind": "activity"}) == kit._result("kit-1"), "documents record nothing"

    def test_a_retry_replaces_a_failed_lesson_plan(self):
        sb = _sb(kit_row={**KIT, "doc_generation_ids": {**KIT["doc_generation_ids"], "lesson_plan": "gen-l"}})
        sb.tables["generations"].append({**_gen("gen-l", "error"), "kind": "lesson_plan"})
        res = kit.after_generation(sb, _gen("gen-p", "done"), "kit-1", {"status": "done", "kind": "presentation", "parts": PARTS})
        assert res["lesson_plan"] != "gen-l" and _kit(sb)["doc_generation_ids"]["lesson_plan"] == res["lesson_plan"]

    def test_timestamps_merge_by_part_on_a_retry(self):
        sb = _sb(kit_row={**KIT, "chapters": [{"part": 1, "chapters": [{"t": 0, "label": "old"}]}, {"part": 3, "chapters": []}],
                          "clips": [{"part": 1, "start": 0, "end": 130, "label": "old", "purpose": "introduce"}],
                          "part_plan": [{"part": 1, "sections": ["old"], "minutes": 1.0}]})
        kit.after_generation(sb, _gen("gen-p", "done"), "kit-1", {"status": "done", "kind": "presentation", "parts": PARTS})
        k = _kit(sb)
        assert [c["part"] for c in k["chapters"]] == [1, 2, 3] and k["chapters"][0]["chapters"] == PARTS[0]["chapters"]
        assert all(c["label"] != "old" for c in k["clips"]) and [p["part"] for p in k["part_plan"]] == [1, 2]

    def test_no_kit_means_no_lifecycle(self):
        sb = _sb()
        res = kit.after_generation(sb, _gen("gen-w"), None, {"status": "done", "kind": "worksheet"})
        assert res["kit"] is None and sb.writes("topic_kits") == [] and sb.writes("generations") == []


# ── the catalogue branch of worker.process ─────────────────────────────


class _StubClient:
    model = "stub-model"

    def __init__(self):
        self.session_usage = {"calls": 1, "input_tokens": 10, "output_tokens": 20, "cost_usd": 0.0}

    def analyze(self, prompt, **k):
        return {"data": {}, "usage": {}}


class _Dump:
    def __init__(self, d):
        self._d = d

    def model_dump(self):
        return json.loads(json.dumps(self._d))


ANALYSIS = {
    "book_id": "catalogue-t-cell", "chapter_num": -1, "chapter_title": ARTICLE["title"],
    "concepts": {"concepts": [{"concept_id": "c001", "name": "cell"}, {"concept_id": "c002", "name": "nucleus"}]},
    "episodes": {"episodes": [
        {"episode_num": 1, "title": "Cells — Part 1", "sections_covered": ["What a cell is", "Structures common to all cells"],
         "key_concepts_introduced": ["c001"], "estimated_duration_minutes": 17.0, "visual_opportunities_in_episode": []},
        {"episode_num": 2, "title": "Cells — Part 2", "sections_covered": ["Plant cells"],
         "key_concepts_introduced": ["c002"], "estimated_duration_minutes": 12.0, "visual_opportunities_in_episode": []},
    ]},
}


def _worker_env(monkeypatch, sb):
    from worker import client as db
    from worker import process

    # tests/test_worker_entrypoint_runs.py replaces worker.process.db with a
    # stub that raises on get_generation and never restores it; pin the real
    # client so this file's outcome does not depend on collection order.
    monkeypatch.setattr(process, "db", db)
    uploads: list[tuple[str, str, bytes]] = []

    def _upload(_sb, local_path, dest):
        # bytes are captured HERE, as the real upload reads them: the job's
        # temp dir is gone by the time a test looks
        assert Path(local_path).exists() and Path(local_path).stat().st_size > 0
        uploads.append((str(local_path), dest, Path(local_path).read_bytes()))
        return dest

    monkeypatch.setattr(db, "upload_artifact", _upload)
    monkeypatch.setattr("worker.branding.load_branding", lambda sb_, owner, out_dir: {})
    monkeypatch.setattr("shared.llm.client_for", lambda lang, kind=None: _StubClient())
    analysis_calls: list[dict] = []

    def fake_analysis(**kw):
        analysis_calls.append(kw)
        return _Dump(ANALYSIS)

    monkeypatch.setattr("agent2_analysis.analyzer.run_full_analysis", fake_analysis)
    return process, uploads, analysis_calls


def _job(job_id, gen_id, kind):
    return {"id": job_id, "type": kind, "status": "processing", "generation_id": gen_id, "book_id": None,
            "params": {"catalogue": True, "topic_id": "t-cell", "kit_id": "kit-1"}, "progress": 1}


def _fake_doc(seen):
    def fake_doc(kind, book, chapter, analysis, client, params, out_dir, template=None, language="en"):
        seen.update(kind=kind, book=book, chapter=chapter, params=params, language=language, analysis=analysis)
        p1, p2 = Path(out_dir) / f"{kind}.docx", Path(out_dir) / "key.docx"
        p1.write_bytes(b"student")
        p2.write_bytes(b"key")
        return [p1, p2]
    return fake_doc


def _presentation_fakes(monkeypatch, tmp_path) -> dict:
    """Every heavy stage of the presentation loop as a recorder: two parts of
    three headings each, 50 s per segment, a two-voice dialogue. Returns the
    recorders (``scripts``, ``slide_inputs``, ``compose_calls``, ``renders``)."""
    scripts, slide_inputs, compose_calls, renders = [], [], [], []
    heads = ["What a cell is", "Structures common to all cells", "Plant cells"]

    def fake_script(episode, analysis, chapter_num, client, narration_style, part_info=None, language="en",
                    must_cover=None, avatars=None, **kw):
        scripts.append({"style": narration_style, "avatars": avatars, "part_info": part_info, "language": language})
        n = episode["episode_num"]
        segs = [{"segment_id": f"s{n}{i:02d}", "type": "explore", "text": f"Teacher {n}.{i}. Student asks.",
                 "elevenlabs_text": f"Teacher {n}.{i}. Student asks.", "slide_heading": h, "slide_points": [],
                 "dialogue": [{"who": "teacher", "line": f"Teacher {n}.{i}."}, {"who": "student", "line": "Student asks."}],
                 "estimated_duration_seconds": 50} for i, h in enumerate(heads, start=1)]
        return _Dump({"script_id": f"sc{n}", "book_id": "catalogue-t-cell", "chapter_num": -1, "episode_num": n,
                      "episode_title": episode["title"], "generated_at": "now", "narrator_persona": narration_style,
                      "segments": segs, "visual_plan": None, "avatars": avatars,
                      "total_estimated_duration_seconds": 150, "question_hook_count": 0})

    def fake_slides(script_data, branding=None, direction="ltr", build_deck=True, **kw):
        segs = script_data["episodes"][0]["segments"]
        slide_inputs.append([s["segment_id"] for s in segs])
        return _Dump({"segments": [{"segment_id": s["segment_id"], "slide_image_path": None} for s in segs], "deck_path": None})

    def fake_compose(script_data, slide_manifest, branding=None, tts_voice=None, allow_premium=False,
                     voice_report=None, direction="ltr", lang=None, student_voice=None, **kw):
        compose_calls.append({"student_voice": student_voice, "tts_voice": tts_voice, "lang": lang})
        segs = slide_manifest["segments"]
        return _Dump({"segments": [{"segment_id": s["segment_id"], "audio_duration_seconds": 50.0,
                                    "video_path": "/tmp/x.mp4", "audio_path": "/tmp/x.mp3"} for s in segs],
                     "total_duration_seconds": 50.0 * len(segs)})

    def fake_render(video_manifest):
        p = tmp_path / f"final{len(renders)}.mp4"
        p.write_bytes(b"mp4")
        renders.append(p)
        return _Dump({"final_video_path": str(p)})

    monkeypatch.setattr("agent3_scripts.script_generator.generate_episode_script", fake_script)
    monkeypatch.setattr("agent3_scripts.script_generator.save_script", lambda s: None)
    monkeypatch.setattr("agent5_slides.slide_generator.generate_episode_slides", fake_slides)
    monkeypatch.setattr("agent6_animation.video_composer.compose_episode_videos", fake_compose)
    monkeypatch.setattr("agent8_render.renderer.render_final_video", fake_render)
    monkeypatch.setattr("shared.coverage.measure", lambda *a, **k: {"verdict": "ok", "covered": 1.0, "addressed": 2,
                                                                  "topics": 2, "missed": []})
    monkeypatch.setattr("shared.coverage.should_retry", lambda *a, **k: False)
    return {"scripts": scripts, "slide_inputs": slide_inputs, "compose_calls": compose_calls, "renders": renders}


def _stages(sb) -> list:
    """Every jobs.stage the worker wrote, in order (None = cleared)."""
    return [e[2]["stage"] for e in sb.writes("jobs") if e[0] == "update" and "stage" in e[2]]


class TestProcessCatalogue:
    def test_a_catalogue_document_is_built_from_the_article_and_drives_the_kit(self, monkeypatch):
        sb = _sb(statuses={"gen-a": "processing", "gen-p": "done"})
        sb.tables["topic_kits"][0]["doc_generation_ids"] = {**KIT["doc_generation_ids"], "lesson_plan": "gen-l"}
        sb.tables["generations"].append({**_gen("gen-l", "done"), "kind": "lesson_plan"})
        sb.tables["jobs"] = [_job("job-a", "gen-a", "activity")]
        process, uploads, analysis_calls = _worker_env(monkeypatch, sb)
        seen: dict = {}
        monkeypatch.setattr("docgen.generate_document", _fake_doc(seen))

        process.process_generation(sb, sb.tables["jobs"][0], "gen-a")

        assert ("select", "books") not in sb.calls, "a kit has no book and never asks for one"
        assert len(analysis_calls) == 1
        call = analysis_calls[0]
        assert call["level"] == "high_school" and call["book_id"] == "catalogue-t-cell"
        assert call["chunks_override"] and all("## " in c["text"] for c in call["chunks_override"]), "the article's own parts, verbatim"
        assert call["chapter_content"]["chapter_num"] == -1
        # params reach the builder untouched (curriculum_header et al. are W2's to read)
        assert seen["params"]["curriculum_header"] == HEADER and seen["params"]["catalogue"] is True
        assert seen["book"]["id"] is None and seen["book"]["title"] == "Cells" and seen["chapter"]["chapter_num"] == -1
        assert seen["language"] == "en"
        rows = [(r["kind"], r["storage_path"]) for r in sb.tables["artifacts"]]
        assert rows == [("docx", "sys/gen-a/activity.docx"), ("answer_key_docx", "sys/gen-a/answer_key.docx")]
        gen = next(g for g in sb.tables["generations"] if g["id"] == "gen-a")
        assert gen["status"] == "done" and gen["title"] == "Cells · Activities"
        assert sb.tables["jobs"][0]["status"] == "done"
        # the last generation finished → the kit and its topic are in review
        assert _kit(sb)["status"] == "in_review" and sb.tables["topics"][0]["status"] == "in_review"
        # nothing book-keyed was written
        assert not any(t in ("chapter_grounding", "books") for _, t in sb.calls)

    def test_a_composed_worksheet_renders_through_the_composer_with_no_analysis(self, monkeypatch):
        sb = _sb(kit_row=None)
        sb.tables["generations"] = [{"id": "gen-q", "kind": "worksheet", "status": "processing", "owner_id": "sys",
                                     "book_id": None, "chapter_ref": None,
                                     "params": {"catalogue": True, "topic_id": "t-cell", "question_set_id": "qs-1",
                                                "language": "en", "curriculum_header": HEADER}}]
        sb.tables["jobs"] = [{**_job("job-q", "gen-q", "worksheet"), "params": {"catalogue": True, "question_set_id": "qs-1"}}]
        process, uploads, analysis_calls = _worker_env(monkeypatch, sb)
        seen: list = []
        mod = types.ModuleType("catalogue.composer")

        def render_question_set(sb_, gen, generation_id, out_dir, base, branding, language):
            seen.append((gen["id"], generation_id, isinstance(out_dir, Path), base, branding, language))
            return "Cells · Worksheet (question bank)"

        mod.render_question_set = render_question_set
        monkeypatch.setitem(sys.modules, "catalogue.composer", mod)
        monkeypatch.setattr("docgen.generate_document", lambda **k: pytest.fail("the composer renders, not docgen"))

        process.process_generation(sb, sb.tables["jobs"][0], "gen-q")

        assert analysis_calls == [], "approved bank items need no analysis call"
        assert seen == [("gen-q", "gen-q", True, "sys/gen-q", {}, "en")]
        gen = sb.tables["generations"][0]
        assert gen["status"] == "done" and gen["title"] == "Cells · Worksheet (question bank)"
        assert sb.tables["jobs"][0]["status"] == "done" and ("select", "books") not in sb.calls

    def test_an_unapproved_article_fails_before_any_model_call(self, monkeypatch):
        sb = _sb(article={**ARTICLE, "status": "draft"})
        sb.tables["jobs"] = [_job("job-a", "gen-a", "activity")]
        process, uploads, analysis_calls = _worker_env(monkeypatch, sb)
        monkeypatch.setattr("docgen.generate_document", lambda **k: pytest.fail("no build after a refusal"))
        with pytest.raises(kit.CatalogueRefused, match="not approved"):
            process.process_generation(sb, sb.tables["jobs"][0], "gen-a")
        assert analysis_calls == [] and uploads == []
        assert _kit(sb)["status"] == "failed"

    def test_a_failing_build_marks_the_kit_failed_and_re_raises(self, monkeypatch):
        sb = _sb(statuses={"gen-a": "processing"})
        sb.tables["jobs"] = [_job("job-a", "gen-a", "activity")]
        process, uploads, analysis_calls = _worker_env(monkeypatch, sb)

        def boom(**k):
            raise RuntimeError("docx exploded")

        monkeypatch.setattr("docgen.generate_document", boom)
        with pytest.raises(RuntimeError, match="docx exploded"):
            process.process_generation(sb, sb.tables["jobs"][0], "gen-a")
        assert _kit(sb)["status"] == "failed" and _kit(sb)["notes"] == "activity failed: docx exploded"
        assert sb.tables["topics"][0]["status"] == "generating"
        assert next(g for g in sb.tables["generations"] if g["id"] == "gen-a")["status"] == "processing", \
            "the job's terminal write is run.py's, after the re-raise"

    def test_the_presentation_path_end_to_end(self, monkeypatch, tmp_path):
        """Two parts: recap/outro framing, script.json beside every mp4, the
        student voice through compose, measured timestamps → topic_kits, and
        the lesson_plan spawned with the clips."""
        sb = _sb(statuses={"gen-p": "processing"})
        sb.tables["jobs"] = [_job("job-p", "gen-p", "presentation")]
        process, uploads, analysis_calls = _worker_env(monkeypatch, sb)
        fx = _presentation_fakes(monkeypatch, tmp_path)
        scripts, slide_inputs, compose_calls = fx["scripts"], fx["slide_inputs"], fx["compose_calls"]

        process.process_generation(sb, sb.tables["jobs"][0], "gen-p")

        assert analysis_calls[0]["chunks_override"] is not None and analysis_calls[0]["level"] == "high_school"
        assert [s["style"] for s in scripts] == ["conversational", "conversational"]
        assert scripts[0]["part_info"]["next_sections"] == ["Plant cells"] and scripts[1]["part_info"]["prev_sections"] == [
            "What a cell is", "Structures common to all cells"]
        # decision 3: framing AFTER the coverage gate, BEFORE the slides
        assert slide_inputs[0][-1] == "outro" and "recap" not in slide_inputs[0]
        assert slide_inputs[1][0] == "recap" and "outro" not in slide_inputs[1]
        # decision 2: the student voice reaches the composer on every part
        assert compose_calls == [{"student_voice": "g-en-student-m", "tts_voice": "g-en-f", "lang": "en"}] * 2
        # decision 4: script.json beside every mp4, in the same block
        rows = [(r["kind"], r["storage_path"]) for r in sb.tables["artifacts"]]
        assert rows == [("video_mp4", "sys/gen-p/lesson.mp4"), ("script_json", "sys/gen-p/script.json"),
                        ("video_mp4", "sys/gen-p/lesson_part2.mp4"), ("script_json", "sys/gen-p/script_part2.json")]
        body = json.loads(next(raw for _, d, raw in uploads if d.endswith("script_part2.json")).decode("utf-8"))
        assert (body["part"], body["of"], body["language"], body["voice"], body["student_voice"]) == (2, 2, "en", "g-en-f", "g-en-student-m")
        assert body["script"]["segments"][0]["segment_id"] == "recap" and set(body["avatars"]) == {"teacher", "student"}
        assert "video_path" not in json.dumps(body["video"]) and "audio_path" not in json.dumps(body["video"])
        assert len(body["video"]["segments"]) == 4
        # decision 5: measured timestamps on the kit, one record per part
        k = _kit(sb)
        assert [c["part"] for c in k["chapters"]] == [1, 2]
        p1 = k["chapters"][0]["chapters"]
        assert p1[0] == {"t": 0, "label": "What a cell is", "section_id": "s1"}
        assert [c["t"] for c in p1] == [0, 50, 100, 150] and p1[-1]["label"].startswith("Next")
        assert k["chapters"][1]["chapters"][0]["label"].startswith("Recap")
        assert k["clips"] and all(120 <= c["end"] - c["start"] <= 240 and c["part"] in (1, 2) for c in k["clips"])
        assert k["part_plan"] == [{"part": 1, "sections": ["What a cell is", "Structures common to all cells"], "minutes": 3.3},
                                  {"part": 2, "sections": ["Plant cells"], "minutes": 3.3}]
        # decision 6: the lesson plan exists now, citing the clips; the kit waits for it
        lp = next(g for g in sb.tables["generations"] if g["kind"] == "lesson_plan")
        assert lp["params"]["clips"] == k["clips"] and lp["params"]["lesson_modes"] is True
        assert k["doc_generation_ids"]["lesson_plan"] == lp["id"] and k["status"] == "generating"
        gen = next(g for g in sb.tables["generations"] if g["id"] == "gen-p")
        assert gen["status"] == "done" and gen["title"] == "Cells · Cells: the basic unit of life (2 parts)"
        assert gen["params"]["video_parts"] == 2
        assert gen["params"]["student_voice_requested"] == "g-en-student-m" and gen["params"]["student_voice_fallback"] is False
        assert ("select", "books") not in sb.calls and not any(t == "chapter_grounding" for _, t in sb.calls)


# ── the review fixes of 2026-09-06, at the worker level ─────────────────


class TestProcessCatalogueReviewFixes:
    def test_a_document_finishing_between_finish_job_and_the_lifecycle_leaves_the_kit_generating(self, monkeypatch, tmp_path):
        """WORKER_CONCURRENCY>1: thread A finishes the presentation; thread B
        finishes the kit's last document before A's lifecycle has inserted
        the lesson_plan. The kit must stay 'generating' — the plan is
        inserted BEFORE finish_job now, and a kit with a presentation but no
        plan is never complete."""
        sb = _sb(statuses={"gen-p": "processing", "gen-d": "processing"})
        sb.tables["jobs"] = [_job("job-p", "gen-p", "presentation")]
        process, uploads, _ = _worker_env(monkeypatch, sb)
        _presentation_fakes(monkeypatch, tmp_path)
        from worker import client as db
        real_finish, sibling = db.finish_job, []

        def finish_then_a_sibling_finishes(sb_, job_id, generation_id=None, error=None):
            real_finish(sb_, job_id, generation_id, error)
            if generation_id != "gen-p":
                return
            plans = [g for g in sb_.tables["generations"] if g["kind"] == "lesson_plan"]
            assert plans and plans[0]["status"] == "queued", "the plan exists BEFORE the presentation reads done"
            deck = next(g for g in sb_.tables["generations"] if g["id"] == "gen-d")
            deck["status"] = "done"                       # thread B, right now
            sibling.append(kit.after_generation(sb_, deck, "kit-1", {"status": "done", "kind": "deck"}))

        monkeypatch.setattr(db, "finish_job", finish_then_a_sibling_finishes)
        process.process_generation(sb, sb.tables["jobs"][0], "gen-p")

        assert sibling and sibling[0]["in_review"] is False, "the deck saw a queued lesson plan"
        k = _kit(sb)
        assert k["status"] == "generating" and sb.tables["topics"][0]["status"] == "generating"
        lp = next(g for g in sb.tables["generations"] if g["kind"] == "lesson_plan")
        assert lp["status"] == "queued" and k["doc_generation_ids"]["lesson_plan"] == lp["id"]
        assert next(g for g in sb.tables["generations"] if g["id"] == "gen-p")["status"] == "done"
        # the plan finishing is what completes the kit
        lp["status"] = "done"
        assert kit.after_generation(sb, lp, "kit-1", {"status": "done", "kind": "lesson_plan"})["in_review"] is True
        assert _kit(sb)["status"] == "in_review" and sb.tables["topics"][0]["status"] == "in_review"

    def test_a_failure_inside_prepare_marks_the_kit_failed(self, monkeypatch):
        """prepare() used to run outside the try: a topic that vanished (or
        any malformed row) raised with the kit left 'generating' forever —
        Generate disabled in the portal, no Retry offered."""
        sb = _sb(topic=None)
        sb.tables["jobs"] = [_job("job-a", "gen-a", "activity")]
        process, uploads, analysis_calls = _worker_env(monkeypatch, sb)
        with pytest.raises(kit.CatalogueRefused, match="topic t-cell not found"):
            process.process_generation(sb, sb.tables["jobs"][0], "gen-a")
        assert analysis_calls == [] and uploads == []
        assert _kit(sb)["status"] == "failed" and _kit(sb)["notes"] == "activity failed: topic t-cell not found"

    def test_a_document_whose_params_carry_no_header_gets_the_worker_composed_one(self, monkeypatch):
        """Decision 10's fallback: kit.prepare composes the header from the
        mappings, and the builder receives it when the params carry none."""
        sb = _sb(statuses={"gen-a": "processing", "gen-p": "done"})
        sb.tables["topic_kits"][0]["doc_generation_ids"] = {**KIT["doc_generation_ids"], "lesson_plan": "gen-l"}
        sb.tables["generations"].append({**_gen("gen-l", "done"), "kind": "lesson_plan"})
        next(g for g in sb.tables["generations"] if g["id"] == "gen-a")["params"] = {**PARAMS, "curriculum_header": None}
        sb.tables["jobs"] = [_job("job-a", "gen-a", "activity")]
        process, uploads, _ = _worker_env(monkeypatch, sb)
        seen: dict = {}
        monkeypatch.setattr("docgen.generate_document", _fake_doc(seen))
        process.process_generation(sb, sb.tables["jobs"][0], "gen-a")
        assert seen["params"]["curriculum_header"] == HEADER and seen["params"]["catalogue"] is True
        assert seen["params"]["teacher_avatar"] == "female", "the rest travels untouched"

    def test_a_composed_worksheet_is_handed_the_composed_header_too(self, monkeypatch):
        sb = _sb(kit_row=None)
        sb.tables["generations"] = [{"id": "gen-q", "kind": "worksheet", "status": "processing", "owner_id": "sys",
                                     "book_id": None, "chapter_ref": None,
                                     "params": {"catalogue": True, "topic_id": "t-cell", "question_set_id": "qs-1", "language": "en"}}]
        sb.tables["jobs"] = [{**_job("job-q", "gen-q", "worksheet"), "params": {"catalogue": True, "question_set_id": "qs-1"}}]
        process, uploads, analysis_calls = _worker_env(monkeypatch, sb)
        seen: list = []
        mod = types.ModuleType("catalogue.composer")
        mod.render_question_set = lambda sb_, gen, *a, **k: (seen.append(gen["params"]), "Cells · Quick check")[1]
        monkeypatch.setitem(sys.modules, "catalogue.composer", mod)
        process.process_generation(sb, sb.tables["jobs"][0], "gen-q")
        assert seen and seen[0]["curriculum_header"] == HEADER and seen[0]["question_set_id"] == "qs-1"

    def test_a_catalogue_unit_without_a_chapter_is_refused_outside_the_composed_branch(self, monkeypatch):
        """The backstop behind kit.prepare's refusals: whatever reaches the
        shared build without an article chapter, and is not the composed
        worksheet, must not analyse an empty stub."""
        sb = _sb()
        process, uploads, analysis_calls = _worker_env(monkeypatch, sb)
        prepared = kit.prepare(sb, _gen("gen-a"))
        prepared.chapter, prepared.chunks, prepared.question_set_id = None, [], None
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            unit = process._catalogue_unit(prepared, tmp)
            with pytest.raises(RuntimeError, match="no article chapter"):
                process._build_from_analysis(sb, _job("job-a", "gen-a", "activity"), "gen-a", _gen("gen-a"), unit, tmp,
                                             allow_premium=False, tier_info={}, canary_provider=None)
        assert analysis_calls == [], "no model call on nothing"

    def _yielding_presentation(self, monkeypatch, tmp_path, answers, **env):
        sb = _sb(statuses={"gen-p": "processing"})
        sb.tables["jobs"] = [_job("job-p", "gen-p", "presentation")]
        process, uploads, _ = _worker_env(monkeypatch, sb)
        fx = _presentation_fakes(monkeypatch, tmp_path)
        monkeypatch.setenv("CATALOGUE_YIELD_POLL_SECONDS", "0")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(kit, "PROBE_TTL_S", 0.0)          # every poll re-reads the queue
        asked: list = []

        def builder_queued(sb_):
            asked.append(len(asked))
            return answers[min(len(asked) - 1, len(answers) - 1)]

        monkeypatch.setattr("catalogue.figures.builder_queued", builder_queued)
        return sb, process, fx, asked

    def test_a_user_builder_arriving_between_parts_pauses_the_render_until_the_queue_clears(self, monkeypatch, tmp_path):
        # the probe answers: part 2 asks → contended, contended, clear
        sb, process, fx, asked = self._yielding_presentation(monkeypatch, tmp_path, [True, True, False])
        process.process_generation(sb, sb.tables["jobs"][0], "gen-p")
        assert len(fx["renders"]) == 2, "both parts rendered once the teacher's job was through"
        assert asked == [0, 1, 2], "polled until the queue cleared, no more"
        paused = [s for s in _stages(sb) if s and s.get("paused")]
        assert paused and paused[0]["paused"] == kit.PAUSED_FOR_USERS and paused[0]["part"] == 2 and paused[0]["total"] == 2
        assert _stages(sb)[-1] is None and _kit(sb)["status"] == "generating"

    def test_a_user_builder_that_outlasts_the_cap_fails_the_kit_instead_of_rendering_over_it(self, monkeypatch, tmp_path):
        sb, process, fx, asked = self._yielding_presentation(monkeypatch, tmp_path, [True], CATALOGUE_YIELD_MAX_SECONDS="0")
        with pytest.raises(RuntimeError, match="retry inside the off-peak window"):
            process.process_generation(sb, sb.tables["jobs"][0], "gen-p")
        assert len(fx["renders"]) == 1, "part 1 was rendered before the teacher arrived; part 2 was never started"
        assert _kit(sb)["status"] == "failed" and "yields" in _kit(sb)["notes"]

    def test_a_quiet_queue_costs_one_probe_per_part_and_no_pause(self, monkeypatch, tmp_path):
        sb, process, fx, asked = self._yielding_presentation(monkeypatch, tmp_path, [False])
        process.process_generation(sb, sb.tables["jobs"][0], "gen-p")
        assert len(fx["renders"]) == 2 and asked == [0], "one read before part 2; nothing before part 1"
        assert not [s for s in _stages(sb) if s and s.get("paused")]

    def test_the_engine_yield_hook_is_armed_for_the_build_and_removed_after(self, monkeypatch):
        """The scene engine consults the same probe before every image call:
        the worker registers the hook for THIS generation before the build
        and removes it afterwards — success or failure."""
        sb = _sb(statuses={"gen-a": "processing", "gen-p": "done"})
        sb.tables["topic_kits"][0]["doc_generation_ids"] = {**KIT["doc_generation_ids"], "lesson_plan": "gen-l"}
        sb.tables["generations"].append({**_gen("gen-l", "done"), "kind": "lesson_plan"})
        sb.tables["jobs"] = [_job("job-a", "gen-a", "activity")]
        process, uploads, _ = _worker_env(monkeypatch, sb)
        monkeypatch.setattr("docgen.generate_document", _fake_doc({}))
        hooks: list = []
        import spike.scene_engine.raster_assets as ra
        monkeypatch.setattr(ra, "set_user_yield", lambda fn, gen_id=None: hooks.append((fn, gen_id)))
        monkeypatch.setenv("CATALOGUE_YIELD_POLL_SECONDS", "0")
        monkeypatch.setattr(kit, "PROBE_TTL_S", 0.0)
        answers = iter([True, False])
        monkeypatch.setattr("catalogue.figures.builder_queued", lambda sb_: next(answers, False))

        process.process_generation(sb, sb.tables["jobs"][0], "gen-a")

        assert [g for _, g in hooks] == ["gen-a", "gen-a"] and hooks[0][0] is not None and hooks[1][0] is None
        assert hooks[0][0]("image for cell") is True, "the hook waits out the contended poll and clears"

        # removed on failure too
        sb2 = _sb(statuses={"gen-a": "processing"})
        sb2.tables["jobs"] = [_job("job-a", "gen-a", "activity")]
        process, _, _ = _worker_env(monkeypatch, sb2)
        hooks.clear()

        def boom(**k):
            raise RuntimeError("docx exploded")

        monkeypatch.setattr("docgen.generate_document", boom)
        with pytest.raises(RuntimeError):
            process.process_generation(sb2, sb2.tables["jobs"][0], "gen-a")
        assert [g for _, g in hooks] == ["gen-a", "gen-a"] and hooks[1][0] is None


class TestRepointLessonPlan:
    """The kit's lesson_plan pointer goes through repoint_kit_generation
    (0115): one merged statement with a compare-and-swap, the same call the
    portal's Retry makes, so neither writer can lose a key the other merged
    in between (review of app PR #40)."""

    def test_a_fresh_lesson_plan_is_pointed_through_the_rpc_replacing_nothing(self):
        sb = _sb()
        res = kit.after_generation(sb, _gen("gen-p", "done"), "kit-1", {"status": "done", "kind": "presentation", "parts": PARTS})
        assert len(sb.rpc_calls) == 1 and sb.rpc_calls[0][0] == "repoint_kit_generation"
        assert sb.rpc_calls[0][1] == {"p_kit": "kit-1", "p_kind": "lesson_plan", "p_generation": res["lesson_plan"], "p_replaces": None}
        assert _kit(sb)["doc_generation_ids"]["lesson_plan"] == res["lesson_plan"]
        # and the merge kept every sibling key the kit already carried
        assert {k: v for k, v in _kit(sb)["doc_generation_ids"].items() if k != "lesson_plan"} == KIT["doc_generation_ids"]
        assert not [w for w in sb.writes("topic_kits") if w[0] == "update" and "doc_generation_ids" in w[2]], \
            "the pointer is written by the function, never by a read-modify-write of the row"

    def test_a_replaced_plan_names_the_id_it_replaces(self):
        old = [{"part": 1, "start": 0, "end": 130, "label": "What a cell is", "purpose": "introduce"}]
        sb = _sb(kit_row={**KIT, "doc_generation_ids": {**KIT["doc_generation_ids"], "lesson_plan": "gen-l"}})
        sb.tables["generations"].append({**_gen("gen-l", "done", clips=old), "kind": "lesson_plan"})
        res = kit.after_generation(sb, _gen("gen-p", "done"), "kit-1", {"status": "done", "kind": "presentation", "parts": PARTS})
        _, args = next(c for c in sb.rpc_calls if c[0] == "repoint_kit_generation")
        assert args["p_replaces"] == "gen-l" and args["p_generation"] == res["lesson_plan"]
        assert _kit(sb)["doc_generation_ids"]["lesson_plan"] == res["lesson_plan"]

    def test_a_pointer_that_moved_underneath_is_refused_not_overwritten(self):
        """The row the worker READ said the plan was gen-l; by the time it
        writes, somebody (a Retry) has pointed the kit at gen-x. The swap is
        refused, the kit still points at gen-x, and the failure is recorded
        on the kit for the portal — never a silent overwrite."""
        old = [{"part": 1, "start": 0, "end": 130, "label": "What a cell is", "purpose": "introduce"}]
        stale = {**KIT, "doc_generation_ids": {**KIT["doc_generation_ids"], "lesson_plan": "gen-l"}}
        sb = _sb(kit_row={**stale, "doc_generation_ids": {**stale["doc_generation_ids"], "lesson_plan": "gen-x"}})
        sb.tables["generations"].append({**_gen("gen-l", "done", clips=old), "kind": "lesson_plan"})
        sb.tables["generations"].append({**_gen("gen-x", "queued", clips=old), "kind": "lesson_plan"})
        with pytest.raises(RuntimeError, match="pointer moved"):
            kit.insert_lesson_plan(sb, _gen("gen-p", "done"), dict(stale))
        assert _kit(sb)["doc_generation_ids"]["lesson_plan"] == "gen-x"

    def test_a_database_without_the_function_falls_back_to_the_merge_with_a_warning(self, caplog):
        from tests.catalogue_fakes import RpcError
        sb = _sb()

        class _NoRpc:
            def __init__(self, *a):
                pass

            def execute(self):
                raise RpcError("PGRST202", "Could not find the function public.repoint_kit_generation in the schema cache")

        sb.rpc = lambda name, params=None: _NoRpc()
        with caplog.at_level("WARNING", logger="worker.kit"):
            res = kit.after_generation(sb, _gen("gen-p", "done"), "kit-1", {"status": "done", "kind": "presentation", "parts": PARTS})
        assert _kit(sb)["doc_generation_ids"]["lesson_plan"] == res["lesson_plan"]
        assert any("apply app migration 0115" in r.getMessage() for r in caplog.records)
