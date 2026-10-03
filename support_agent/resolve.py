"""Resolve a console issue from the worker — the console's own write, with
the owner email the founder's direction requires (2026-09-25: every
resolution reaches the client, and a reply reopens it).

The console's PATCH /api/console/issues does three things on the first move
into ``resolved``: the row, the audit line, and the owner's email through
Resend. The key for that email lives on the app and on the worker, nowhere
else — so an operator resolving an issue by hand from outside the console
(2026-10-01: a lesson re-run after the JSON salvage fix) could update the
row but not mail the teacher. This job closes that gap: one ``issue_resolve``
job in ``jobs`` (``issue_id`` on the row, ``params.note`` the sentence the
owner reads, ``params.actor_id`` the staff member it is audited under) and
the worker does exactly what the console would have.

Idempotent: an issue already resolved is left as it is and no second notice
is queued. The owner's email is the digest (support_agent/notices.py): one per
owner once everything of theirs is resolved, never one per issue. The job
never touches the generation the issue reports on.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from support_agent.notices import queue_owner_notice, what_for
from worker import client as db

logger = logging.getLogger(__name__)

JOB_TYPE = "issue_resolve"


def _row(res) -> Optional[dict]:
    data = getattr(res, "data", None)
    if isinstance(data, list):
        return data[0] if data else None
    return data


def resolve_issue(sb, issue_id: str, note: str, actor_id: Optional[str] = None) -> dict:
    """The console's resolution, from here. Returns ``{status, queued,
    already}``; raises when the issue does not exist."""
    issue = _row(sb.table("platform_issues").select("*").eq("id", issue_id).limit(1).execute())
    if not issue:
        raise RuntimeError(f"issue {issue_id} not found")
    if str(issue.get("status") or "") == "resolved":
        return {"status": "resolved", "notified": False, "already": True}
    note = " ".join(str(note or "").split())[:2000]
    gen = None
    if issue.get("generation_id"):
        gen = _row(sb.table("generations").select("*").eq("id", issue["generation_id"]).limit(1).execute())
    before = {"status": issue.get("status"), "severity": issue.get("severity")}
    patch = {"status": "resolved", "resolved_at": datetime.now(timezone.utc).isoformat(),
             "resolution_note": note or None}
    sb.table("platform_issues").update(patch).eq("id", issue_id).execute()
    owner = (gen or {}).get("owner_id") or issue.get("reporter_id")
    # the owner's sentence joins their digest (support_agent/notices.py):
    # one email per owner once everything of theirs is resolved, never one
    # per issue (four emails about one deck, 2026-10-02)
    queued = bool(owner) and queue_owner_notice(sb, owner, issue_id, what_for(sb, gen, issue), note)
    try:
        sb.table("platform_audit_log").insert({
            "actor_id": actor_id, "action": "issue_status", "target_kind": "issue", "target_id": issue_id,
            "detail": {"before": before, "after": patch, "notified": False, "queued": queued, "via": JOB_TYPE},
        }).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("issue %s: audit write failed: %s", issue_id, exc)
    logger.info("issue %s resolved from the worker (owner notice queued: %s)", issue_id, queued)
    return {"status": "resolved", "queued": queued, "already": False}


def run_issue_resolve_job(sb, job: dict) -> Optional[dict]:
    """Entry point for run.py. Self-contained: finishes its own row with the
    outcome in ``stage``, or error with the sentence."""
    job_id = job["id"]
    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    try:
        issue_id = str(job.get("issue_id") or params.get("issue_id") or "")
        if not issue_id:
            raise RuntimeError("issue_resolve job without issue_id")
        summary = resolve_issue(sb, issue_id, str(params.get("note") or ""), params.get("actor_id"))
        db.set_stage(sb, job_id, {"issue_id": issue_id, **summary})
        db.finish_job(sb, job_id)
        return summary
    except Exception as exc:  # noqa: BLE001
        logger.error("issue_resolve job %s failed: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=f"{type(exc).__name__}: {exc}"[:4000])
        except Exception as exc2:  # noqa: BLE001
            logger.error("issue_resolve job %s: could not record the failure: %s", job_id, exc2)
        return None


__all__ = ["JOB_TYPE", "resolve_issue", "run_issue_resolve_job"]
