"""One email per owner, at the end — the digest of issue resolutions.

Until 2026-10-03 every resolution mailed the owner on the spot, from three
places: the console's PATCH, the support agent's self-heals and the worker's
issue_resolve job. A teacher whose slide deck failed three times, and who
reported it once by hand, got four emails about one fault inside three
minutes (book 3318e1d1, 2026-10-02). The founder's direction: one email per
user, a combined status update once everything of theirs is resolved.

So a resolution no longer sends. It QUEUES a notice (issue_notices: the
owner, the issue, what the item was, the sentence the owner reads), and the
worker's housekeeping tick sends each owner ONE digest when

  - none of their issues is still open, triaged or in progress, and the
    newest unsent notice is at least SETTLE_MINUTES old (a batch of
    resolutions lands in one email, not two), or
  - the oldest unsent notice is MAX_HOLD_HOURS old — a resolved item is not
    held hostage by an unrelated issue that waits on staff for days; the
    digest then says how many items are still being looked at.

Several notices about the same item (three failed attempts at one deck)
collapse into one line carrying the latest sentence. A notice is queued once
per issue (unique issue_id), a sent notice is never sent again, and a send
that fails leaves the rows unsent for the next tick.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from support_agent.actions import REPLY_LINE, notify_owner

logger = logging.getLogger(__name__)

TABLE = "issue_notices"
SETTLE_MINUTES = 15
MAX_HOLD_HOURS = 24
OPEN_STATUSES = ("open", "triaged", "in_progress")


def queue_owner_notice(sb, owner_id: str, issue_id: str, what: str, note: str) -> bool:
    """Queue the owner's sentence about one issue. True when queued; False
    when the issue already has a notice (the first sentence stands) or the
    write failed — never raises, a resolution is not undone by its mail."""
    if not owner_id or not issue_id:
        return False
    row = {"owner_id": owner_id, "issue_id": issue_id, "what": " ".join(str(what or "").split())[:200],
           "note": " ".join(str(note or "").split())[:2000]}
    try:
        sb.table(TABLE).insert(row).execute()
        return True
    except Exception as exc:  # noqa: BLE001 — a duplicate issue_id, or the table unreachable
        logger.info("issue notice for %s not queued: %s", issue_id, exc)
        return False


def _when(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _rows(res) -> list[dict]:
    data = getattr(res, "data", None)
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def pending_notices(sb) -> dict[str, list[dict]]:
    """Unsent notices, by owner, oldest first."""
    rows = _rows(sb.table(TABLE).select("*").is_("sent_at", "null").execute())
    rows.sort(key=lambda r: (_when(r.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)))
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(str(r.get("owner_id") or ""), []).append(r)
    out.pop("", None)
    return out


def open_issues(sb, owner_id: str) -> int:
    """How many of the owner's issues are still open, triaged or in progress."""
    rows = _rows(sb.table("platform_issues").select("id,status").eq("reporter_id", owner_id)
                 .in_("status", list(OPEN_STATUSES)).execute())
    return len(rows)


def due(notices: list[dict], still_open: int, now: datetime) -> bool:
    """The digest rule, on one owner's unsent notices."""
    stamps = [t for t in (_when(n.get("created_at")) for n in notices) if t is not None]
    if not stamps:
        return False
    newest, oldest = max(stamps), min(stamps)
    if oldest <= now - timedelta(hours=MAX_HOLD_HOURS):
        return True
    return still_open == 0 and newest <= now - timedelta(minutes=SETTLE_MINUTES)


def _names_an_item(what: str) -> bool:
    """Does ``what`` name ONE thing — a kind with its book ('slide deck for
    "Where\'s my bag?"') or a report's own title ('test paper (Class Eight ·
    Chapter 1 · Part 3)') — rather than a bare category ("generation
    failed")? Only notices about one item may share a line."""
    return ' for "' in what or "(" in what


def _lines(notices: list[dict]) -> list[tuple[str, str]]:
    """One (what, note) per item: several notices about the SAME item carry
    its latest sentence — three failed attempts at one deck are one line.
    Notices under a bare category stay one line each: two of a teacher's
    reports both read "generation failed" and the digest kept one sentence
    of two (2026-10-10, the report the teacher had just made was the one
    dropped)."""
    latest: dict[str, str] = {}
    order: list[str] = []
    out: list[tuple[str, str]] = []
    for n in notices:  # oldest first, so the last write wins
        what = str(n.get("what") or "request").strip()
        note = str(n.get("note") or "").strip()
        if not _names_an_item(what):
            out.append((what, note))
            continue
        if what not in latest:
            order.append(what)
        latest[what] = note
    return [(w, latest[w]) for w in order] + out


def digest(notices: list[dict], still_open: int = 0) -> tuple[str, str]:
    """Subject and body of the owner's one email."""
    items = _lines(notices)
    lines = ["Hi,", ""]
    if len(items) == 1:
        what, note = items[0]
        subject = f"About your {what} on SketchCast"
        lines.append(f"The problem with your {what} on SketchCast has been addressed.")
        if note:
            lines += ["", note]
    else:
        subject = "An update on your SketchCast items"
        lines.append(f"Here is an update on the {len(items)} items we looked into for you on SketchCast.")
        for i, (what, note) in enumerate(items, 1):
            lines += ["", f"{i}. Your {what}: {note}" if note else f"{i}. Your {what}: addressed."]
    if still_open:
        lines += ["", (f"{still_open} other item of yours is still being looked at; you will hear from us when it is done."
                       if still_open == 1 else
                       f"{still_open} other items of yours are still being looked at; you will hear from us when they are done.")]
    lines += ["", REPLY_LINE, "", "Thanks for using SketchCast.", "", "SketchCast AI"]
    return subject, "\n".join(lines)


def flush_due_notices(sb, now: Optional[datetime] = None) -> int:
    """Send every owner whose notices are due their one digest. Returns the
    number of digests sent. Rides the worker's housekeeping tick."""
    now = now or datetime.now(timezone.utc)
    sent = 0
    for owner, notices in pending_notices(sb).items():
        try:
            still_open = open_issues(sb, owner)
            if not due(notices, still_open, now):
                continue
            subject, text = digest(notices, still_open)
            ok = bool(notify_owner(sb, owner, subject, text))
            ids = [str(n.get("issue_id") or "") for n in notices]
            if not ok:
                logger.warning("issue digest for %s not sent (%d notice(s)); left for the next tick", owner, len(ids))
                continue
            stamp = now.isoformat()
            sb.table(TABLE).update({"sent_at": stamp}).in_("issue_id", ids).execute()
            for iid in ids:
                try:
                    sb.table("platform_audit_log").insert({
                        "actor_id": None, "action": "issue_notice_sent", "target_kind": "issue", "target_id": iid,
                        "detail": {"owner": owner, "with": [x for x in ids if x != iid], "items": len(_lines(notices)),
                                   "still_open": still_open, "subject": subject},
                    }).execute()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("issue %s: digest audit write failed: %s", iid, exc)
            sent += 1
            logger.info("issue digest sent to %s: %d notice(s), %d item(s), %d still open",
                        owner, len(ids), len(_lines(notices)), still_open)
        except Exception as exc:  # noqa: BLE001 — one owner's trouble never blocks the others
            logger.error("issue digest for %s failed: %s", owner, exc)
    return sent


def what_for(sb, gen: Optional[dict], issue: dict) -> str:
    """"slide deck for \\"Where's my bag?\\"": the item in the owner's words,
    with its book when it has one."""
    from support_agent.agent import _what
    if gen is None:
        # a report made by hand carries no generation; its title names the
        # item ("Test paper failed — Class Eight · Chapter 1 · Part 3")
        title = " ".join(str(issue.get("title") or "").split())
        if " failed" in title:
            kind, _sep, where = title.partition(" failed")
            where = where.lstrip(" —-–:").strip()
            kind = kind.strip().lower()
            if kind:
                return f"{kind} ({where})" if where else kind
    what = _what(gen, issue)
    book_id = (gen or {}).get("book_id") or issue.get("book_id")
    if book_id:
        try:
            res = sb.table("books").select("title").eq("id", book_id).limit(1).execute()
            rows = _rows(res)
            title = " ".join(str(rows[0].get("title") or "").split()) if rows else ""
            if title:
                what = f'{what} for "{title[:80]}"'
        except Exception as exc:  # noqa: BLE001
            logger.info("book title for notice not read: %s", exc)
    return what


__all__ = ["TABLE", "SETTLE_MINUTES", "MAX_HOLD_HOURS", "queue_owner_notice", "pending_notices", "open_issues",
           "due", "digest", "flush_due_notices", "what_for"]
