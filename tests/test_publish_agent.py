"""The YouTube Publishing Agent (catalogue/publish_agent.py): one video per
language per day, a "Run once more" round on request, one upload at a
time, never while a teacher's job is live. Every test runs on FakeSB; the
agent only inserts ``topic_publish`` jobs, so nothing here can reach YouTube.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from catalogue import publish as P
from catalogue import publish_agent as A
from tests.catalogue_fakes import FakeSB

# 2026-10-21 08:00 UTC = 01:00 PDT: after the default 00:30 daily time
AFTER = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc)
# 2026-10-21 07:10 UTC = 00:10 PDT: the new quota day, before 00:30
BEFORE = datetime(2026, 10, 21, 7, 10, tzinfo=timezone.utc)
ADMIN = "admin-1"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(A.ENV, "1")
    monkeypatch.delenv(P.AUDIT_FLAG, raising=False)
    monkeypatch.setattr(P, "builders_are_queued", lambda sb: False)
    monkeypatch.setattr(P, "own_channel", lambda lang: True)
    A._state.update({"last": 0.0, "missing_logged": False})


def _sb(*items) -> FakeSB:
    sb = FakeSB()
    sb.tables.update({A.QUEUE: [], A.ROUNDS: [], "platform_settings": [], "platform_audit_log": []})
    for i, (kit, lang, added) in enumerate(items):
        sb.tables["topic_kits"].append({"id": kit, "topic_id": f"t-{kit}", "language": lang})
        sb.tables[A.QUEUE].append({"id": f"q{i}", "kit_id": kit, "topic_id": f"t-{kit}", "language": lang,
                                   "status": "pending", "attempts": 0, "added_by": ADMIN,
                                   "added_at": f"2026-10-20T0{i}:00:00+00:00"})
    return sb


def _item(sb, kit):
    return next(r for r in sb.tables[A.QUEUE] if r["kit_id"] == kit)


def _jobs(sb):
    return [j for j in sb.tables["jobs"] if j.get("type") == P.JOB_TYPE]


def _finish(sb, kit, *, posted=True, status="done", error=None):
    job = next(j for j in _jobs(sb) if j["params"]["kit_id"] == kit)
    job["status"], job["error"] = status, error
    if posted:
        sb.tables["topic_publications"].append({"topic_kit_id": kit, "part": 1, "channel_language": "es",
                                                "youtube_video_id": f"yt-{kit}"})


def test_dark_without_the_environment_flag(monkeypatch):
    monkeypatch.delenv(A.ENV)
    sb = _sb(("k1", "es", 0))
    assert A.maybe_tick(sb, now=AFTER) is None and sb.tables[A.ROUNDS] == []


def test_a_daily_round_takes_the_oldest_kit_of_each_language_and_posts_one_at_a_time():
    sb = _sb(("es-old", "es", 0), ("fr-1", "fr", 1), ("es-new", "es", 2))
    A.tick(sb, now=AFTER)
    rnd = sb.tables[A.ROUNDS][0]
    assert rnd["trigger"] == "daily" and rnd["quota_day"] == "2026-10-21" and rnd["status"] == "running"
    assert _item(sb, "es-old")["status"] == "queued" and _item(sb, "fr-1")["status"] == "selected"
    assert _item(sb, "es-new")["status"] == "pending", "one per language per day"
    assert len(_jobs(sb)) == 1

    assert A.tick(sb, now=AFTER).startswith("waiting"), "the next kit waits for the running upload"
    assert len(_jobs(sb)) == 1

    _finish(sb, "es-old")
    A.tick(sb, now=AFTER)
    assert _item(sb, "es-old")["status"] == "done" and _item(sb, "fr-1")["status"] == "queued"

    _finish(sb, "fr-1")
    assert A.tick(sb, now=AFTER) == "round done"
    assert sb.tables[A.ROUNDS][0]["status"] == "done"
    assert A.tick(sb, now=AFTER) is None, "one daily round per quota day"
    assert _item(sb, "es-new")["status"] == "pending"


def test_no_daily_round_before_the_daily_time_or_with_nothing_pending():
    assert A.tick(_sb(("k1", "es", 0)), now=BEFORE) is None
    sb = _sb()
    assert A.tick(sb, now=AFTER) is None and sb.tables[A.ROUNDS] == []


def test_run_once_more_starts_a_round_even_after_todays_daily_one():
    sb = _sb(("es-1", "es", 0), ("es-2", "es", 1))
    sb.tables[A.ROUNDS].append({"id": "daily", "trigger": "daily", "quota_day": "2026-10-21", "status": "done"})
    sb.tables[A.ROUNDS].append({"id": "manual", "trigger": "manual", "quota_day": "2026-10-21",
                                "status": "requested", "requested_by": ADMIN, "created_at": "2026-10-21T08:00:00+00:00"})
    A.tick(sb, now=AFTER)
    assert _item(sb, "es-1")["status"] == "queued" and _item(sb, "es-1")["round_id"] == "manual"
    assert _item(sb, "es-2")["status"] == "pending"


def test_nothing_is_queued_while_a_teachers_job_is_live(monkeypatch):
    monkeypatch.setattr(P, "builders_are_queued", lambda sb: True)
    sb = _sb(("k1", "es", 0))
    assert A.tick(sb, now=AFTER) == "waiting: a teacher's job is live"
    assert _jobs(sb) == [] and _item(sb, "k1")["status"] == "selected"


def test_nothing_is_queued_while_another_publish_job_is_live():
    sb = _sb(("k1", "es", 0))
    sb.tables["jobs"].append({"id": "manual-post", "type": P.JOB_TYPE, "status": "processing", "params": {}})
    assert A.tick(sb, now=AFTER) == "waiting: another publish job is live"


def test_a_failed_job_goes_back_to_the_queue_then_gives_up():
    sb = _sb(("k1", "es", 0))
    A.tick(sb, now=AFTER)
    _finish(sb, "k1", posted=False, status="error", error="part 1: RuntimeError: boom")
    A.tick(sb, now=AFTER)
    item = _item(sb, "k1")
    assert item["status"] == "pending" and item["attempts"] == 1 and item["round_id"] is None
    assert "boom" in item["last_error"]
    item.update({"attempts": A.MAX_ATTEMPTS - 1, "status": "queued", "round_id": "r", "job_id": "gone"})
    A.settle(sb, item, AFTER)
    assert _item(sb, "k1")["status"] == "failed"


def test_a_kit_already_on_youtube_is_done_without_a_job():
    sb = _sb(("k1", "es", 0))
    sb.tables["topic_publications"].append({"topic_kit_id": "k1", "part": 1, "channel_language": "es",
                                            "youtube_video_id": "posted-by-hand"})
    A.tick(sb, now=AFTER)
    assert _item(sb, "k1")["status"] == "done" and _jobs(sb) == []


def test_a_language_without_a_channel_is_skipped(monkeypatch):
    monkeypatch.setattr(P, "own_channel", lambda lang: False)
    sb = _sb(("k1", "es", 0))
    A.tick(sb, now=AFTER)
    assert _item(sb, "k1")["status"] == "skipped" and _jobs(sb) == []


def test_paused_does_nothing():
    sb = _sb(("k1", "es", 0))
    sb.tables["platform_settings"].append({"key": A.SETTINGS_KEY, "value": {"paused": True}})
    assert A.tick(sb, now=AFTER) is None and sb.tables[A.ROUNDS] == []


def test_the_job_is_the_post_buttons_job_and_is_audited():
    sb = _sb(("k1", "es", 0))
    A.tick(sb, now=AFTER)
    job = _jobs(sb)[0]
    assert job["status"] == "queued" and job["generation_id"] is None
    assert job["params"]["kit_id"] == "k1" and job["params"]["privacy"] == "private"
    assert job["params"]["queue_item_id"] == "q0"
    audit = sb.tables["platform_audit_log"][0]
    assert audit["actor_id"] == ADMIN and audit["action"] == "library_publish"
    assert audit["detail"]["via"] == "youtube publish agent"


def test_public_once_the_audit_is_passed(monkeypatch):
    monkeypatch.setenv(P.AUDIT_FLAG, "1")
    sb = _sb(("k1", "es", 0))
    A.tick(sb, now=AFTER)
    assert _jobs(sb)[0]["params"]["privacy"] == "public"


def test_the_daily_time_reads_hh_mm_and_falls_back():
    assert A.daily_minutes({"daily_at": "06:15"}) == 375
    assert A.daily_minutes({"daily_at": "nonsense"}) == 30
    assert A.daily_minutes({}) == 30


def test_a_database_without_the_tables_is_logged_once_and_never_raises():
    class _Old:
        def table(self, name):
            raise RuntimeError('relation "public.youtube_publish_rounds" does not exist')

    assert A.maybe_tick(_Old(), now=AFTER) is None
    assert A._state["missing_logged"] is True
