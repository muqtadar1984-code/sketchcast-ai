"""The Pinterest agent's tick (catalogue/pinterest/agent.py) and the board job
(catalogue/pinterest/boards.py) on FakeSB. The agent only inserts jobs; every
test here proves what it inserts, and when it does nothing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from catalogue.pinterest import agent as AG
from catalogue.pinterest import api as A
from catalogue.pinterest import boards as B
from catalogue.pinterest import common as C
from tests.pinterest_fakes import ADMIN, TOPIC, FakeApi, PinSB, world

AFTER = datetime(2026, 10, 23, 21, 30, tzinfo=timezone.utc)    # after the 21:00 UTC default
BEFORE = datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(C.ENV, "1")
    monkeypatch.setenv("CATALOGUE_WINDOW_UTC", "always")
    monkeypatch.setattr(C, "builders_queued", lambda sb, exclude_job_id=None: False)
    monkeypatch.setattr("catalogue.auto_translate.send_email", lambda to, s, t: True)
    AG._state.update({"last": 0.0, "missing_logged": False})


def _approved(sb, pid, *, topic=TOPIC, lang="en", at="2026-10-22T10:00:00+00:00", **kw):
    row = {"id": pid, "source_kind": "figure", "source_ref": pid, "topic_id": topic, "language": lang,
           "format": "static", "template": "diagram", "status": "approved", "approved_by": ADMIN,
           "approved_at": at, "round_id": None, "attempts": 0}
    row.update(kw)
    sb.pins().append(row)
    return row


def _jobs(sb, t):
    return [j for j in sb.tables["jobs"] if j.get("type") == t]


def _pin(sb, pid):
    return next(p for p in sb.pins() if p["id"] == pid)


def _quiet(sb):
    """No drafting noise: every language at its cap of waiting pins."""
    sb.set_settings(max_waiting_per_language=1)


# ── dark ────────────────────────────────────────────────────────────────


def test_dark_without_the_environment_flag(monkeypatch):
    monkeypatch.delenv(C.ENV)
    sb = world()
    _approved(sb, "p1")
    assert AG.maybe_tick(sb, now=AFTER) is None and sb.tables["jobs"] == [] and sb.calls == []


def test_dark_while_the_settings_switch_is_off():
    sb = world()
    sb.set_settings(enabled=False)
    _approved(sb, "p1")
    assert AG.tick(sb, now=AFTER) is None and sb.tables["jobs"] == [] and sb.tables[C.ROUNDS] == []
    sb.tables["platform_settings"].clear()
    assert AG.tick(sb, now=AFTER) is None, "no row at all reads as off"


def test_paused_does_nothing():
    sb = world()
    sb.set_settings(paused=True)
    _approved(sb, "p1")
    assert AG.tick(sb, now=AFTER) is None and sb.tables["jobs"] == []


def test_a_tripped_breaker_leaves_only_the_stats():
    sb = world()
    sb.set_settings(breaker={"tripped": True, "failures": 3})
    _approved(sb, "p1")
    assert AG.tick(sb, now=AFTER) == "breaker tripped: stats only" and sb.tables["jobs"] == []


def test_a_live_builder_holds_the_tick(monkeypatch):
    monkeypatch.setattr(C, "builders_queued", lambda sb, exclude_job_id=None: True)
    sb = world()
    _approved(sb, "p1")
    assert AG.tick(sb, now=AFTER) == "waiting: a teacher's job is live"
    assert sb.tables["jobs"] == [] and sb.tables[C.ROUNDS] == []


def test_a_database_without_0154_is_logged_once():
    class _Old:
        def table(self, name):
            raise RuntimeError('relation "public.pinterest_pins" does not exist')

    assert AG.maybe_tick(_Old(), now=AFTER) is None and AG._state["missing_logged"] is True


# ── rounds ──────────────────────────────────────────────────────────────


def test_one_daily_round_per_day_after_daily_at():
    sb = world()
    _quiet(sb)
    _approved(sb, "p1")
    assert AG.rounds(sb, C.read_settings(sb), BEFORE) is None and sb.tables[C.ROUNDS] == []
    AG.tick(sb, now=AFTER)
    rnd = sb.tables[C.ROUNDS][0]
    assert rnd["trigger"] == "daily" and rnd["day"] == "2026-10-23" and rnd["status"] == "running"
    assert _pin(sb, "p1")["status"] == "scheduled" and _pin(sb, "p1")["round_id"] == rnd["id"]
    assert len(_jobs(sb, "pin_post")) == 1


def test_a_racing_daily_round_reads_23505_as_already_there():
    sb = world()
    sb.tables[C.ROUNDS].append({"id": "other", "trigger": "daily", "day": "2026-10-23", "status": "done"})
    assert AG.create_daily_round(sb, "2026-10-23", AFTER) is None
    _approved(sb, "p1")
    _quiet(sb)
    AG.tick(sb, now=AFTER)
    assert len(sb.tables[C.ROUNDS]) == 1 and _pin(sb, "p1")["status"] == "approved"


def test_a_manual_round_is_picked_up():
    sb = world()
    _quiet(sb)
    sb.tables[C.ROUNDS].append({"id": "daily", "trigger": "daily", "day": "2026-10-23", "status": "done"})
    sb.tables[C.ROUNDS].append({"id": "manual", "trigger": "manual", "day": "2026-10-23", "status": "requested",
                                "requested_by": ADMIN, "created_at": "2026-10-23T21:10:00+00:00"})
    _approved(sb, "p1")
    AG.tick(sb, now=AFTER)
    assert _pin(sb, "p1")["round_id"] == "manual" and _pin(sb, "p1")["status"] == "scheduled"
    assert next(r for r in sb.tables[C.ROUNDS] if r["id"] == "manual")["status"] == "running"


def test_per_language_selection_with_the_topic_gap():
    settings = {**C.DEFAULTS, "languages": ["en", "fr"], "per_language_per_day": 2, "min_topic_gap_days": 7}
    pins = [
        {"id": "en-old", "language": "en", "topic_id": "t1", "approved_at": "2026-10-01"},
        {"id": "en-same-topic", "language": "en", "topic_id": "t1", "approved_at": "2026-10-02"},
        {"id": "en-gap", "language": "en", "topic_id": "t2", "approved_at": "2026-10-03"},
        {"id": "en-3", "language": "en", "topic_id": "t3", "approved_at": "2026-10-04"},
        {"id": "en-4", "language": "en", "topic_id": "t4", "approved_at": "2026-10-05"},
        {"id": "fr-1", "language": "fr", "topic_id": "t1", "approved_at": "2026-10-09"},
        {"id": "es-1", "language": "es", "topic_id": "t9", "approved_at": "2026-10-01"},
        {"id": "book", "language": "fr", "topic_id": None, "approved_at": "2026-10-10"},
    ]
    last = {"t2|en": "2026-10-20T00:00:00+00:00", "t1|fr": "2026-10-01T00:00:00+00:00"}
    picked = [p["id"] for p in AG.select_for_round(pins, last, settings, AFTER)]
    assert picked == ["en-old", "en-3", "fr-1", "book"]
    # the app's nextRoundPreview clamps to 5 per language
    assert len(AG.select_for_round(pins, {}, {**settings, "per_language_per_day": 99}, AFTER)) <= 10


def test_one_post_at_a_time_with_the_post_gap():
    sb = world()
    _quiet(sb)
    sb.set_settings(per_language_per_day=2, post_gap_minutes=5)
    _approved(sb, "p1", at="2026-10-22T09:00:00+00:00")
    _approved(sb, "p2", topic="t2", at="2026-10-22T10:00:00+00:00")
    AG.tick(sb, now=AFTER)
    posts = _jobs(sb, "pin_post")
    assert len(posts) == 1 and posts[0]["params"]["pin_row_id"] == "p1", "oldest approved first"
    assert AG.tick(sb, now=AFTER) == "waiting: a post is running"
    # the post job finishes: p1 posted a minute ago
    posts[0]["status"] = "done"
    _pin(sb, "p1").update(status="posted", posted_at="2026-10-23T21:29:00+00:00")
    assert AG.tick(sb, now=AFTER) == "waiting: the post gap"
    later = datetime(2026, 10, 23, 21, 35, tzinfo=timezone.utc)
    AG.tick(sb, now=later)
    assert [j["params"]["pin_row_id"] for j in _jobs(sb, "pin_post")] == ["p1", "p2"]


def test_a_round_settles_when_nothing_is_scheduled_or_posting():
    sb = world()
    _quiet(sb)
    _approved(sb, "p1")
    AG.tick(sb, now=AFTER)
    rnd = sb.tables[C.ROUNDS][0]
    _jobs(sb, "pin_post")[0]["status"] = "done"
    _pin(sb, "p1").update(status="posted", posted_at="2026-10-23T21:31:00+00:00")
    assert AG.tick(sb, now=AFTER).endswith("round done")
    assert rnd["status"] == "done" and rnd["summary"]["outcome"] == {"posted": 1} and rnd["finished_at"]
    assert AG.tick(sb, now=AFTER) is None, "one daily round per day"


def test_three_failures_in_a_row_trip_the_breaker():
    sb = world()
    sb.set_settings(breaker={"tripped": False, "failures": 3, "last_failure": "HTTP 500"})
    _approved(sb, "p1")
    assert AG.tick(sb, now=AFTER) == "breaker tripped"
    s = sb.settings()
    assert s["breaker"]["tripped"] is True and s["paused"] is True and s["breaker"]["tripped_at"]
    assert sb.tables["jobs"] == []


# ── drafting feeder ─────────────────────────────────────────────────────


def test_the_feeder_queues_one_draft_for_the_next_topic():
    sb = world()
    AG.tick(sb, now=BEFORE)
    drafts = _jobs(sb, "pin_draft")
    assert len(drafts) == 1 and drafts[0]["params"] == {"topic_id": TOPIC, "language": "en"}
    AG.tick(sb, now=BEFORE)
    assert len(_jobs(sb, "pin_draft")) == 1, "at most one pin_draft live"


def test_max_waiting_caps_drafting():
    sb = world()
    sb.set_settings(max_waiting_per_language=2)
    sb.current_kits.append({"topic_id": "t-2", "language": "en", "topic_kit_id": "k2",
                            "approved_at": "2026-10-05T00:00:00+00:00"})
    _approved(sb, "a1")
    sb.pins().append({"id": "r1", "topic_id": "t-x", "language": "en", "status": "in_review"})
    AG.tick(sb, now=BEFORE)
    assert _jobs(sb, "pin_draft") == []
    sb.pins().pop()
    AG.tick(sb, now=BEFORE)
    assert [j["params"]["topic_id"] for j in _jobs(sb, "pin_draft")] == ["t-2"]


def test_a_rerender_is_drafted_before_a_new_topic():
    sb = world()
    sb.pins().append({"id": "d1", "topic_id": "t-redo", "language": "en", "status": "draft",
                      "updated_at": "2026-10-23T10:00:00+00:00"})
    AG.tick(sb, now=BEFORE)
    assert [j["params"]["topic_id"] for j in _jobs(sb, "pin_draft")] == ["t-redo"]


def test_no_drafting_in_a_language_without_a_created_board():
    sb = world(boards="planned")
    AG.tick(sb, now=BEFORE)
    assert _jobs(sb, "pin_draft") == []


def test_a_topic_drafted_this_week_is_not_drafted_again():
    sb = world()
    sb.tables["jobs"].append({"id": "old", "type": "pin_draft", "status": "done",
                              "params": {"topic_id": TOPIC, "language": "en"},
                              "created_at": "2026-10-22T00:00:00+00:00"})
    AG.tick(sb, now=BEFORE)
    assert len(_jobs(sb, "pin_draft")) == 1


def test_the_feeder_follows_the_catalogue_window(monkeypatch):
    monkeypatch.setattr("catalogue.kit.catalogue_window_open", lambda *a, **k: False)
    sb = world()
    AG.tick(sb, now=BEFORE)
    assert _jobs(sb, "pin_draft") == []


def test_an_edited_pin_is_rechecked_by_the_tick(monkeypatch):
    sb = world()
    seen = []
    monkeypatch.setattr("catalogue.pinterest.draft.recheck_pending",
                        lambda sb, link_check=None, limit=3, now=None: seen.append(1) or 1)
    out = AG.tick(sb, now=BEFORE)
    assert seen and "re-checked 1 pin(s)" in out


# ── boards ──────────────────────────────────────────────────────────────


def test_an_approved_board_queues_pin_boards_once():
    sb = world(boards="approved")
    AG.tick(sb, now=BEFORE)
    AG.tick(sb, now=BEFORE)
    assert len(_jobs(sb, "pin_boards")) == 1


def test_pin_boards_adopts_by_name_then_creates_public_boards_with_sections():
    sb = world(boards="approved")
    sb.set_settings(board_sections=["Grades 6-8"])
    api = FakeApi(boards=[{"id": "pb-existing", "name": "biology"}])
    out = B.create_boards(sb, "job-b", api=api)
    rows = {b["board_key"]: b for b in sb.tables[C.BOARDS]}
    assert rows["biology"]["pinterest_board_id"] == "pb-existing" and rows["biology"]["status"] == "created"
    assert rows["free_worksheets"]["status"] == "created" and rows["free_worksheets"]["pinterest_board_id"].startswith("pb-new")
    assert [c for c in api.calls if c == ("create_board",)] == [("create_board",)], "biology adopted, not recreated"
    assert rows["biology"]["sections"] == {"Grades 6-8": "sec-Grades 6-8"}
    assert len(out["created"]) == 2 and out["errors"] == []


def test_pin_boards_trips_the_breaker_on_a_refused_token():
    sb = world(boards="approved")
    out = B.create_boards(sb, "job-b", api=FakeApi(raise_on={"list_boards": A.TokenInvalid(401, 2, "no")}))
    assert out["step"] == "breaker" and sb.settings()["breaker"]["tripped"] is True
    assert all(b["status"] == "approved" for b in sb.tables[C.BOARDS])


def test_pin_boards_yields_to_builders(monkeypatch):
    from worker import client as db

    monkeypatch.setattr(C, "builders_queued", lambda sb, exclude_job_id=None: True)
    with pytest.raises(db.DeferredJob):
        B.create_boards(world(boards="approved"), "job-b", api=FakeApi())
