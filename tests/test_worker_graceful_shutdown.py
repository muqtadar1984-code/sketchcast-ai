"""A worker told to stop hands its jobs back — it does not take them with it.

THE INCIDENT (Railway deploy 1bfdbde2, 2026-09-13 13:56 UTC). Railway's
teardown is: the NEW container goes active, THEN the old one is sent SIGTERM
and given a drain window. The new container's boot-time reaper requeued the
one job the old container still held; the old container — alive, polling,
told nothing yet — re-claimed it at 13:56:24 (jobs row 'processing',
progress 1) and was removed at 13:56:39. The job sat 'processing' with no
worker until the 15-minute stale reaper would have caught it; the founder
requeued it by hand.

Two halves, both behavioural against the fake postgrest in
tests/test_observer_job_guard.py (every write is recorded):

* on SIGTERM/SIGINT the worker stops CLAIMING, and every job it holds goes
  back to 'queued' with its attempt KEPT and its generation mirrored
  'queued' — through db.requeue_job, the same writer the boot and windowed
  reapers use, so the job/generation pair can never disagree;
* db.requeue_job is fenced on `attempts`, so the old container's release
  cannot pull a job out from under a new container that has already reaped
  and re-claimed it (the same race, mirrored).
"""

from __future__ import annotations

import inspect
import os
import signal
import sys
import threading
import time
import types

import pytest

import worker.run as run
from tests.test_observer_job_guard import BOOK, _builder, _fresh, _gen_status, _gen_writes, _support
from worker import client as db


def _clear():
    run._shutdown.clear()
    with run._inflight_lock:
        run._inflight_jobs.clear()
        run._inflight_sketches.clear()


@pytest.fixture(autouse=True)
def _clean_worker_state(monkeypatch):
    """The shutdown flag and the in-flight tables are process globals, and a
    real request_shutdown(signum) re-points that signal at the hard exit."""
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)
    # Lane 6 imports the catalogue package (visual library and all); an
    # empty queue in these tests must not reach it.
    monkeypatch.setattr(run, "_claim_catalogue_generation", lambda sb: None)
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    _clear()
    yield
    _clear()
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def _sketch_renderer(monkeypatch, render):
    """run_once imports worker.tutor_sketch lazily; stand in for the module
    so no renderer dependency is loaded."""
    monkeypatch.setitem(sys.modules, "worker.tutor_sketch", types.SimpleNamespace(render_sketch=render))


def _with_sketch(sb, sketch_id="sk-1", status="queued"):
    sb.tables["tutor_sketch"] = [{"id": sketch_id, "status": status, "book_id": BOOK, "created_at": "1"}]
    return sb


def _user_claim(sb):
    """run.py's generic user lane, as run_once issues it."""
    return db.claim_next_job(sb, exclude_types=db.OBSERVER_JOB_TYPES, catalogue=False)


# ── a held job is handed back ───────────────────────────────────────────


def test_a_job_held_mid_render_is_requeued_on_shutdown_with_its_attempt_kept(monkeypatch):
    """Driven through run_once itself: the claim, the in-flight entry, and
    the release all happen the way the daemon does them."""
    sb = _fresh("queued", jobs=[_builder("job-p", "presentation", attempts=1)])
    started, release = threading.Event(), threading.Event()

    def rendering(sb_, job, gen_id):  # process_generation, minutes from done
        started.set()
        release.wait(5)

    monkeypatch.setattr(run, "process_generation", rendering)
    t = threading.Thread(target=run.run_once, args=(sb,), daemon=True)
    t.start()
    assert started.wait(5), "the builder never started"
    assert sb.tables["jobs"][0]["status"] == "processing" and _gen_status(sb) == "processing"
    assert run._inflight_snapshot() == {"job-p"}

    run.request_shutdown(signal.SIGTERM, None)  # Railway's teardown signal
    assert run.release_held_jobs(sb) == 1

    row = sb.tables["jobs"][0]
    assert row["status"] == "queued" and row["progress"] == 0
    assert row["attempts"] == 1, "a deploy is not the job's fault — no attempt is spent"
    assert _gen_status(sb) == "queued", "the Library must not say 'being built' with nobody building it"
    release.set()
    t.join(5)


def test_a_held_observer_job_is_requeued_without_touching_the_lesson_it_reports_on():
    """The observer-job rule (tests/test_observer_job_guard.py) holds on this
    edge too: a diagnosis in flight at deploy time goes back to the queue,
    and the healthy, assigned lesson it reports on stays 'done'."""
    sb = _fresh("done", jobs=[_support(status="processing")])
    run._inflight_add(dict(sb.tables["jobs"][0]))
    run.request_shutdown()
    assert run.release_held_jobs(sb) == 1
    assert sb.tables["jobs"][0]["status"] == "queued"
    assert _gen_status(sb) == "done" and _gen_writes(sb) == []


def test_one_row_that_cannot_be_written_does_not_strand_the_rest(monkeypatch):
    sb = _fresh("processing", jobs=[_builder("job-a", "presentation", "processing"),
                                    _builder("job-b", "worksheet", "processing")])
    for row in sb.tables["jobs"]:
        run._inflight_add(dict(row))
    real = db.requeue_job

    def flaky(sb_, job, **kw):
        if job["id"] == "job-a":
            raise ConnectionError("postgrest went away")
        return real(sb_, job, **kw)

    monkeypatch.setattr(db, "requeue_job", flaky)
    run.request_shutdown()
    assert run.release_held_jobs(sb) == 1
    assert {r["id"]: r["status"] for r in sb.tables["jobs"]} == {"job-a": "processing", "job-b": "queued"}


# ── no new claims after the signal ──────────────────────────────────────


def test_run_once_claims_nothing_after_the_signal():
    sb = _fresh("queued", jobs=[_builder()])
    run.request_shutdown()
    assert run.run_once(sb) is False
    assert sb.tables["jobs"][0]["status"] == "queued"
    assert sb.log == [], "not one write: anything claimed now would only be handed straight back"


def test_the_worker_loop_ends_once_shutdown_is_requested(monkeypatch):
    sb = _fresh("queued", jobs=[_builder()])
    monkeypatch.setattr(db, "admin", lambda: sb)
    run.request_shutdown()
    t = threading.Thread(target=run._worker_loop, args=(0,), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive() and sb.log == []


def test_an_idle_worker_thread_wakes_from_its_poll_sleep(monkeypatch):
    """The poll sleep used to be time.sleep(POLL_SECONDS); a thread caught in
    it would sit out the whole drain window. It now waits on the flag."""
    sb = _fresh("queued", jobs=[])
    monkeypatch.setattr(db, "admin", lambda: sb)
    monkeypatch.setattr(run, "POLL_SECONDS", 30)
    polled, real = threading.Event(), run.run_once

    def once(sb_):  # the loop's first empty poll — the next line is the sleep
        r = real(sb_)
        polled.set()
        return r

    monkeypatch.setattr(run, "run_once", once)
    t = threading.Thread(target=run._worker_loop, args=(0,), daemon=True)
    t.start()
    assert polled.wait(3), "the loop never polled"
    run.request_shutdown()
    t.join(3)
    assert not t.is_alive(), "still asleep — the shutdown did not cut the poll wait short"


# ── the race, both ways ─────────────────────────────────────────────────


def test_the_incident_sequence_now_ends_queued():
    """Old container holds J. New container boots and reaps J (attempt
    spent). Old container, not yet signalled, re-claims J — exactly what
    happened at 13:56:24. THEN the SIGTERM lands: J must go back to the
    queue for the new container, not die 'processing' with the old one."""
    sb = _fresh("processing", jobs=[_builder("job-p", "presentation", "processing")])
    assert db.requeue_stale_jobs(sb) == 1                 # the new container's boot reap
    reclaimed = _user_claim(sb)                           # the old container, still polling
    assert reclaimed and reclaimed["attempts"] == 1
    run._inflight_add(reclaimed)

    run.request_shutdown(signal.SIGTERM, None)            # 13:56:39
    assert run.release_held_jobs(sb) == 1
    row = sb.tables["jobs"][0]
    assert row["status"] == "queued" and row["attempts"] == 1
    assert _gen_status(sb) == "queued"


def test_a_job_the_new_container_already_reclaimed_is_not_pulled_back():
    """The same race mirrored: the new container reaps AND re-claims J
    before the old one's release runs. The old container's copy of the row
    carries attempts=0; the row now says 1. The fence must refuse."""
    sb = _fresh("processing", jobs=[_builder("job-p", "presentation", "processing", attempts=0)])
    run._inflight_add(dict(sb.tables["jobs"][0]))         # what the OLD container holds
    assert db.requeue_stale_jobs(sb) == 1                 # new container: boot reap …
    assert _user_claim(sb)["id"] == "job-p"               # … and its worker thread claims it
    assert sb.tables["jobs"][0]["attempts"] == 1
    mark = len(sb.log)

    run.request_shutdown()
    assert run.release_held_jobs(sb) == 0
    assert sb.tables["jobs"][0]["status"] == "processing", "the new container's build is not interrupted"
    assert _gen_status(sb) == "processing"
    assert _gen_writes(sb, mark) == [], "a row that did not move must not relabel its generation"
    # The one write issued was the fenced UPDATE — and the fence is what
    # made it match nothing.
    (kind, table, payload, filters), = sb.log[mark:]
    assert (kind, table) == ("update", "jobs") and ("eq", "attempts", 0) in filters


def test_a_row_the_console_moved_is_left_alone():
    """The reaper's status guard, kept: a job the console cancelled while
    the worker held it is not resurrected as 'queued'."""
    sb = _fresh("processing", jobs=[_builder("job-p", "presentation", "processing")])
    run._inflight_add(dict(sb.tables["jobs"][0]))
    sb.tables["jobs"][0]["status"] = "error"
    run.request_shutdown()
    assert run.release_held_jobs(sb) == 0
    assert sb.tables["jobs"][0]["status"] == "error" and _gen_status(sb) == "processing"


# ── one writer for every requeue ────────────────────────────────────────


def test_the_reaper_spends_the_attempt_and_the_shutdown_does_not():
    reaped = _fresh("processing", jobs=[_builder("job-p", "presentation", "processing", attempts=1)])
    assert db.requeue_stale_jobs(reaped) == 1
    assert reaped.tables["jobs"][0]["attempts"] == 2 and _gen_status(reaped) == "queued"

    released = _fresh("processing", jobs=[_builder("job-p", "presentation", "processing", attempts=1)])
    run._inflight_add(dict(released.tables["jobs"][0]))
    run.request_shutdown()
    assert run.release_held_jobs(released) == 1
    assert released.tables["jobs"][0]["attempts"] == 1 and _gen_status(released) == "queued"

    # Same shape either way: status, progress, and the guards.
    reaper_write = [e for e in reaped.log if e[1] == "jobs" and e[0] == "update"][-1]
    release_write = [e for e in released.log if e[1] == "jobs" and e[0] == "update"][-1]
    assert reaper_write[2] == {"status": "queued", "progress": 0, "attempts": 2}
    assert release_write[2] == {"status": "queued", "progress": 0}
    assert reaper_write[3] == release_write[3] == [("eq", "id", "job-p"), ("eq", "status", "processing"),
                                                   ("eq", "attempts", 1)]


def test_every_requeue_goes_through_the_one_helper():
    """Three writers, one shape. A fourth hand-rolled `{"status": "queued"}`
    UPDATE in either module would be the next mirror drift."""
    assert "requeue_job(sb, j, bump_attempts=True)" in inspect.getsource(db.requeue_stale_jobs)
    assert "db.requeue_job(sb, job, bump_attempts=False)" in inspect.getsource(run.release_held_jobs)
    assert "db.requeue_job(sb, job, bump_attempts=True)" in inspect.getsource(run.run_once)
    assert "db.requeue_job(sb, job, bump_attempts=False)" in inspect.getsource(run.run_once), (
        "a job claimed as the signal lands is handed back through the same writer"
    )
    # No hand-rolled requeue UPDATE left in run.py (the support-job INSERT
    # of a new 'queued' row is a different thing and stays).
    assert 'update({"status": "queued"' not in inspect.getsource(run)
    # The deck's defer is the fourth caller, not a fourth writer.
    assert "requeue_job(sb, job, bump_attempts=False, extra=" in inspect.getsource(db.defer_job)
    assert 'update({"status": "queued"' not in inspect.getsource(db.defer_job)
    # (The helper's own body — mirror after the row moved, both guards — is
    # pinned where the mirror rule lives: tests/test_generation_status_mirror.py.)


def test_a_tier_outage_requeue_still_spends_the_attempt_and_mirrors_queued(monkeypatch):
    """run_once's third caller, now on the helper: unchanged behaviour."""
    sb = _fresh("queued", jobs=[_builder("job-p", "presentation")])

    def tier_down(sb_, job, gen_id):
        raise db.TransientTierError("plan_tier timed out")

    monkeypatch.setattr(run, "process_generation", tier_down)
    assert run.run_once(sb) is True
    row = sb.tables["jobs"][0]
    assert row["status"] == "queued" and row["attempts"] == 1 and _gen_status(sb) == "queued"


# ── the wiring ──────────────────────────────────────────────────────────


def test_sigterm_and_sigint_are_wired_to_the_graceful_path(monkeypatch):
    seen = {}
    monkeypatch.setattr(run.signal, "signal", lambda sig, handler: seen.__setitem__(sig, handler))
    run.install_signal_handlers()
    assert seen == {signal.SIGTERM: run.request_shutdown, signal.SIGINT: run.request_shutdown}
    assert not run._shutdown.is_set()
    run.request_shutdown(signal.SIGTERM, None)
    assert run._shutdown.is_set()

    src = inspect.getsource(run.main)
    assert "install_signal_handlers()" in src
    assert src.index("install_signal_handlers()") < src.index("requeue_stale_jobs(sb"), (
        "handlers go in before the boot reap: a SIGTERM during boot must take the graceful path too"
    )
    assert src.index('"--once"') < src.index("install_signal_handlers()"), (
        "--once runs one job and returns; it has nothing to release"
    )


def test_the_main_thread_wakes_from_its_reaper_wait_and_releases():
    """_serve sits in a 60 s reaper wait; a signal must cut it short, release
    the held jobs, and return so main can exit — inside the drain window.
    grace=0: the held row never finishes (nothing is rendering it here)."""
    sb = _fresh("processing", jobs=[_builder("job-p", "presentation", "processing")])
    run._inflight_add(dict(sb.tables["jobs"][0]))
    t = threading.Thread(target=run._serve, args=(sb, 15), kwargs={"reap_every": 60, "grace": 0}, daemon=True)
    t.start()
    time.sleep(0.2)  # into the wait
    run.request_shutdown(signal.SIGTERM, None)
    t.join(3)
    assert not t.is_alive(), "_serve did not return — the process would be SIGKILLed with the job held"
    assert sb.tables["jobs"][0]["status"] == "queued" and _gen_status(sb) == "queued"


def test_main_exits_hard_after_the_release():
    """The worker threads are daemons mid-render. A plain return would let a
    late finish_job mark a requeued job 'done' — and let the render pool's
    exit hook wait on them past the drain window."""
    src = inspect.getsource(run.main)
    assert src.index("_serve(sb, stale_min)") < src.index("os._exit(0)")


def test_a_second_signal_exits_at_once_instead_of_deadlocking(monkeypatch):
    """Event.set() takes a plain Lock; a second handled signal re-entering it
    mid-set would hang the main thread before anything was released. The
    first signal re-points that signal at a lock-free hard exit."""
    installed, exits = {}, []
    monkeypatch.setattr(run.signal, "signal", lambda sig, h: installed.__setitem__(sig, h))
    monkeypatch.setattr(run.os, "_exit", lambda code: exits.append(code))
    run.request_shutdown(signal.SIGTERM, None)
    assert run._shutdown.is_set() and installed[signal.SIGTERM] is run._exit_now
    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert exits == [1]


# ── the gap between the check and the claim ─────────────────────────────


def test_a_job_claimed_as_the_signal_lands_is_handed_straight_back(monkeypatch):
    """run_once checks the flag, then claims — a round trip. A signal in that
    gap used to leave the row 'processing' with no one holding it: not yet
    in-flight, so the release never saw it, and the process was gone."""
    sb = _fresh("queued", jobs=[_builder("job-p", "presentation", attempts=2)])
    real = db.claim_next_job

    def claim_then_signal(sb_, *a, **k):
        row = real(sb_, *a, **k)
        if row:
            run.request_shutdown()  # lands while the claim's UPDATE is in flight
        return row

    monkeypatch.setattr(db, "claim_next_job", claim_then_signal)
    monkeypatch.setattr(run, "process_generation", lambda *a, **k: pytest.fail("must not start work"))
    assert run.run_once(sb) is False
    row = sb.tables["jobs"][0]
    assert row["status"] == "queued" and row["attempts"] == 2, "handed back, attempt kept"
    assert _gen_status(sb) == "queued"
    assert run._inflight_snapshot() == set()


def test_a_sketch_claimed_as_the_signal_lands_is_handed_straight_back(monkeypatch):
    sb = _with_sketch(_fresh("done", jobs=[]))
    real = db.claim_next_sketch

    def claim_then_signal(sb_):
        row = real(sb_)
        if row:
            run.request_shutdown()
        return row

    monkeypatch.setattr(db, "claim_next_sketch", claim_then_signal)
    _sketch_renderer(monkeypatch, lambda *a: pytest.fail("must not render"))
    assert run.run_once(sb) is False
    assert sb.tables["tutor_sketch"][0]["status"] == "queued"


# ── the sketch lane is held and released like a job ─────────────────────


def test_a_sketch_held_mid_render_is_requeued_on_shutdown(monkeypatch):
    """Sketches are claimed FIRST every poll, so a deploy mid-render is the
    likely case; and the new container's boot reap of sketches has already
    run by the time the old one is signalled, so nothing else would recover
    it for STALE_JOB_MINUTES — a student watching a frozen coach doodle."""
    sb = _with_sketch(_fresh("done", jobs=[]))
    started, release = threading.Event(), threading.Event()

    def rendering(sb_, sketch):
        started.set()
        release.wait(5)

    _sketch_renderer(monkeypatch, rendering)
    t = threading.Thread(target=run.run_once, args=(sb,), daemon=True)
    t.start()
    assert started.wait(5)
    assert sb.tables["tutor_sketch"][0]["status"] == "processing"
    run.request_shutdown(signal.SIGTERM, None)
    assert run.release_held_jobs(sb) == 1
    assert sb.tables["tutor_sketch"][0]["status"] == "queued"
    release.set()
    t.join(5)
    assert run._held_sketches() == []


# ── the drain window is used, not just bought ───────────────────────────


def test_the_grace_lets_a_job_that_is_about_to_finish_finish(monkeypatch):
    """A worksheet two seconds from done must not be thrown away and re-bought
    by the next container: _serve waits (bounded) for in-flight work, and a
    job that finishes leaves the held set, so it is never handed back."""
    sb = _fresh("queued", jobs=[_builder("job-w", "worksheet")])
    started = threading.Event()

    def nearly_done(sb_, job, gen_id):
        started.set()
        time.sleep(0.3)
        db.finish_job(sb_, job["id"], gen_id)

    monkeypatch.setattr(run, "process_generation", nearly_done)
    t = threading.Thread(target=run.run_once, args=(sb,), daemon=True)
    t.start()
    assert started.wait(3)
    run.request_shutdown()
    run._serve(sb, 15, reap_every=60, grace=5)
    t.join(5)
    assert sb.tables["jobs"][0]["status"] == "done" and _gen_status(sb) == "done"
    assert not [e for e in sb.log if e[1] == "jobs" and e[2].get("status") == "queued"], "never handed back"


def test_the_grace_is_bounded_and_the_rest_is_released(monkeypatch):
    sb = _fresh("queued", jobs=[_builder("job-p", "presentation")])
    started, release = threading.Event(), threading.Event()

    def long_render(sb_, job, gen_id):
        started.set()
        release.wait(5)

    monkeypatch.setattr(run, "process_generation", long_render)
    t = threading.Thread(target=run.run_once, args=(sb,), daemon=True)
    t.start()
    assert started.wait(3)
    run.request_shutdown()
    t0 = time.monotonic()
    run._serve(sb, 15, reap_every=60, grace=0.4)
    assert 0.3 < time.monotonic() - t0 < 3, "waited the grace, then gave up"
    assert sb.tables["jobs"][0]["status"] == "queued" and _gen_status(sb) == "queued"
    release.set()
    t.join(5)


# ── the writers that race the release ───────────────────────────────────


def test_progress_never_lands_on_a_row_that_was_handed_back():
    """set_progress is guarded on 'processing': a late progress write from a
    departing thread must not put "62% built" on a queued row — non-zero
    progress on a queued row is what refuses the teacher's refund."""
    queued = _fresh("queued", jobs=[_builder("job-p", "presentation")])
    db.set_progress(queued, "job-p", 62)
    assert queued.tables["jobs"][0].get("progress", 0) == 0

    live = _fresh("processing", jobs=[_builder("job-p", "presentation", "processing")])
    db.set_progress(live, "job-p", 62)
    assert live.tables["jobs"][0]["progress"] == 62


def test_a_deferred_deck_goes_through_the_helper_fenced_and_mirrored():
    """defer_job was the fourth 'back to queued' writer, with a status guard
    only and its generation mirrored separately. Now it is the helper's
    fourth caller: same fence, same mirror, plus its wake-up stamp."""
    sb = _fresh("processing", jobs=[{**_builder("job-d", "deck", "processing"), "params": {"part": 1}}])
    assert db.defer_job(sb, sb.tables["jobs"][0], 60, "waiting for the video")
    row = sb.tables["jobs"][0]
    assert row["status"] == "queued" and row["attempts"] == 0 and row["progress"] == 0
    assert row["stage"]["phase"] == "waiting" and row["params"]["deferred_until"] and row["params"]["part"] == 1
    assert _gen_status(sb) == "queued", "the dashboard must not show a build that is not happening"

    # The mirrored race, on this edge: the peer reaped and re-claimed the row
    # while this container was waiting for the video.
    sb = _fresh("processing", jobs=[_builder("job-d", "deck", "processing")])
    old = dict(sb.tables["jobs"][0])
    assert db.requeue_stale_jobs(sb) == 1 and _user_claim(sb)["id"] == "job-d"
    assert not db.defer_job(sb, old, 60, "x")
    assert sb.tables["jobs"][0]["status"] == "processing" and "stage" not in sb.tables["jobs"][0]
    assert _gen_status(sb) == "processing"


# ── the drain is as long as a render: heartbeat + windowed boot reap ─────


@pytest.fixture(autouse=True)
def _heartbeat_state():
    run._heartbeat_stop.clear()
    yield
    run._heartbeat_stop.set()
    run._heartbeat_stop.clear()


def test_the_heartbeat_touches_every_held_job_and_nothing_else():
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    sb = _fresh("processing", jobs=[{**_builder("job-a", "presentation", "processing"), "updated_at": old},
                                    {**_builder("job-b", "worksheet", "processing"), "updated_at": old}])
    run._inflight_add(dict(sb.tables["jobs"][0]))          # only job-a is ours
    assert run.heartbeat_once(sb) == 1
    a, b = sb.tables["jobs"]
    assert a["updated_at"] != old and b["updated_at"] == old
    # a windowed reaper now sees job-a as alive and job-b as the orphan it is
    assert db.requeue_stale_jobs(sb, older_than_minutes=5) == 1
    assert a["status"] == "processing" and b["status"] == "queued"


def test_a_held_row_the_reaper_already_moved_is_not_revived_by_the_heartbeat():
    sb = _fresh("queued", jobs=[_builder("job-a", "presentation", "queued")])
    run._inflight_add({**sb.tables["jobs"][0], "status": "processing"})
    assert run.heartbeat_once(sb) == 0
    assert sb.tables["jobs"][0]["status"] == "queued"


def test_the_heartbeat_keeps_beating_through_the_drain(monkeypatch):
    """The departing container is exactly the one whose rows must keep
    reading alive: its beat runs on its own event, not the shutdown flag."""
    sb = _fresh("processing", jobs=[_builder("job-a", "presentation", "processing")])
    run._inflight_add(dict(sb.tables["jobs"][0]))
    t = threading.Thread(target=run._heartbeat_loop, args=(sb,), kwargs={"every": 0.03}, daemon=True)
    t.start()
    run.request_shutdown()
    time.sleep(0.2)
    beats = [e for e in sb.log if e[1] == "jobs" and "updated_at" in e[2]]
    assert len(beats) >= 3, "the beat stopped when the shutdown was requested"
    run._heartbeat_stop.set()
    t.join(2)
    assert not t.is_alive()


def test_the_boot_reap_is_windowed_and_the_incident_no_longer_reproduces():
    """2026-09-13: the new container's reap-all took the job the old one was
    still rendering. Now the old one heartbeats it and the boot reap is
    windowed, so the job stays with its holder through the drain."""
    src = inspect.getsource(run.main)
    assert "requeue_stale_jobs(sb, older_than_minutes=stale_min)" in src
    assert "requeue_stale_sketches(sb, older_than_minutes=stale_min)" in src
    assert src.index("_heartbeat_loop") < src.index("_serve(sb, stale_min)")
    sb = _fresh("processing", jobs=[_builder("job-a", "presentation", "processing")])
    run._inflight_add(dict(sb.tables["jobs"][0]))
    run.heartbeat_once(sb)                                  # the old container, mid-render
    assert db.requeue_stale_jobs(sb, older_than_minutes=5) == 0   # the new container's boot reap
    assert sb.tables["jobs"][0]["status"] == "processing"


def test_the_drain_outlasts_the_grace_and_the_grace_outlasts_a_render():
    import json
    from pathlib import Path
    drain = json.loads((Path(run.__file__).resolve().parent.parent / "railway.json").read_text())["deploy"]["drainingSeconds"]
    assert float(drain) >= run.SHUTDOWN_GRACE_SECONDS + 60, "the hand-back needs room inside the drain"
    assert run.SHUTDOWN_GRACE_SECONDS >= 1800, "shorter than the slowest render measured (42 min deck, 2026-09)"
    assert run.HEARTBEAT_SECONDS * 4 <= int(os.environ.get("STALE_JOB_MINUTES", "5")) * 60, \
        "the reaper's window must allow several missed beats"
