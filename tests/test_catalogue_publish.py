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
    assert [u["title"] for u in yt.uploads] == ["Cells Explained | Cambridge Stage 7 Biology — Part 1 of 3",
                                                "Cells Explained | Cambridge Stage 7 Biology — Part 2 of 3",
                                                "Cells Explained | Cambridge Stage 7 Biology — Part 3 of 3"]
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


def test_a_single_part_kit_is_titled_topic_terms_audience():
    """One structure for every video (catalogue.youtube_meta): the topic,
    then the key terms, then the audience block a teacher searches for. This
    kit's summary enumerates no terms, so the middle block is absent."""
    sb = _sb(parts=(1,))
    yt = FakeYouTube()
    P.run_publish_job(sb, _job(), transport=yt)
    assert yt.uploads[0]["title"] == "Cells Explained | Cambridge Stage 7 Biology"


def test_the_stored_words_win_the_title_the_intro_the_terms_and_the_tags():
    """What the reviewer saw and edited in the library (topic_kits.youtube_meta)
    is what goes up — and a blank field falls back to its default."""
    sb = _sb(parts=(1,))
    sb.tables["topic_kits"][0]["youtube_meta"] = {
        "title": "Cells for Beginners | Membrane, Nucleus | Cambridge Stage 7 Biology",
        "intro": "What is a cell? A teacher and a student work through it.",
        "key_terms": ["cell membrane", "nucleus"], "hashtags": ["Cells", "SketchCast"], "source": "edited"}
    yt = FakeYouTube()
    P.run_publish_job(sb, _job(), transport=yt)
    up = yt.uploads[0]
    assert up["title"] == "Cells for Beginners | Membrane, Nucleus | Cambridge Stage 7 Biology"
    blocks = up["description"].split("\n\n")
    assert blocks[0] == "What is a cell? A teacher and a student work through it."
    assert "Key terms: cell membrane, nucleus." in blocks
    # the reviewer's hashtags stay first; the discipline, board and form
    # hashtags follow them (catalogue/youtube_enrich.py)
    assert blocks[-1].startswith("#Cells #SketchCast #Biology")
    assert "#Stage7Biology" in blocks[-1] and "More Biology lessons" not in up["description"], "no playlist configured"
    assert up["tags"][:4] == ["Cells", "cell membrane", "nucleus", "Biology"] and "Cambridge Stage 7 Biology" in up["tags"]
    assert "Every living thing is made of cells." not in up["description"]


def test_a_stored_thumbnail_is_uploaded_instead_of_a_freshly_drawn_one():
    """The card the library SHOWED the reviewer (kind thumbnail_png beside the
    video) is the card that goes up."""
    sb = _sb(parts=(1,))
    sb.tables["artifacts"].append({"generation_id": GEN, "kind": "thumbnail_png", "storage_path": f"{OWNER}/{GEN}/thumb.png"})
    sb.files[("artifacts", f"{OWNER}/{GEN}/thumb.png")] = b"png-from-the-library"
    yt = FakeYouTube()
    P.run_publish_job(sb, _job(), transport=yt)
    assert len(yt.thumbnails) == 1
    assert pathlib.Path(yt.thumbnails[0]["path"]).name == "stored_thumb_part1.png"
    assert _pubs(sb)[1]["thumbnail_set"] is True


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
    assert "Next: Cells Explained | Cambridge Stage 7 Biology — Part 2 of 2" in yt.uploads[0]["description"]
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


def test_a_publish_lands_in_the_playlist_the_worker_created_itself(monkeypatch):
    """The youtube_playlists job records the ids it created in
    platform_settings; a publish with NO environment variable still finds
    them, keyed by the topic's discipline (7Bs.01 → biology)."""
    monkeypatch.delenv("YOUTUBE_PLAYLISTS_EN", raising=False)
    sb = _sb(parts=(1,))
    sb.tables["topics"][0]["subject"] = "Science"
    sb.tables.setdefault("platform_settings", []).append({"key": "youtube_playlists_en", "value": {"biology": "PL-job-bio"}})
    yt = FakeYouTube()
    P.run_publish_job(sb, _job(), transport=yt)
    assert yt.playlist_adds == [{"video_id": "yt-1", "playlist_id": "PL-job-bio"}]
    assert _pubs(sb)[1]["playlist_ids"] == ["PL-job-bio"]


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


def test_a_builder_arriving_mid_run_sends_the_publish_back_to_the_queue(monkeypatch):
    """Each part is several hundred MB of Supabase egress — bandwidth a
    teacher's render wants. The run does not finish "paused" for a reviewer
    to click again: it raises DeferredJob (run.py requeues it with a wake-up
    time) and the re-run skips the part already up — so a kit that "says
    queued" posts by itself once the teacher's build is through."""
    monkeypatch.setenv(P.MAX_PARTS_ENV, "10")
    sb = _sb(parts=(1, 2, 3))
    seen = {"n": 0}

    def probe(_sb):
        seen["n"] += 1
        return seen["n"] > 1  # the first part goes up, then a teacher queues one

    monkeypatch.setattr(P, "builders_are_queued", probe)
    yt = FakeYouTube()
    with pytest.raises(db.DeferredJob) as exc:
        P.run_publish_job(sb, _job(), transport=yt)
    assert exc.value.seconds == P.PUBLISH_WAIT_SECONDS and exc.value.note == P.WAITING_BUILDERS
    assert len(yt.uploads) == 1 and _pubs(sb)[1]["youtube_video_id"]
    assert _job_row(sb)["status"] == "processing", "run.py, not this module, moves the row back"

    # the teacher's build is through: the re-run finishes the rest
    monkeypatch.setattr(P, "builders_are_queued", lambda sb: False)
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert summary["already"] == [1] and summary["published"] == [2, 3] and summary["step"] == "done"
    assert len(yt.uploads) == 3


def test_a_publish_that_has_waited_the_whole_bound_finishes_paused(monkeypatch):
    """The wait is bounded (YOUTUBE_PUBLISH_MAX_WAIT_MINUTES): a job that
    has been going back to the queue for longer than that finishes DONE with
    the rest deferred and the note saying why — the old shape — so an endless
    "queued" cannot hide a worker that is stuck."""
    monkeypatch.setenv(P.MAX_PARTS_ENV, "10")
    monkeypatch.setenv(P.PUBLISH_MAX_WAIT_ENV, "60")
    sb = _sb(parts=(1, 2, 3))
    seen = {"n": 0}

    def probe(_sb):
        seen["n"] += 1
        return seen["n"] > 1

    monkeypatch.setattr(P, "builders_are_queued", probe)
    yt = FakeYouTube()
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(minutes=61)).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    summary = P.run_publish_job(sb, _job(deferred_since=since), transport=yt)
    assert summary["published"] == [1] and summary["deferred"] == [2, 3]
    assert summary["paused"] == P.PAUSED_BUILDERS and summary["step"] == "paused"
    assert P.PAUSED_BUILDERS in summary["note"]
    assert len(yt.uploads) == 1
    assert _job_row(sb)["status"] == "done", "a pause is a finished run"


def test_max_publish_wait_reads_minutes_and_falls_back(monkeypatch):
    monkeypatch.delenv(P.PUBLISH_MAX_WAIT_ENV, raising=False)
    assert P.max_publish_wait() == P.PUBLISH_MAX_WAIT_DEFAULT * 60
    monkeypatch.setenv(P.PUBLISH_MAX_WAIT_ENV, "30")
    assert P.max_publish_wait() == 30 * 60
    monkeypatch.setenv(P.PUBLISH_MAX_WAIT_ENV, "soon")
    assert P.max_publish_wait() == P.PUBLISH_MAX_WAIT_DEFAULT * 60
    monkeypatch.setenv(P.PUBLISH_MAX_WAIT_ENV, "-5")
    assert P.max_publish_wait() == P.PUBLISH_MAX_WAIT_DEFAULT * 60


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
        retries = []

        def __init__(self, out):
            self.out = out

        def next_chunk(self, num_retries=0):
            _Req.retries.append(num_retries)
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
    assert _Req.retries == [P.CHUNK_RETRIES], "every chunk is retried by the library on a 5xx or a dropped socket"
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


def test_own_channel_is_english_always_and_a_translation_once_its_token_is_set(monkeypatch):
    monkeypatch.delenv("YOUTUBE_REFRESH_TOKEN_MS_ARAB", raising=False)
    assert P.own_channel("en") and not P.own_channel("ms-arab")
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN_MS_ARAB", "token-present")
    assert P.own_channel("ms-arab") and P.channel_for("ms-arab") == "ms-arab"


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


# ── a TRANSLATED kit (Kit Translator T1, 2026-10-17) ───────────────────────


def _arabic(sb: FakeSB, translation_status: str = "approved") -> FakeSB:
    """The approved kit becomes an Arabic translation of an English kit: its
    own approved Arabic article, translation_status on the row."""
    sb.tables["topic_articles"].append({"id": "art-ar", "topic_id": TOPIC, "language": "ar", "version": 1,
                                        "status": "approved", "title": "الخلايا"})
    kit = sb.tables["topic_kits"][0]
    kit.update({"language": "ar", "article_id": "art-ar", "source_kit_id": "kit-en",
                "translation_status": translation_status})
    sb.tables["jobs"][0]["params"] = {"kit_id": KIT, "language": "en"}   # the route's old constant: ignored
    return sb


def test_a_translated_kit_is_refused_while_its_language_has_no_channel(monkeypatch):
    """Founder, 2026-10-20: a translation is uploaded only to its OWN
    language's channel, never to the English one (T1's fallback is gone)."""
    monkeypatch.delenv("YOUTUBE_REFRESH_TOKEN_AR", raising=False)
    sb = _arabic(_sb(parts=(1,)))
    yt = FakeYouTube()
    assert P.run_publish_job(sb, _job(language="en"), transport=yt) is None
    assert yt.uploads == [] and _pubs(sb) == {}
    assert _job_row(sb)["error"] == P.no_channel_why("ar")
    assert "no Arabic YouTube channel yet" in P.no_channel_why("ar")
    assert P.no_channel_why("ms-arab").startswith("There is no Jawi YouTube channel yet")


def test_a_translated_kit_publishes_in_its_language_on_its_own_channel(monkeypatch):
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN_AR", "token-present")
    sb = _arabic(_sb(parts=(1,)))
    yt = FakeYouTube()
    summary = P.run_publish_job(sb, _job(language="en"), transport=yt)
    assert summary["published"] == [1] and summary["language"] == "ar" and summary["channel"] == "ar"
    up = yt.uploads[0]
    assert up["language"] == "ar", "defaultAudioLanguage is the video's language"
    # the English structure in Arabic (2026-10-17): the Arabic headline, no
    # stored English words, the Latin audience block; the labels in Arabic
    assert up["title"] == "شرح الخلايا | Cambridge Stage 7 Biology", "the translated article's title in the headline, no English words"
    assert "Every living thing" not in up["description"], "the English summary stays off an Arabic video"
    assert "0:00" in up["description"] and "الفصول" in up["description"], "the measured chapters stay, labelled in Arabic"
    assert "Aligned to" not in up["description"] and "Chapters" not in up["description"]
    assert yt.captions and yt.captions[0]["language"] == "ar"
    row = _pubs(sb)[1]
    assert row["channel_language"] == "ar" and row["youtube_video_id"] == "yt-1"


def test_a_translated_kit_goes_to_its_own_channel_when_that_channel_is_configured(monkeypatch):
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN_AR", "token-present")
    sb = _arabic(_sb(parts=(1,)))
    yt = FakeYouTube()
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert summary["channel"] == "ar" and _pubs(sb)[1]["channel_language"] == "ar"
    assert P.channel_for("ar") == "ar" and P.channel_for("en") == "en"
    monkeypatch.delenv("YOUTUBE_REFRESH_TOKEN_AR")
    assert P.channel_for("ar") == "en" and P.channel_for("hi") == "en"


def test_a_translation_still_in_review_is_refused_before_any_upload():
    sb = _arabic(_sb(parts=(1,)), translation_status="in_review")
    yt = FakeYouTube()
    assert P.run_publish_job(sb, _job(), transport=yt) is None
    assert yt.uploads == [] and "not approved" in _job_row(sb)["error"]


def test_a_translated_rerun_skips_the_part_already_on_the_channel(monkeypatch):
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN_AR", "token-present")
    sb = _arabic(_sb(parts=(1,), publications=[{"topic_kit_id": KIT, "part": 1, "channel_language": "ar",
                                                "youtube_video_id": "yt-old", "privacy": "private"}]))
    yt = FakeYouTube()
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert summary["already"] == [1] and yt.uploads == []


def test_jawi_env_names_use_an_underscore():
    """Railway refuses a hyphen in a variable name: Jawi (ms-arab) is MS_ARAB."""
    assert P.refresh_token_env("ms-arab") == "YOUTUBE_REFRESH_TOKEN_MS_ARAB"
    assert P.playlists_env("ms-arab") == "YOUTUBE_PLAYLISTS_MS_ARAB"
    assert P.lang_from_env("MS_ARAB") == "ms-arab"
    assert P.lang_from_env("AR") == "ar"


def test_jawi_posts_to_its_own_channel_once_its_token_is_set(monkeypatch):
    monkeypatch.delenv("YOUTUBE_REFRESH_TOKEN_MS_ARAB", raising=False)
    assert P.channel_for("ms-arab") == "en"
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN_MS_ARAB", "tok")
    assert P.channel_for("ms-arab") == "ms-arab"


# ── upload safety (Bulk YouTube Posting plan, Phase 1) ──────────────────


class _QuotaError(Exception):
    """What googleapiclient raises for a daily limit: the reason is in the text."""

    def __init__(self, reason="quotaExceeded"):
        super().__init__(f"<HttpError 403 returned \"The request cannot be completed because you have exceeded "
                         f"your quota.\". Details: \"[{{'reason': '{reason}'}}]\">")


def test_the_row_carries_a_token_before_the_upload_and_the_video_carries_its_tag():
    sb = _sb(parts=(1,))
    seen = {}

    class _Watch(FakeYouTube):
        def upload_video(self, path, **kw):
            seen["row"] = dict(_pubs(sb)[1])   # the database AT the moment of the upload
            return super().upload_video(path, **kw)

    yt = _Watch()
    P.run_publish_job(sb, _job(), transport=yt)
    row = seen["row"]
    assert row.get("upload_token") and not row.get("youtube_video_id"), "the intent is written first"
    assert yt.uploads[0]["tags"][-1] == P.upload_tag(row["upload_token"]), "the marker is the LAST tag"
    assert yt.uploads[0]["tags"][0] != P.upload_tag(row["upload_token"]), "the reviewer's tags keep the front"
    final = _pubs(sb)[1]
    assert final["youtube_video_id"] == "yt-1" and final["error"] is None


def test_the_video_id_is_recorded_before_the_follow_ups_run():
    sb = _sb(parts=(1,))
    seen = {}

    class _Watch(FakeYouTube):
        def insert_caption(self, video_id, path, **kw):
            seen["row"] = dict(_pubs(sb)[1])
            return super().insert_caption(video_id, path, **kw)

    P.run_publish_job(sb, _job(), transport=_Watch())
    assert seen["row"]["youtube_video_id"] == "yt-1", "a crash during captions cannot lose the video"


def test_an_interrupted_upload_found_on_the_channel_is_adopted_not_uploaded_again():
    token = "abc0123456" + "f" * 22
    interrupted = [{"topic_kit_id": KIT, "part": 1, "channel_language": "en", "youtube_video_id": None,
                    "privacy": "private", "upload_token": token, "error": P.UPLOADING_NOTE}]
    sb = _sb(parts=(1,), publications=interrupted)
    yt = FakeYouTube(channel={"vid-landed": ["Cells", P.upload_tag(token)]})
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert yt.uploads == [], "nothing is uploaded a second time"
    assert summary["published"] == [1] and summary["adopted"] == [1]
    row = _pubs(sb)[1]
    assert row["youtube_video_id"] == "vid-landed" and row["error"] is None
    assert [c["video_id"] for c in yt.captions] == ["vid-landed"], "the follow-ups run on the adopted video"


def test_an_interrupted_upload_that_never_landed_is_uploaded_once():
    token = "fff0123456" + "0" * 22
    interrupted = [{"topic_kit_id": KIT, "part": 1, "channel_language": "en", "youtube_video_id": None,
                    "privacy": "private", "upload_token": token, "error": P.UPLOADING_NOTE}]
    sb = _sb(parts=(1,), publications=interrupted)
    yt = FakeYouTube(channel={"someone-else": ["sc-0000000000"]})
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert yt.finds == [P.upload_tag(token)]
    assert len(yt.uploads) == 1 and summary["published"] == [1] and "adopted" not in summary


def test_a_channel_that_cannot_be_read_fails_the_part_rather_than_risk_a_duplicate():
    interrupted = [{"topic_kit_id": KIT, "part": 1, "channel_language": "en", "youtube_video_id": None,
                    "privacy": "private", "upload_token": "a" * 32, "error": P.UPLOADING_NOTE}]
    sb = _sb(parts=(1,), publications=interrupted)
    yt = FakeYouTube(fail_on=("find_upload",))
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert yt.uploads == [] and summary["failed"] == [1]
    assert "cannot be posted twice" in _pubs(sb)[1]["error"]


def test_a_part_posted_on_another_channel_counts_as_posted():
    elsewhere = [{"topic_kit_id": KIT, "part": 1, "channel_language": "ar",
                  "youtube_video_id": "on-another-channel", "privacy": "private"}]
    sb = _sb(parts=(1,), publications=elsewhere)
    yt = FakeYouTube()
    summary = P.run_publish_job(sb, _job(), transport=yt)
    assert summary["already"] == [1] and yt.uploads == []


def test_quota_at_the_upload_waits_for_the_next_quota_day_instead_of_failing():
    sb = _sb(parts=(1,))
    yt = FakeYouTube(raise_on={"upload_video": _QuotaError("quotaExceeded")})
    with pytest.raises(db.DeferredJob) as exc:
        P.run_publish_job(sb, _job(), transport=yt)
    assert P.WAITING_QUOTA in exc.value.note and "quotaExceeded" in exc.value.note
    assert exc.value.seconds > P.QUOTA_GRACE_SECONDS
    # the builder-wait clock restarts at the wake-up, in the future
    assert db.deferred_seconds({"params": {"deferred_since": exc.value.extra["deferred_since"]}}) == 0.0
    row = _pubs(sb)[1]
    assert row.get("upload_token") is None, "refused at the start: nothing to look for next time"
    assert not row.get("youtube_video_id") and "waiting for YouTube quota" in row["error"]
    assert _job_row(sb)["status"] != "error", "a quota wait is not a failure"


def test_the_channels_daily_upload_limit_is_a_quota_wait_too(monkeypatch):
    monkeypatch.setenv(P.MAX_PARTS_ENV, "10")
    sb = _sb(parts=(1, 2))

    class _SecondRefused(FakeYouTube):
        def upload_video(self, path, **kw):
            if self.uploads:
                raise _QuotaError("uploadLimitExceeded")
            return super().upload_video(path, **kw)

    with pytest.raises(db.DeferredJob) as exc:
        P.run_publish_job(sb, _job(), transport=_SecondRefused())
    assert "uploadLimitExceeded" in exc.value.note
    assert _pubs(sb)[1]["youtube_video_id"] == "yt-1", "the part already up stays recorded; the re-run skips it"


def test_quota_reason_reads_the_text_and_the_body_and_ignores_rate_limits():
    assert P.quota_reason(_QuotaError("quotaExceeded")) == "quotaExceeded"

    class _Http(Exception):
        content = b'{"error": {"errors": [{"reason": "dailyLimitExceeded"}]}}'

    assert P.quota_reason(_Http("403")) == "dailyLimitExceeded"
    assert P.quota_reason(RuntimeError("rateLimitExceeded")) is None
    assert P.quota_reason(RuntimeError("invalidDescription")) is None


def test_the_quota_day_is_pacific_and_resets_at_its_midnight():
    from datetime import date as _date, datetime as _dt, timezone as _tz

    pdt = _dt(2026, 10, 21, 6, 59, tzinfo=_tz.utc)          # 23:59 PDT on the 20th
    assert P.quota_day(pdt) == _date(2026, 10, 20)
    assert P.seconds_to_next_quota_day(pdt) == 60
    assert P.quota_day(_dt(2026, 10, 21, 7, 0, tzinfo=_tz.utc)) == _date(2026, 10, 21)
    pst = _dt(2026, 12, 1, 8, 0, tzinfo=_tz.utc)             # midnight PST
    assert P.quota_day(pst) == _date(2026, 12, 1) and P.seconds_to_next_quota_day(pst) == 86400


def test_every_call_is_added_to_the_quota_ledger():
    sb = _sb(parts=(1,))
    P.run_publish_job(sb, _job(), transport=FakeYouTube())
    calls = [p for name, p in sb.rpc_calls if name == P.LEDGER_RPC]
    methods = [c["p_method"] for c in calls]
    assert methods[:3] == ["videos.insert", "captions.insert", "thumbnails.set"]
    insert = calls[0]
    assert insert["p_uploads"] == 1 and insert["p_units"] == 0 and insert["p_channel"] == "en"
    assert calls[1]["p_units"] == 400 and calls[1]["p_uploads"] == 0
    assert insert["p_day"] == P.quota_day().isoformat()


def test_a_database_without_the_ledger_never_fails_the_upload():
    sb = _sb(parts=(1,))   # FakeSB answers PGRST202 for every function but one
    summary = P.run_publish_job(sb, _job(), transport=FakeYouTube())
    assert summary["published"] == [1] and _job_row(sb)["status"] == "done"


def test_a_database_without_0149_writes_the_row_without_the_safety_columns():
    written = []

    class _Old:
        def table(self, name):
            return self

        def upsert(self, row, on_conflict=None):
            self.row = row
            return self

        def execute(self):
            for col in ("upload_token", "upload_started_at"):
                if col in self.row:
                    raise RuntimeError(f"column \"{col}\" of relation \"topic_publications\" does not exist")
            written.append(dict(self.row))

    P.write_publication(_Old(), {"topic_kit_id": KIT, "part": 1, "channel_language": "en",
                                 "upload_token": "t", "upload_started_at": "now", "error": "x"})
    assert written == [{"topic_kit_id": KIT, "part": 1, "channel_language": "en", "error": "x"}]


def test_the_marker_always_fits_youtubes_tag_field():
    tags = [f"tag number {i:02d}" for i in range(60)]   # far more than 500 characters
    out = P.tags_with_marker(tags, "sc-0123456789")
    cost = sum(len(t) + (2 if " " in t else 0) + 1 for t in out)
    assert out[-1] == "sc-0123456789" and cost <= P.TAGS_CHAR_LIMIT
    assert out[:3] == tags[:3], "the order is kept"
