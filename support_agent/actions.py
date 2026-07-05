"""Agent actions. The guards here are hard rules, not model suggestions:

* Regeneration NEVER creates or modifies generation_shares — an assigned
  artifact keeps serving students unchanged and the correction lands with the
  adult as a pending item they re-assign themselves.
* The old artifact is never deleted: the old generations row (and its storage
  files) stay intact, cross-linked via params.
* MAX_REGENS caps automatic regeneration per (book, chapter); past the cap the
  agent stops and escalates.
* Auto-regeneration requires HIGH confidence AND a post-reindex verification
  that the requested chapter's source now matches its title.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

MAX_REGENS = 2
MIN_REGEN_CONFIDENCE = 0.75
MAX_TRANSIENT_RETRIES = 2

_REGEN_MARK = "support_regenerated_from"


def _regen_count(sb, book_id: str) -> int:
    """Prior auto-regenerations for this book, counted from platform_issues —
    rows the reporter can neither update nor delete (RLS: select-own only), so
    the cap cannot be reset by deleting the regenerated generations."""
    r = (
        sb.table("platform_issues")
        .select("id")
        .eq("book_id", book_id)
        .in_("agent_action", ["regenerated", "regenerated_pending"])
        .execute()
    )
    return len(getattr(r, "data", None) or [])


def retry_transient(sb, gen: dict) -> str:
    """Requeue a FAILED generation (bounded by prior attempts). Guards, found
    in adversarial review: re-running a job overwrites artifacts at the
    generation's deterministic storage path, so a retry is only safe when the
    generation actually failed AND nothing is assigned to students — otherwise
    it would swap or blank content students are viewing."""
    if (gen.get("status") or "") != "error":
        return "not_failed"
    r = sb.table("generation_shares").select("id").eq("generation_id", gen["id"]).limit(1).execute()
    if getattr(r, "data", None):
        return "assigned_blocked"
    r = sb.table("jobs").select("id").eq("generation_id", gen["id"]).execute()
    attempts = len(getattr(r, "data", None) or [])
    if attempts > MAX_TRANSIENT_RETRIES:
        return "retry_cap_reached"
    sb.table("generations").update({"status": "queued"}).eq("id", gen["id"]).execute()
    sb.table("jobs").insert(
        {"generation_id": gen["id"], "type": str(gen.get("kind") or "presentation"), "status": "queued"}
    ).execute()
    return "requeued"


def reindex_and_regenerate(sb, issue: dict, gen: dict, book: dict, diagnosis: dict, client, job_id: str) -> dict:
    """The wrong-chapter fix: re-detect the book's chapters (running headers +
    audit gates now catch what the old split missed), verify the requested
    chapter reads correctly, then generate a fresh row. Returns a result dict
    with 'action' ∈ regenerated | regenerated_pending | regen_blocked_* ."""
    from worker.process import index_book
    from agent1_ingestion.chapter_check import verify_chapter_content

    owner_id = gen["owner_id"]
    chapter_ref = gen.get("chapter_ref")

    # Loop/cost cap first — a pathological book must not burn money forever.
    if _regen_count(sb, book["id"]) >= MAX_REGENS:
        return {"action": "regen_blocked_cap", "detail": f"{MAX_REGENS} auto-regenerations already spent"}

    if diagnosis.get("confidence", 0) < MIN_REGEN_CONFIDENCE:
        return {"action": "regen_blocked_confidence", "detail": "confidence below threshold"}

    # Assigned-content check BEFORE any mutation (drives the pending path).
    r = sb.table("generation_shares").select("id").eq("generation_id", gen["id"]).limit(1).execute()
    assigned = bool(getattr(r, "data", None))

    # Snapshot the book's stored split: if this run commits nothing, the book
    # must be restored EXACTLY — students' assignment headings render from
    # books.chapters, and the escalation message promises nothing changed.
    snapshot = {"chapters": book.get("chapters"), "status": book.get("status")}

    # 1) Re-detect chapters (index_book stores the audited split on the book).
    #    It reuses OUR job id for its usage write (merged additively).
    index_book(sb, {"book_id": book["id"], "id": job_id})

    # 2) Verify the requested chapter NOW reads as its title — regenerating an
    #    unfixed split would just reproduce the wrong lesson.
    from support_agent.bundle import _chapter_source_text
    import tempfile
    from pathlib import Path

    r = sb.table("books").select("*").eq("id", book["id"]).maybe_single().execute()
    fresh_book = getattr(r, "data", None) or book
    with tempfile.TemporaryDirectory() as tmp:
        src, meta = _chapter_source_text(sb, fresh_book, chapter_ref, Path(tmp))
    title = (meta.get("stored_chapter") or {}).get("title") or ""
    ok, actual = verify_chapter_content(title, src, client)
    if not ok:
        # Roll the split back — nothing was committed, so nothing may change.
        sb.table("books").update(snapshot).eq("id", book["id"]).execute()
        return {"action": "regen_blocked_verify", "detail": f"post-reindex slice still reads as {actual!r}"}

    # 4) New generation row (same kind/chapter; the DB trigger queues its job).
    #    Old row is untouched apart from a cross-link — fully recoverable.
    params = dict(gen.get("params") or {})
    params[_REGEN_MARK] = gen["id"]
    params["support_chapter_ref"] = str(chapter_ref)
    params["support_issue_id"] = issue["id"]
    ins = (
        sb.table("generations")
        .insert(
            {
                "kind": gen.get("kind"),
                "book_id": book["id"],
                "owner_id": owner_id,
                "school_id": gen.get("school_id"),
                "chapter_ref": chapter_ref,
                "params": params,
                "status": "queued",
            }
        )
        .execute()
    )
    new_id = (getattr(ins, "data", None) or [{}])[0].get("id")
    old_params = dict(gen.get("params") or {})
    old_params["superseded_by"] = new_id
    sb.table("generations").update({"params": old_params}).eq("id", gen["id"]).execute()

    return {
        "action": "regenerated_pending" if assigned else "regenerated",
        "new_generation_id": new_id,
        "assigned": assigned,
    }


def notify_owner(sb, owner_id: str, subject: str, text: str) -> None:
    """Best-effort Resend email to the content owner (worker-side)."""
    key = os.getenv("RESEND_API_KEY")
    if not key:
        return
    try:
        r = sb.auth.admin.get_user_by_id(owner_id)
        email = getattr(getattr(r, "user", None), "email", None)
        if not email or email.endswith("@students.sketchcast.app"):
            return
        import requests

        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "from": "SketchCast AI <noreply@sketchcast.app>",
                "to": [email],
                "subject": subject,
                "text": text,
            },
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("owner notify failed: %s", exc)
