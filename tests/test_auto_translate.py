"""The Auto Translate Agent (catalogue/auto_translate.py) — plan Revision 3.

The decision is pure and tested directly; the tick is tested through a small
in-memory client that answers exactly the reads and RPCs the agent makes.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from catalogue import auto_translate as at

NOW = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)
ON = {"enabled": True, "paused": False, "dry_run": False, "retranslate": ["blocked", "stale"],
      "languages": ["ar", "ms", "ms-arab"], "summary_email": "f@example.com", "summary_hour_utc": 3.5,
      "cost_warn_usd": 2.0}


def cand(rank, lang="ar", reason="backlog", hold=None, detail=None, src=None):
    return {"rank": rank, "language": lang, "reason": reason, "hold": hold, "hold_detail": detail,
            "source_kit_id": src or f"src-{rank}", "topic_id": f"t-{rank}", "topic_title": f"Topic {rank}"}


# ── decide ──────────────────────────────────────────────────────────────────


class TestDecide:
    def go(self, settings=ON, state=None, lane=None, bedrock=None, cands=None, uses=lambda lang: True):
        return at.decide(settings, state or {}, now=NOW, lane_busy=lane, bedrock_busy=bedrock,
                         candidates=[cand(1)] if cands is None else cands, uses_bedrock=uses)

    def test_off_without_settings_or_switch(self):
        assert self.go(settings=None).action == "off"
        assert self.go(settings={**ON, "enabled": False}).action == "off"

    def test_queues_the_first_candidate_when_everything_is_free(self):
        d = self.go(cands=[cand(2, "ms"), cand(1, "ar")])
        assert d.action == "queue" and d.candidate["language"] == "ar" and not d.retry

    def test_pause_is_the_development_hold(self):
        d = self.go(settings={**ON, "paused": True})
        assert d.action == "wait" and d.why == "paused by you"

    def test_the_breaker_holds(self):
        d = self.go(state={"breaker": True, "breaker_reason": "2 failures in a row"})
        assert d.action == "wait" and "breaker" in d.why

    def test_a_throttle_backoff_holds_until_it_ends(self):
        assert self.go(state={"paused_until": (NOW + timedelta(minutes=5)).isoformat()}).action == "wait"
        assert self.go(state={"paused_until": (NOW - timedelta(minutes=1)).isoformat()}).action == "queue"

    def test_one_translation_at_a_time(self):
        d = self.go(lane="translation job x is staging")
        assert d.action == "wait" and d.why == "translation job x is staging"

    def test_bedrock_client_work_holds_a_bedrock_translation(self):
        d = self.go(bedrock="a client presentation job in ar is using Bedrock")
        assert d.action == "wait" and "Bedrock" in d.why

    def test_bedrock_client_work_does_not_hold_a_translation_that_is_not_on_bedrock(self):
        d = self.go(bedrock="a client job is using Bedrock", uses=lambda lang: False)
        assert d.action == "queue"

    def test_nothing_left(self):
        d = self.go(cands=[cand(1, hold="translated")])
        assert d.action == "wait" and d.why == "nothing left to translate"


class TestPick:
    def test_held_pairs_are_skipped(self):
        c, retry = at.pick([cand(1, hold="rejected_or_failed"), cand(2, hold="queued"), cand(3)], ON)
        assert c["rank"] == 3 and not retry

    def test_a_rejected_translation_is_never_retried(self):
        assert at.pick([cand(1, hold="rejected_or_failed", detail="rejected/failed")], ON) is None

    def test_one_throttle_failure_gets_one_retry(self):
        c, retry = at.pick([cand(1, hold="job_failed", detail="1 failed job(s) in 7 days: ThrottlingException: Rate exceeded")], ON)
        assert retry and c["rank"] == 1

    def test_a_second_failure_is_held(self):
        assert at.pick([cand(1, hold="job_failed", detail="2 failed job(s) in 7 days: ThrottlingException")], ON) is None

    def test_a_non_throttle_failure_is_held(self):
        assert at.pick([cand(1, hold="job_failed", detail="1 failed job(s) in 7 days: numbers differ")], ON) is None

    def test_retranslation_follows_the_setting(self):
        rows = [cand(1, reason="stale"), cand(2, reason="backlog")]
        assert at.pick(rows, ON)[0]["rank"] == 1
        assert at.pick(rows, {**ON, "retranslate": ["blocked"]})[0]["rank"] == 2

    def test_jawi_held_for_malay_is_not_queued(self):
        assert at.pick([cand(1, "ms-arab", hold="malay_not_approved")], ON) is None


# ── judge ───────────────────────────────────────────────────────────────────


class TestJudge:
    def test_nothing_to_judge_while_running(self):
        st = {"last_job_id": "j1"}
        assert at.judge(st, {"id": "j1", "status": "processing"}, None, now=NOW) is None
        assert at.judge(st, {"id": "j1", "status": "done"}, {"status": "generating"}, now=NOW) is None
        assert "judged_job_id" not in st

    def test_success_resets(self):
        st = {"last_job_id": "j1", "consecutive_failures": 1, "backoff_min": 60}
        assert at.judge(st, {"id": "j1", "status": "done"}, {"status": "in_review"}, now=NOW) == "success"
        assert st["consecutive_failures"] == 0 and st["backoff_min"] == 0 and st["judged_job_id"] == "j1"

    def test_judged_once(self):
        st = {"last_job_id": "j1"}
        at.judge(st, {"id": "j1", "status": "error", "error": "boom"}, None, now=NOW)
        assert at.judge(st, {"id": "j1", "status": "error", "error": "boom"}, None, now=NOW) is None
        assert st["consecutive_failures"] == 1

    def test_throttle_backs_off_and_doubles_to_four_hours(self):
        st = {"last_job_id": "j1"}
        assert at.judge(st, {"id": "j1", "status": "error", "error": "ThrottlingException"}, None, now=NOW) == "throttle"
        assert st["backoff_min"] == 30
        assert at._when(st["paused_until"]) == NOW + timedelta(minutes=30)
        for want in (60, 120, 240, 240):
            st["last_job_id"] = f"j{want}"
            at.judge(st, {"id": st["last_job_id"], "status": "error", "error": "429 Too Many Requests"}, None, now=NOW)
            assert st["backoff_min"] == want
        assert not st.get("breaker")

    def test_two_failures_in_a_row_trip_the_breaker(self):
        st = {"last_job_id": "j1"}
        assert at.judge(st, {"id": "j1", "status": "done"}, {"status": "failed", "notes": "acceptance failed"},
                        now=NOW) == "failure"
        st["last_job_id"] = "j2"
        assert at.judge(st, {"id": "j2", "status": "error", "error": "numbers differ"}, None, now=NOW) == "breaker"
        assert st["breaker"] and "2 failures" in st["breaker_reason"]

    def test_a_translation_that_left_no_kit_is_a_failure(self):
        st = {"last_job_id": "j1"}
        assert at.judge(st, {"id": "j1", "status": "done"}, None, now=NOW) == "failure"

    def test_resume_clears_the_breaker(self):
        st = {"breaker": True, "breaker_at": NOW.isoformat()}
        assert not at.breaker_cleared({"breaker_cleared_at": (NOW - timedelta(minutes=1)).isoformat()}, st)
        assert at.breaker_cleared({"breaker_cleared_at": (NOW + timedelta(minutes=1)).isoformat()}, st)


# ── cost ────────────────────────────────────────────────────────────────────


class TestCost:
    def test_job_spend_follows_kit_cost_ts(self):
        assert at.job_spend({"total_usd": 1.5, "cost_usd": 0.4})["usd"] == 1.5
        s = at.job_spend({"cost_usd": 0.4, "images_usd": 0.1, "tts_usd": 0.2})
        assert s["usd"] == pytest.approx(0.7) and s["all_in"]
        assert not at.job_spend({"cost_usd": 0.4})["all_in"]
        assert at.job_spend(None) is None and at.job_spend({"calls": 1}) is None

    def test_text_only_usage_gets_a_labelled_tts_estimate(self):
        c = at.kit_unit_cost([{"id": "a", "usage": {"cost_usd": 0.5}}, {"id": "b", "usage": {"cost_usd": 0.1}}])
        assert c["measured_usd"] == pytest.approx(0.6)
        assert c["tts_estimate_usd"] == at.TTS_ESTIMATE_USD
        assert c["total_usd"] == pytest.approx(0.6 + at.TTS_ESTIMATE_USD)
        assert c["basis"] == "measured text + estimated TTS"

    def test_all_in_usage_has_no_estimate(self):
        c = at.kit_unit_cost([{"id": "a", "usage": {"total_usd": 2.4, "cost_usd": 1.0, "tts_usd": 0.3, "images_usd": 0}}])
        assert c["tts_estimate_usd"] == 0.0 and c["total_usd"] == pytest.approx(2.4) and c["basis"] == "measured"

    def test_a_job_is_counted_once(self):
        job = {"id": "a", "usage": {"total_usd": 1.0}}
        assert at.kit_unit_cost([job, job])["measured_usd"] == pytest.approx(1.0)


def test_throttle_error_classes():
    for e in ("ThrottlingException: Rate exceeded", "429 Too Many Requests", "RESOURCE_EXHAUSTED",
              "ServiceUnavailableException", "overloaded_error 529"):
        assert at.throttle_error(e), e
    for e in ("numbers differ", "acceptance failed: card overlap", ""):
        assert not at.throttle_error(e), e


# ── summary ─────────────────────────────────────────────────────────────────


class TestSummary:
    def test_due_once_a_day_after_the_hour(self):
        s = {"summary_hour_utc": 3.5}
        assert at.summary_due(s, {}, NOW.replace(hour=3, minute=31))
        assert not at.summary_due(s, {}, NOW.replace(hour=3, minute=29))
        assert not at.summary_due(s, {"summary_date": NOW.date().isoformat()}, NOW)

    def test_no_email_on_an_empty_day(self):
        assert at.build_summary(since=NOW - timedelta(days=1), now=NOW, queued=[], landed=[], failed=[], held=[],
                                in_review={}, left={"ar": 3}, jawi_waiting=0, warnings=[], state={}) is None

    def test_the_email_carries_review_samples_failures_and_cost(self):
        landed = [{"language": "ar", "topic_id": f"t{i}", "topic_title": f"T{i}"} for i in range(5)]
        warn = {"language": "hi", "topic_title": "Cells", "total_usd": 2.31, "text_usd": 1.9, "tts_usd": 0,
                "tts_estimate_usd": 0.21, "images_usd": 0.2, "basis": "measured text + estimated TTS", "at": NOW.isoformat()}
        subject, text = at.build_summary(
            since=NOW - timedelta(days=1), now=NOW, queued=[{"language": "ar"}] * 6, landed=landed,
            failed=[{"language": "ar", "topic_title": "Heat", "error": "acceptance failed"}],
            held=[{"language": "ms", "topic_title": "Angles", "hold": "rejected_or_failed", "hold_detail": "rejected/failed"}],
            in_review={"ar": 5}, left={"ar": 37, "ms": 48}, jawi_waiting=48, warnings=[warn],
            state={"cost_warnings": [warn] * 3}, rng=random.Random(1))
        assert "5 in review" in subject and "1 failed" in subject and "1 over cost" in subject
        assert "REPEATED COST BREACHES" in text
        assert text.count("/library/topics/") == at.SAMPLE_PER_LANGUAGE
        assert "Cells: $2.31" in text and "est." in text
        assert "Held for you" in text and "Angles" in text
        assert "Jawi waiting for Malay approval: 48" in text


# ── the tick, against an in-memory client ──────────────────────────────────


class _Res:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, db, table):
        self.db, self.table, self.filters, self._neg = db, table, [], False
        self.op, self.payload = "select", None

    def select(self, *_a, **_k):
        return self

    def eq(self, k, v):
        self.filters.append(("eq", k, v)); return self

    def neq(self, k, v):
        self.filters.append(("neq", k, v)); return self

    def gte(self, k, v):
        return self

    def in_(self, k, vs):
        self.filters.append(("nin" if self._neg else "in", k, list(vs))); self._neg = False; return self

    @property
    def not_(self):
        self._neg = True; return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a):
        return self

    def upsert(self, row, **_k):
        self.op, self.payload = "upsert", row; return self

    def _ok(self, r):
        for op, k, v in self.filters:
            val = r.get(k)
            if op == "eq" and str(val) != str(v): return False
            if op == "neq" and str(val) == str(v): return False
            if op == "in" and val not in v: return False
            if op == "nin" and val in v: return False
        return True

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self.op == "upsert":
            for r in rows:
                if r.get("key") == self.payload["key"]:
                    r.update(self.payload); return _Res([r])
            rows.append(dict(self.payload)); return _Res([self.payload])
        return _Res([dict(r) for r in rows if self._ok(r)])


class _Rpc:
    def __init__(self, db, name, params):
        self.db, self.name, self.params = db, name, params

    def execute(self):
        self.db.rpc_calls.append((self.name, dict(self.params)))
        if self.name == "auto_translate_candidates":
            return _Res(list(self.db.cands))
        if self.name == "auto_translate_enqueue":
            if self.params["p_dry_run"]:
                return _Res({"queued": False, "dry_run": True})
            job = {"id": f"job-{len(self.db.tables['jobs']) + 1}", "type": "kit_translate", "status": "queued",
                   "params": {"source_kit_id": self.params["p_source_kit_id"], "language": self.params["p_language"],
                              "auto": True}}
            self.db.tables["jobs"].append(job)
            return _Res({"queued": True, "job_id": job["id"]})
        raise AssertionError(self.name)


class FakeDB:
    def __init__(self, settings=None, jobs=None, cands=None):
        self.tables = {"platform_settings": [], "jobs": list(jobs or []), "topic_kits": [], "generations": [],
                       "books": [], "platform_audit_log": []}
        if settings is not None:
            self.tables["platform_settings"].append({"key": "auto_translate", "value": settings})
        self.cands = cands if cands is not None else [cand(1, src="en-kit-1")]
        self.rpc_calls = []

    def table(self, name):
        return _Q(self, name)

    def rpc(self, name, params=None):
        return _Rpc(self, name, dict(params or {}))

    def state(self):
        rows = [r for r in self.tables["platform_settings"] if r["key"] == "auto_translate_state"]
        return rows[0]["value"] if rows else None

    def enqueued(self):
        return [p for n, p in self.rpc_calls if n == "auto_translate_enqueue"]


@pytest.fixture(autouse=True)
def _bedrock(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "bedrock")
    monkeypatch.setenv("CATALOGUE_TEXT_PROVIDER", "anthropic")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)


class TestSweep:
    def test_off_reads_only_the_settings(self):
        db = FakeDB(settings={**ON, "enabled": False})
        assert at.sweep(db, now=NOW).action == "off"
        assert db.rpc_calls == [] and db.state() is None

    def test_dry_run_records_what_it_would_queue_and_inserts_nothing(self):
        db = FakeDB(settings={**ON, "dry_run": True})
        d = at.sweep(db, now=NOW)
        assert d.why == "dry run: would queue"
        assert db.enqueued()[0]["p_dry_run"] is True
        assert not [j for j in db.tables["jobs"] if j["type"] == "kit_translate"]
        st = db.state()
        assert st["decision"]["topic"] == "Topic 1" and st["decision"]["dry_run"] is True

    def test_live_queues_one_and_waits_for_it(self):
        db = FakeDB(settings=ON)
        assert at.sweep(db, now=NOW).action == "queue"
        assert db.state()["last_job_id"] == "job-1"
        d = at.sweep(db, now=NOW + timedelta(minutes=1))
        assert d.action == "wait" and "queued" in d.why
        assert len([j for j in db.tables["jobs"] if j["type"] == "kit_translate"]) == 1

    def test_an_arabic_client_job_holds_the_agent(self):
        db = FakeDB(settings=ON, jobs=[{"id": "u1", "type": "presentation", "status": "processing",
                                        "params": {"language": "ar"}, "updated_at": NOW.isoformat()}])
        d = at.sweep(db, now=NOW)
        assert d.action == "wait" and "Bedrock" in d.why and db.enqueued() == []

    def test_an_index_book_holds_the_agent(self):
        db = FakeDB(settings=ON, jobs=[{"id": "u1", "type": "index_book", "status": "queued", "params": {},
                                        "updated_at": NOW.isoformat()}])
        assert "indexed" in at.sweep(db, now=NOW).why

    def test_a_vertex_client_job_does_not_hold_the_agent(self):
        db = FakeDB(settings=ON, jobs=[{"id": "u1", "type": "presentation", "status": "processing",
                                        "params": {"language": "en"}, "updated_at": NOW.isoformat()}])
        assert at.sweep(db, now=NOW).action == "queue"

    def test_the_language_comes_from_the_generation_when_the_job_has_none(self):
        db = FakeDB(settings=ON, jobs=[{"id": "u1", "type": "worksheet", "status": "queued", "params": {},
                                        "generation_id": "g1", "updated_at": NOW.isoformat()}])
        db.tables["generations"].append({"id": "g1", "language": "ur"})
        assert at.sweep(db, now=NOW).action == "wait"

    def test_catalogue_and_publishing_jobs_do_not_hold_the_agent(self):
        db = FakeDB(settings=ON, jobs=[
            {"id": "c1", "type": "presentation", "status": "processing", "params": {"catalogue": True, "language": "ar"},
             "updated_at": NOW.isoformat()},
            {"id": "p1", "type": "topic_publish", "status": "processing", "params": {"kit_id": "k"},
             "updated_at": NOW.isoformat()}])
        assert at.sweep(db, now=NOW).action == "queue"

    def test_without_bedrock_client_claude_work_never_holds(self, monkeypatch):
        monkeypatch.setenv("LLM_BACKEND", "anthropic")
        db = FakeDB(settings=ON, jobs=[{"id": "u1", "type": "index_book", "status": "queued", "params": {},
                                        "updated_at": NOW.isoformat()}])
        assert at.sweep(db, now=NOW).action == "queue"

    def test_a_failed_translation_is_judged_and_two_trip_the_breaker(self):
        db = FakeDB(settings=ON, cands=[cand(1, src="s1"), cand(2, src="s2")])
        at.sweep(db, now=NOW)
        db.tables["jobs"][0].update(status="error", error="numbers differ")
        db.cands = [cand(2, src="s2")]
        assert at.sweep(db, now=NOW + timedelta(minutes=1)).action == "queue"
        db.tables["jobs"][1].update(status="error", error="acceptance failed")
        d = at.sweep(db, now=NOW + timedelta(minutes=2))
        assert d.action == "wait" and "breaker" in d.why and db.state()["breaker"]

    def test_the_database_failing_queues_nothing(self, monkeypatch):
        monkeypatch.setenv("AUTO_TRANSLATE", "1")

        class Broken(FakeDB):
            def rpc(self, name, params=None):
                raise RuntimeError("connection refused")
        db = Broken(settings=ON)
        at._last_run = 0.0
        assert at.maybe_sweep(db, clock=lambda: 10_000.0, now=NOW) is None   # swallowed, logged
        assert [j for j in db.tables["jobs"] if j["type"] == "kit_translate"] == []

    def test_a_lane_check_that_cannot_read_fails_closed(self):
        class NoJobs(FakeDB):
            def table(self, name):
                if name == "jobs":
                    raise RuntimeError("timeout")
                return super().table(name)
        db = NoJobs(settings=ON)
        d = at.sweep(db, now=NOW)
        assert d.action == "wait" and d.why == "the database did not answer"
        assert db.enqueued() == []

    def test_the_tick_is_inert_without_the_env_switch(self, monkeypatch):
        monkeypatch.delenv("AUTO_TRANSLATE", raising=False)
        db = FakeDB(settings=ON)
        assert at.maybe_sweep(db) is None and db.rpc_calls == []

    def test_the_tick_runs_once_a_minute(self, monkeypatch):
        monkeypatch.setenv("AUTO_TRANSLATE", "1")
        at._last_run = 0.0
        db = FakeDB(settings={**ON, "dry_run": True})
        assert at.maybe_sweep(db, clock=lambda: 1000.0, now=NOW) is not None
        assert at.maybe_sweep(db, clock=lambda: 1030.0, now=NOW) is None
        assert at.maybe_sweep(db, clock=lambda: 1061.0, now=NOW) is not None
