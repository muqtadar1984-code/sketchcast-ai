"""The daily analytics poll for OUR pins (guide §12, decision D8: our own
pins' daily metrics, nothing about anyone else).

``maybe_poll`` rides the reaper loop and only decides: when work is due it
inserts one ``pin_stats`` job, and that job (last lane) makes every call.
Once per UTC day, after 02:00, the job asks
Pinterest for the last 30 days of IMPRESSION / SAVE / PIN_CLICK /
OUTBOUND_CLICK of every pin posted in the last 90 days (Pinterest's own
window), skipping dry-run pins (``dryrun:<id>`` was never on Pinterest), and
upserts one ``pinterest_pin_stats`` row per pin per day. The data delay is
unverified (probe Q4 could not run on Trial), so the last two days are
``provisional`` and are simply overwritten by tomorrow's poll.

It runs while the breaker is tripped (reading is not posting) but not when
the agent is switched off. A rate limit stops the pass and resumes it after
the reset; a refused token stops it for the day. The same job runs the
token's daily expiry countdown (``tokens.check_expiry``), and on Mondays it
emails the founder the week: the top pins, totals by template and language.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from catalogue.pinterest import common as C
from worker import client as db

log = logging.getLogger("worker.pinterest.stats")

POLL_AFTER_HOUR = 2
WINDOW_DAYS = 90
METRIC_DAYS = 30
PROVISIONAL_DAYS = 2
METRICS = ("IMPRESSION", "SAVE", "PIN_CLICK", "OUTBOUND_CLICK")
COLUMN = {"IMPRESSION": "impression", "SAVE": "save", "PIN_CLICK": "pin_click", "OUTBOUND_CLICK": "outbound_click"}
LOW_REMAINING = 3
TICK_SECONDS = 300

_state = {"last": 0.0, "missing_logged": False}


def _s(v: object) -> str:
    return C._s(v)


def due(stats: dict, now: datetime) -> bool:
    utc = now.astimezone(timezone.utc)
    if utc.hour < POLL_AFTER_HOUR or _s(stats.get("polled_on")) == utc.date().isoformat():
        return False
    resume = C.when(stats.get("resume_after"))
    return resume is None or resume <= now


def pins_to_poll(sb, now: datetime) -> list[dict]:
    since = now - timedelta(days=WINDOW_DAYS)
    out = []
    for p in C.rows(sb.table(C.PINS).select("id,pinterest_pin_id,posted_at,template,language,title")
                    .eq("status", "posted").execute()):
        pid = _s(p.get("pinterest_pin_id"))
        posted = C.when(p.get("posted_at"))
        if pid and not pid.startswith("dryrun:") and posted is not None and posted >= since:
            out.append(p)
    return sorted(out, key=lambda p: _s(p.get("posted_at")))


def daily_rows(pin_row_id: str, body: dict, today: date, now: datetime) -> list[dict]:
    """``pinterest_pin_stats`` rows from one analytics answer."""
    block = body.get("all") if isinstance(body.get("all"), dict) else body
    out = []
    for d in (block.get("daily_metrics") or []) if isinstance(block, dict) else []:
        if not isinstance(d, dict) or not d.get("date"):
            continue
        try:
            day = date.fromisoformat(_s(d.get("date"))[:10])
        except ValueError:
            continue
        metrics = d.get("metrics") if isinstance(d.get("metrics"), dict) else {}
        provisional = (today - day).days < PROVISIONAL_DAYS or _s(d.get("data_status")).upper() not in ("", "READY")
        row = {"pin_row_id": pin_row_id, "day": day.isoformat(), "provisional": provisional, "fetched_at": C.iso(now)}
        for metric, col in COLUMN.items():
            v = metrics.get(metric)
            row[col] = int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        out.append(row)
    return out


def poll(sb, settings: dict, now: datetime, *, api=None) -> str:
    from catalogue.pinterest.api import Api, PinterestError

    today = now.astimezone(timezone.utc).date()
    stats = settings.get("stats") if isinstance(settings.get("stats"), dict) else {}
    pins = pins_to_poll(sb, now)
    written = 0
    try:
        if pins and api is None:
            from catalogue.pinterest import tokens

            api = Api(tokens.access_token(sb, now=now))
        for p in pins:
            posted = C.when(p.get("posted_at")).date()
            start = max(posted, today - timedelta(days=METRIC_DAYS))
            body = api.pin_analytics(_s(p.get("pinterest_pin_id")), start.isoformat(), today.isoformat(), METRICS)
            got = daily_rows(p["id"], body or {}, today, now)
            if got:
                sb.table(C.STATS).upsert(got, on_conflict="pin_row_id,day").execute()
                written += len(got)
            remaining = _s((getattr(api, "last_ratelimit", {}) or {}).get("x-ratelimit-remaining")).split(",")[0]
            if remaining.isdigit() and int(remaining) < LOW_REMAINING:
                raise db.DeferredJob(60, "the analytics rate limit is nearly spent")
    except db.DeferredJob as exc:
        C.merge_settings(sb, {"stats": {**stats, "resume_after": C.iso(now + timedelta(seconds=exc.seconds))}}, now)
        return f"rate limited after {written} row(s); resuming in {exc.seconds}s"
    except PinterestError as exc:
        C.merge_settings(sb, {"stats": {**stats, "polled_on": today.isoformat(), "last_error": str(exc)[:300]}}, now)
        return f"stopped for today: {str(exc)[:200]}"
    C.merge_settings(sb, {"stats": {**stats, "polled_on": today.isoformat(), "pins": len(pins), "rows": written,
                                    "resume_after": None, "last_error": None}}, now)
    return f"polled {len(pins)} pin(s), {written} row(s)"


def weekly_summary(sb, now: datetime) -> Optional[tuple[str, str]]:
    """(subject, text) for the last 7 days, or None when nothing posted."""
    today = now.astimezone(timezone.utc).date()
    since = (today - timedelta(days=7)).isoformat()
    pins = {p["id"]: p for p in C.rows(sb.table(C.PINS).select("id,title,template,language,pinterest_pin_id")
                                       .eq("status", "posted").execute()) if p.get("id")}
    rows = [r for r in C.rows(sb.table(C.STATS).select("*").execute())
            if _s(r.get("day")) >= since and r.get("pin_row_id") in pins]
    if not rows:
        return None
    per_pin: dict[str, dict] = {}
    for r in rows:
        acc = per_pin.setdefault(r["pin_row_id"], {"impression": 0, "save": 0, "outbound_click": 0})
        for k in acc:
            acc[k] += int(r.get(k) or 0)
    def total_by(key):
        out: dict[str, dict] = {}
        for pid, acc in per_pin.items():
            g = out.setdefault(_s(pins[pid].get(key)) or "?", {"impression": 0, "save": 0, "outbound_click": 0})
            for k in g:
                g[k] += acc[k]
        return out
    top = sorted(per_pin.items(), key=lambda kv: (-kv[1]["outbound_click"], -kv[1]["impression"]))[:5]
    lines = [f"Pinterest — the week to {today.isoformat()}", "", "Top pins (outbound clicks, impressions, saves):"]
    lines += [f"  · {pins[pid].get('title') or pid} [{pins[pid].get('language')}, {pins[pid].get('template')}]: "
              f"{a['outbound_click']} / {a['impression']} / {a['save']}" for pid, a in top]
    for label, key in (("By template", "template"), ("By language", "language")):
        lines += ["", f"{label}:"]
        lines += [f"  {k}: {v['impression']} impressions, {v['save']} saves, {v['outbound_click']} outbound clicks"
                  for k, v in sorted(total_by(key).items())]
    lines += ["", C.library_url("/library/pinterest/scheduled")]
    total = sum(a["impression"] for a in per_pin.values())
    return f"Pinterest week: {total} impressions on {len(per_pin)} pin(s)", "\n".join(lines)


def weekly_due(stats: dict, now: datetime) -> bool:
    utc = now.astimezone(timezone.utc)
    today = utc.date().isoformat()
    return utc.weekday() == 0 and _s(stats.get("polled_on")) == today and _s(stats.get("weekly_on")) != today


def token_check_due(settings: dict, now: datetime) -> bool:
    return _s(settings.get("token_checked_on")) != now.astimezone(timezone.utc).date().isoformat()


def work_due(settings: dict, now: datetime) -> bool:
    stats = settings.get("stats") if isinstance(settings.get("stats"), dict) else {}
    return due(stats, now) or weekly_due(stats, now) or token_check_due(settings, now)


def run_stats(sb, now: datetime, *, api=None) -> str:
    """What one ``pin_stats`` job does: the token countdown (once a day),
    the analytics poll when due, Monday's summary when due."""
    settings = C.read_settings(sb)
    if settings.get("enabled") is not True:
        return "off"
    out: list[str] = []
    if token_check_due(settings, now):
        from catalogue.pinterest import tokens

        did = tokens.check_expiry(sb, settings, now)
        C.merge_settings(sb, {"token_checked_on": now.astimezone(timezone.utc).date().isoformat()}, now)
        if did:
            out.append(f"token expiry: {did}")
        settings = C.read_settings(sb)
    stats = settings.get("stats") if isinstance(settings.get("stats"), dict) else {}
    if due(stats, now):
        out.append(poll(sb, settings, now, api=api))
        settings = C.read_settings(sb)
        stats = settings.get("stats") if isinstance(settings.get("stats"), dict) else {}
    if weekly_due(stats, now):
        built = weekly_summary(sb, now)
        C.merge_settings(sb, {"stats": {**stats, "weekly_on": now.astimezone(timezone.utc).date().isoformat()}}, now)
        if built:
            C.send_email(settings, *built)
            out.append("weekly summary sent")
    return "; ".join(out) or "nothing due"


def run_stats_job(sb, job: dict, *, api=None, now: Optional[datetime] = None) -> Optional[dict]:
    """The ``pin_stats`` job (last lane). Yields to builders; finishes its
    own row; raises only ``DeferredJob``."""
    job_id = job["id"]
    try:
        C.yield_to_builders(sb, job_id)
        out = run_stats(sb, now or datetime.now(timezone.utc), api=api)
        summary = {"step": "done", "result": out}
        db.set_stage(sb, job_id, summary)
        db.finish_job(sb, job_id)
        log.info("pinterest stats: %s", out)
        return summary
    except db.DeferredJob:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("pin_stats %s failed: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=f"{type(exc).__name__}: {exc}"[:4000])
        except Exception as exc2:  # noqa: BLE001
            log.error("pin_stats %s: could not record the failure: %s", job_id, exc2)
        return None


def maybe_poll(sb, *, clock=time.monotonic, now: Optional[datetime] = None) -> Optional[str]:
    """The reaper loop's entry: it only DECIDES — when a poll, the token
    countdown or the weekly email is due it inserts one ``pin_stats`` job;
    the network calls run on the last lane. Dark without
    ``PINTEREST_AGENT=1`` and the settings switch; runs while the breaker is
    tripped; never raises; a database without 0154 is logged once."""
    if not C.enabled_by_env():
        return None
    t = clock()
    if _state["last"] and t - _state["last"] < TICK_SECONDS:
        return None
    _state["last"] = t
    now = now or datetime.now(timezone.utc)
    try:
        settings = C.read_settings(sb)
        if settings.get("enabled") is not True or not work_due(settings, now):
            return None
        job = C.queue_once(sb, C.JOB_STATS, {})
        return f"queued pin_stats {job}" if job else None
    except Exception as exc:  # noqa: BLE001
        if C.table_missing(exc):
            if not _state["missing_logged"]:
                _state["missing_logged"] = True
                log.warning("pinterest stats: its tables are not in this database (apply app migration 0154): %s",
                            exc)
            return None
        log.error("pinterest stats tick failed: %s", exc)
        return None


__all__ = ["maybe_poll", "run_stats", "run_stats_job", "work_due", "poll", "due", "pins_to_poll", "daily_rows", "weekly_summary", "METRICS"]
