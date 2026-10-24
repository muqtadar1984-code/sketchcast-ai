"""Fair-share claiming and stop requests — the worker half of migration 0153.

Sara's Sci 7 (2026-10-22): one Generate all queued ~250 jobs and the worker
claimed strictly oldest-first, so anyone arriving behind them waited up to
143 min. With FAIR_CLAIM=1 the user lanes claim through claim_fair_job
(round-robin by teacher, holds, stop requests); any RPC failure must fall back
to the oldest-first claim — fairness is an improvement, never a reason to stop
building. A stop request (jobs.cancel_requested_at) aborts the build at its
next progress write.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from worker import client as db


class _Q:
    """Just enough of the PostgREST builder for claim_next_job's old path."""

    def __init__(self, sb, table):
        self.sb, self.table, self.op = sb, table, "select"

    def select(self, *_a, **_k):
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def __getattr__(self, _name):  # eq / in_ / not_ / or_ / order / limit …
        return lambda *a, **k: self

    @property
    def not_(self):
        return self

    def execute(self):
        self.sb.calls.append((self.table, self.op))
        if self.table == "jobs" and self.op == "select":
            return SimpleNamespace(data=[{"id": "old-1", "generation_id": "g-old", "type": "worksheet"}])
        if self.table == "jobs" and self.op == "update":
            return SimpleNamespace(data=self.sb.update_rows)
        return SimpleNamespace(data=[])


class _SB:
    def __init__(self, rpc_rows=None, rpc_exc=None, update_rows=None):
        self.rpc_rows, self.rpc_exc = rpc_rows, rpc_exc
        self.update_rows = update_rows if update_rows is not None else [{"id": "old-1", "generation_id": "g-old", "type": "worksheet"}]
        self.calls, self.rpcs = [], []

    def table(self, name):
        return _Q(self, name)

    def rpc(self, name, params):
        self.rpcs.append((name, params))
        return self

    def execute(self):
        if self.rpc_exc:
            raise self.rpc_exc
        return SimpleNamespace(data=self.rpc_rows)


@pytest.fixture(autouse=True)
def _no_mirror(monkeypatch):
    mirrored = []
    monkeypatch.setattr(db, "mirror_generation_status", lambda _sb, gid, st: mirrored.append((gid, st)))
    db._FAIR_MUTED_UNTIL["t"] = 0.0
    return mirrored


def test_flag_off_never_calls_the_rpc(monkeypatch):
    monkeypatch.delenv("FAIR_CLAIM", raising=False)
    sb = _SB(rpc_rows=[{"id": "fair-1"}])
    assert db.claim_next_job(sb, job_type=["worksheet"], catalogue=False)["id"] == "old-1"
    assert sb.rpcs == []


def test_flag_on_claims_through_the_rpc_with_lane_filters_and_caps(monkeypatch, _no_mirror):
    monkeypatch.setenv("FAIR_CLAIM", "1")
    monkeypatch.setenv("FAIR_OWNER_CAP", "4")
    sb = _SB(rpc_rows=[{"id": "fair-1", "generation_id": "g-fair", "type": "worksheet"}])
    got = db.claim_next_job(sb, job_type=["worksheet", "lesson_plan"], exclude_types={"topic_harvest"}, catalogue=False)
    assert got["id"] == "fair-1"
    name, params = sb.rpcs[0]
    assert name == "claim_fair_job"
    assert params == {"p_types": ["worksheet", "lesson_plan"], "p_exclude_types": ["topic_harvest"],
                      "p_user_only": True, "p_owner_cap": 4, "p_video_cap": 1}
    assert ("jobs", "select") not in sb.calls          # no second, unfair claim
    assert _no_mirror == [("g-fair", "processing")]


def test_nothing_eligible_is_none_not_a_fallback(monkeypatch):
    monkeypatch.setenv("FAIR_CLAIM", "1")
    sb = _SB(rpc_rows=[])
    assert db.claim_next_job(sb, job_type="worksheet", catalogue=False) is None
    assert ("jobs", "select") not in sb.calls          # a held/capped queue stays held


def test_rpc_failure_falls_back_to_oldest_first(monkeypatch):
    monkeypatch.setenv("FAIR_CLAIM", "1")
    sb = _SB(rpc_exc=RuntimeError("PGRST202: function claim_fair_job does not exist"))
    assert db.claim_next_job(sb, job_type="worksheet", catalogue=False)["id"] == "old-1"


def test_catalogue_lanes_keep_their_own_claim(monkeypatch):
    monkeypatch.setenv("FAIR_CLAIM", "1")
    sb = _SB(rpc_rows=[{"id": "fair-1"}])
    db.claim_next_job(sb, catalogue=True, kit_ids=["k1"])
    assert sb.rpcs == []


def test_a_stop_request_aborts_at_the_next_progress_write():
    sb = _SB(update_rows=[{"id": "j1", "progress": 40, "cancel_requested_at": "2026-10-22T10:00:00+00:00"}])
    with pytest.raises(db.JobCancelled):
        db.set_progress(sb, "j1", 40)


def test_progress_without_a_stop_request_is_unchanged():
    db.set_progress(_SB(update_rows=[{"id": "j1", "progress": 40}]), "j1", 40)
    db.set_progress(_SB(update_rows=[]), "j1", 40)     # a requeued row: guard matched nothing
