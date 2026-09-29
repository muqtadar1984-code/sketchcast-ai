"""catalogue/playlists.py — the manual job that creates the channel's
discipline playlists and moves every live video into its one (founder,
2026-09-29: physics, chemistry, biology, and algebra for the maths videos
to come). A publish only ADDS to playlists it knows; this creates them and
reaches back to the videos posted before they existed."""

from __future__ import annotations

import json

import pytest

from catalogue import playlists as PL
from catalogue import publish as P
from tests.catalogue_fakes import FakeSB


class FakePlaylists:
    """``PlaylistTransport`` in memory: a channel with ``existing`` playlists
    (title → id → members), recording every create and add."""

    def __init__(self, existing=None, members=None, fail_adds=()):
        self.playlists = dict(existing or {})            # title → id
        self.members = {pid: list(v) for pid, v in (members or {}).items()}
        self.fail_adds = set(fail_adds)
        self.created: list[dict] = []
        self.adds: list[tuple[str, str]] = []
        self.list_calls = 0

    def list_playlists(self):
        self.list_calls += 1
        return [{"id": pid, "title": title} for title, pid in self.playlists.items()]

    def create_playlist(self, title, description, *, privacy, language):
        pid = f"PL-{title.lower()}"
        self.created.append({"title": title, "description": description, "privacy": privacy, "language": language})
        self.playlists[title] = pid
        self.members[pid] = []
        return pid

    def playlist_video_ids(self, playlist_id):
        return list(self.members.get(playlist_id, []))

    def add_to_playlist(self, video_id, playlist_id):
        if video_id in self.fail_adds:
            raise RuntimeError("videoNotFound")
        self.adds.append((video_id, playlist_id))
        self.members.setdefault(playlist_id, []).append(video_id)


TOPICS = {
    # title: (codes, video id)
    "Cells": (["7Bs.01", "7Bs.02"], "vid-cells"),
    "Structure of the Atom": (["8Cm.01", "8Cm.02"], "vid-atom"),
    "Forces and Motion": (["8Pf.03", "8Pf.04"], "vid-forces"),
    "States of Matter": (["7Cm.06", "8Pf.07"], "vid-matter"),
    "Weather and Climate": (["8ESc.01", "9ESc.02"], "vid-weather"),   # Earth science files under physics
    "Expanding Brackets": (["9Ae.02", "cbse:8:ALG:02"], "vid-brackets"),
    "Rocks and Fossils": (["cbse:9:U2:01"], "vid-rocks"),               # no code names a discipline
}


def _sb(*, superseded=(), extra_publications=()):
    sb = FakeSB()
    sb.tables["curricula"] = [{"id": "cur-1", "code": "cambridge", "name": "Cambridge Lower Secondary Science 0893"}]
    n = 0
    for i, (title, (codes, video)) in enumerate(TOPICS.items(), start=1):
        subject = "Mathematics" if "Ae" in codes[0] else "Science"
        sb.tables["topics"].append({"id": f"topic-{i}", "title": title, "subject": subject, "status": "video_approved"})
        sb.tables["topic_kits"].append({"id": f"kit-{i}", "topic_id": f"topic-{i}", "status": "approved"})
        for code in codes:
            n += 1
            sb.tables["curriculum_nodes"].append({"id": f"node-{n}", "curriculum_id": "cur-1", "code": code,
                                                  "kind": "objective"})
            sb.tables["topic_curriculum_map"].append({"topic_id": f"topic-{i}", "node_id": f"node-{n}"})
        sb.tables["topic_publications"].append({
            "id": f"pub-{i}", "topic_kit_id": f"kit-{i}", "part": 1, "channel_language": "en",
            "youtube_video_id": video, "privacy": "public", "playlist_ids": [],
            "superseded_by": "pub-x" if title in superseded else None})
    sb.tables["topic_publications"] += list(extra_publications)
    sb.tables["jobs"].append({"id": "job-1", "type": PL.JOB_TYPE, "status": "processing", "attempts": 0,
                              "params": {"language": "en"}})
    return sb


def _job(sb):
    return next(j for j in sb.tables["jobs"] if j["id"] == "job-1")


def _pub(sb, i):
    return next(r for r in sb.tables["topic_publications"] if r["id"] == f"pub-{i}")


# ── the classifier ─────────────────────────────────────────────────────────


class TestTheDiscipline:
    @pytest.mark.parametrize("code,want", [
        ("7Bs.01", "biology"), ("8Cm.02", "chemistry"), ("9Pf.05", "physics"), ("8ESc.01", "physics"),
        ("9Ae.02", "algebra"), ("7As.04", "algebra"), ("8Ni.01", "number"), ("cbse:8:ALG:03", "algebra"),
        ("cbse:9:U2:01", ""), ("", ""), ("Class 9 · Cells", ""),
    ])
    def test_one_code(self, code, want):
        assert P.code_discipline(code) == want

    def test_the_majority_wins_and_a_tie_goes_to_the_first_code(self):
        assert P.discipline_key(["7Cm.01", "7Cm.02", "9Pf.01"]) == "chemistry"
        assert P.discipline_key(["7Cm.06", "8Pf.07"]) == "chemistry"   # States of Matter
        assert P.discipline_key(["8Pf.07", "7Cm.06"]) == "physics"
        assert P.discipline_key(["cbse:9:U2:01", "cbse:8:ALG:02"]) == "algebra"
        assert P.discipline_key([]) == "" and P.discipline_key(["cbse:9:U2:01"]) == ""

    def test_the_discipline_is_a_playlist_key_after_the_subject(self):
        keys = P.playlist_keys({"subject": "Science"}, ["Cambridge Lower Secondary Science 0893 · 8Pf.03"],
                               ["8Pf.03", "8Pf.04"])
        assert keys == ["science", "physics", "cambridge lower secondary science 0893"]
        # a subject that IS the discipline is not listed twice
        assert P.playlist_keys({"subject": "Biology"}, [], ["7Bs.01"]) == ["biology"]


class TestTheStoredMap:
    def test_the_worker_recorded_map_is_read_and_the_environment_wins_on_a_shared_key(self, monkeypatch):
        sb = FakeSB()
        sb.tables.setdefault("platform_settings", []).append({"key": "youtube_playlists_en",
                                               "value": {"biology": "PL-stored-bio", "physics": "PL-stored-phy"}})
        monkeypatch.delenv("YOUTUBE_PLAYLISTS_EN", raising=False)
        assert P.configured_playlists("en", sb) == {"biology": "PL-stored-bio", "physics": "PL-stored-phy"}
        monkeypatch.setenv("YOUTUBE_PLAYLISTS_EN", json.dumps({"biology": "PL-env-bio", "science": "PL-sci"}))
        assert P.configured_playlists("en", sb) == {"biology": "PL-env-bio", "physics": "PL-stored-phy",
                                                    "science": "PL-sci"}
        assert P.configured_playlists("en") == {"biology": "PL-env-bio", "science": "PL-sci"}, "no database, env only"

    def test_a_malformed_row_is_an_empty_map(self):
        sb = FakeSB()
        sb.tables.setdefault("platform_settings", []).append({"key": "youtube_playlists_en", "value": ["PL-x"]})
        assert P.stored_playlists(sb, "en") == {}


# ── the job ────────────────────────────────────────────────────────────────


class TestTheJob:
    def test_creates_the_four_playlists_places_every_video_and_records_the_map(self, monkeypatch):
        monkeypatch.delenv("YOUTUBE_PLAYLISTS_EN", raising=False)
        sb = _sb()
        yt = FakePlaylists()
        summary = PL.run_playlists_job(sb, _job(sb), transport=yt)
        assert [c["title"] for c in yt.created] == ["Biology", "Chemistry", "Physics", "Algebra"]
        assert all(c["privacy"] == "public" and c["language"] == "en" for c in yt.created)
        assert sorted(yt.adds) == sorted([("vid-cells", "PL-biology"), ("vid-atom", "PL-chemistry"),
                                          ("vid-forces", "PL-physics"), ("vid-matter", "PL-chemistry"),
                                          ("vid-weather", "PL-physics"), ("vid-brackets", "PL-algebra")])
        assert [u["title"] for u in summary["unplaced"]] == ["Rocks and Fossils"]
        assert summary["unplaced"][0]["discipline"] == ""
        assert summary["counts"] == {"placed": 6, "already": 0, "unplaced": 1, "failed": 0}
        assert _pub(sb, 1)["playlist_ids"] == ["PL-biology"]
        assert _pub(sb, 5)["playlist_ids"] == ["PL-physics"], "Earth science files under physics"
        assert _pub(sb, 7)["playlist_ids"] == [], "no video lands in a playlist by guesswork"
        stored = next(r for r in sb.tables["platform_settings"] if r["key"] == "youtube_playlists_en")
        assert stored["value"] == {"biology": "PL-biology", "chemistry": "PL-chemistry",
                                   "physics": "PL-physics", "algebra": "PL-algebra"}
        assert P.configured_playlists("en", sb)["physics"] == "PL-physics", "a later publish resolves it"
        job = _job(sb)
        assert job["status"] == "done" and job["stage"]["playlists"]["algebra"]["created"] is True

    def test_a_re_run_creates_nothing_and_adds_nothing_twice(self, monkeypatch):
        monkeypatch.delenv("YOUTUBE_PLAYLISTS_EN", raising=False)
        sb = _sb()
        yt = FakePlaylists()
        PL.run_playlists_job(sb, _job(sb), transport=yt)
        adds = list(yt.adds)
        _job(sb)["status"] = "processing"
        summary = PL.run_playlists_job(sb, _job(sb), transport=yt)
        assert len(yt.created) == 4 and yt.adds == adds
        assert summary["counts"] == {"placed": 0, "already": 6, "unplaced": 1, "failed": 0}
        assert _pub(sb, 1)["playlist_ids"] == ["PL-biology"]

    def test_an_existing_playlist_of_that_title_is_reused_and_a_configured_id_wins(self, monkeypatch):
        monkeypatch.setenv("YOUTUBE_PLAYLISTS_EN", json.dumps({"physics": "PL-founder-phy"}))
        sb = _sb()
        yt = FakePlaylists(existing={"biology": "PL-old-bio"}, members={"PL-old-bio": ["vid-cells"]})
        summary = PL.run_playlists_job(sb, _job(sb), transport=yt)
        assert [c["title"] for c in yt.created] == ["Chemistry", "Algebra"]
        assert summary["playlists"]["biology"] == {"id": "PL-old-bio", "title": "Biology", "created": False,
                                                   "source": "channel"}
        assert summary["playlists"]["physics"]["id"] == "PL-founder-phy"
        assert ("vid-forces", "PL-founder-phy") in yt.adds
        assert ("vid-cells", "PL-old-bio") not in yt.adds, "already a member"
        assert _pub(sb, 1)["playlist_ids"] == ["PL-old-bio"], "the row learns the membership it already had"
        assert yt.list_calls == 1

    def test_a_superseded_video_and_another_channel_are_left_alone(self):
        other = {"id": "pub-fr", "topic_kit_id": "kit-1", "part": 1, "channel_language": "fr",
                 "youtube_video_id": "vid-cells-fr", "privacy": "public", "playlist_ids": [], "superseded_by": None}
        sb = _sb(superseded=("Cells",), extra_publications=(other,))
        yt = FakePlaylists()
        PL.run_playlists_job(sb, _job(sb), transport=yt)
        assert "vid-cells" not in {v for v, _ in yt.adds} and "vid-cells-fr" not in {v for v, _ in yt.adds}

    def test_one_failing_video_is_recorded_the_rest_are_placed_and_a_re_run_picks_it_up(self, monkeypatch):
        monkeypatch.delenv("YOUTUBE_PLAYLISTS_EN", raising=False)
        sb = _sb()
        yt = FakePlaylists(fail_adds={"vid-atom"})
        summary = PL.run_playlists_job(sb, _job(sb), transport=yt)
        assert summary["counts"]["failed"] == 1 and summary["counts"]["placed"] == 5
        job = _job(sb)
        assert job["status"] == "error" and "Structure of the Atom" in job["error"] and "videoNotFound" in job["error"]
        assert _pub(sb, 2)["playlist_ids"] == []
        yt.fail_adds.clear()
        job["status"] = "processing"
        summary = PL.run_playlists_job(sb, _job(sb), transport=yt)
        assert summary["counts"] == {"placed": 1, "already": 5, "unplaced": 1, "failed": 0}
        assert _pub(sb, 2)["playlist_ids"] == ["PL-chemistry"] and _job(sb)["status"] == "done"

    def test_a_subset_of_playlists_and_an_unknown_key(self):
        sb = _sb()
        _job(sb)["params"] = {"playlists": ["Algebra"]}
        yt = FakePlaylists()
        summary = PL.run_playlists_job(sb, _job(sb), transport=yt)
        assert [c["title"] for c in yt.created] == ["Algebra"]
        assert yt.adds == [("vid-brackets", "PL-algebra")]
        assert summary["counts"]["unplaced"] == 6
        _job(sb)["params"] = {"playlists": ["geology"]}
        _job(sb)["status"] = "processing"
        assert PL.run_playlists_job(sb, _job(sb), transport=FakePlaylists()) is None
        assert _job(sb)["status"] == "error" and "unknown playlist key(s): geology" in _job(sb)["error"]

    def test_a_youtube_failure_before_any_placement_fails_the_job(self):
        sb = _sb()

        class Broken(FakePlaylists):
            def list_playlists(self):
                raise RuntimeError("quotaExceeded")

        assert PL.run_playlists_job(sb, _job(sb), transport=Broken()) is None
        assert _job(sb)["status"] == "error" and "quotaExceeded" in _job(sb)["error"]
        assert not sb.tables.get("platform_settings")


class TestTheGoogleTransport:
    def test_pages_are_followed_and_the_bodies_are_the_documented_ones(self):
        calls = []

        class _Req:
            def __init__(self, res):
                self.res = res

            def execute(self):
                return self.res

        class _Playlists:
            def list(self, **kw):
                calls.append(("playlists.list", kw))
                if kw.get("pageToken") is None:
                    return _Req({"items": [{"id": "PL-1", "snippet": {"title": "Biology"}}], "nextPageToken": "t2"})
                return _Req({"items": [{"id": "PL-2", "snippet": {"title": "Physics"}}]})

            def insert(self, **kw):
                calls.append(("playlists.insert", kw))
                return _Req({"id": "PL-new"})

        class _Items:
            def list(self, **kw):
                calls.append(("playlistItems.list", kw))
                return _Req({"items": [{"contentDetails": {"videoId": "v1"}}, {"contentDetails": {"videoId": "v2"}}]})

            def insert(self, **kw):
                calls.append(("playlistItems.insert", kw))
                return _Req({})

        class _Service:
            def playlists(self):
                return _Playlists()

            def playlistItems(self):
                return _Items()

        t = PL._GooglePlaylists(_Service())
        assert t.list_playlists() == [{"id": "PL-1", "title": "Biology"}, {"id": "PL-2", "title": "Physics"}]
        assert calls[0][1]["mine"] is True and calls[1][1]["pageToken"] == "t2"
        assert t.create_playlist("Algebra", "desc", privacy="public", language="en") == "PL-new"
        body = calls[2][1]["body"]
        assert calls[2][1]["part"] == "snippet,status"
        assert body == {"snippet": {"title": "Algebra", "description": "desc", "defaultLanguage": "en"},
                        "status": {"privacyStatus": "public"}}
        assert t.playlist_video_ids("PL-1") == ["v1", "v2"]
        assert calls[3][1]["playlistId"] == "PL-1" and calls[3][1]["part"] == "contentDetails"
        t.add_to_playlist("v3", "PL-1")
        assert calls[4][1]["body"]["snippet"] == {"playlistId": "PL-1",
                                                  "resourceId": {"kind": "youtube#video", "videoId": "v3"}}


class TestWiring:
    def test_the_worker_dispatches_and_observes_the_job_type(self):
        from pathlib import Path
        import worker.run as run
        from worker import client as db
        assert PL.JOB_TYPE in run.CATALOGUE_JOB_TYPES
        assert PL.JOB_TYPE in db.OBSERVER_JOB_TYPES, "it owns no generation"
        src = Path(run.__file__).read_text(encoding="utf-8")
        assert 'job_type == "youtube_playlists"' in src and "run_playlists_job(sb, job)" in src
