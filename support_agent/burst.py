"""Burst dedupe for auto-filed support issues (surge plan §4.5, items 1–2).

2026-10-22: one teacher's "Generate all" queued ~250 jobs and the failure
hook filed 17 issues, ran 17 Sonnet diagnoses and sent up to 17 staff emails
for THREE root causes. The rules here turn that into one issue per cause:

* SIGNATURE — ``(owner_id, book_id, normalize(error))``. ``normalize`` drops
  ids, numbers and part indices, so "'Light' has 3 part(s) — part 5 does not
  exist" and "… has 2 part(s) — part 4 …" are one signature per chapter.
* ATTACH — a failure whose signature already has an unresolved auto issue
  from the last 2 h is attached to it (``context.related_generation_ids``,
  ``related_count`` and the raw error per generation, so staff can split an
  over-merge later). No new issue, no diagnosis job, no email.
* BURST — when a failure would open a NEW issue and the owner already has
  ≥ 5 failures in 15 min, one "burst" issue goes to staff instead: the
  signature table and a deep link to the console queue for that owner. No
  per-generation diagnosis runs for it; later new signatures from the same
  owner attach to the open burst. Identical failures never reach this path
  (they attach above), so 13 identical part-map failures stay ONE issue.

The find-or-insert runs under a per-owner lease (``acquire_provider_lease``,
the advisory-locked count-then-insert from migration 0125) so two worker
threads failing at once cannot both miss and both file.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SIGNATURE_WINDOW = timedelta(hours=2)
BURST_WINDOW = timedelta(minutes=15)
BURST_THRESHOLD = 5
# What one issue keeps per attached failure. The issue's own error keeps the
# full evidence budget; an attached one only needs enough to split on.
RELATED_ERROR_CHARS = 600
MAX_RELATED_KEPT = 200

CONSOLE_QUEUE_URL = "https://console.sketchcast.app/console/queue?owner={owner}"

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEX = re.compile(r"\b[0-9a-f]{12,}\b", re.I)
_TMP = re.compile(r"\btmp[\w-]+", re.I)
_NUM = re.compile(r"\d+(?:\.\d+)?")
_SPACE = re.compile(r"\s+")
# The head of the message names the fault; the tail is dumps and paths.
_NORMALIZED_CHARS = 240


def normalize_error(error) -> str:
    text = str(error or "").lower()
    text = _UUID.sub("<id>", text)
    text = _HEX.sub("<id>", text)
    text = _TMP.sub("<tmp>", text)
    text = _NUM.sub("#", text)
    return _SPACE.sub(" ", text).strip()[:_NORMALIZED_CHARS]


def signature(owner_id, book_id, error) -> str:
    raw = f"{owner_id or ''}|{book_id or ''}|{normalize_error(error)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(dt: datetime) -> str:
    """UTC with a Z: no "+" for a query string to turn into a space."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rows(res) -> list:
    data = getattr(res, "data", None)
    return data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])


# ── per-owner lease ───────────────────────────────────────────────────────────
_LEASE_TTL_S = 30
_LEASE_WAIT_S = 8.0


def _holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


def acquire_lease(sb, key: str, ttl: int, *, wait: float = 0.0, sleep=time.sleep):
    """A lease id on ``key`` (one holder at a time), "" when the store cannot
    answer (missing function, network), None when someone else holds it and
    `wait` ran out. Callers decide what "" means: the burst hook proceeds
    unlocked (filing an issue must not depend on a lock), the re-index refuses."""
    deadline = time.monotonic() + wait
    while True:
        try:
            res = sb.rpc("acquire_provider_lease", {"p_provider": key, "p_max": 1, "p_holder": _holder(),
                                                    "p_ttl_seconds": int(ttl)}).execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("lease %s unavailable: %s", key, exc)
            return ""
        data = getattr(res, "data", None)
        if isinstance(data, str) and data:
            return data
        if time.monotonic() >= deadline:
            return None
        sleep(0.25)


def release_lease(sb, lease_id) -> None:
    if not lease_id:
        return
    try:
        sb.rpc("release_provider_lease", {"p_id": lease_id}).execute()
    except Exception as exc:  # noqa: BLE001 — it expires on its own
        logger.warning("lease release failed (%s); it expires by TTL", exc)


# ── the decision ──────────────────────────────────────────────────────────────
def _recent_auto_issues(sb, owner_id: str, since: datetime) -> list:
    return _rows(
        sb.table("platform_issues")
        .select("id, status, created_at, context")
        .eq("reporter_id", owner_id)
        .eq("trigger_source", "auto")
        .gte("created_at", _stamp(since))
        .order("created_at", desc=True)
        .execute()
    )


def _failure_times(issue: dict) -> list[datetime]:
    """Every failure an issue stands for: its own, then each attached one."""
    ctx = issue.get("context") if isinstance(issue.get("context"), dict) else {}
    out = [_parse(ctx.get("failed_at")) or _parse(issue.get("created_at"))]
    out += [_parse(r.get("at")) for r in (ctx.get("related") or []) if isinstance(r, dict)]
    return [t for t in out if t]


def _attach(sb, issue: dict, failure: dict) -> int:
    """Append one failure to an existing issue's context; returns the new
    related_count. A read-modify-write, safe under the owner lease."""
    ctx = dict(issue.get("context") or {})
    gens = list(ctx.get("related_generation_ids") or [])
    if failure["generation_id"] not in gens:
        gens.append(failure["generation_id"])
    related = list(ctx.get("related") or [])
    related.append(failure)
    ctx["related_generation_ids"] = gens[-MAX_RELATED_KEPT:]
    ctx["related"] = related[-MAX_RELATED_KEPT:]
    ctx["related_count"] = int(ctx.get("related_count") or 0) + 1
    if ctx.get("burst"):
        sigs = dict(ctx.get("signatures") or {})
        row = dict(sigs.get(failure["signature"]) or {"sample_error": failure["error"][:200], "count": 0})
        row["count"] = int(row.get("count") or 0) + 1
        sigs[failure["signature"]] = row
        ctx["signatures"] = sigs
    sb.table("platform_issues").update({"context": ctx}).eq("id", issue["id"]).execute()
    return ctx["related_count"]


def _signature_table(issues: list, failure: dict) -> dict:
    """{signature: {sample_error, count}} over the owner's recent failures,
    this one included — what the burst issue and its email carry."""
    table: dict = {}
    for iss in issues:
        ctx = iss.get("context") if isinstance(iss.get("context"), dict) else {}
        sig = ctx.get("signature")
        if not sig:
            continue
        row = table.setdefault(sig, {"sample_error": str(ctx.get("error") or "")[:200], "count": 0,
                                     "issue_id": iss.get("id")})
        row["count"] += 1 + int(ctx.get("related_count") or 0)
    row = table.setdefault(failure["signature"], {"sample_error": failure["error"][:200], "count": 0})
    row["count"] += 1
    return table


def file_or_attach(sb, *, owner_id: str, book_id, gen_id: str, job: dict, error_text: str,
                   new_issue_row: dict, now: datetime | None = None) -> dict:
    """Decide what one failure becomes. Returns {"action": "attached" |
    "burst_attached" | "burst" | "new", "issue_id": …}. Only "new" asks the
    caller to queue a diagnosis; "burst" has already emailed staff."""
    now = now or _now()
    sig = signature(owner_id, book_id, error_text)
    failure = {"generation_id": gen_id, "job_id": job.get("id"), "signature": sig,
               "error": str(error_text or "")[:RELATED_ERROR_CHARS], "at": _stamp(now)}

    lease = acquire_lease(sb, f"support_file:{owner_id}", _LEASE_TTL_S, wait=_LEASE_WAIT_S)
    try:
        issues = _recent_auto_issues(sb, owner_id, now - SIGNATURE_WINDOW)
        open_issues = [i for i in issues if (i.get("status") or "") != "resolved"]

        # 1) same signature, still open → attach
        for iss in open_issues:
            ctx = iss.get("context") if isinstance(iss.get("context"), dict) else {}
            if ctx.get("signature") == sig and not ctx.get("burst"):
                n = _attach(sb, iss, failure)
                logger.info("support: failure of %s attached to issue %s (+%d like it)", gen_id, iss["id"], n)
                return {"action": "attached", "issue_id": iss["id"], "related_count": n}

        # 2) an open burst for this owner absorbs every new signature
        for iss in open_issues:
            ctx = iss.get("context") if isinstance(iss.get("context"), dict) else {}
            if ctx.get("burst"):
                n = _attach(sb, iss, failure)
                logger.info("support: failure of %s attached to burst issue %s", gen_id, iss["id"])
                return {"action": "burst_attached", "issue_id": iss["id"], "related_count": n}

        # 3) ≥ BURST_THRESHOLD failures in BURST_WINDOW (this one included) → one burst issue
        since = now - BURST_WINDOW
        recent = sum(1 for iss in issues for t in _failure_times(iss) if t >= since) + 1
        if recent >= BURST_THRESHOLD:
            return _file_burst(sb, owner_id, issues, failure, recent, new_issue_row, now)

        # 4) a new cause → a new issue (the caller queues its diagnosis)
        row = dict(new_issue_row)
        row["context"] = {**(row.get("context") or {}), "signature": sig, "failed_at": _stamp(now)}
        ins = sb.table("platform_issues").insert(row).execute()
        issue_id = (_rows(ins) or [{}])[0].get("id")
        return {"action": "new", "issue_id": issue_id}
    finally:
        release_lease(sb, lease)


def _file_burst(sb, owner_id, issues, failure, recent, new_issue_row, now) -> dict:
    from support_agent.actions import send_staff_email

    table = _signature_table(issues, failure)
    link = CONSOLE_QUEUE_URL.format(owner=owner_id)
    ctx = {**(new_issue_row.get("context") or {}), "burst": True, "signature": None,
           "failed_at": _stamp(now), "failures_in_window": recent,
           "window_minutes": int(BURST_WINDOW.total_seconds() // 60),
           "signatures": table, "queue_url": link,
           "related_generation_ids": [], "related": [], "related_count": 0}
    row = {**new_issue_row,
           "title": f"Generation burst: {recent} failures in {ctx['window_minutes']} min",
           "status": "triaged", "agent_action": "escalated",
           "diagnosis": {"category": "unknown",
                         "user_message": ("Several of your recent generations failed. The SketchCast team has "
                                          "been alerted and is looking at them together.")},
           "context": ctx}
    ins = sb.table("platform_issues").insert(row).execute()
    issue_id = (_rows(ins) or [{}])[0].get("id")

    lines = [f"{recent} generation failures from one owner in the last {ctx['window_minutes']} minutes.",
             "No per-generation diagnoses were run for the burst; new failures attach to this issue.", "",
             f"Owner:  {owner_id}", f"Queue:  {link}",
             f"Issue:  https://app.sketchcast.app/console/issues/{issue_id}", "",
             "Signatures (count — sample error — existing issue):"]
    for sig, r in sorted(table.items(), key=lambda kv: -kv[1]["count"]):
        lines.append(f"  {r['count']:>3} — {r['sample_error'][:140]} — {r.get('issue_id') or 'burst'}  [{sig}]")
    send_staff_email(f"[Burst] {recent} generation failures in {ctx['window_minutes']} min", "\n".join(lines),
                     issue_id)
    logger.warning("support: burst issue %s filed for owner %s (%d failures)", issue_id, owner_id, recent)
    return {"action": "burst", "issue_id": issue_id}
