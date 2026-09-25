"""The video lane cap: at most MAX_VIDEO_JOBS presentations render at once
in one process, and a document is never queued behind them.

2026-09-25: two videos and a deck for one teacher shared a 24-core box and
every one of them crawled, while the claim loop would happily have taken a
third and a fourth video. The cap keeps render pools from stacking and
keeps threads free for the forty-second jobs."""

from __future__ import annotations

import pytest

import worker.run as run
from tests.test_observer_job_guard import _builder, _fresh
from worker import client as db


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(run, "_claim_catalogue_generation", lambda sb: None)
    run._shutdown.clear()
    with run._inflight_lock:
        run._inflight_jobs.clear()
    yield
    with run._inflight_lock:
        run._inflight_jobs.clear()


def _finish(sb, job, gen_id):
    db.finish_job(sb, job["id"], gen_id)


def test_above_the_cap_a_video_waits_and_a_worksheet_does_not(monkeypatch):
    monkeypatch.setattr(run, "MAX_VIDEO_JOBS", 1)
    monkeypatch.setattr(run, "process_generation", _finish)
    sb = _fresh("queued", jobs=[_builder("job-v2", "presentation"), _builder("job-w", "worksheet")])
    run._inflight_add({**_builder("job-v1", "presentation", "processing")})   # one slot, taken
    assert run._video_slots_full()
    assert run.run_once(sb) is True
    by_id = {j["id"]: j for j in sb.tables["jobs"]}
    assert by_id["job-w"]["status"] == "done", "the worksheet went ahead"
    assert by_id["job-v2"]["status"] == "queued", "the second video waited for a slot"
    # nothing else is claimable: the thread reports idle rather than taking the video
    assert run.run_once(sb) is False
    assert by_id["job-v2"]["status"] == "queued"


def test_when_a_slot_frees_the_video_is_claimed(monkeypatch):
    monkeypatch.setattr(run, "MAX_VIDEO_JOBS", 1)
    monkeypatch.setattr(run, "process_generation", _finish)
    sb = _fresh("queued", jobs=[_builder("job-v2", "presentation")])
    run._inflight_add({**_builder("job-v1", "presentation", "processing")})
    assert run.run_once(sb) is False
    run._inflight_remove("job-v1")
    assert not run._video_slots_full()
    assert run.run_once(sb) is True
    assert sb.tables["jobs"][0]["status"] == "done"


def test_the_cap_counts_only_videos(monkeypatch):
    monkeypatch.setattr(run, "MAX_VIDEO_JOBS", 2)
    run._inflight_add(_builder("job-d", "deck", "processing"))
    run._inflight_add(_builder("job-i", "index_book", "processing"))
    run._inflight_add(_builder("job-v", "presentation", "processing"))
    assert run._videos_in_flight() == 1 and not run._video_slots_full()
    assert "presentation" not in run._user_builder_exclusions()
    run._inflight_add(_builder("job-v2", "presentation", "processing"))
    assert run._video_slots_full() and "presentation" in run._user_builder_exclusions()


def test_the_default_cap_fits_the_box():
    """4 videos x 8 render workers on the 24-core Railway box (2026-09-25)."""
    assert run.MAX_VIDEO_JOBS == 4 or "MAX_VIDEO_JOBS" in __import__("os").environ
