"""support_agent/resolve.py — the console's resolution from the worker, with
the owner's sentence queued for their one digest (support_agent/notices.py)."""

from __future__ import annotations

from support_agent import resolve as R
from tests.catalogue_fakes import FakeSB


def _sb(status="triaged", with_generation=True):
    sb = FakeSB()
    sb.tables["platform_issues"] = [{"id": "iss-1", "status": status, "severity": "normal",
                                     "category": "generation_failed", "reporter_id": "u-reporter",
                                     "generation_id": "gen-1" if with_generation else None, "resolution_note": None}]
    sb.tables["generations"] += [{"id": "gen-1", "kind": "presentation", "owner_id": "u-owner", "status": "done"}]
    sb.tables["jobs"].append({"id": "job-1", "type": R.JOB_TYPE, "status": "processing", "attempts": 0,
                              "issue_id": "iss-1", "params": {"note": "We fixed the fault and your lesson is ready.",
                                                               "actor_id": "staff-1"}})
    return sb


def _queued(sb):
    return [(n["owner_id"], n["what"], n["note"]) for n in sb.tables.get("issue_notices", [])]


class TestResolve:
    def test_resolves_audits_and_queues_the_generations_owner_a_notice(self, monkeypatch):
        sb = _sb()
        out = R.run_issue_resolve_job(sb, sb.tables["jobs"][0])
        assert out == {"status": "resolved", "queued": True, "already": False}
        issue = sb.tables["platform_issues"][0]
        assert issue["status"] == "resolved" and issue["resolved_at"] and issue["resolution_note"].startswith("We fixed")
        assert _queued(sb) == [("u-owner", "lesson video", "We fixed the fault and your lesson is ready.")]
        audit = [r for r in sb.tables["platform_audit_log"] if r["target_id"] == "iss-1"]
        assert audit and audit[0]["actor_id"] == "staff-1" and audit[0]["action"] == "issue_status"
        assert audit[0]["detail"]["before"]["status"] == "triaged"
        assert audit[0]["detail"]["queued"] is True and audit[0]["detail"]["notified"] is False
        job = sb.tables["jobs"][0]
        assert job["status"] == "done" and job["stage"]["issue_id"] == "iss-1"

    def test_without_a_generation_the_reporter_is_the_owner(self, monkeypatch):
        sb = _sb(with_generation=False)
        R.run_issue_resolve_job(sb, sb.tables["jobs"][0])
        assert _queued(sb)[0][0] == "u-reporter" and _queued(sb)[0][1] == "generation failed"

    def test_an_already_resolved_issue_is_left_alone_and_no_second_notice_is_queued(self, monkeypatch):
        sb = _sb(status="resolved")
        out = R.run_issue_resolve_job(sb, sb.tables["jobs"][0])
        assert out["already"] is True and _queued(sb) == []
        assert not [r for r in sb.tables.get("platform_audit_log", []) if r.get("target_id") == "iss-1"]

    def test_a_missing_issue_fails_the_job_in_one_sentence(self, monkeypatch):
        sb = _sb()
        sb.tables["platform_issues"] = []
        assert R.run_issue_resolve_job(sb, sb.tables["jobs"][0]) is None
        assert sb.tables["jobs"][0]["status"] == "error" and "not found" in sb.tables["jobs"][0]["error"]

    def test_a_failed_queue_write_is_recorded_not_fatal(self, monkeypatch):
        monkeypatch.setattr(R, "queue_owner_notice", lambda *a: False)
        sb = _sb()
        out = R.run_issue_resolve_job(sb, sb.tables["jobs"][0])
        assert out["queued"] is False and sb.tables["platform_issues"][0]["status"] == "resolved"
        audit = [r for r in sb.tables["platform_audit_log"] if r["target_id"] == "iss-1"]
        assert audit[0]["detail"]["queued"] is False


class TestWiring:
    def test_the_worker_claims_dispatches_and_observes_the_job_type(self):
        from pathlib import Path
        import worker.run as run
        from worker import client as db
        assert R.JOB_TYPE in run.OBSERVER_JOB_TYPES and R.JOB_TYPE in db.OBSERVER_JOB_TYPES
        src = Path(run.__file__).read_text(encoding="utf-8")
        assert 'job_type=["support_diagnose", "issue_resolve"]' in src
        assert 'job_type == "issue_resolve"' in src and "run_issue_resolve_job(sb, job)" in src
        assert '"support_diagnose", "issue_resolve", "index_book"' in src, "its own failure never files an issue"
