"""catalogue/youtube_enrich.py — the discipline playlist line, the board and
class hashtags and the tags field, on the videos already posted (the
manual job) and on every publish (founder, 2026-09-29)."""

from __future__ import annotations

from catalogue import youtube_enrich as E
from catalogue.youtube_meta import DESCRIPTION_MAX, SKETCHCAST_LINE, compose_description
from shared.outro import DESCRIPTION_CTA
from tests.catalogue_fakes import FakeSB
from tests.test_youtube_backfill import FakeSnippets

BOARDS = [("Cambridge Lower Secondary Science 0893", "7"), ("CBSE Science (Class 6-10)", "9")]
LINK = "https://sketchcast.app/?utm_source=youtube"


def _description(hashtags=("Cells", "Organelles", "Science", "CambridgeStage7", "SketchCast")):
    return compose_description(topic_title="Cells", summary="The cell as the basic unit of life.", meta=None,
                               header_lines=["Cambridge Lower Secondary Science 0893 · 7Bs.01"],
                               chapter_lines_=["0:00 Intro", "1:00 Organelles", "3:00 Recap"],
                               key_terms=["cell", "nucleus"], hashtags=list(hashtags), part=1, total=1,
                               next_title=None, link=LINK)


class TestThePlaylistLine:
    def test_goes_directly_above_the_sketchcast_line_and_nothing_else_moves(self):
        before = _description()
        after = E.with_playlist_line(before, E.playlist_line("biology", "Science", "PL-bio"))
        paras = after.split("\n\n")
        i = paras.index("More Biology lessons: https://www.youtube.com/playlist?list=PL-bio")
        assert paras[i + 1].startswith(SKETCHCAST_LINE) and paras[i - 1] == DESCRIPTION_CTA
        assert paras[:i] + paras[i + 1:] == before.split("\n\n")

    def test_a_second_run_replaces_the_line_instead_of_stacking(self):
        once = E.with_playlist_line(_description(), E.playlist_line("biology", "Science", "PL-bio"))
        again = E.with_playlist_line(once, E.playlist_line("biology", "Science", "PL-bio2"))
        assert again.count("More Biology lessons") == 1 and "PL-bio2" in again and "PL-bio\n" not in again + "\n"

    def test_no_playlist_means_no_line(self):
        assert E.playlist_line("biology", "Science", "") == ""
        assert E.with_playlist_line(_description(), "") == _description()

    def test_without_a_sketchcast_line_it_goes_above_the_hashtags_or_at_the_end(self):
        assert E.with_playlist_line("Intro.\n\n#A #B", "More X lessons: u").split("\n\n") == ["Intro.", "More X lessons: u", "#A #B"]
        assert E.with_playlist_line("Intro.", "More X lessons: u") == "Intro.\n\nMore X lessons: u"

    def test_earth_science_reads_as_physics_and_no_discipline_falls_back_to_the_subject(self):
        assert E.discipline_name("physics", "Science") == "Physics"
        assert E.discipline_name("", "Mathematics") == "Mathematics"


class TestTheHashtags:
    def test_the_topics_own_tags_stay_first_and_the_line_is_capped_at_fifteen(self):
        before = _description(hashtags=[f"T{i}" for i in range(12)])
        after = E.enrich_description(before, discipline="biology", subject="Science", boards=BOARDS)
        tags = E.hashtags_in(after)
        assert tags[:12] == [f"T{i}" for i in range(12)] and len(tags) == E.HASHTAG_LIMIT
        assert tags[12:] == ["Biology", "Science", "Cambridge"]

    def test_the_additions_name_the_discipline_the_boards_the_class_and_the_form(self):
        assert E.extra_hashtags("biology", "Science", BOARDS) == [
            "Biology", "Science", "Cambridge", "CambridgeStage7", "CBSE", "CBSEClass9", "NCERT",
            "Stage7Science", "Class9Science", "ScienceLesson", "WhiteboardAnimation", "SketchCast"]
        assert "NCERT" not in E.extra_hashtags("physics", "Science", BOARDS[:1])
        assert E.extra_hashtags("algebra", "Mathematics", [("Cambridge Lower Secondary Mathematics 0862", "8")])[:4] == [
            "Algebra", "Mathematics", "Cambridge", "CambridgeStage8"]

    def test_merging_is_case_insensitive_and_keeps_order(self):
        assert E.merge_hashtags(["Science", "cbse"], ["Biology", "CBSE", "science"]) == ["Science", "Cbse", "Biology"]

    def test_the_line_is_rebuilt_in_place_and_a_description_without_one_gains_one(self):
        assert E.with_hashtags("Intro.\n\n#A #B", ["C"]) == "Intro.\n\n#C"
        assert E.with_hashtags("Intro.", ["C", "D"]) == "Intro.\n\n#C #D"
        assert E.hashtags_in("Intro.") == [] and E.hashtags_in("Intro.\n\n#A #B c") == ["A", "B"]

    def test_the_whole_enrichment_is_idempotent_and_never_passes_the_limit(self):
        once = E.enrich_description(_description(), discipline="biology", subject="Science", boards=BOARDS,
                                    playlist_id="PL-bio")
        assert E.enrich_description(once, discipline="biology", subject="Science", boards=BOARDS,
                                    playlist_id="PL-bio") == once
        long = "x" * (DESCRIPTION_MAX - 10) + "\n\n#A"
        assert E.enrich_description(long, discipline="biology", subject="Science", boards=BOARDS,
                                    playlist_id="PL-bio") == long, "left alone rather than cut"

    def test_a_superseded_videos_pointer_stays_first(self):
        pointed = "An updated version of this lesson is available: https://www.youtube.com/watch?v=new\n\n" + _description()
        after = E.enrich_description(pointed, discipline="biology", subject="Science", boards=BOARDS, playlist_id="PL-bio")
        assert after.split("\n\n")[0].startswith("An updated version")


class TestTheTags:
    def test_what_is_there_comes_first_then_title_terms_discipline_boards_and_form(self):
        tags = E.video_tags(topic_title="Cells", key_terms=["cell", "nucleus"], discipline="biology", subject="Science",
                            boards=BOARDS, existing=["Science"])
        assert tags[:5] == ["Science", "Cells", "cell", "nucleus", "Biology"]
        for want in ("Biology lesson", "Science lesson", "Cambridge Stage 7 Science", "Stage 7 Biology",
                     "CBSE Class 9 Science", "Class 9 Science", "NCERT", "whiteboard animation", "SketchCast"):
            assert want in tags
        assert len(tags) == len({t.lower() for t in tags}), "distinct, case-insensitively"

    def test_the_field_stays_within_youtubes_500_characters_and_a_long_tag_is_dropped(self):
        terms = [f"a rather long key term number {i}" for i in range(40)]
        tags = E.video_tags(topic_title="T", key_terms=terms, discipline="physics", subject="Science", boards=BOARDS)
        assert sum(len(t) + (2 if " " in t else 0) + 1 for t in tags) <= E.TAGS_CHAR_LIMIT
        assert all(len(t) <= E.TAG_MAX_LEN for t in tags)
        assert "SketchCast" in tags, "a short tag past a long one still fits"


# ── the job ────────────────────────────────────────────────────────────────

def _sb():
    sb = FakeSB()
    sb.tables["curricula"] = [{"id": "cur-1", "code": "cambridge", "name": "Cambridge Lower Secondary Science 0893"},
                              {"id": "cur-2", "code": "cbse", "name": "CBSE Science (Class 6-10)"}]
    sb.tables["curriculum_nodes"] = [
        {"id": "n1", "curriculum_id": "cur-1", "code": "7Bs.01", "kind": "objective", "grade": "7"},
        {"id": "n2", "curriculum_id": "cur-2", "code": "cbse:9:U1:01", "kind": "topic", "grade": "9", "title": "Cells"},
        {"id": "n3", "curriculum_id": "cur-1", "code": "8ESc.01", "kind": "objective", "grade": "8"},
    ]
    sb.tables["topics"] = [
        {"id": "t1", "title": "Cells", "subject": "Science", "summary": "Nucleus, vacuole — the parts of a cell."},
        {"id": "t2", "title": "Weather and Climate", "subject": "Science", "summary": "Air masses, fronts — weather."},
    ]
    sb.tables["topic_curriculum_map"] = [{"topic_id": "t1", "node_id": "n1"}, {"topic_id": "t1", "node_id": "n2"},
                                         {"topic_id": "t2", "node_id": "n3"}]
    sb.tables["topic_kits"] = [
        {"id": "k1", "topic_id": "t1", "status": "approved",
         "youtube_meta": {"key_terms": ["cell", "nucleus"], "hashtags": ["Cells", "Organelles"]}},
        {"id": "k2", "topic_id": "t2", "status": "approved", "youtube_meta": None},
        {"id": "k3", "topic_id": "t1", "status": "approved", "youtube_meta": None},
    ]
    sb.tables["topic_publications"] = [
        {"id": "p1", "topic_kit_id": "k1", "part": 1, "channel_language": "en", "youtube_video_id": "v-cells",
         "privacy": "public", "published_at": "2026-09-10T00:00:00Z", "superseded_by": "p3"},
        {"id": "p2", "topic_kit_id": "k2", "part": 1, "channel_language": "en", "youtube_video_id": "v-weather",
         "privacy": "public", "published_at": "2026-09-11T00:00:00Z", "superseded_by": None},
        {"id": "p3", "topic_kit_id": "k3", "part": 1, "channel_language": "en", "youtube_video_id": "v-cells-2",
         "privacy": "private", "published_at": "2026-09-12T00:00:00Z", "superseded_by": None},
        {"id": "p4", "topic_kit_id": "k-gone", "part": 1, "channel_language": "en", "youtube_video_id": "v-orphan",
         "privacy": "public", "published_at": "2026-09-13T00:00:00Z", "superseded_by": None},
    ]
    sb.tables.setdefault("platform_settings", []).append({"key": "youtube_playlists_en",
                                                          "value": {"biology": "PL-bio", "physics": "PL-phy"}})
    sb.tables["jobs"].append({"id": "job-1", "type": E.JOB_TYPE, "status": "processing", "attempts": 0,
                              "params": {"language": "en"}})
    return sb


def _job(sb):
    return next(j for j in sb.tables["jobs"] if j["id"] == "job-1")


def _snippet(description, tags=("Science",)):
    return {"title": "T", "description": description, "tags": list(tags), "categoryId": "27",
            "defaultLanguage": "en", "defaultAudioLanguage": "en", "publishedAt": "x", "channelId": "c"}


class TestTheJob:
    def test_every_video_gains_the_line_the_hashtags_and_the_tags(self, monkeypatch):
        monkeypatch.delenv("YOUTUBE_PLAYLISTS_EN", raising=False)
        sb = _sb()
        yt = FakeSnippets({
            "v-cells": _snippet("An updated version of this lesson is available: u\n\n" + _description()),
            "v-weather": _snippet("Weather.\n\n" + SKETCHCAST_LINE + "\n" + LINK + "\n\n#Weather #Science"),
            "v-cells-2": _snippet(_description()),
        })
        summary = E.run_enrich_job(sb, _job(sb), transport=yt)
        assert summary["counts"] == {"checked": 3, "updated": 3, "unchanged": 0, "missing": 1, "skipped": 0, "errors": 0}
        assert summary["missing"] == ["v-orphan"], "a kit that is gone is reported, never guessed"
        by = dict(yt.updates)
        assert "More Biology lessons: https://www.youtube.com/playlist?list=PL-bio" in by["v-cells"]["description"]
        assert by["v-cells"]["description"].startswith("An updated version")
        assert "More Physics lessons: https://www.youtube.com/playlist?list=PL-phy" in by["v-weather"]["description"]
        assert E.hashtags_in(by["v-weather"]["description"])[:4] == ["Weather", "Science", "Physics", "Cambridge"]
        assert by["v-cells"]["tags"][:4] == ["Science", "Cells", "cell", "nucleus"]
        assert "CBSE Class 9 Science" in by["v-cells"]["tags"] and "NCERT" in by["v-cells"]["tags"]
        assert "NCERT" not in by["v-weather"]["tags"]
        assert set(by["v-cells"]) == set(E.SNIPPET_FIELDS), "read-only snippet fields are not sent back"
        assert by["v-cells"]["title"] == "T" and by["v-cells"]["categoryId"] == "27"
        assert _job(sb)["status"] == "done" and _job(sb)["stage"]["counts"]["updated"] == 3

    def test_a_re_run_writes_nothing(self, monkeypatch):
        monkeypatch.delenv("YOUTUBE_PLAYLISTS_EN", raising=False)
        sb = _sb()
        yt = FakeSnippets({"v-cells": _snippet(_description()), "v-weather": _snippet("Weather."),
                           "v-cells-2": _snippet(_description())})
        E.run_enrich_job(sb, _job(sb), transport=yt)
        for vid, body in yt.updates:
            yt.snippets_by_id[vid] = {**yt.snippets_by_id[vid], **body}
        yt.updates.clear()
        _job(sb)["status"] = "processing"
        summary = E.run_enrich_job(sb, _job(sb), transport=yt)
        assert yt.updates == [] and summary["counts"]["unchanged"] == 3

    def test_one_failing_update_is_recorded_and_the_rest_go_on(self):
        sb = _sb()
        yt = FakeSnippets({"v-cells": _snippet("a"), "v-weather": _snippet("b"), "v-cells-2": _snippet("c")},
                          fail={"v-weather"})
        summary = E.run_enrich_job(sb, _job(sb), transport=yt)
        assert summary["counts"]["updated"] == 2 and summary["counts"]["errors"] == 1
        assert _job(sb)["status"] == "error" and "Weather and Climate" in _job(sb)["error"]

    def test_a_youtube_failure_fails_the_job(self):
        sb = _sb()
        assert E.run_enrich_job(sb, _job(sb), transport=FakeSnippets({}, fail={"list"})) is None
        assert _job(sb)["status"] == "error" and "quota" in _job(sb)["error"]


class TestWiring:
    def test_the_worker_dispatches_and_observes_the_job_type(self):
        from pathlib import Path
        import worker.run as run
        from worker import client as db
        assert E.JOB_TYPE in run.CATALOGUE_JOB_TYPES and E.JOB_TYPE in db.OBSERVER_JOB_TYPES
        src = Path(run.__file__).read_text(encoding="utf-8")
        assert 'job_type == "youtube_enrich"' in src and "run_enrich_job(sb, job)" in src
