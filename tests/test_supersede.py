"""catalogue/supersede.py — the manual step that points an old YouTube video
at its re-rendered replacement (founder, 2026-09-26: no automation; the
library shows which videos predate the current format, the founder decides
which to redo, and Supersede does the YouTube side)."""

from __future__ import annotations

import pytest

from catalogue import supersede as S
from catalogue.youtube_meta import DESCRIPTION_MAX
from tests.catalogue_fakes import FakeSB

OLD_DESC = ("Plant and animal cells — what they share.\n\n"
            "Chapters\n0:00 Intro\n1:10 Organelles\n\n"
            "This lesson was generated with SketchCast.\nhttps://sketchcast.app/topics/plant-cells\n\n"
            "#PlantCell #SketchCast")


class FakeVideos:
    def __init__(self, videos: dict, fail: set | None = None):
        self.videos = {k: {"snippet": dict(v.get("snippet") or {}), "status": dict(v.get("status") or {})}
                       for k, v in videos.items()}
        self.fail = fail or set()
        self.snippet_updates: list[tuple[str, dict]] = []
        self.privacy_updates: list[tuple[str, dict, str]] = []

    def video(self, video_id):
        return self.videos.get(video_id)

    def update_snippet(self, video_id, snippet):
        if "snippet" in self.fail:
            raise RuntimeError("quota")
        self.snippet_updates.append((video_id, dict(snippet)))
        self.videos[video_id]["snippet"].update(snippet)

    def set_privacy(self, video_id, status, privacy):
        if "privacy" in self.fail:
            raise RuntimeError("forbidden")
        self.privacy_updates.append((video_id, dict(status), privacy))
        self.videos[video_id]["status"]["privacyStatus"] = privacy


def _sb(*, old_privacy="public", old_video="vid-old", new_video="vid-new", same_topic=True,
        old_part=1, new_part=1, superseded_by=None):
    sb = FakeSB()
    sb.tables["topic_kits"] += [
        {"id": "kit-old", "topic_id": "topic-1", "status": "approved"},
        {"id": "kit-new", "topic_id": "topic-1" if same_topic else "topic-2", "status": "approved"},
    ]
    sb.tables["topic_publications"] += [
        {"id": "pub-old", "topic_kit_id": "kit-old", "part": old_part, "channel_language": "en",
         "youtube_video_id": old_video, "privacy": old_privacy, "superseded_by": superseded_by, "superseded_at": None},
        {"id": "pub-new", "topic_kit_id": "kit-new", "part": new_part, "channel_language": "en",
         "youtube_video_id": new_video, "privacy": "public", "superseded_by": None, "superseded_at": None},
    ]
    sb.tables["jobs"].append({"id": "job-1", "type": S.JOB_TYPE, "status": "processing", "attempts": 0,
                              "params": {"old_publication_id": "pub-old", "new_publication_id": "pub-new"}})
    return sb


def _yt(old_privacy="public"):
    return FakeVideos({"vid-old": {"snippet": {"title": "Old", "description": OLD_DESC, "categoryId": "27",
                                               "tags": ["x"], "publishedAt": "2026-09-10T00:00:00Z"},
                                   "status": {"privacyStatus": old_privacy, "embeddable": True, "license": "youtube"}}})


class TestTheDescriptionPointer:
    def test_goes_first_and_leaves_every_other_paragraph_alone(self):
        out = S.supersede_description(OLD_DESC, "vid-new")
        paras = out.split("\n\n")
        assert paras[0] == S.updated_line("vid-new")
        assert paras[0].endswith("https://www.youtube.com/watch?v=vid-new")
        assert paras[1:] == OLD_DESC.split("\n\n")

    def test_a_second_supersede_replaces_the_pointer_instead_of_stacking(self):
        once = S.supersede_description(OLD_DESC, "vid-new")
        twice = S.supersede_description(once, "vid-newer")
        assert twice.count(S.UPDATED_LINE_PREFIX) == 1
        assert twice.split("\n\n")[0].endswith("vid-newer")
        assert twice.split("\n\n")[1:] == OLD_DESC.split("\n\n")

    def test_idempotent(self):
        once = S.supersede_description(OLD_DESC, "vid-new")
        assert S.supersede_description(once, "vid-new") == once

    def test_an_empty_description_is_just_the_pointer(self):
        assert S.supersede_description("", "v") == S.updated_line("v")
        assert S.supersede_description(None, "v") == S.updated_line("v")

    def test_the_limit_trims_the_old_text_never_the_pointer(self):
        out = S.supersede_description("x" * (DESCRIPTION_MAX + 50), "vid-new")
        assert len(out) <= DESCRIPTION_MAX and out.startswith(S.updated_line("vid-new"))


class TestPrivacy:
    def test_public_becomes_unlisted_and_nothing_else_moves(self):
        assert S.next_privacy("public") == "unlisted"
        assert S.next_privacy("unlisted") == "unlisted"
        assert S.next_privacy("private") == "private"


class TestTheJob:
    def test_points_unlists_and_records(self):
        sb, yt = _sb(), _yt()
        out = S.run_supersede_job(sb, sb.tables["jobs"][0], transport=yt)
        assert out["description"] == "pointer added" and out["privacy"] == "public -> unlisted"
        vid, snippet = yt.snippet_updates[0]
        assert vid == "vid-old" and snippet["description"].startswith(S.updated_line("vid-new"))
        assert snippet["title"] == "Old" and snippet["tags"] == ["x"], "the words go back as read"
        vid, status, privacy = yt.privacy_updates[0]
        assert privacy == "unlisted" and status["embeddable"] is True, "the status goes back whole"
        old = next(r for r in sb.tables["topic_publications"] if r["id"] == "pub-old")
        assert old["superseded_by"] == "pub-new" and old["superseded_at"] and old["privacy"] == "unlisted"
        job = sb.tables["jobs"][0]
        assert job["status"] == "done" and job["stage"]["new_video"] == "vid-new"

    def test_an_unlisted_or_private_old_video_keeps_its_privacy(self):
        for priv in ("unlisted", "private"):
            sb, yt = _sb(old_privacy=priv), _yt(priv)
            out = S.run_supersede_job(sb, sb.tables["jobs"][0], transport=yt)
            assert yt.privacy_updates == [] and out["privacy"] == priv
            old = next(r for r in sb.tables["topic_publications"] if r["id"] == "pub-old")
            assert old["superseded_by"] == "pub-new" and old["privacy"] == priv

    def test_a_re_run_is_a_no_op(self):
        sb, yt = _sb(superseded_by="pub-new"), _yt("unlisted")
        out = S.run_supersede_job(sb, sb.tables["jobs"][0], transport=yt)
        assert out["already"] is True and yt.snippet_updates == [] and yt.privacy_updates == []
        assert sb.tables["jobs"][0]["status"] == "done"

    @pytest.mark.parametrize("tweak,why", [
        (dict(new_video=""), "no YouTube video yet"),
        (dict(old_video=""), "no YouTube video to point away from"),
        (dict(same_topic=False), "different topics"),
        (dict(new_part=2), "parts differ"),
        (dict(superseded_by="pub-other"), "already superseded by publication pub-other"),
        (dict(old_video="vid-new"), "same YouTube video"),
    ])
    def test_refusals_are_one_sentence_on_the_job(self, tweak, why):
        sb, yt = _sb(**tweak), _yt()
        assert S.run_supersede_job(sb, sb.tables["jobs"][0], transport=yt) is None
        job = sb.tables["jobs"][0]
        assert job["status"] == "error" and why in job["error"]
        assert yt.snippet_updates == [] and yt.privacy_updates == []
        old = next(r for r in sb.tables["topic_publications"] if r["id"] == "pub-old")
        assert old["superseded_by"] == tweak.get("superseded_by"), "the row is untouched"

    def test_a_video_youtube_no_longer_knows_is_a_refusal(self):
        sb = _sb()
        yt = FakeVideos({})
        assert S.run_supersede_job(sb, sb.tables["jobs"][0], transport=yt) is None
        assert "no longer knows video vid-old" in sb.tables["jobs"][0]["error"]

    def test_a_youtube_failure_fails_the_job_and_leaves_the_row_clean(self):
        sb, yt = _sb(), _yt()
        yt.fail.add("privacy")
        assert S.run_supersede_job(sb, sb.tables["jobs"][0], transport=yt) is None
        job = sb.tables["jobs"][0]
        assert job["status"] == "error" and "forbidden" in job["error"]
        old = next(r for r in sb.tables["topic_publications"] if r["id"] == "pub-old")
        assert old["superseded_by"] is None, "a re-run starts clean"


class TestWiring:
    def test_the_worker_dispatches_and_observes_the_job_type(self):
        from pathlib import Path
        import worker.run as run
        from worker import client as db
        assert S.JOB_TYPE in run.CATALOGUE_JOB_TYPES
        assert S.JOB_TYPE in db.OBSERVER_JOB_TYPES, "it owns no generation"
        src = Path(run.__file__).read_text(encoding="utf-8")
        assert 'job_type == "topic_supersede"' in src and "run_supersede_job(sb, job)" in src
