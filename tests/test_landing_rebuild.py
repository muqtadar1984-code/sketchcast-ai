"""catalogue/landing.py — the nudge that puts a newly published lesson on
sketchcast.app/topics within minutes: a ``repository_dispatch`` to the
landing repo, sent once at the end of a publish or supersede run. It must be
dormant without a token, send exactly the documented event with nothing
identifying in the payload, never raise, and fire only when the run changed
what the public catalogue shows."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from catalogue import landing as L
from catalogue import publish as P
from catalogue import supersede as S
from tests.catalogue_fakes import FakeSB, FakeYouTube

TOKEN = "github_pat_test_token_long_enough"


class FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class Poster:
    """``requests.post``'s shape, recording every call."""

    def __init__(self, status: int = 204, text: str = "", raise_with: Exception | None = None):
        self.status, self.text, self.raise_with = status, text, raise_with
        self.calls: list[dict] = []

    def __call__(self, url, *, json, headers, timeout):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if self.raise_with:
            raise self.raise_with
        return FakeResponse(self.status, self.text)


@pytest.fixture(autouse=True)
def _no_token(monkeypatch):
    for name in (L.TOKEN_ENV, L.OWNER_ENV, L.REPO_ENV):
        monkeypatch.delenv(name, raising=False)


# ── the helper ──────────────────────────────────────────────────────────


def test_dormant_without_a_token_nothing_is_sent():
    post = Poster()
    assert L.configured() is False
    assert L.request_landing_rebuild(L.REASON_PUBLISH, post=post) == {"ok": False, "unconfigured": True}
    assert post.calls == []


def test_a_blank_or_short_token_counts_as_unset(monkeypatch):
    post = Poster()
    monkeypatch.setenv(L.TOKEN_ENV, "   ")
    assert L.request_landing_rebuild(L.REASON_PUBLISH, post=post)["unconfigured"] is True
    monkeypatch.setenv(L.TOKEN_ENV, "short")
    assert L.request_landing_rebuild(L.REASON_PUBLISH, post=post)["unconfigured"] is True
    assert post.calls == []


def test_sends_the_documented_event_with_only_a_reason_and_a_time(monkeypatch):
    monkeypatch.setenv(L.TOKEN_ENV, TOKEN)
    post = Poster(204)
    assert L.configured() is True
    assert L.request_landing_rebuild(L.REASON_PUBLISH, post=post) == {"ok": True, "status": 204}
    assert len(post.calls) == 1
    call = post.calls[0]
    assert call["url"] == "https://api.github.com/repos/muqtadar1984-code/sketchcast-landing/dispatches"
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert call["headers"]["Accept"] == "application/vnd.github+json"
    assert call["timeout"] == L.TIMEOUT_SECONDS
    body = call["json"]
    assert body["event_type"] == L.EVENT == "catalogue-changed"
    # The landing repo and its workflow runs are PUBLIC: no id, title or
    # video id ever travels — the reason word and a timestamp only.
    assert sorted(body["client_payload"]) == ["at", "reason"]
    assert body["client_payload"]["reason"] == "publish_done"
    datetime.fromisoformat(body["client_payload"]["at"])  # parseable


def test_the_payload_timestamp_is_utc_iso_seconds():
    when = datetime(2026, 10, 16, 7, 5, 9, 123456, tzinfo=timezone.utc)
    assert L.payload("x", now=when) == {"event_type": "catalogue-changed",
                                        "client_payload": {"reason": "x", "at": "2026-10-16T07:05:09+00:00"}}


def test_owner_and_repo_can_be_overridden_for_a_fork(monkeypatch):
    monkeypatch.setenv(L.OWNER_ENV, "someone")
    monkeypatch.setenv(L.REPO_ENV, "landing-fork")
    assert L.dispatch_url() == "https://api.github.com/repos/someone/landing-fork/dispatches"


def test_a_refusal_is_logged_and_swallowed_with_its_status(monkeypatch, caplog):
    monkeypatch.setenv(L.TOKEN_ENV, TOKEN)
    post = Poster(403, "Resource not accessible by personal access token")
    with caplog.at_level(logging.WARNING, logger="catalogue.landing"):
        r = L.request_landing_rebuild(L.REASON_SUPERSEDE, post=post)
    assert r["ok"] is False and r["status"] == 403 and "not accessible" in r["error"]
    assert any("not accepted" in rec.message for rec in caplog.records)
    # The token never reaches a log line.
    assert TOKEN not in caplog.text


def test_a_network_failure_is_swallowed(monkeypatch, caplog):
    monkeypatch.setenv(L.TOKEN_ENV, TOKEN)
    post = Poster(raise_with=TimeoutError("signal timed out"))
    with caplog.at_level(logging.WARNING, logger="catalogue.landing"):
        r = L.request_landing_rebuild(L.REASON_PUBLISH, post=post)
    assert r == {"ok": False, "error": "TimeoutError: signal timed out"}
    assert TOKEN not in caplog.text


# ── the call sites ──────────────────────────────────────────────────────
#
# The publish and supersede runs are exercised through their own suites'
# fakes; here only WHEN the nudge fires matters, so the helper is replaced by
# a recorder and nothing is ever posted.

TOPIC, ARTICLE, KIT, GEN, OWNER, JOB = "t-1", "a-1", "k-1", "g-1", "owner-1", "job-1"


@pytest.fixture
def recorder(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(P, "request_landing_rebuild", lambda reason: sent.append(reason) or {"ok": True})
    monkeypatch.setattr(S, "request_landing_rebuild", lambda reason: sent.append(reason) or {"ok": True})
    return sent


@pytest.fixture(autouse=True)
def _publish_env(monkeypatch):
    monkeypatch.setenv(P.FEATURE_FLAG, "1")
    monkeypatch.setenv(P.AUDIT_FLAG, "1")
    monkeypatch.setenv(P.MAX_PARTS_ENV, "10")
    monkeypatch.setattr(P, "builders_are_queued", lambda sb: False)


def _publish_sb(*, parts=(1,), publications=()) -> FakeSB:
    sb = FakeSB()
    sb.tables["topics"] = [{"id": TOPIC, "canonical_key": "cells", "title": "Cells", "subject": "Biology",
                            "summary": "Every living thing is made of cells.", "bank_maturity": "basic",
                            "status": "in_review"}]
    sb.tables["topic_articles"] = [{"id": ARTICLE, "topic_id": TOPIC, "language": "en", "version": 1,
                                    "status": "approved"}]
    sb.tables["topic_kits"] = [{"id": KIT, "topic_id": TOPIC, "article_id": ARTICLE, "language": "en",
                                "presentation_generation_id": GEN, "chapters": [], "status": "approved"}]
    sb.tables["curricula"], sb.tables["curriculum_nodes"], sb.tables["topic_curriculum_map"] = [], [], []
    artifacts = []
    for p in parts:
        suffix = "" if p == 1 else f"_part{p}"
        video = f"{OWNER}/{GEN}/lesson{suffix}.mp4"
        artifacts.append({"generation_id": GEN, "kind": "video_mp4", "storage_path": video})
        sb.files[("artifacts", video)] = f"mp4-part-{p}".encode()
    sb.tables["artifacts"] = artifacts
    sb.tables["topic_publications"] = [dict(r) for r in publications]
    sb.tables["jobs"] = [{"id": JOB, "type": P.JOB_TYPE, "status": "processing", "generation_id": None,
                          "book_id": None, "params": {"kit_id": KIT, "language": "en"}, "attempts": 0}]
    return sb


def _publish_job(**params) -> dict:
    return {"id": JOB, "type": P.JOB_TYPE, "generation_id": None, "book_id": None,
            "params": {"kit_id": KIT, "language": "en", **params}}


def test_a_public_publish_sends_the_nudge_once_however_many_parts(recorder):
    summary = P.run_publish_job(_publish_sb(parts=(1, 2, 3)), _publish_job(privacy="public"), transport=FakeYouTube())
    assert summary["published"] == [1, 2, 3]
    assert recorder == ["publish_done"]


def test_a_private_publish_sends_nothing(recorder):
    summary = P.run_publish_job(_publish_sb(), _publish_job(privacy="private"), transport=FakeYouTube())
    assert summary["published"] == [1]
    assert recorder == []


def test_a_rerun_that_uploads_nothing_sends_nothing(recorder):
    done = [{"topic_kit_id": KIT, "part": 1, "channel_language": "en", "youtube_video_id": "vid1",
             "privacy": "public"}]
    summary = P.run_publish_job(_publish_sb(publications=done), _publish_job(privacy="public"), transport=FakeYouTube())
    assert summary["already"] == [1] and summary["published"] == []
    assert recorder == []


def test_a_refused_publish_sends_nothing(recorder):
    sb = _publish_sb()
    sb.tables["topic_kits"][0]["status"] = "in_review"
    assert P.run_publish_job(sb, _publish_job(privacy="public"), transport=FakeYouTube()) is None
    assert recorder == []


def test_a_part_that_went_public_sends_the_nudge_even_when_another_part_failed(recorder):
    """The job row reads error, but a public video IS on the channel and in
    the catalogue; the page must follow it."""
    yt = FakeYouTube()
    real, calls = yt.upload_video, {"n": 0}

    def second_upload_fails(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("quota")
        return real(*a, **kw)

    yt.upload_video = second_upload_fails
    summary = P.run_publish_job(_publish_sb(parts=(1, 2)), _publish_job(privacy="public"), transport=yt)
    assert summary["published"] == [1] and summary["failed"] == [2]
    assert recorder == ["publish_done"]


# supersede — the old row's superseded_by changes which kit is current.

OLD_PUB, NEW_PUB = "pub-old", "pub-new"


class FakeVideos:
    def __init__(self):
        self.videos = {"old-vid": {"snippet": {"description": "old"}, "status": {"privacyStatus": "public"}}}

    def video(self, video_id):
        return self.videos.get(video_id)

    def update_snippet(self, video_id, snippet):
        self.videos[video_id]["snippet"].update(snippet)

    def set_privacy(self, video_id, status, privacy):
        self.videos[video_id]["status"]["privacyStatus"] = privacy


def _supersede_sb(*, already=False) -> FakeSB:
    sb = FakeSB()
    sb.tables["topic_kits"] = [{"id": "k-old", "topic_id": TOPIC}, {"id": "k-new", "topic_id": TOPIC}]
    sb.tables["topic_publications"] = [
        {"id": OLD_PUB, "topic_kit_id": "k-old", "part": 1, "channel_language": "en", "youtube_video_id": "old-vid",
         "privacy": "public", "superseded_by": NEW_PUB if already else None},
        {"id": NEW_PUB, "topic_kit_id": "k-new", "part": 1, "channel_language": "en", "youtube_video_id": "new-vid",
         "privacy": "public", "superseded_by": None},
    ]
    sb.tables["jobs"] = [{"id": JOB, "type": S.JOB_TYPE, "status": "processing", "generation_id": None,
                          "book_id": None, "params": {}, "attempts": 0}]
    return sb


def _supersede_job() -> dict:
    return {"id": JOB, "type": S.JOB_TYPE, "params": {"old_publication_id": OLD_PUB, "new_publication_id": NEW_PUB}}


def test_a_supersede_that_stamps_the_old_row_sends_the_nudge_once(recorder):
    summary = S.run_supersede_job(_supersede_sb(), _supersede_job(), transport=FakeVideos())
    assert summary is not None and summary["already"] is False
    assert recorder == ["supersede_done"]


def test_a_supersede_already_done_sends_nothing(recorder):
    summary = S.run_supersede_job(_supersede_sb(already=True), _supersede_job(), transport=FakeVideos())
    assert summary is not None and summary["already"] is True
    assert recorder == []


def test_a_refused_supersede_sends_nothing(recorder):
    sb = _supersede_sb()
    sb.tables["topic_publications"] = sb.tables["topic_publications"][1:]  # the old row is gone
    assert S.run_supersede_job(sb, _supersede_job(), transport=FakeVideos()) is None
    assert recorder == []
