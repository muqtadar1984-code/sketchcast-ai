"""Burst control across the worker: the catalogue job cap, one kit at a time,
the catalogue waiting budget, the paused-not-failed message, the deck
deferral that is no longer a recorded error, and the two model paths
reporting their outcomes to the gates."""
from __future__ import annotations

import time

import pytest
import requests

import worker.run as run
from shared import phase_clock
from shared import provider_gate as pg
from tests.gate_fakes import GateRpcSB
from tests.test_observer_job_guard import BOOK, GEN, _builder, _fresh
from worker import client as db

KIT_A, KIT_B = "kit-aaaa", "kit-bbbb"


def _fake_time(g):
    """Give a gate a clock its own sleeps advance (a recorded sleep that does
    not move time would spin a cooldown wait forever)."""
    st = {"t": 1000.0, "slept": []}
    g._wall = lambda: st["t"]
    g._clock = lambda: st["t"]

    def _sleep(s):
        st["slept"].append(float(s))
        st["t"] += float(s)
    g._sleep = _sleep
    return st


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    run._shutdown.clear()
    with run._inflight_lock:
        run._inflight_jobs.clear()
    yield
    with run._inflight_lock:
        run._inflight_jobs.clear()
    for gid in list(phase_clock._CLOCKS):
        phase_clock.stop(gid)


def _kit_job(job_id, kind, kit, status="queued", created="1"):
    return {"id": job_id, "type": kind, "status": status, "generation_id": GEN, "book_id": None,
            "attempts": 0, "created_at": created, "updated_at": "0",
            "params": {"catalogue": True, "kit_id": kit}}


def _rpc_sb(jobs):
    """A queue that answers the transactional claims (migration 0126)."""
    sb = GateRpcSB()
    sb.tables["generations"] = [{"id": GEN, "status": "queued", "kind": "worksheet",
                                 "owner_id": "u1", "book_id": BOOK, "params": {"catalogue": True}}]
    sb.tables["jobs"] = [dict(j) for j in jobs]
    return sb


def _figure(job_id, status="queued"):
    return {"id": job_id, "type": "figure_render", "status": status, "generation_id": None,
            "book_id": None, "attempts": 0, "created_at": "1", "updated_at": "0", "params": {}}


# ── the catalogue job cap ───────────────────────────────────────────────
class TestTheCatalogueJobCap:
    def test_above_the_cap_a_figure_job_waits_and_a_user_job_does_not(self, monkeypatch):
        monkeypatch.setattr(run, "MAX_CATALOGUE_JOBS", 1)
        monkeypatch.setattr(run, "_claim_catalogue_generation", lambda sb: None)
        monkeypatch.setattr(run, "process_generation", lambda sb, job, gid: db.finish_job(sb, job["id"], gid))
        sb = _rpc_sb(jobs=[_figure("fig-2"), _figure("fig-1", status="processing"),
                           _builder("job-w", "worksheet")])
        assert db.claim_catalogue_observer_job(sb, ["figure_render"], 1) is None, \
            "a processing figure job on ANY replica fills the one slot"
        assert run.run_once(sb) is True
        by_id = {j["id"]: j for j in sb.tables["jobs"]}
        assert by_id["job-w"]["status"] == "done"
        assert by_id["fig-2"]["status"] == "queued", "the second figure job waited"
        assert run.run_once(sb) is False

    def test_when_the_slot_frees_the_figure_job_is_claimed(self, monkeypatch):
        monkeypatch.setattr(run, "MAX_CATALOGUE_JOBS", 1)
        monkeypatch.setattr(run, "_claim_catalogue_generation", lambda sb: None)
        ran = []
        monkeypatch.setattr("catalogue.figures.run_figure_render_job",
                            lambda sb, job: ran.append(job["id"]) or db.finish_job(sb, job["id"]))
        sb = _rpc_sb(jobs=[_figure("fig-2"), _figure("fig-1", status="processing")])
        assert run.run_once(sb) is False
        sb.tables["jobs"][1]["status"] = "done"
        assert run.run_once(sb) is True and ran == ["fig-2"]

    def test_the_local_count_sees_only_catalogue_types(self):
        run._inflight_add(_builder("v", "presentation", "processing"))
        run._inflight_add(_figure("f", "processing"))
        assert run._catalogue_jobs_in_flight() == 1

    def test_an_unreadable_queue_keeps_the_gate_shut(self):
        class _Broken:
            def table(self, *a):
                raise ConnectionError("down")
        assert db.catalogue_jobs_processing(_Broken(), ["figure_render"]) >= 10**6
        assert db.catalogue_kits_processing(_Broken()) == {"<unreadable>"}
        down = GateRpcSB(fail=True)
        down.tables["jobs"] = [_figure("fig-1"), _kit_job("a-w", "worksheet", KIT_A)]
        assert db.claim_catalogue_observer_job(down, ["figure_render"], 2) is None, "fail closed"
        assert db.claim_catalogue_kit_job(down, [], 1) is None, "fail closed"
        assert all(j["status"] == "queued" for j in down.tables["jobs"])


# ── one kit at a time ───────────────────────────────────────────────────
class TestOneKitAtATime:
    @pytest.fixture(autouse=True)
    def _window_open(self, monkeypatch):
        monkeypatch.setattr("catalogue.kit.catalogue_window_open", lambda *a, **k: True)
        monkeypatch.setattr("catalogue.figures.builder_queued", lambda sb, *a, **k: False)
        monkeypatch.setattr(run, "MAX_CATALOGUE_KITS", 1)

    def test_a_second_kit_waits_while_the_first_generates(self):
        sb = _rpc_sb(jobs=[_kit_job("a-video", "presentation", KIT_A, status="processing"),
                           _kit_job("b-worksheet", "worksheet", KIT_B, created="2"),
                           _kit_job("a-deck", "deck", KIT_A, created="3")])
        assert db.catalogue_kits_processing(sb) == {KIT_A}
        job = run._claim_catalogue_generation(sb)
        assert job and job["id"] == "a-deck", "kit A's own deck is claimed, kit B's older worksheet is not"
        assert run._claim_catalogue_generation(sb) is None
        by_id = {j["id"]: j for j in sb.tables["jobs"]}
        assert by_id["b-worksheet"]["status"] == "queued"

    def test_with_nothing_generating_the_oldest_kit_starts(self):
        sb = _rpc_sb(jobs=[_kit_job("b-worksheet", "worksheet", KIT_B, created="2"),
                           _kit_job("a-worksheet", "worksheet", KIT_A, created="1")])
        job = run._claim_catalogue_generation(sb)
        assert job and job["id"] == "a-worksheet"

    def test_teachers_are_not_under_the_rule(self, monkeypatch):
        """A kit generating does not stop a teacher's worksheet: the user lanes
        never consult the kit counter."""
        monkeypatch.setattr(run, "process_generation", lambda sb, job, gid: db.finish_job(sb, job["id"], gid))
        sb = _fresh("queued", jobs=[_kit_job("a-video", "presentation", KIT_A, status="processing"),
                                    _builder("job-w", "worksheet")])
        assert run.run_once(sb) is True
        assert {j["id"]: j for j in sb.tables["jobs"]}["job-w"]["status"] == "done"

    def test_the_claim_filter_is_by_kit_id(self):
        sb = _rpc_sb(jobs=[_kit_job("b-w", "worksheet", KIT_B, created="1"),
                           _kit_job("a-w", "worksheet", KIT_A, created="2")])
        assert db.claim_next_job(sb, catalogue=True, kit_ids=[KIT_A])["id"] == "a-w"
        assert db.claim_next_job(sb, catalogue=True, kit_ids=[KIT_A]) is None


# ── the waiting budget ──────────────────────────────────────────────────
class TestTheWaitingBudget:
    def test_a_kit_waits_longer_in_longer_steps_than_a_teacher(self):
        kit = {"id": "k", "params": {"catalogue": True}}
        user = {"id": "u", "params": {}}
        assert db.rate_limit_budget(kit) == (300, 10800)
        assert db.rate_limit_budget(user) == (180, 1800)
        exc = RuntimeError('{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}')
        assert db.rate_limit_deferral(kit, exc).seconds == 300
        assert "of 10800s" in db.rate_limit_deferral(kit, exc).note
        assert db.rate_limit_deferral(user, exc).seconds == 180

    def test_a_spent_budget_is_a_pause_the_console_can_resume_not_a_fault(self):
        since = db._stamp(db.datetime.now(db.timezone.utc) - db.timedelta(seconds=10801))
        kit = {"id": "k", "params": {"catalogue": True, "deferred_since": since}}
        exc = RuntimeError('{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}')
        assert db.rate_limit_deferral(kit, exc) is None
        msg = db.rate_limit_exhausted_message(kit, exc)
        assert msg and msg.startswith("paused after") and "Retry re-runs it" in msg and "10800s" in msg
        assert db.rate_limit_exhausted_message(kit, RuntimeError("ffmpeg died")) is None
        fresh = {"id": "k", "params": {"catalogue": True}}
        assert db.rate_limit_exhausted_message(fresh, exc) is None, "a budget not yet spent is a deferral"

    def test_the_worker_stores_the_pause_as_the_error(self, monkeypatch):
        """A teacher's worksheet that has already waited its whole budget and
        is refused once more goes to error with the PAUSE text, not the raw
        429 body — the support agent and the console read a reason. (Since
        2026-10-14 a teacher's job first gets ONE run on the script fallback
        model — params.script_fallback — and pauses after that run.)"""
        since = db._stamp(db.datetime.now(db.timezone.utc) - db.timedelta(seconds=1801))
        job = {**_builder("job-ws", "worksheet"),
               "params": {"deferred_since": since, "script_fallback": True}}
        sb = _fresh("queued", jobs=[job])

        def boom(sb_, job_, gid):
            raise RuntimeError('{"error": {"code": 429, "message": "Resource exhausted"}}')
        monkeypatch.setattr(run, "process_generation", boom)
        monkeypatch.setattr(run, "_support_agent_enabled", lambda: False)
        monkeypatch.setattr(run, "_claim_catalogue_generation", lambda sb: None)
        assert run.run_once(sb) is True
        row = {j["id"]: j for j in sb.tables["jobs"]}["job-ws"]
        assert row["status"] == "error" and str(row["error"]).startswith("paused after")

    def test_within_the_budget_the_worker_defers_instead(self, monkeypatch):
        job = {**_builder("job-ws", "worksheet"), "params": {}}
        sb = _fresh("queued", jobs=[job])

        def boom(sb_, job_, gid):
            raise RuntimeError('{"error": {"code": 429, "message": "Resource exhausted"}}')
        monkeypatch.setattr(run, "process_generation", boom)
        monkeypatch.setattr(run, "_claim_catalogue_generation", lambda sb: None)
        assert run.run_once(sb) is True
        row = {j["id"]: j for j in sb.tables["jobs"]}["job-ws"]
        assert row["status"] == "queued" and row["params"]["deferred_until"]
        assert "waited 0s of 1800s" in row["params"]["deferred_note"]


# ── a deferral is not an outcome ────────────────────────────────────────
def test_a_deferred_deck_records_no_timings(monkeypatch):
    from worker import process
    monkeypatch.setattr(process, "db", db)
    merged = []
    monkeypatch.setattr(db, "merge_generation_params", lambda sb, gid, patch: merged.append(patch))

    def defer(sb, job, gid):
        raise db.DeferredJob(60, "waiting for the lesson video")
    monkeypatch.setattr(process, "_generate", defer)
    with pytest.raises(db.DeferredJob):
        process.process_generation(object(), {"id": "j", "type": "deck", "created_at": "1"}, GEN)
    assert merged == [], "no timings row for a job that merely went back in the queue"
    assert phase_clock.get(GEN) is None


# ── the image path reports to its gate ──────────────────────────────────
class TestTheImagePathReportsToItsGate:
    def _429(self, headers=None):
        r = requests.Response()
        r.status_code = 429
        r._content = b'{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}'
        for k, v in (headers or {}).items():
            r.headers[k] = v
        return requests.HTTPError("429 Client Error", response=r)

    def test_a_refusal_halves_the_pace_and_silences_the_provider(self, monkeypatch):
        from spike.scene_engine import raster_assets as ra
        g = pg.gate("vertex_image")
        g.rate, g.s["floor"], g.s["ceiling"], g.s["cooldown"] = 8.0, 2.0, 12.0, 30.0
        g.s["max_cooldown"] = 120.0          # the quiet test gate caps silence at 0
        clock = _fake_time(g)
        retry_sleeps = []
        monkeypatch.setattr(time, "sleep", lambda s: retry_sleeps.append(s))
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise self._429({"Retry-After": "40"})
            return "png"
        assert ra._with_backoff(flaky, "Vertex image", tries=2) == "png"
        assert g.rate == 4.0 and g.refusals == 1
        assert any(40.0 <= s <= 42.0 for s in retry_sleeps), "the server's 40 s is the retry wait"
        assert sum(clock["slept"]) == pytest.approx(40.0), "and the provider stayed silent for it"
        assert g.snapshot()["cooldown_left_s"] == 0.0, "the second attempt waited the silence out"

    def test_a_clean_call_counts_towards_the_next_step_up(self):
        from spike.scene_engine import raster_assets as ra
        g = pg.gate("vertex_image")
        g.rate, g.s["floor"], g.s["ceiling"], g.s["step_after"] = 4.0, 2.0, 12.0, 1
        assert ra._with_backoff(lambda: "png", "Vertex image", tries=1) == "png"
        assert g.rate == 5.0

    def test_vision_does_not_touch_the_image_gate(self):
        from spike.scene_engine import raster_assets as ra
        g = pg.gate("vertex_image")
        g.rate, g.s["floor"], g.s["ceiling"], g.s["step_after"] = 4.0, 2.0, 12.0, 1
        assert ra._with_backoff(lambda: "labels", "vision", tries=1, kind="vision") == "labels"
        assert g.rate == 4.0 and g.successes == 0


# ── the text path reports to its gate ───────────────────────────────────
class TestTheTextPathReportsToItsGate:
    def _client(self):
        from shared import gemini_client as gcm
        c = gcm.GeminiClient.__new__(gcm.GeminiClient)
        return gcm, c

    def test_a_429_with_retry_after_is_honoured_recorded_and_retried(self, monkeypatch):
        gcm, c = self._client()
        g = pg.gate("vertex_text")
        g.s["cooldown"], g.s["max_cooldown"] = 20.0, 120.0
        clock = _fake_time(g)
        retry_sleeps = []
        monkeypatch.setattr(time, "sleep", lambda s: retry_sleeps.append(s))
        posts = []

        def post(*a, **k):
            posts.append(1)
            if len(posts) == 1:
                raise gcm._RateLimited("busy", 35.0)
            return {"ok": True}
        monkeypatch.setattr(c, "_post", post)
        assert c._call([], "sys", 100, retries=2) == {"ok": True}
        assert g.refusals == 1 and g.successes == 1
        assert any(35.0 <= s <= 37.0 for s in retry_sleeps), "the server's 35 s is the retry wait"
        assert sum(clock["slept"]) == pytest.approx(35.0), "the silence is the server's 35 s, not the 20 s default"

    def test_the_last_attempt_raises_the_refusal(self, monkeypatch):
        gcm, c = self._client()
        monkeypatch.setattr(time, "sleep", lambda s: None)
        _fake_time(pg.gate("vertex_text"))
        monkeypatch.setattr(c, "_post", lambda *a, **k: (_ for _ in ()).throw(gcm._RateLimited("busy")))
        with pytest.raises(gcm._RateLimited):
            c._call([], "sys", 100, retries=1)
        assert pg.gate("vertex_text").refusals == 2, "two attempts, two refusals recorded"

    def test_the_retry_after_header_rides_on_the_exception(self):
        gcm, _ = self._client()
        r = requests.Response(); r.headers["Retry-After"] = "12"
        assert gcm._retry_after_header(r) == 12.0
        r2 = requests.Response()
        assert gcm._retry_after_header(r2) is None
        assert gcm._RateLimited("x", 3.0).retry_after == 3.0 and gcm._RateLimited("x").retry_after is None
