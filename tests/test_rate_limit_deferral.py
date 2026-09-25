"""A model quota refusal is a wait, not a failure.

2026-09-25 19:12 and 19:15: two consecutive presentation builds of the
States of Matter kit died on the FIRST model call — Vertex 429
RESOURCE_EXHAUSTED on the analysis — after the client's own 2+4+8 s retry
budget, with the box otherwise idle. Each went to `error`, the kit to
`failed`, and a reviewer had to click Retry into the same refusal. A job hit
by a rate limit now goes back to the queue with a wake-up time and no attempt
spent, until it has waited its whole budget.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import worker.run as run
from shared.gemini_client import _RateLimited
from tests.test_observer_job_guard import _builder, _fresh
from worker import client as db

VERTEX_429 = ('{\n  "error": {\n    "code": 429,\n    "message": "Resource exhausted. Please try '
              'again later.",\n    "status": "RESOURCE_EXHAUSTED"\n  }\n}\n')


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(run, "_claim_catalogue_generation", lambda sb: None)
    run._shutdown.clear()
    with run._inflight_lock:
        run._inflight_jobs.clear()
    yield
    with run._inflight_lock:
        run._inflight_jobs.clear()


class TestWhatCountsAsARateLimit:
    def test_vertex_and_anthropic_by_class(self):
        assert db.is_rate_limited(_RateLimited(VERTEX_429))

        class RateLimitError(Exception):
            pass

        assert db.is_rate_limited(RateLimitError("429"))

    def test_the_text_of_a_wrapped_refusal(self):
        try:
            try:
                raise _RateLimited(VERTEX_429)
            except _RateLimited as inner:
                raise RuntimeError("analysis failed") from inner
        except RuntimeError as wrapped:
            assert db.is_rate_limited(wrapped)
        assert db.is_rate_limited(RuntimeError("Vertex said RESOURCE_EXHAUSTED"))

    def test_an_ordinary_error_is_not_one(self):
        assert not db.is_rate_limited(RuntimeError("'tuple' object has no attribute 'is_Symbol'"))
        assert not db.is_rate_limited(ValueError("malformed JSON at char 5659"))


class TestTheWaitItEarns:
    def test_a_fresh_job_waits_the_configured_time(self, monkeypatch):
        monkeypatch.setattr(db, "RATE_LIMIT_DEFER_SECONDS", 180)
        wait = db.rate_limit_deferral(_builder(), _RateLimited(VERTEX_429))
        assert isinstance(wait, db.DeferredJob) and wait.seconds == 180
        assert "quota" in wait.note

    def test_a_job_that_has_waited_its_budget_is_a_failure(self, monkeypatch):
        monkeypatch.setattr(db, "RATE_LIMIT_MAX_WAIT_SECONDS", 1800)
        monkeypatch.setattr(db, "deferred_seconds", lambda job: 1800.0)
        assert db.rate_limit_deferral(_builder(), _RateLimited(VERTEX_429)) is None

    def test_not_a_rate_limit_means_no_wait(self):
        assert db.rate_limit_deferral(_builder(), RuntimeError("boom")) is None


class TestTheRunLoop:
    def test_a_book_job_goes_back_to_the_queue_with_no_attempt_and_no_error(self, monkeypatch):
        def boom(sb, job, gen_id):
            raise _RateLimited(VERTEX_429)

        monkeypatch.setattr(run, "process_generation", boom)
        monkeypatch.setattr(db, "RATE_LIMIT_DEFER_SECONDS", 180)
        sb = _fresh("queued", jobs=[_builder("job-1", "presentation")])
        assert run.run_once(sb) is True
        row = sb.tables["jobs"][0]
        assert row["status"] == "queued" and row["attempts"] == 0
        assert not row.get("error")
        assert row["params"]["deferred_until"] > row["params"]["deferred_since"]
        assert row["stage"]["phase"] == "waiting"
        assert sb.tables["generations"][0]["status"] == "queued"
        with run._inflight_lock:
            assert not run._inflight_jobs, "the slot is released"

    def test_after_the_budget_it_fails_as_before(self, monkeypatch):
        def boom(sb, job, gen_id):
            raise _RateLimited(VERTEX_429)

        monkeypatch.setattr(run, "process_generation", boom)
        monkeypatch.setattr(db, "deferred_seconds", lambda job: 99999.0)
        monkeypatch.setattr(run, "_support_agent_enabled", lambda: False)
        sb = _fresh("queued", jobs=[_builder("job-1", "presentation")])
        assert run.run_once(sb) is True
        row = sb.tables["jobs"][0]
        assert row["status"] == "error" and "RESOURCE_EXHAUSTED" in str(row.get("error"))

    def test_an_ordinary_failure_is_untouched(self, monkeypatch):
        def boom(sb, job, gen_id):
            raise RuntimeError("malformed JSON")

        monkeypatch.setattr(run, "process_generation", boom)
        monkeypatch.setattr(run, "_support_agent_enabled", lambda: False)
        sb = _fresh("queued", jobs=[_builder("job-1", "worksheet")])
        assert run.run_once(sb) is True
        assert sb.tables["jobs"][0]["status"] == "error"


class TestTheCatalogueBranch:
    """_process_catalogue marks its kit `failed` on any exception before
    re-raising. The translation has to happen BEFORE that write, or the
    job waits in the queue under a kit the portal shows as failed."""

    def test_the_wait_is_raised_before_the_kit_is_marked_failed(self):
        src = Path(run.__file__).with_name("process.py").read_text(encoding="utf-8")
        body = src[src.index("def _process_catalogue"):src.index("def process_generation")]
        assert body.index("except db.DeferredJob:") < body.index("except Exception as exc:")
        assert body.index("db.rate_limit_deferral(job, exc)") < body.index('{"status": "failed", "kind": gen.get("kind")')
        assert "raise wait from exc" in body
