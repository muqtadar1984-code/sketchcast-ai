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
import signal
import threading
import time

import pytest

import worker.run as run
from tests.test_observer_job_guard import _builder, _fresh, _gen_status, _gen_writes, _support
from worker import client as db


@pytest.fixture(autouse=True)
def _clean_worker_state(monkeypatch):
    """The shutdown flag and the in-flight table are process globals."""
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)
    # Lane 6 imports the catalogue package (visual library and all); an
    # empty queue in these tests must not reach it.
    monkeypatch.setattr(run, "_claim_catalogue_generation", lambda sb: None)
    run._shutdown.clear()
    with run._inflight_lock:
        run._inflight_jobs.clear()
    yield
    run._shutdown.clear()
    with run._inflight_lock:
        run._inflight_jobs.clear()


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
    t = threading.Thread(target=run._worker_loop, args=(0,), daemon=True)
    t.start()
    time.sleep(0.2)  # into the sleep
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
    # No hand-rolled requeue UPDATE left in run.py (the support-job INSERT
    # of a new 'queued' row is a different thing and stays).
    assert 'update({"status": "queued"' not in inspect.getsource(run)
    body = inspect.getsource(db.requeue_job)
    assert 'mirror_generation_status(sb, generation_to_mirror(job), "queued")' in body
    assert body.index("if not getattr(upd") < body.index("mirror_generation_status"), (
        "the generation is relabelled only when the job row actually moved"
    )


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
    assert src.index("install_signal_handlers()") < src.index("requeue_stale_jobs(sb)"), (
        "handlers go in before the boot reap: a SIGTERM during boot must take the graceful path too"
    )
    assert src.index('"--once"') < src.index("install_signal_handlers()"), (
        "--once runs one job and returns; it has nothing to release"
    )


def test_the_main_thread_wakes_from_its_reaper_wait_and_releases():
    """_serve sits in a 60 s reaper wait; a signal must cut it short, release
    the held jobs, and return so main can exit — inside the drain window."""
    sb = _fresh("processing", jobs=[_builder("job-p", "presentation", "processing")])
    run._inflight_add(dict(sb.tables["jobs"][0]))
    t = threading.Thread(target=run._serve, args=(sb, 15), kwargs={"reap_every": 60}, daemon=True)
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
