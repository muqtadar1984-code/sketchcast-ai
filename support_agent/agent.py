"""Orchestrator: one support_diagnose job = one audited agent run.

Flow: load issue → assemble tenant-scoped bundle → Sonnet-5 diagnosis →
guarded action → update the issue (user-safe fields only) → audit (staff
detail) → notify the owner when something changed. Any internal failure
escalates honestly; it never fakes a fix. Spend is written in a finally so
even crashing runs are attributed.
"""

from __future__ import annotations

import logging

from shared.claude_client import ClaudeClient
from worker import client as db

from support_agent.actions import notify_owner, notify_staff, reindex_and_regenerate, retry_transient
from support_agent.bundle import ScopeViolation, assemble_bundle
from support_agent.diagnose import DIAGNOSIS_MODEL, diagnose

logger = logging.getLogger(__name__)

# status mapping: resolved = handled; triaged = honestly escalated to staff.
_ESCALATED_MSG = (
    "We've diagnosed this and flagged it to the SketchCast team — you'll hear "
    "back. Nothing about your existing content was changed."
)


def _audit(sb, issue: dict, action: str, detail: dict) -> None:
    try:
        sb.table("platform_audit_log").insert(
            {
                "actor_id": None,  # the agent, not a human
                "action": f"support_agent:{action}",
                "target_kind": "issue",
                "target_id": issue["id"],
                "detail": detail,
            }
        ).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit write failed: %s", exc)


def _update_issue(sb, issue_id: str, patch: dict) -> None:
    sb.table("platform_issues").update(patch).eq("id", issue_id).execute()


def run_support_job(sb, job: dict) -> None:
    issue_id = job.get("issue_id")
    if not issue_id:
        raise RuntimeError("support_diagnose job without issue_id")
    r = sb.table("platform_issues").select("*").eq("id", issue_id).maybe_single().execute()
    issue = getattr(r, "data", None)
    if not issue:
        raise RuntimeError(f"issue {issue_id} not found")

    client = ClaudeClient(model=DIAGNOSIS_MODEL)
    try:
        _run(sb, job, issue, client)
    except Exception as exc:  # noqa: BLE001 — the agent never fakes a fix; it
        # escalates and lets the support JOB finish clean (run.py must not
        # mirror an agent crash onto the reported generation).
        logger.error("support agent crashed on issue %s: %s", issue_id, exc)
        try:
            _update_issue(
                sb,
                issue_id,
                {
                    "status": "triaged",
                    "agent_action": "escalated",
                    "diagnosis": {"category": "unknown", "user_message": _ESCALATED_MSG},
                },
            )
            _audit(sb, issue, "agent_error", {"error": str(exc)[:300]})
            notify_staff(issue, f"agent crashed: {str(exc)[:300]}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        # Spend attribution even on crashing paths (merged additively with any
        # inline re-index usage already written to this job id).
        db.set_job_usage(sb, job["id"], client.session_usage)


def _run(sb, job: dict, issue: dict, client) -> None:
    issue_id = issue["id"]
    _update_issue(sb, issue_id, {"status": "in_progress"})

    try:
        bundle = assemble_bundle(sb, issue)
    except ScopeViolation as exc:
        # Refuse loudly: no diagnosis happens outside the reporter's tenant.
        _update_issue(
            sb,
            issue_id,
            {
                "status": "triaged",
                "agent_action": "escalated",
                "diagnosis": {"category": "scope_refused", "user_message": _ESCALATED_MSG},
            },
        )
        _audit(sb, issue, "scope_refused", {"reason": str(exc)})
        notify_staff(issue, f"scope refused: {str(exc)[:300]}")
        return

    dx = diagnose(client, bundle)

    # Reporter-readable diagnosis: user-safe fields ONLY. Staff detail → audit.
    user_dx = {
        "category": dx["category"],
        "confidence": dx["confidence"],
        "user_message": dx["user_message"],
        "recommended_action": dx["recommended_action"],
    }
    _audit(
        sb,
        issue,
        "diagnosed",
        {
            "category": dx["category"],
            "confidence": dx["confidence"],
            "staff_note": dx["staff_note"],
            "gate_signals": dx.get("gate_signals"),
            "model": DIAGNOSIS_MODEL,
        },
    )

    gen = None
    if issue.get("generation_id"):
        r = sb.table("generations").select("*").eq("id", issue["generation_id"]).maybe_single().execute()
        gen = getattr(r, "data", None)
    book = None
    book_id = (gen or {}).get("book_id") or issue.get("book_id")
    if book_id:
        r = sb.table("books").select("*").eq("id", book_id).maybe_single().execute()
        book = getattr(r, "data", None)

    action = dx["recommended_action"]

    if action == "retry_transient" and gen:
        outcome = retry_transient(sb, gen)
        if outcome == "requeued":
            _update_issue(
                sb,
                issue_id,
                {
                    "status": "resolved",
                    "agent_action": "self_heal_retry",
                    "diagnosis": user_dx,
                    "resolution_note": "Automatic retry queued — the failure looked transient.",
                },
            )
            _audit(sb, issue, "self_heal_retry", {"outcome": outcome})
        else:
            # not_failed / assigned_blocked / retry_cap_reached — all human
            # territory; a retry would overwrite content in place.
            _escalate(sb, issue, user_dx, f"retry refused: {outcome}")
            _audit(sb, issue, "retry_refused", {"outcome": outcome})
        return

    if action == "user_fix":
        _update_issue(
            sb,
            issue_id,
            {
                "status": "resolved",
                "agent_action": "user_fix",
                "diagnosis": user_dx,
                "resolution_note": dx["user_message"][:500],
            },
        )
        _audit(sb, issue, "user_fix", {"message": dx["user_message"][:300]})
        return

    if action == "reindex_regenerate" and gen and book:
        result = reindex_and_regenerate(sb, issue, gen, book, dx, client, job["id"])
        if result["action"] in ("regenerated", "regenerated_pending"):
            pending = result["action"] == "regenerated_pending"
            note = (
                "The corrected version was generated as a NEW pending item. Your students still "
                "see the previous version — review the new one and re-assign when ready. "
                "(Chapter labels may have updated from the corrected detection.)"
                if pending
                else "We regenerated the correct chapter. The previous version is retained on your account."
            )
            _update_issue(
                sb,
                issue_id,
                {
                    "status": "resolved" if not pending else "in_progress",
                    "agent_action": result["action"],
                    "diagnosis": user_dx,
                    "resolution_note": note,
                },
            )
            _audit(sb, issue, result["action"], result)
            notify_owner(
                sb,
                gen["owner_id"],
                "SketchCast fixed a chapter mix-up",
                f"{dx['user_message']}\n\n{note}",
            )
        else:
            _escalate(sb, issue, user_dx, result.get("detail", result["action"]))
            _audit(sb, issue, "regen_blocked", result)
        return

    # escalate / none / missing refs
    _escalate(sb, issue, user_dx, dx["staff_note"] or "no safe automatic action")


def _escalate(sb, issue: dict, user_dx: dict, staff_reason: str) -> None:
    user_dx = dict(user_dx)
    user_dx["user_message"] = f"{user_dx.get('user_message', '').strip()} {_ESCALATED_MSG}".strip()
    _update_issue(
        sb,
        issue["id"],
        {
            "status": "triaged",
            "agent_action": "escalated",
            "diagnosis": user_dx,
        },
    )
    _audit(sb, issue, "escalated", {"reason": staff_reason[:300]})
    notify_staff(issue, staff_reason)
