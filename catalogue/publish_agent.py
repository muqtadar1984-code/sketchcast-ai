"""The YouTube Publishing Agent — posts the translations staff queued, ONE
VIDEO PER LANGUAGE PER DAY (Bulk YouTube Posting plan §2.5, founder
2026-10-21: "post 1 video per language a day (if available). that keeps
uploads consistent", and "the ability to trigger another set of uploads
manually").

WHAT IT IS. Not an uploader: the upload is the ordinary ``topic_publish``
job (catalogue/publish.py), with its gate, its own-channel rule, its
idempotency, its builder waits and its quota waits. This module only
decides WHICH kit gets that job and WHEN, from two tables (app migration
0150):

  youtube_publish_queue   what staff ticked in the Library's YouTube view —
                          one row per kit, oldest first per language;
  youtube_publish_rounds  a ROUND takes the oldest pending kit of every
                          language (at most one each) and posts them one at
                          a time. A 'daily' round starts once per Pacific
                          quota day after ``daily_at`` (default 00:30, just
                          after YouTube's reset) when anything is pending;
                          a 'manual' round is the Library's "Run once more"
                          button (the app inserts it as 'requested').

One round is open at a time (0150's index), and inside it one upload at a
time: a kit's job must finish before the next is queued. So the agent never
competes with itself, and it never starts an upload while a teacher's job
is live (the never-starve probe, the same one topic_publish re-checks).

WHEN IT RUNS. ``maybe_tick`` rides the worker's reaper loop (run.py) every
minute. Dark unless ``YOUTUBE_PUBLISH_AGENT=1`` in the worker's environment,
and paused while ``platform_settings.youtube_publish_agent.paused`` is true
(the Library's Pause button). A database without 0150 is logged once.

OUTCOMES. An item's job ``done`` with a video on YouTube → ``done``. A job
``error`` → back to ``pending`` (out of this round, so it waits for the next
one) until it has failed ``MAX_ATTEMPTS`` times, then ``failed`` with the
job's message. A kit that is already on YouTube by another path → ``done``;
a language with no channel → ``skipped``. Every job the agent queues writes
the same ``platform_audit_log`` row the Library's Post button writes, with
the staff member who queued the kit as the actor.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from catalogue import publish as P

log = logging.getLogger("worker.publish_agent")

ENV = "YOUTUBE_PUBLISH_AGENT"
SETTINGS_KEY = "youtube_publish_agent"
QUEUE = "youtube_publish_queue"
ROUNDS = "youtube_publish_rounds"
TICK_SECONDS = 55
MAX_ATTEMPTS = 3
DEFAULT_DAILY_AT = "00:30"   # Pacific, just after the quota reset

OPEN_ITEM = ("pending", "selected", "queued")
LIVE_JOB = ("queued", "processing")

_state = {"last": 0.0, "missing_logged": False}


def _s(v: object) -> str:
    return " ".join(str(v or "").split())


def _rows(res) -> list[dict]:
    return list(getattr(res, "data", None) or [])


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def enabled_by_env() -> bool:
    return os.getenv(ENV, "").strip().lower() in ("1", "true", "yes", "on")


def read_settings(sb) -> dict:
    rows = _rows(sb.table("platform_settings").select("value").eq("key", SETTINGS_KEY).limit(1).execute())
    value = rows[0].get("value") if rows else None
    return value if isinstance(value, dict) else {}


def daily_minutes(settings: dict) -> int:
    """``daily_at`` ("HH:MM", Pacific) as minutes after midnight; nonsense
    falls back to the default."""
    raw = _s(settings.get("daily_at")) or DEFAULT_DAILY_AT
    try:
        h, m = (int(x) for x in raw.split(":", 1))
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except ValueError:
        pass
    h, m = (int(x) for x in DEFAULT_DAILY_AT.split(":"))
    return h * 60 + m


def daily_due(settings: dict, now: datetime) -> bool:
    """Whether today's daily round may start: the Pacific clock is past
    ``daily_at``. (Whether it already ran is the database's question.)"""
    local = P._pacific(now)
    return local.hour * 60 + local.minute >= daily_minutes(settings)


def privacy() -> str:
    """Founder 2026-10-21: public once the compliance audit is passed;
    until then an upload can only be private."""
    return P.PRIVACY_PUBLIC if P.audit_passed() else P.PRIVACY_PRIVATE


# ── reads ──────────────────────────────────────────────────────────────


def open_round(sb) -> Optional[dict]:
    rows = _rows(sb.table(ROUNDS).select("*").in_("status", ["requested", "running"])
                 .order("created_at").limit(1).execute())
    return rows[0] if rows else None


def has_daily_round(sb, day: str) -> bool:
    return bool(_rows(sb.table(ROUNDS).select("id").eq("trigger", "daily").eq("quota_day", day).limit(1).execute()))


def pending_items(sb) -> list[dict]:
    return _rows(sb.table(QUEUE).select("*").eq("status", "pending").order("added_at").execute())


def round_items(sb, round_id: str) -> list[dict]:
    return _rows(sb.table(QUEUE).select("*").eq("round_id", round_id).order("added_at").execute())


def publish_job_live(sb) -> bool:
    return bool(_rows(sb.table("jobs").select("id").eq("type", P.JOB_TYPE).in_("status", list(LIVE_JOB))
                      .limit(1).execute()))


def job_row(sb, job_id: str) -> Optional[dict]:
    if not job_id:
        return None
    rows = _rows(sb.table("jobs").select("id,status,error,stage").eq("id", job_id).limit(1).execute())
    return rows[0] if rows else None


def kit_posted(sb, kit_id: str) -> bool:
    """On YouTube already, on any channel (catalogue.publish's own rule)."""
    return any(_s(r.get("youtube_video_id")) for r in P.load_kit_publications(sb, kit_id))


# ── writes ─────────────────────────────────────────────────────────────


def _update_item(sb, item_id: str, fields: dict, now: datetime) -> None:
    sb.table(QUEUE).update({**fields, "updated_at": _iso(now)}).eq("id", item_id).execute()


def oldest_per_language(items: list[dict]) -> list[dict]:
    """At most one item per language: the oldest pending one. Pure."""
    seen: set[str] = set()
    out: list[dict] = []
    for it in sorted(items, key=lambda r: (_s(r.get("added_at")), _s(r.get("id")))):
        lang = _s(it.get("language")).lower()
        if lang and lang not in seen:
            seen.add(lang)
            out.append(it)
    return out


def create_daily_round(sb, day: str, now: datetime) -> Optional[dict]:
    """Insert today's daily round as 'requested'. Two workers racing get one
    row (0150's unique index on the daily quota day); the loser's 23505 is
    read as "already there"."""
    try:
        res = sb.table(ROUNDS).insert({"trigger": "daily", "quota_day": day, "status": "requested",
                                       "created_at": _iso(now)}).execute()
        rows = _rows(res)
        return rows[0] if rows else None
    except Exception as exc:  # noqa: BLE001
        if "23505" in str(exc) or "duplicate" in str(exc).lower():
            return None
        raise


def start_round(sb, rnd: dict, now: datetime) -> dict:
    """Pick the round's kits — the oldest pending of each language — and mark
    them 'selected'. A round with nothing to pick finishes at once."""
    picked = oldest_per_language(pending_items(sb))
    for it in picked:
        _update_item(sb, it["id"], {"status": "selected", "round_id": rnd["id"]}, now)
    fields = {"status": "running" if picked else "done", "started_at": _iso(now),
              "summary": {"selected": len(picked), "languages": [_s(i.get("language")) for i in picked]}}
    if not picked:
        fields["finished_at"] = _iso(now)
    sb.table(ROUNDS).update(fields).eq("id", rnd["id"]).execute()
    log.info("publish agent: %s round %s started with %d kit(s): %s", rnd.get("trigger"), rnd["id"], len(picked),
             ", ".join(_s(i.get("language")) for i in picked) or "nothing pending")
    return {**rnd, **fields}


def queue_job(sb, item: dict, rnd: dict, now: datetime) -> Optional[str]:
    """The same row the Library's Post button inserts, plus the queue item's
    id, and the same audit row. None when the kit already has a live publish
    job (0116's index): the item stays 'selected' and is tried next tick."""
    kit = P.load_kit(sb, _s(item.get("kit_id"))) or {}
    topic_id = _s(item.get("topic_id")) or _s(kit.get("topic_id"))
    language = _s(item.get("language")) or _s(kit.get("language"))
    params = {"kit_id": _s(item.get("kit_id")), "topic_id": topic_id, "language": language,
              "privacy": privacy(), "queue_item_id": item["id"], "round_id": rnd["id"]}
    try:
        res = sb.table("jobs").insert({"type": P.JOB_TYPE, "params": params, "book_id": None,
                                       "generation_id": None, "status": "queued"}).execute()
    except Exception as exc:  # noqa: BLE001
        if "23505" in str(exc) or "duplicate" in str(exc).lower():
            log.info("publish agent: kit %s already has a live publish job; waiting", params["kit_id"])
            return None
        raise
    job_id = _s((_rows(res) or [{}])[0].get("id"))
    _update_item(sb, item["id"], {"status": "queued", "job_id": job_id or None}, now)
    try:
        sb.table("platform_audit_log").insert({
            "actor_id": item.get("added_by"), "action": "library_publish", "target_kind": "topic",
            "target_id": topic_id or None,
            "detail": {"kit_id": params["kit_id"], "job_id": job_id, "action": "publish", "language": language,
                       "privacy": params["privacy"], "via": "youtube publish agent", "round_id": rnd["id"],
                       "trigger": rnd.get("trigger"), "queue_item_id": item["id"]}}).execute()
    except Exception as exc:  # noqa: BLE001 — the audit never blocks the post
        log.warning("publish agent: audit row for kit %s not written: %s", params["kit_id"], exc)
    log.info("publish agent: queued %s (%s) as job %s", params["kit_id"], language, job_id)
    return job_id


def settle(sb, item: dict, now: datetime) -> str:
    """Read the outcome of a queued item's job: 'done', 'retry', 'failed',
    or 'waiting' while the job is still live."""
    job = job_row(sb, _s(item.get("job_id")))
    status = _s((job or {}).get("status"))
    if status in LIVE_JOB:
        return "waiting"
    if status == "done" and kit_posted(sb, _s(item.get("kit_id"))):
        _update_item(sb, item["id"], {"status": "done", "last_error": None}, now)
        return "done"
    # error, a missing job row, or 'done' with nothing on YouTube (a run that
    # finished paused): not posted, so it goes round again or gives up
    error = _s((job or {}).get("error")) or (
        _s(((job or {}).get("stage") or {}).get("note")) if job else "") or "the publish job finished without a video"
    attempts = int(item.get("attempts") or 0) + 1
    if attempts >= MAX_ATTEMPTS:
        _update_item(sb, item["id"], {"status": "failed", "attempts": attempts, "last_error": error[:1000]}, now)
        return "failed"
    _update_item(sb, item["id"], {"status": "pending", "attempts": attempts, "round_id": None, "job_id": None,
                                  "last_error": error[:1000]}, now)
    return "retry"


def advance(sb, rnd: dict, now: datetime) -> str:
    """One step of a running round. Returns what happened, for the log."""
    items = round_items(sb, rnd["id"])
    for it in items:
        if it.get("status") == "queued":
            outcome = settle(sb, it, now)
            if outcome == "waiting":
                return "waiting: an upload is running"
    items = round_items(sb, rnd["id"])
    nxt = next((it for it in items if it.get("status") == "selected"), None)
    if nxt is None:
        counts: dict[str, int] = {}
        for it in items:
            counts[_s(it.get("status"))] = counts.get(_s(it.get("status")), 0) + 1
        sb.table(ROUNDS).update({"status": "done", "finished_at": _iso(now),
                                 "summary": {**(rnd.get("summary") or {}), "outcome": counts}}).eq("id", rnd["id"]).execute()
        log.info("publish agent: round %s done: %s", rnd["id"], counts)
        return "round done"
    kit_id = _s(nxt.get("kit_id"))
    if kit_posted(sb, kit_id):
        _update_item(sb, nxt["id"], {"status": "done", "last_error": None}, now)
        return f"{kit_id} was already on YouTube"
    language = _s(nxt.get("language"))
    if language != P.DEFAULT_LANGUAGE and not P.own_channel(language):
        _update_item(sb, nxt["id"], {"status": "skipped", "last_error": P.no_channel_why(language)}, now)
        return f"{kit_id} skipped: no {language} channel"
    if P.builders_are_queued(sb):
        return "waiting: a teacher's job is live"
    if publish_job_live(sb):
        return "waiting: another publish job is live"
    job_id = queue_job(sb, nxt, rnd, now)
    return f"queued {kit_id} as {job_id}" if job_id else "waiting: the kit has a live publish job"


def tick(sb, now: Optional[datetime] = None) -> Optional[str]:
    """One decision. None when there was nothing to do."""
    now = now or datetime.now(timezone.utc)
    settings = read_settings(sb)
    if settings.get("paused"):
        return None
    rnd = open_round(sb)
    if rnd is None:
        day = P.quota_day(now).isoformat()
        if daily_due(settings, now) and not has_daily_round(sb, day) and pending_items(sb):
            rnd = create_daily_round(sb, day, now)
        if rnd is None:
            return None
    if rnd.get("status") == "requested":
        rnd = start_round(sb, rnd, now)
        if rnd.get("status") == "done":
            return "round done: nothing pending"
    return advance(sb, rnd, now)


def maybe_tick(sb, *, clock=time.monotonic, now: Optional[datetime] = None) -> Optional[str]:
    """The reaper loop's entry: at most once per TICK_SECONDS, dark without
    the env flag, and never raising."""
    if not enabled_by_env():
        return None
    t = clock()
    if _state["last"] and t - _state["last"] < TICK_SECONDS:
        return None
    _state["last"] = t
    try:
        out = tick(sb, now=now)
        if out:
            log.info("publish agent: %s", out)
        return out
    except Exception as exc:  # noqa: BLE001
        text = str(exc).lower()
        if any(m in text for m in ("does not exist", "schema cache", "pgrst205", "could not find")):
            if not _state["missing_logged"]:
                _state["missing_logged"] = True
                log.warning("publish agent: its tables are not in this database (apply app migration 0150): %s", exc)
            return None
        log.error("publish agent tick failed: %s", exc)
        return None


__all__ = ["ENV", "SETTINGS_KEY", "QUEUE", "ROUNDS", "MAX_ATTEMPTS", "DEFAULT_DAILY_AT", "enabled_by_env",
           "read_settings", "daily_minutes", "daily_due", "privacy", "oldest_per_language", "start_round",
           "queue_job", "settle", "advance", "tick", "maybe_tick"]
