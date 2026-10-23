"""The daily analytics poll (catalogue/pinterest/stats.py) and the engine's
wiring into the worker (worker/run.py, worker/client.py)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from catalogue.pinterest import common as C
from catalogue.pinterest import stats as ST
from tests.pinterest_fakes import FakeApi, PinSB

NOW = datetime(2026, 10, 23, 3, 0, tzinfo=timezone.utc)        # a Friday, after 02:00
EARLY = datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc)
MONDAY = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(C.ENV, "1")
    ST._state.update({"last": 0.0, "missing_logged": False})


def _sb():
    sb = PinSB()
    sb.set_settings(enabled=True, summary_email="f@example.com")
    sb.pins().extend([
        {"id": "live", "status": "posted", "pinterest_pin_id": "9001", "posted_at": "2026-10-10T21:00:00+00:00",
         "template": "diagram", "language": "en", "title": "Plant cells"},
        {"id": "dry", "status": "posted", "pinterest_pin_id": "dryrun:dry", "posted_at": "2026-10-20T21:00:00+00:00",
         "template": "explainer", "language": "en", "title": "Dry"},
        {"id": "old", "status": "posted", "pinterest_pin_id": "9002", "posted_at": "2026-06-01T21:00:00+00:00",
         "template": "worksheet", "language": "en", "title": "Old"},
        {"id": "queued", "status": "approved", "pinterest_pin_id": None},
    ])
    return sb


def _analytics():
    return {"9001": {"all": {"daily_metrics": [
        {"date": "2026-10-20", "data_status": "READY",
         "metrics": {"IMPRESSION": 40, "SAVE": 2, "PIN_CLICK": 5, "OUTBOUND_CLICK": 3}},
        {"date": "2026-10-22", "data_status": "READY",
         "metrics": {"IMPRESSION": 10, "SAVE": 0, "PIN_CLICK": 1, "OUTBOUND_CLICK": 1}},
        {"date": "2026-10-23", "data_status": "PROCESSING", "metrics": {}}]}}}


def test_once_per_day_after_two_and_only_our_live_pins_in_the_90_day_window():
    sb = _sb()
    api = FakeApi(analytics=_analytics())
    assert ST.maybe_poll(sb, now=EARLY, api=api, clock=lambda: 1.0) is None and api.calls == []
    ST._state["last"] = 0.0
    out = ST.maybe_poll(sb, now=NOW, api=api, clock=lambda: 2.0)
    assert out.startswith("polled 1 pin(s)")
    analytics = [c for c in api.calls if c[0] == "analytics"]
    assert analytics == [("analytics", "9001", "2026-10-10", "2026-10-23", ST.METRICS)], \
        "dry-run pins and pins older than 90 days are not polled; the window starts at the post"
    ST._state["last"] = 0.0
    assert ST.maybe_poll(sb, now=NOW, api=api, clock=lambda: 400.0) is None, "once per UTC day"
    assert len([c for c in api.calls if c[0] == "analytics"]) == 1


def test_the_last_two_days_are_provisional_and_rows_upsert_by_day():
    sb = _sb()
    ST.poll(sb, C.read_settings(sb), NOW, api=FakeApi(analytics=_analytics()))
    rows = {r["day"]: r for r in sb.tables[C.STATS]}
    assert rows["2026-10-20"]["provisional"] is False and rows["2026-10-20"]["impression"] == 40
    assert rows["2026-10-22"]["provisional"] is True
    assert rows["2026-10-23"]["provisional"] is True and rows["2026-10-23"]["impression"] is None
    ST.poll(sb, C.read_settings(sb), NOW, api=FakeApi(analytics=_analytics()))
    assert len(sb.tables[C.STATS]) == 3, "a second poll overwrites, never duplicates"


def test_the_metric_window_is_thirty_days_for_an_older_pin():
    sb = _sb()
    sb.pins()[0]["posted_at"] = "2026-08-01T21:00:00+00:00"
    api = FakeApi()
    ST.poll(sb, C.read_settings(sb), NOW, api=api)
    assert [c for c in api.calls if c[0] == "analytics"][0][2] == "2026-09-23"


def test_a_rate_limit_stops_the_pass_and_resumes_after_the_reset():
    from worker import client as db

    sb = _sb()
    out = ST.poll(sb, C.read_settings(sb), NOW, api=FakeApi(raise_on={"pin_analytics": db.DeferredJob(120, "429")}))
    assert out.startswith("rate limited")
    stats = sb.settings()["stats"]
    assert stats["resume_after"] and "polled_on" not in stats
    assert not ST.due(stats, NOW) and ST.due(stats, datetime(2026, 10, 23, 3, 5, tzinfo=timezone.utc))


def test_dark_without_the_switch():
    sb = _sb()
    sb.set_settings(enabled=False)
    api = FakeApi()
    assert ST.maybe_poll(sb, now=NOW, api=api) is None and api.calls == []


def test_monday_sends_the_weekly_summary(monkeypatch):
    sent = []
    monkeypatch.setattr("catalogue.auto_translate.send_email", lambda to, s, t: sent.append((to, s, t)) or True)
    sb = _sb()
    sb.pins()[0]["posted_at"] = "2026-10-20T21:00:00+00:00"
    api = FakeApi(analytics={"9001": {"all": {"daily_metrics": [
        {"date": "2026-10-22", "data_status": "READY",
         "metrics": {"IMPRESSION": 50, "SAVE": 3, "PIN_CLICK": 4, "OUTBOUND_CLICK": 2}}]}}})
    out = ST.maybe_poll(sb, now=MONDAY, api=api, clock=lambda: 1.0)
    assert "weekly summary sent" in out
    to, subject, text = sent[0]
    assert to == "f@example.com" and "50 impressions" in subject
    assert "By template" in text and "diagram" in text and "By language" in text
    ST._state["last"] = 0.0
    ST.maybe_poll(sb, now=MONDAY, api=api, clock=lambda: 999.0)
    assert len(sent) == 1, "one weekly email"


# ── wiring ──────────────────────────────────────────────────────────────

TYPES = ("pin_draft", "pin_post", "pin_boards")


def test_run_dispatches_the_three_types():
    import worker.run as run

    src = Path(run.__file__).read_text(encoding="utf-8")
    for t, fn in (("pin_draft", "run_draft_job"), ("pin_post", "run_post_job"), ("pin_boards", "run_boards_job")):
        assert f'job_type == "{t}"' in src and f"{fn}(sb, job)" in src


def test_both_observer_lists_and_the_last_lane_carry_them():
    import worker.run as run
    from worker import client as db

    for t in TYPES:
        assert t in db.OBSERVER_JOB_TYPES, "they own no generation"
        assert t in run.CATALOGUE_JOB_TYPES, "claimed on the last lane"
        assert t in run.OBSERVER_JOB_TYPES, "never on a user lane"
        assert t in run._user_builder_exclusions()


def test_the_reaper_calls_both_ticks():
    import worker.run as run

    src = Path(run.__file__).read_text(encoding="utf-8")
    serve = src[src.index("def _serve("):]
    assert "from catalogue.pinterest.agent import maybe_tick" in serve and "maybe_pinterest_agent(sb)" in serve
    assert "from catalogue.pinterest.stats import maybe_poll" in serve and "maybe_pinterest_stats(sb)" in serve
    assert serve.index("maybe_publish_agent(sb)") < serve.index("maybe_pinterest_agent(sb)")


def test_the_three_jobs_are_not_builders_for_the_never_starve_gate():
    from catalogue.figures import builder_queued

    sb = PinSB()
    for i, t in enumerate(TYPES):
        sb.tables["jobs"].append({"id": f"j{i}", "type": t, "status": "queued", "params": {}})
    assert builder_queued(sb) is False, "a pin job must never hold back its own lane"
