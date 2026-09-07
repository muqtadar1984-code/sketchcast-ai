"""``topic_publish`` — the second human gate, re-checked in the worker.

These tests CALL the job. Every one of them uses ``FakeYouTube`` (the
transport protocol in memory) and ``FakeSB`` (an in-memory postgrest with a
storage shim), so NOTHING here can reach the network. The real transport is
built only by ``default_transport``, which refuses before it imports anything
unless three environment variables carry credentials — and this suite never
sets them (``test_the_real_transport_is_never_built_without_credentials``
pins that, and every other test injects the fake).

What the tests hold in place:
  * the gate (plan §1.3): a kit that is not approved, a topic whose bank is
    empty, an article superseded after the kit was built, and anything but
    private without the compliance audit are REFUSED before a request;
  * ordering: parts go up by part NUMBER, never by storage path (as strings,
    lesson_part10 sorts before lesson_part2 and lesson.mp4 sorts last);
  * the description carries the curriculum codes and a 0:00 chapter;
  * idempotence: a part that already has a youtube_video_id is skipped, so a
    re-run finishes an interrupted publish rather than doubling it;
  * a caption failure records itself on the row and keeps the video;
  * the per-run cap says what it left behind;
  * observer classification and the run.py dispatch.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from catalogue import publish as P
from tests.catalogue_fakes import FakeSB, FakeYouTube
from worker import client as db

# Captured before the autouse fixture below replaces it, so the one test that
# is ABOUT the probe can call the real thing.
REAL_BUILDER_PROBE = P.builders_are_queued

KIT = "kit-1"
TOPIC = "topic-1"
ARTICLE = "art-1"
GEN = "gen-1"
JOB = "job-pub"
OWNER = "owner-1"


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """Phase 4 is dark by default; every test but the flag test turns it on."""
    monkeypatch.setenv(P.FEATURE_FLAG, "1")
    monkeypatch.delenv(P.AUDIT_FLAG, raising=False)
    monkeypatch.delenv(P.MAX_PARTS_ENV, raising=False)
    monkeypatch.delenv("YOUTUBE_PLAYLISTS_EN", raising=False)
    for name in (P.CLIENT_ID_ENV, P.CLIENT_SECRET_ENV, "YOUTUBE_REFRESH_TOKEN_EN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_builders(monkeypatch):
    """The never-starve probe is exercised by its own test; elsewhere the
    queue is empty so the run is not about contention."""
    monkeypatch.setattr(P, "builders_are_queued", lambda sb: False)


def _script_json(part: int) -> str:
    return json.dumps({
        "part": part, "of": 3, "language": "en",
        "script": {"segments": [{"segment_id": "s001", "text": "A cell is the basic unit. It has a membrane."},
                                {"segment_id": "s002", "text": "Plants also have a wall."}]},
        "video": {"segments": [{"segment_id": "s001", "audio_duration_seconds": 12.0},
                               {"segment_id": "s002", "audio_duration_seconds": 8.0}]},
    })


def _sb(*, parts=(1,), kit_status="approved", article_status="approved", bank="basic",
        chapters=None, publications=(), with_script=True) -> FakeSB:
    """A database with one approved kit whose presentation rendered ``parts``."""
    sb = FakeSB()
    sb.tables["topics"] = [{"id": TOPIC, "canonical_key": "cells", "title": "Cells", "subject": "Biology",
                            "summary": "Every living thing is made of cells.", "bank_maturity": bank,
                            "status": "in_review"}]
    sb.tables["topic_articles"] = [{"id": ARTICLE, "topic_id": TOPIC, "language": "en", "version": 1,
                                    "status": article_status}]
    if chapters is None:
        chapters = [{"part": p, "chapters": [{"t": 0, "label": "Introduction"},
                                             {"t": 95, "label": f"Part {p} body"},
                                             {"t": 400, "label": "Recap"}]} for p in parts]
    sb.tables["topic_kits"] = [{"id": KIT, "topic_id": TOPIC, "article_id": ARTICLE, "language": "en",
                                "presentation_generation_id": GEN, "chapters": chapters,
                                "status": kit_status}]
    # Curriculum mapping → the header lines every document already prints.
    sb.tables["curricula"] = [{"id": "cur-1", "code": "cambridge_ls_science_0893",
                               "name": "Cambridge Lower Secondary Science 0893"}]
    sb.tables["curriculum_nodes"] = [{"id": "node-1", "curriculum_id": "cur-1", "code": "7Bs.01",
                                      "kind": "objective", "grade": "7", "title": "Cell structure"}]
    sb.tables["topic_curriculum_map"] = [{"topic_id": TOPIC, "node_id": "node-1", "coverage": "full"}]

    artifacts = []
    for p in parts:
        suffix = "" if p == 1 else f"_part{p}"
        video = f"{OWNER}/{GEN}/lesson{suffix}.mp4"
        artifacts.append({"generation_id": GEN, "kind": "video_mp4", "storage_path": video})
        sb.files[("artifacts", video)] = f"mp4-part-{p}".encode()
        if with_script:
            script = f"{OWNER}/{GEN}/script{suffix}.json"
            artifacts.append({"generation_id": GEN, "kind": "script_json", "storage_path": script})
            sb.files[("artifacts", script)] = _script_json(p).encode()
    sb.tables["artifacts"] = artifacts
    sb.tables["topic_publications"] = [dict(r) for r in publications]
    sb.tables["jobs"] = [{"id": JOB, "type": P.JOB_TYPE, "status": "processing", "generation_id": None,
                          "book_id": None, "params": {"kit_id": KIT, "language": "en"}, "attempts": 0}]
    return sb


def _job(**params) -> dict:
    return {"id": JOB, "type": P.JOB_TYPE, "generation_id": None, "book_id": None,
            "params": {"kit_id": KIT, "language": "en", **params}}


def _job_row(sb) -> dict:
    return next(r for r in sb.tables["jobs"] if r["id"] == JOB)


def _pubs(sb) -> dict:
    return {int(r["part"]): r for r in sb.tables["topic_publications"]}


# ── the gate: plan §1.3, re-checked here, before any request ────────────


def test_an_unapproved_kit_is_refused_and_nothing_is_uploaded():
    sb = _sb(kit_status="in_review")
    yt = FakeYouTube()
    assert P.run_publish_job(sb, _job(), transport=yt) is None
    assert yt.uploads == [], "the gate ran before the transport"
    row = _job_row(sb)
    assert row["status"] == "error" and "not approved" in row["error"]
    assert sb.tables["topic_publications"] == []


@pytest.mark.parametrize("status", ["generating", "rejected", "failed"])
def test_every_non_approved_kit_status_is_refused(status):
    sb = _sb(kit_status=status)
    yt = FakeYouTube()
    assert P.run_publish_job(sb, _job(), transport=yt) is None
    assert yt.uploads == []
    assert status in _job_row(sb)["error"]


def test_a_topic_with_no_question_bank_is_refused():
    """plan §1.7: the description links to a worksheet, and an empty bank
    damages trust more than a missing video does."""
    sb = _sb(bank="none")
    yt = FakeYouTube()
    assert P.run_publish_job(sb, _job(), transport=yt) is None
    assert yt.uploads == []
    assert "bank_maturity" in _job_row(sb)["error"]


def test_a_bank_at_basic_or_above_passes_the_maturity_gate():
    for bank in ("basic", "good", "strong", "assessment", "exam_ready"):
        sb = _sb(bank=bank)
        yt = FakeYouTube()
        assert P.run_publish_job(sb, _job(), transport=yt) is not None, bank
        assert len(yt.uploads) == 1, bank


def test_a_superseded_article_is_refused():
    """The article was edited after the kit was built, so the video teaches a
    version nobody approved."""
    sb = _sb(article_status="draft")
    yt = FakeYouTube()
    assert P.run_publish_job(sb, _job(), transport=yt) is None
    assert yt.uploads == []
    assert "not approved" in _job_row(sb)["error"]


def test_the_flag_off_refuses_before_anything_is_read(monkeypatch):
    monkeypatch.delenv(P.FEATURE_FLAG, raising=False)
    sb = _sb()
    yt = FakeYouTube()
    assert P.run_publish_job(sb, _job(), transport=yt) is None
    assert yt.uploads == []
    assert P.FEATURE_FLAG in _job_row(sb)["error"]


def test_a_kit_with_no_video_artifact_is_refused():
    sb = _sb()
    sb.tables["artifacts"] = [r for r in sb.tables["artifacts"] if r["kind"] != "video_mp4"]
    yt = FakeYouTube()
    assert P.run_publish_job(sb, _job(), transport=yt) is None
    assert yt.uploads == [] and "no video_mp4" in _job_row(sb)["error"]


def test_a_job_without_a_kit_id_is_refused():
    sb = _sb()
    assert P.run_publish_job(sb, {"id": JOB, "params": {}}, transport=FakeYouTube()) is None
    assert "kit_id" in _job_row(sb)["error"]


# ── privacy: an unaudited project cannot publish anything but private ───


@pytest.mark.parametrize("privacy", ["public", "unlisted"])
def test_public_and_unlisted_are_refused_until_the_audit_passes(privacy):
    sb = _sb()
    yt = FakeYouTube()
    assert P.run_publish_job(sb, _job(privacy=privacy), transport=yt) is None
    assert yt.uploads == []
    assert "compliance audit" in _job_row(sb)["error"]


def test_the_audit_flag_lifts_the_privacy_refusal(monkeypatch):
    monkeypatch.setenv(P.AUDIT_FLAG, "1")
    assert P.check_privacy("unlisted") == "unlisted"


def test_an_unknown_privacy_value_is_refused():
    with pytest.raises(P.PublishRefused):
        P.check_privacy("semi-public")


def test_the_default_privacy_is_private():
    assert P.check_privacy(None) == P.PRIVACY_PRIVATE
    sb = _sb()
    yt = FakeYouTube()
    P.run_publish_job(sb, _job(), transport=yt)
    assert yt.uploads[0]["privacy"] == "private"
    assert _pubs(sb)[1]["privacy"] == "private"


# ── ordering: by part number, never by storage path ─────────────────────


def test_part_number_reads_the_artifact_name():
    assert P.part_number("o/g/lesson.mp4") == 1
    assert P.part_number("o/g/lesson_part2.mp4") == 2
    assert P.part_number("o/g/lesson_part10.mp4") == 10
    assert P.script_part_number("o/g/script.json") == 1
    assert P.script_part_number("o/g/script_part7.json") == 7


def test_parts_are_ordered_by_number_not_by_path():
    """Sorted as strings, lesson_part10 precedes lesson_part2 and lesson.mp4
    comes last — Part 1 would go up third."""
    rows = [{"storage_path": "o/g/lesson_part10.mp4"}, {"storage_path": "o/g/lesson_part2.mp4"},
            {"storage_path": "o/g/lesson.mp4"}]
    assert [p["part"] for p in P.ordered_parts(rows)] == [1, 2, 10]
    assert [p["part"] for p in P.ordered_parts(sorted(rows, key=lambda r: r["storage_path"]))] == [1, 2, 10]


def test_a_multi_part_kit_uploads_its_parts_in_order(monkeypatch):
    """The artifact rows arrive REVERSED, so passing this means the run
    ordered them and did not merely follow the table."""
    monkeypatch.setenv(P.MAX_PARTS_ENV, "10")
    sb = _sb(parts=(1, 2, 3))
    sb.tables["artifacts"] = list(reversed(sb.tables["artifacts"]))
    yt = FakeYouTube()
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert summary["published"] == [1, 2, 3]
    assert [u["bytes"] for u in yt.uploads] == [b"mp4-part-1", b"mp4-part-2", b"mp4-part-3"]
    assert [u["title"] for u in yt.uploads] == ["Cells — Part 1 of 3", "Cells — Part 2 of 3",
                                                "Cells — Part 3 of 3"]
    assert sorted(_pubs(sb)) == [1, 2, 3]
    assert _job_row(sb)["status"] == "done"


def test_a_kit_missing_a_middle_part_is_refused():
    """"Part 2 of 4" on a channel that will never carry Part 3 is worse than
    no video; a rendered presentation always has 1..N."""
    sb = _sb(parts=(1, 2, 10))
    yt = FakeYouTube()
    assert P.run_publish_job(sb, _job(), transport=yt) is None
    assert yt.uploads == []
    assert "not a run of 1..N" in _job_row(sb)["error"]


def test_a_single_part_kit_is_titled_with_the_topic_alone():
    sb = _sb(parts=(1,))
    yt = FakeYouTube()
    P.run_publish_job(sb, _job(), transport=yt)
    assert yt.uploads[0]["title"] == "Cells"


def test_a_long_title_keeps_its_part_label():
    """The label is what orders the parts for a viewer; the topic is what is
    cut when YouTube's 100 characters run out."""
    long = "The Structure and Function of Eukaryotic and Prokaryotic Cells in Living Organisms Everywhere"
    title = P.build_title(long, 2, 3)
    assert len(title) <= P.TITLE_MAX
    assert title.endswith(" — Part 2 of 3")


# ── the description ─────────────────────────────────────────────────────


def test_the_description_carries_the_curriculum_codes_and_a_zero_timestamp():
    sb = _sb(parts=(1, 2))
    yt = FakeYouTube()
    P.run_publish_job(sb, _job(), transport=yt)
    desc = yt.uploads[0]["description"]
    assert "Cambridge Lower Secondary Science 0893 · 7Bs.01" in desc
    assert "0:00 Introduction" in desc
    assert "1:35 Part 1 body" in desc, "m:ss, not raw seconds"
    assert "utm_source=youtube" in desc and "sketchcast.app" in desc
    assert "Every living thing is made of cells." in desc


def test_the_codes_are_the_documents_own_header_lines():
    """One alignment statement, one implementation: the publish description
    prints exactly what catalogue.kit.curriculum_header_lines composes for a
    document's header."""
    from catalogue.kit import curriculum_header_lines

    sb = _sb()
    assert curriculum_header_lines(sb, TOPIC) == ["Cambridge Lower Secondary Science 0893 · 7Bs.01"]
    yt = FakeYouTube()
    P.run_publish_job(sb, _job(), transport=yt)
    for line in curriculum_header_lines(sb, TOPIC):
        assert line in yt.uploads[0]["description"]


def test_a_multi_part_description_points_at_the_next_part_and_the_last_does_not(monkeypatch):
    monkeypatch.setenv(P.MAX_PARTS_ENV, "10")
    sb = _sb(parts=(1, 2))
    yt = FakeYouTube()
    P.run_publish_job(sb, _job(), transport=yt)
    assert "Next: Cells — Part 2 of 2" in yt.uploads[0]["description"]
    assert "Next:" not in yt.uploads[1]["description"]
    assert "Part 2 of 2." in yt.uploads[1]["description"]


def test_a_chapter_list_shorter_than_three_is_omitted_entirely():
    """YouTube ignores a malformed list and shows nothing; posting it would
    only be noise."""
    assert P.chapter_lines([{"t": 0, "label": "One"}, {"t": 10, "label": "Two"}]) == []
    assert len(P.chapter_lines([{"t": 0, "label": "One"}, {"t": 10, "label": "Two"},
                                {"t": 20, "label": "Three"}])) == 3


def test_a_chapter_list_not_starting_at_zero_is_omitted_entirely():
    assert P.chapter_lines([{"t": 5, "label": "One"}, {"t": 10, "label": "Two"},
                            {"t": 20, "label": "Three"}]) == []


def test_chapters_are_read_from_the_kits_own_per_part_shape():
    stored = [{"part": 1, "chapters": [{"t": 0, "label": "A"}]},
              {"part": 2, "chapters": [{"t": 0, "label": "B"}, {"t": 9, "label": "C"}]}]
    assert P.chapters_of(stored, 2) == [{"t": 0, "label": "B"}, {"t": 9, "label": "C"}]
    assert P.chapters_of(stored, 3) == []


def test_the_description_never_exceeds_youtubes_limit():
    topic = {"title": "Cells", "canonical_key": "cells", "summary": "x" * 200}
    chapters = [{"t": i * 7, "label": f"Chapter {i} " + "y" * 60} for i in range(400)]
    chapters[0]["t"] = 0
    desc = P.build_description(topic, ["Curriculum " + "z" * 200], chapters, 1, 1)
    assert len(desc) <= P.DESCRIPTION_MAX
    assert not desc.endswith("\n")


def test_hhmmss_switches_form_at_an_hour():
    assert P.hhmmss(0) == "0:00" and P.hhmmss(95) == "1:35"
    assert P.hhmmss(3599) == "59:59" and P.hhmmss(3600) == "1:00:00"


# ── idempotence ─────────────────────────────────────────────────────────


def test_a_part_that_already_has_a_video_id_is_skipped(monkeypatch):
    monkeypatch.setenv(P.MAX_PARTS_ENV, "10")
    published = [{"topic_kit_id": KIT, "part": 1, "channel_language": "en",
                  "youtube_video_id": "already-1", "privacy": "private"}]
    sb = _sb(parts=(1, 2), publications=published)
    yt = FakeYouTube()
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert summary["already"] == [1] and summary["published"] == [2]
    assert [u["bytes"] for u in yt.uploads] == [b"mp4-part-2"], "part 1 was not uploaded twice"
    assert _pubs(sb)[1]["youtube_video_id"] == "already-1", "the existing row is untouched"


def test_a_rerun_of_a_finished_kit_uploads_nothing_and_needs_no_credentials():
    published = [{"topic_kit_id": KIT, "part": 1, "channel_language": "en",
                  "youtube_video_id": "already-1", "privacy": "private"}]
    sb = _sb(parts=(1,), publications=published)
    # transport=None: default_transport would refuse for want of credentials,
    # so reaching 'done' proves nothing tried to build one.
    summary = P.run_publish_job(sb, _job(), transport=None)
    assert summary["already"] == [1] and summary["published"] == []
    assert _job_row(sb)["status"] == "done"


def test_a_row_left_by_a_failed_attempt_is_retried_not_skipped():
    failed = [{"topic_kit_id": KIT, "part": 1, "channel_language": "en", "youtube_video_id": None,
               "privacy": "private", "error": "part 1: RuntimeError: boom"}]
    sb = _sb(parts=(1,), publications=failed)
    yt = FakeYouTube()
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert summary["published"] == [1] and len(yt.uploads) == 1
    row = _pubs(sb)[1]
    assert row["youtube_video_id"] == "yt-1" and row["error"] is None


# ── captions, thumbnail and playlists never fail the video ─────────────


def test_captions_are_built_from_the_parts_transcript():
    sb = _sb(parts=(1,))
    yt = FakeYouTube()
    P.run_publish_job(sb, _job(), transport=yt)
    assert len(yt.captions) == 1
    srt = yt.captions[0]["srt"]
    assert "00:00:00,000 -->" in srt and "A cell is the basic unit." in srt
    assert "Plants also have a wall." in srt
    assert _pubs(sb)[1]["captions_uploaded"] == ["en"]


def test_a_caption_failure_does_not_fail_the_video_and_is_recorded():
    """captions.insert costs 400 of the day's 10,000 units and is the first
    call a quota ceiling takes away; a video without captions is still a
    published video."""
    sb = _sb(parts=(1,))
    yt = FakeYouTube(fail_on={"insert_caption"})
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert summary["published"] == [1] and summary["failed"] == []
    row = _pubs(sb)[1]
    assert row["youtube_video_id"] == "yt-1"
    assert row["captions_uploaded"] == []
    assert "captions failed" in row["error"]
    assert _job_row(sb)["status"] == "done", "the job succeeded: the video is up"


def test_a_kit_with_no_transcript_publishes_without_captions():
    sb = _sb(parts=(1,), with_script=False)
    yt = FakeYouTube()
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert summary["published"] == [1] and yt.captions == []
    assert "no transcript" in _pubs(sb)[1]["error"]


def test_a_thumbnail_failure_does_not_fail_the_video():
    sb = _sb(parts=(1,))
    yt = FakeYouTube(fail_on={"set_thumbnail"})
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert summary["published"] == [1]
    row = _pubs(sb)[1]
    assert row["thumbnail_set"] is False and "thumbnail failed" in row["error"]


def test_the_thumbnail_is_drawn_locally_and_costs_no_image_quota(tmp_path):
    """A generated thumbnail would take a slot from the one Vertex image pool
    a teacher's render shares — the never-starve rule. It is PIL, on this
    machine, from the topic's own title."""
    out = P.build_thumbnail({"title": "Cells", "subject": "Biology"}, 2, 3, tmp_path / "t.png")
    assert out is not None and out.exists()
    from PIL import Image

    assert Image.open(out).size == P.THUMBNAIL_SIZE


def test_playlists_come_from_the_environment_and_a_missing_one_is_only_logged(monkeypatch, caplog):
    """An unconfigured playlist is the SAME on every video until the founder
    creates it, so it is logged and kept off the row: a caveat that is always
    there teaches the reviewer to ignore the caveat field."""
    monkeypatch.setenv("YOUTUBE_PLAYLISTS_EN", json.dumps({"biology": "PL-bio"}))
    sb = _sb(parts=(1,))
    yt = FakeYouTube()
    with caplog.at_level("INFO", logger="worker.publish"):
        P.run_publish_job(sb, _job(), transport=yt)
    assert yt.playlist_adds == [{"video_id": "yt-1", "playlist_id": "PL-bio"}]
    row = _pubs(sb)[1]
    assert row["playlist_ids"] == ["PL-bio"]
    assert row["error"] is None
    assert "no playlist configured for cambridge lower secondary science 0893" in caplog.text


def test_a_failing_playlist_add_is_recorded_and_keeps_the_video(monkeypatch):
    monkeypatch.setenv("YOUTUBE_PLAYLISTS_EN", json.dumps({"biology": "PL-bio"}))
    sb = _sb(parts=(1,))
    yt = FakeYouTube(fail_on={"add_to_playlist"})
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert summary["published"] == [1]
    row = _pubs(sb)[1]
    assert row["playlist_ids"] == [] and "playlist PL-bio failed" in row["error"]


def test_no_configured_playlists_is_not_a_failure():
    sb = _sb(parts=(1,))
    yt = FakeYouTube()
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert summary["published"] == [1] and yt.playlist_adds == []


def test_playlist_keys_are_the_subject_then_each_curriculum():
    keys = P.playlist_keys({"subject": "Biology"},
                           ["Cambridge Lower Secondary Science 0893 · 7Bs.01", "CBSE Science · Class 9 · Cells"])
    assert keys == ["biology", "cambridge lower secondary science 0893", "cbse science"]
    ids, missing = P.resolve_playlists(keys, {"biology": "PL-b", "cbse science": "PL-c"})
    assert ids == ["PL-b", "PL-c"] and missing == ["cambridge lower secondary science 0893"]


def test_an_unparseable_playlist_variable_is_ignored(monkeypatch):
    monkeypatch.setenv("YOUTUBE_PLAYLISTS_EN", "{not json")
    assert P.configured_playlists("en") == {}


# ── the per-run cap (quota is the real constraint) ─────────────────────


def test_the_per_run_cap_reports_what_it_left_behind(monkeypatch):
    monkeypatch.setenv(P.MAX_PARTS_ENV, "2")
    sb = _sb(parts=(1, 2, 3))
    yt = FakeYouTube()
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert summary["published"] == [1, 2] and summary["deferred"] == [3]
    assert len(yt.uploads) == 2
    assert "1 part(s) left for the next run" in summary["note"] and P.MAX_PARTS_ENV in summary["note"]
    assert _job_row(sb)["status"] == "done", "a capped run is a finished run, not a failure"


def test_the_next_run_finishes_the_capped_kit(monkeypatch):
    monkeypatch.setenv(P.MAX_PARTS_ENV, "2")
    sb = _sb(parts=(1, 2, 3))
    P.run_publish_job(sb, _job(), transport=FakeYouTube())
    yt2 = FakeYouTube(video_ids=["yt-third"])
    summary = P.run_publish_job(sb, _job(), transport=yt2)
    assert summary["already"] == [1, 2] and summary["published"] == [3]
    assert summary["deferred"] == [] and "note" not in summary
    assert _pubs(sb)[3]["youtube_video_id"] == "yt-third"


def test_an_already_published_part_does_not_spend_the_cap(monkeypatch):
    monkeypatch.setenv(P.MAX_PARTS_ENV, "1")
    published = [{"topic_kit_id": KIT, "part": 1, "channel_language": "en",
                  "youtube_video_id": "already-1", "privacy": "private"}]
    sb = _sb(parts=(1, 2), publications=published)
    yt = FakeYouTube()
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert summary["already"] == [1] and summary["published"] == [2] and summary["deferred"] == []


def test_the_cap_default_is_five_and_survives_nonsense(monkeypatch):
    assert P.max_parts_per_run() == P.MAX_PARTS_DEFAULT
    monkeypatch.setenv(P.MAX_PARTS_ENV, "0")
    assert P.max_parts_per_run() == P.MAX_PARTS_DEFAULT
    monkeypatch.setenv(P.MAX_PARTS_ENV, "seven")
    assert P.max_parts_per_run() == P.MAX_PARTS_DEFAULT
    monkeypatch.setenv(P.MAX_PARTS_ENV, "3")
    assert P.max_parts_per_run() == 3


# ── the never-starve rule, between parts ───────────────────────────────


def test_a_builder_arriving_mid_run_pauses_the_publish(monkeypatch):
    """Each part is several hundred MB of Supabase egress — bandwidth a
    teacher's render wants. The run stops DONE with the rest deferred, the
    same shape catalogue.figures pauses with."""
    monkeypatch.setenv(P.MAX_PARTS_ENV, "10")
    sb = _sb(parts=(1, 2, 3))
    seen = {"n": 0}

    def probe(_sb):
        seen["n"] += 1
        return seen["n"] > 1  # the first part goes up, then a teacher queues one

    monkeypatch.setattr(P, "builders_are_queued", probe)
    yt = FakeYouTube()
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert summary["published"] == [1] and summary["deferred"] == [2, 3]
    assert summary["paused"] == P.PAUSED_BUILDERS and summary["step"] == "paused"
    assert P.PAUSED_BUILDERS in summary["note"]
    assert len(yt.uploads) == 1
    assert _job_row(sb)["status"] == "done", "a pause is a finished run"


def test_the_probe_is_the_figure_jobs_own_definition():
    """One reading of "a real user is waiting", shared with catalogue.figures
    rather than copied."""
    from catalogue.figures import builder_queued

    sb = FakeSB()
    sb.tables["jobs"] = [{"id": "j-user", "type": "worksheet", "status": "queued", "params": {}}]
    assert REAL_BUILDER_PROBE(sb) is True and builder_queued(sb) is True
    sb.tables["jobs"][0]["status"] = "done"
    assert REAL_BUILDER_PROBE(sb) is False
    sb.tables["jobs"] = [{"id": "j-obs", "type": "topic_publish", "status": "queued", "params": {}}]
    assert REAL_BUILDER_PROBE(sb) is False, "an observer is never a user waiting"


# ── failures ───────────────────────────────────────────────────────────


def test_a_failed_part_is_recorded_and_the_others_still_publish(monkeypatch):
    monkeypatch.setenv(P.MAX_PARTS_ENV, "10")
    sb = _sb(parts=(1, 2))
    del sb.files[("artifacts", f"{OWNER}/{GEN}/lesson.mp4")]  # part 1's object is gone
    yt = FakeYouTube()
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert summary["failed"] == [1] and summary["published"] == [2]
    assert _pubs(sb)[1]["error"].startswith("part 1:")
    assert _pubs(sb)[1].get("youtube_video_id") in (None, "")
    row = _job_row(sb)
    assert row["status"] == "error" and "1 of 2 parts failed" in row["error"]


def test_the_job_never_raises_when_the_database_explodes():
    class _Boom:
        def table(self, name):
            raise RuntimeError("postgrest is down")

        def __getattr__(self, name):
            raise RuntimeError("postgrest is down")

    assert P.run_publish_job(_Boom(), _job(), transport=FakeYouTube()) is None


# ── credentials come from the environment, and only from it ────────────


def test_the_real_transport_is_never_built_without_credentials():
    """No test in this suite sets these, so nothing here can reach Google."""
    with pytest.raises(P.PublishRefused) as exc:
        P.default_transport("en")
    for name in (P.CLIENT_ID_ENV, P.CLIENT_SECRET_ENV, "YOUTUBE_REFRESH_TOKEN_EN"):
        assert name in str(exc.value)


def test_the_refusal_names_the_variable_and_never_a_value(monkeypatch):
    monkeypatch.setenv(P.CLIENT_ID_ENV, "id-123")
    monkeypatch.setenv(P.CLIENT_SECRET_ENV, "secret-abc")
    with pytest.raises(P.PublishRefused) as exc:
        P.read_credentials("en")
    message = str(exc.value)
    assert "YOUTUBE_REFRESH_TOKEN_EN" in message
    assert "id-123" not in message and "secret-abc" not in message


def test_the_real_transports_four_calls_execute():
    """``_GoogleTransport`` is the one class no other test can reach —
    ``default_transport`` needs credentials this suite never sets, and the
    running worker will not have them until the founder deploys Phase 4. So
    drive it against a STUB google-api-client service (no discovery, no
    socket) and prove its bodies run and its request bodies are the ones
    YouTube documents. A NameError in here would otherwise surface on the
    founder's first real publish."""
    class _Req:
        def __init__(self, out):
            self.out = out

        def next_chunk(self):
            return None, self.out

        def execute(self):
            return self.out

    class _Resource:
        def __init__(self, out):
            self.out, self.kw = out, None

        def insert(self, **kw):
            self.kw = kw
            return _Req(self.out)

        set = insert

    class _Service:
        def __init__(self):
            self.v, self.t = _Resource({"id": "vid-1"}), _Resource({})
            self.c, self.p = _Resource({"id": "cap-1"}), _Resource({})

        def videos(self):
            return self.v

        def thumbnails(self):
            return self.t

        def captions(self):
            return self.c

        def playlistItems(self):
            return self.p

    svc = _Service()
    transport = P._GoogleTransport(svc, lambda *a, **k: ("media", a, k))
    assert isinstance(transport, P.YouTubeTransport), "the protocol is satisfied"

    assert transport.upload_video(pathlib.Path("x.mp4"), title="Cells", description="D",
                                  privacy="private", language="en", tags=["Biology"]) == "vid-1"
    body = svc.v.kw["body"]
    assert body["status"]["privacyStatus"] == "private"
    assert body["snippet"]["categoryId"] == "27", "Education"
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert svc.v.kw["part"] == "snippet,status"

    transport.set_thumbnail("vid-1", pathlib.Path("t.png"))
    assert svc.t.kw["videoId"] == "vid-1"
    assert transport.insert_caption("vid-1", pathlib.Path("c.srt"), language="en", name="SketchCast") == "cap-1"
    assert svc.c.kw["body"]["snippet"]["videoId"] == "vid-1"
    transport.add_to_playlist("vid-1", "PL-x")
    assert svc.p.kw["body"]["snippet"]["resourceId"] == {"kind": "youtube#video", "videoId": "vid-1"}


def test_the_token_variable_is_named_per_channel_language():
    assert P.refresh_token_env("en") == "YOUTUBE_REFRESH_TOKEN_EN"
    assert P.refresh_token_env("ar") == "YOUTUBE_REFRESH_TOKEN_AR"
    assert P.refresh_token_env(None) == "YOUTUBE_REFRESH_TOKEN_EN"


def test_no_credential_ever_reaches_the_database(monkeypatch, caplog):
    """Every write this job makes AND every line it logs, scanned for the
    values the environment carried. The publication row records video ids,
    playlists and privacy; a token belongs in neither, and a token in a log
    stream is a token in Railway's log retention."""
    monkeypatch.setenv(P.CLIENT_ID_ENV, "client-id-SECRETVALUE")
    monkeypatch.setenv(P.CLIENT_SECRET_ENV, "client-secret-SECRETVALUE")
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN_EN", "refresh-token-SECRETVALUE")
    sb = _sb(parts=(1,))
    with caplog.at_level("DEBUG", logger="worker.publish"):
        P.run_publish_job(sb, _job(), transport=FakeYouTube())
    written = json.dumps(sb.log, default=str) + json.dumps(sb.tables, default=str)
    assert "SECRETVALUE" not in written
    assert "SECRETVALUE" not in caplog.text


# ── observer classification and dispatch ───────────────────────────────


def test_topic_publish_is_an_observer_job():
    import worker.run as run

    assert P.JOB_TYPE in db.OBSERVER_JOB_TYPES
    assert P.JOB_TYPE in run.OBSERVER_JOB_TYPES and P.JOB_TYPE in run.CATALOGUE_JOB_TYPES
    assert db.generation_to_mirror({"type": P.JOB_TYPE, "generation_id": GEN}) is None


def test_the_publish_job_never_writes_the_generation_it_reads_from():
    """It reads the presentation's artifacts; that generation was built and
    approved long before, and relabelling it would overwrite a verdict."""
    sb = _sb(parts=(1,))
    sb.tables["generations"] = [{"id": GEN, "status": "done", "params": {}}]
    P.run_publish_job(sb, _job(), transport=FakeYouTube())
    assert [e for e in sb.log if e[1] == "generations"] == []
    assert sb.tables["generations"][0]["status"] == "done"
