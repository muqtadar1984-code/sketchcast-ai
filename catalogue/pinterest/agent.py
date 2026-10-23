"""The Pinterest agent's tick: it decides WHAT gets drafted and posted, and
WHEN; the jobs (``pin_boards``, ``pin_draft``, ``pin_post``) do the work. The
same shape as the YouTube Publishing Agent (``catalogue/publish_agent.py``),
with Pinterest's one hard difference: a human approves every pin, so the
agent only ever posts what an admin chose (Developer Guidelines: "the end
user must choose each Pin to be published").

WHEN IT RUNS. ``maybe_tick`` rides the worker's reaper loop, at most every
55 s. Dark without ``PINTEREST_AGENT=1``; then dark while
``platform_settings.pinterest_agent.enabled`` is not true. A tripped breaker
leaves only the stats poll running (``stats.maybe_poll``, its own tick);
paused does nothing; a live user build does nothing (never starve).

EACH TICK, in order:
  1. a ``pin_boards`` job when a board is approved and not yet created;
  2. re-checks of pins a Library edit left without ``checked_at`` (inline:
     no model call);
  3. the drafting feeder: at most ONE live ``pin_draft``, and only for a
     language with fewer than ``max_waiting_per_language`` pins in review or
     approved — the review queue never floods. A pin the Library sent back
     to ``draft`` (Re-render) is redrawn before a new topic is started;
  4. rounds: today's ``daily`` round after ``daily_at`` (UTC, default 21:00,
     inside the off-peak window; 0154's one-daily index makes a race a
     23505), or a ``requested`` manual round ("Run once more"). A round
     schedules, per language, up to ``per_language_per_day`` of the OLDEST
     approved pins, never two of one topic and never a topic posted in that
     language within ``min_topic_gap_days`` — the app's ``nextRoundPreview``
     rule, so the panel shows what the round will do;
  5. posting: one ``pin_post`` at a time, ``post_gap_minutes`` apart;
  6. settling: a round with nothing scheduled or posting is ``done``;
  7. the breaker: three post failures in a row trip it (pause + email).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from catalogue.pinterest import common as C

log = logging.getLogger("worker.pinterest.agent")

TICK_SECONDS = 55
DEFAULT_DAILY_AT = "21:00"
REDRAFT_COOLDOWN_DAYS = 7
OPEN_ROUND = ("requested", "running")
IN_ROUND = ("scheduled", "posting")

_state = {"last": 0.0, "missing_logged": False}


def _s(v: object) -> str:
    return C._s(v)


# ── pure rules ──────────────────────────────────────────────────────────


def daily_minutes(settings: dict) -> int:
    """``daily_at`` ("HH:MM", UTC) as minutes after midnight; nonsense falls
    back to the default (the app's ``dailyMinutes``)."""
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
    utc = now.astimezone(timezone.utc)
    return utc.hour * 60 + utc.minute >= daily_minutes(settings)


def topic_gap_ok(last_posted: Optional[str], now: datetime, gap_days: int) -> bool:
    t = C.when(last_posted) if last_posted else None
    return t is None or now - t >= timedelta(days=gap_days)


def select_for_round(approved: list[dict], last_posted: dict[str, str], settings: dict, now: datetime) -> list[dict]:
    """Per language, the oldest approved pins up to ``per_language_per_day``,
    skipping a topic inside its gap or already taken this round. Pure."""
    per = C.int_setting(settings, "per_language_per_day", 1, 5)
    gap = C.int_setting(settings, "min_topic_gap_days", 0, 365)
    picked: list[dict] = []
    for language in C.languages(settings):
        mine = sorted((p for p in approved if _s(p.get("language")).lower() == language.lower()),
                      key=lambda p: (_s(p.get("approved_at")), _s(p.get("id"))))
        taken: set[str] = set()
        n = 0
        for p in mine:
            topic = _s(p.get("topic_id"))
            if topic and (topic in taken or not topic_gap_ok(last_posted.get(f"{topic}|{language}"), now, gap)):
                continue
            if n >= per:
                break
            picked.append(p)
            n += 1
            if topic:
                taken.add(topic)
    return picked


# ── reads ───────────────────────────────────────────────────────────────


def open_round(sb) -> Optional[dict]:
    found = C.rows(sb.table(C.ROUNDS).select("*").in_("status", list(OPEN_ROUND)).limit(5).execute())
    return sorted(found, key=lambda r: _s(r.get("created_at")))[0] if found else None


def has_daily_round(sb, day: str) -> bool:
    return bool(C.rows(sb.table(C.ROUNDS).select("id").eq("trigger", "daily").eq("day", day).limit(1).execute()))


def pins_with(sb, statuses, cols: str = "*") -> list[dict]:
    return C.rows(sb.table(C.PINS).select(cols).in_("status", list(statuses)).execute())


def last_posted_map(sb) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in pins_with(sb, ["posted"], "id,topic_id,language,posted_at"):
        if not p.get("topic_id") or not p.get("posted_at"):
            continue
        k = f"{_s(p.get('topic_id'))}|{_s(p.get('language')).lower()}"
        if _s(p.get("posted_at")) > out.get(k, ""):
            out[k] = _s(p.get("posted_at"))
    return out


def last_post_time(sb) -> Optional[datetime]:
    times = [C.when(p.get("posted_at")) for p in pins_with(sb, ["posted"], "id,posted_at")]
    times = [t for t in times if t is not None]
    return max(times) if times else None


def recently_drafted_topics(sb, language: str, now: datetime) -> set[str]:
    """Topics a ``pin_draft`` ran for in the last week: a topic that yields no
    pin (no board yet, nothing pinnable) is not re-drafted every tick."""
    found = C.rows(sb.table("jobs").select("params,created_at,status").eq("type", C.JOB_DRAFT).limit(1000).execute())
    since = now - timedelta(days=REDRAFT_COOLDOWN_DAYS)
    out = set()
    for j in found:
        p = j.get("params") if isinstance(j.get("params"), dict) else {}
        made = C.when(j.get("created_at"))
        if _s(p.get("language")).lower() == language.lower() and (made is None or made >= since):
            out.add(_s(p.get("topic_id")))
    return out


# ── steps ───────────────────────────────────────────────────────────────


def queue_boards(sb) -> Optional[str]:
    approved = C.rows(sb.table(C.BOARDS).select("id").eq("status", "approved").limit(1).execute())
    if not approved or C.live_jobs(sb, C.JOB_BOARDS):
        return None
    job = C.queue_job(sb, C.JOB_BOARDS, {})
    return f"queued pin_boards {job}" if job else None


def feed(sb, settings: dict, now: datetime) -> Optional[str]:
    """At most one ``pin_draft`` live at a time, for the first language
    under its waiting cap."""
    from catalogue.kit import catalogue_window_open
    from catalogue.pinterest import sources as S

    if not catalogue_window_open(now) or C.live_jobs(sb, C.JOB_DRAFT):
        return None
    cap = C.int_setting(settings, "max_waiting_per_language", 1, 500)
    open_pins = pins_with(sb, ["draft", "in_review", "approved"], "id,topic_id,language,status,updated_at")
    for language in C.languages(settings):
        mine = [p for p in open_pins if _s(p.get("language")).lower() == language.lower()]
        waiting = sum(1 for p in mine if p.get("status") in ("in_review", "approved"))
        if waiting >= cap:
            continue
        redraw = sorted((p for p in mine if p.get("status") == "draft" and p.get("topic_id")),
                        key=lambda p: _s(p.get("updated_at")))
        topic = _s(redraw[0].get("topic_id")) if redraw else None
        if not topic:
            boards = C.rows(sb.table(C.BOARDS).select("id").eq("language", language).eq("status", "created")
                            .limit(1).execute())
            if not boards:
                continue          # nothing can be pinned in a language with no board yet
            skip = recently_drafted_topics(sb, language, now)
            topic = S.next_topic(sb, language, settings, exclude=skip)
        if not topic:
            continue
        job = C.queue_job(sb, C.JOB_DRAFT, {"topic_id": topic, "language": language})
        if job:
            return f"queued pin_draft {topic} ({language})"
    return None


def create_daily_round(sb, day: str, now: datetime) -> Optional[dict]:
    try:
        res = sb.table(C.ROUNDS).insert({"trigger": "daily", "day": day, "status": "requested",
                                         "created_at": C.iso(now)}).execute()
    except Exception as exc:  # noqa: BLE001
        if C.is_duplicate(exc):
            return None       # another worker made today's (or a round is open)
        raise
    found = C.rows(res)
    return found[0] if found else None


def start_round(sb, rnd: dict, settings: dict, now: datetime) -> dict:
    approved = [p for p in pins_with(sb, ["approved"]) if _s(p.get("language")).lower()
                in {x.lower() for x in C.languages(settings)}]
    picked = select_for_round(approved, last_posted_map(sb), settings, now)
    for p in picked:
        sb.table(C.PINS).update({"status": "scheduled", "round_id": rnd["id"], "updated_at": C.iso(now)}).eq(
            "id", p["id"]).eq("status", "approved").execute()
    fields = {"status": "running" if picked else "done", "started_at": C.iso(now),
              "summary": {"scheduled": len(picked), "pins": [p["id"] for p in picked],
                          "languages": sorted({_s(p.get("language")) for p in picked}),
                          "dry_run": C.dry_run(settings)}}
    if not picked:
        fields["finished_at"] = C.iso(now)
    sb.table(C.ROUNDS).update(fields).eq("id", rnd["id"]).execute()
    log.info("pinterest agent: %s round %s scheduled %d pin(s)", rnd.get("trigger"), rnd["id"], len(picked))
    return {**rnd, **fields}


def advance(sb, rnd: dict, settings: dict, now: datetime) -> str:
    pins = C.rows(sb.table(C.PINS).select("*").eq("round_id", rnd["id"]).execute())
    if any(p.get("status") == "posting" for p in pins) or C.live_jobs(sb, C.JOB_POST):
        return "waiting: a post is running"
    scheduled = sorted((p for p in pins if p.get("status") == "scheduled"),
                       key=lambda p: (_s(p.get("approved_at")), _s(p.get("id"))))
    if not scheduled:
        counts: dict[str, int] = {}
        for p in pins:
            counts[_s(p.get("status"))] = counts.get(_s(p.get("status")), 0) + 1
        sb.table(C.ROUNDS).update({"status": "done", "finished_at": C.iso(now),
                                   "summary": {**(rnd.get("summary") or {}), "outcome": counts}}).eq(
            "id", rnd["id"]).execute()
        log.info("pinterest agent: round %s done: %s", rnd["id"], counts)
        return "round done"
    gap = C.int_setting(settings, "post_gap_minutes", 0, 1440)
    last = last_post_time(sb)
    if last is not None and now - last < timedelta(minutes=gap):
        return "waiting: the post gap"
    nxt = scheduled[0]
    job = C.queue_job(sb, C.JOB_POST, {"pin_row_id": nxt["id"], "round_id": rnd["id"]})
    if not job:
        return "waiting: a pin_post is already live"
    sb.table(C.PINS).update({"post_job_id": job, "updated_at": C.iso(now)}).eq("id", nxt["id"]).execute()
    return f"queued pin_post {nxt['id']} as {job}"


def rounds(sb, settings: dict, now: datetime) -> Optional[str]:
    rnd = open_round(sb)
    if rnd is None:
        day = now.astimezone(timezone.utc).date().isoformat()
        if not daily_due(settings, now) or has_daily_round(sb, day):
            return None
        langs = {x.lower() for x in C.languages(settings)}
        if not any(_s(p.get("language")).lower() in langs for p in pins_with(sb, ["approved"], "id,language")):
            return None
        rnd = create_daily_round(sb, day, now)
        if rnd is None:
            return None
    if rnd.get("status") == "requested":
        rnd = start_round(sb, rnd, settings, now)
        if rnd.get("status") == "done":
            return "round done: nothing to schedule"
    return advance(sb, rnd, settings, now)


def tick(sb, now: Optional[datetime] = None, *, link_check=None) -> Optional[str]:
    """One pass. None when the agent is off or had nothing to do."""
    now = now or datetime.now(timezone.utc)
    settings = C.read_settings(sb)
    if settings.get("enabled") is not True:
        return None
    br = settings.get("breaker") if isinstance(settings.get("breaker"), dict) else {}
    if br.get("tripped"):
        return "breaker tripped: stats only"
    if int(br.get("failures") or 0) >= C.BREAKER_FAILURES:
        C.trip_breaker(sb, f"{br.get('failures')} post failures in a row; last: {br.get('last_failure') or '?'}", now)
        return "breaker tripped"
    try:
        from catalogue.pinterest import tokens

        if tokens.check_expiry(sb, settings, now) == "pause":
            return "paused: the token is about to expire"
    except Exception as exc:  # noqa: BLE001 — the countdown never stops a tick
        log.warning("pinterest agent: token expiry check failed: %s", exc)
    if settings.get("paused"):
        return None
    if C.builders_queued(sb):
        return "waiting: a teacher's job is live"

    from catalogue.pinterest.draft import recheck_pending

    done = [x for x in (queue_boards(sb),) if x]
    n = recheck_pending(sb, link_check=link_check, limit=2, now=now)
    if n:
        done.append(f"re-checked {n} pin(s)")
    fed = feed(sb, settings, now)
    if fed:
        done.append(fed)
    r = rounds(sb, settings, now)
    if r:
        done.append(r)
    return "; ".join(done) or None


def maybe_tick(sb, *, clock=time.monotonic, now: Optional[datetime] = None) -> Optional[str]:
    """The reaper loop's entry: at most once per TICK_SECONDS, dark without
    the env flag, never raising. A database without 0154 is logged once."""
    if not C.enabled_by_env():
        return None
    t = clock()
    if _state["last"] and t - _state["last"] < TICK_SECONDS:
        return None
    _state["last"] = t
    try:
        out = tick(sb, now=now)
        if out:
            log.info("pinterest agent: %s", out)
        return out
    except Exception as exc:  # noqa: BLE001
        if C.table_missing(exc):
            if not _state["missing_logged"]:
                _state["missing_logged"] = True
                log.warning("pinterest agent: its tables are not in this database (apply app migration 0154): %s",
                            exc)
            return None
        log.error("pinterest agent tick failed: %s", exc)
        return None


__all__ = ["tick", "maybe_tick", "select_for_round", "daily_minutes", "daily_due", "topic_gap_ok", "start_round",
           "advance", "rounds", "feed", "queue_boards", "create_daily_round", "TICK_SECONDS"]
