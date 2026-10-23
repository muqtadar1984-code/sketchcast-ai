"""What every part of the Pin Engine shares: the names in migration 0154, the
founder's settings row, job inserts, the pin's link, and the breaker.

The settings row (``platform_settings.pinterest_agent``) is written by BOTH
sides: the Library writes ``paused`` / ``breaker`` (Pause, Resume, Clear
breaker) by spreading the value it read, and the worker writes ``paused``,
``breaker`` and its own bookkeeping keys (``token_alerted_on``, ``stats``).
So every worker write is a MERGE of a few keys into a fresh read, never a
replace of the value it read a tick ago.
"""

from __future__ import annotations

import hashlib
import logging
import os
import urllib.parse
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger("worker.pinterest")

ENV = "PINTEREST_AGENT"
SETTINGS_KEY = "pinterest_agent"
BOARDS = "pinterest_boards"
PINS = "pinterest_pins"
ROUNDS = "pinterest_publish_rounds"
STATS = "pinterest_pin_stats"
OAUTH = "pinterest_oauth"
BUCKET = "pinterest-pins"
AUDIT = "platform_audit_log"

JOB_DRAFT = "pin_draft"
JOB_POST = "pin_post"
JOB_BOARDS = "pin_boards"
JOB_RECHECK = "pin_recheck"    # re-check edited pins (link HEAD, storage read)
JOB_STATS = "pin_stats"        # analytics poll, token expiry, the weekly email
JOB_TYPES = (JOB_DRAFT, JOB_POST, JOB_BOARDS, JOB_RECHECK, JOB_STATS)
LIVE_JOB = ("queued", "processing")

FORMAT_STATIC = "static"
LINK_BASE = "https://sketchcast.app"
UTM_STATIC = {"utm_source": "pinterest", "utm_medium": "organic", "utm_campaign": "pin_engine"}

BUILDER_WAIT_SECONDS = 120
WAITING_BUILDERS = "waiting for builders"
BREAKER_FAILURES = 3
MAX_ATTEMPTS = 3
LINK_TIMEOUT = 10

# 0154's seeded value: what an absent key means
DEFAULTS = {"enabled": False, "dry_run": True, "paused": False, "daily_at": "21:00", "per_language_per_day": 1,
            "languages": ["en"], "min_topic_gap_days": 7, "post_gap_minutes": 5, "max_waiting_per_language": 14,
            "summary_email": None, "breaker": {"tripped": False, "failures": 0}}

MISSING_MARKERS = ("does not exist", "schema cache", "pgrst205", "could not find", "42p01")


def _s(v: object) -> str:
    return " ".join(str(v or "").split())


def rows(res) -> list[dict]:
    data = getattr(res, "data", None)
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def when(v: object) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(_s(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def enabled_by_env() -> bool:
    return os.getenv(ENV, "").strip().lower() in ("1", "true", "yes", "on")


def is_duplicate(exc: BaseException) -> bool:
    """Postgres 23505 as postgrest surfaces it."""
    return str(getattr(exc, "code", "") or "") == "23505" or "23505" in str(exc) or "duplicate" in str(exc).lower()


def table_missing(exc: BaseException) -> bool:
    return any(m in str(exc).lower() for m in MISSING_MARKERS)


# ── settings ────────────────────────────────────────────────────────────


def read_settings(sb) -> dict:
    """The founder's row merged over 0154's defaults. A row that is not an
    object reads as the defaults — which are OFF."""
    found = rows(sb.table("platform_settings").select("value").eq("key", SETTINGS_KEY).limit(1).execute())
    value = found[0].get("value") if found else None
    return {**DEFAULTS, **(value if isinstance(value, dict) else {})}


def merge_settings(sb, fields: dict, now: datetime) -> dict:
    """Write ``fields`` into the row, keeping every other key as the database
    holds it NOW (the Library may have written since this tick read it)."""
    found = rows(sb.table("platform_settings").select("value").eq("key", SETTINGS_KEY).limit(1).execute())
    value = found[0].get("value") if found and isinstance(found[0].get("value"), dict) else {}
    merged = {**value, **fields}
    sb.table("platform_settings").upsert({"key": SETTINGS_KEY, "value": merged, "updated_at": iso(now)},
                                         on_conflict="key").execute()
    return {**DEFAULTS, **merged}


def dry_run(settings: dict) -> bool:
    """True unless the founder wrote ``dry_run: false`` — anything else, a
    string included, keeps the engine from posting."""
    return settings.get("dry_run") is not False


def languages(settings: dict) -> list[str]:
    langs = [_s(x) for x in (settings.get("languages") or []) if _s(x)]
    return langs or ["en"]


def int_setting(settings: dict, key: str, lo: int, hi: int) -> int:
    try:
        v = int(settings.get(key) if settings.get(key) is not None else DEFAULTS[key])
    except (TypeError, ValueError):
        v = int(DEFAULTS[key])
    return max(lo, min(hi, v))


# ── email and the breaker ───────────────────────────────────────────────


def send_email(settings: dict, subject: str, text: str) -> bool:
    """Through the Auto Translate Agent's sender (Resend), to the founder's
    ``summary_email``. Never raises; never carries a token."""
    try:
        from catalogue.auto_translate import send_email as _send

        return _send(_s(settings.get("summary_email")), subject, text)
    except Exception as exc:  # noqa: BLE001
        log.warning("pinterest: email not sent (%s): %s", exc, subject)
        return False


def library_url(path: str = "/library/pinterest") -> str:
    try:
        from catalogue.auto_translate import library_url as _url

        return _url(path)
    except Exception:  # noqa: BLE001
        return "https://library.sketchcast.app" + path


def trip_breaker(sb, reason: str, now: datetime, *, failures: Optional[int] = None, notify: bool = True) -> dict:
    """Stop all posting: breaker tripped, agent paused, the founder emailed.
    Nothing resumes it but the Library's Clear breaker / Resume."""
    current = read_settings(sb)
    br = current.get("breaker") if isinstance(current.get("breaker"), dict) else {}
    fails = int(br.get("failures") or 0) if failures is None else failures
    settings = merge_settings(sb, {"paused": True, "pause_reason": "breaker",
                                   "breaker": {"tripped": True, "failures": fails, "reason": reason[:300],
                                               "tripped_at": iso(now)}}, now)
    log.error("pinterest: BREAKER TRIPPED — %s", reason)
    if not notify:
        return settings
    send_email(settings, "Pinterest agent PAUSED: breaker tripped",
               f"The Pinterest agent stopped posting.\n\n{reason}\n\n"
               f"Nothing posts until you clear the breaker and resume on {library_url('/library/pinterest/scheduled')}.")
    return settings


def count_failure(sb, now: datetime, reason: str) -> int:
    """One more post failure in a row; the agent trips at BREAKER_FAILURES."""
    current = read_settings(sb)
    br = current.get("breaker") if isinstance(current.get("breaker"), dict) else {}
    fails = int(br.get("failures") or 0) + 1
    merge_settings(sb, {"breaker": {**br, "failures": fails, "last_failure": reason[:300]}}, now)
    return fails


def reset_failures(sb, now: datetime) -> None:
    current = read_settings(sb)
    br = current.get("breaker") if isinstance(current.get("breaker"), dict) else {}
    if int(br.get("failures") or 0) and not br.get("tripped"):
        merge_settings(sb, {"breaker": {"tripped": False, "failures": 0}}, now)


# ── jobs ────────────────────────────────────────────────────────────────


def queue_job(sb, job_type: str, params: dict) -> Optional[str]:
    """A job row straight into ``jobs`` (no book, no generation), as the
    YouTube agent does. None when 0154's one-live index says one is already
    live (23505)."""
    try:
        res = sb.table("jobs").insert({"type": job_type, "params": params, "book_id": None,
                                       "generation_id": None, "status": "queued"}).execute()
    except Exception as exc:  # noqa: BLE001
        if is_duplicate(exc):
            log.info("pinterest: a %s job is already live (%s)", job_type, params)
            return None
        raise
    return _s((rows(res) or [{}])[0].get("id")) or None


def queue_once(sb, job_type: str, params: dict) -> Optional[str]:
    """A job of a type 0154 has no one-live index for: inserted only when
    none is queued or processing. (Two replicas racing can both insert; the
    job itself is idempotent, so a second run finds nothing to do.)"""
    if live_jobs(sb, job_type):
        return None
    return queue_job(sb, job_type, params)


def live_jobs(sb, job_type: str) -> list[dict]:
    return rows(sb.table("jobs").select("id,type,status,params").eq("type", job_type)
                .in_("status", list(LIVE_JOB)).limit(20).execute())


def builders_queued(sb, exclude_job_id: Optional[str] = None) -> bool:
    from catalogue.figures import builder_queued

    return builder_queued(sb, exclude_job_id=exclude_job_id)


def yield_to_builders(sb, job_id: Optional[str] = None) -> None:
    """THE never-starve rule: a real user's build is live, so this job goes
    back to the queue without spending an attempt."""
    from worker import client as db

    if builders_queued(sb, exclude_job_id=job_id):
        raise db.DeferredJob(BUILDER_WAIT_SECONDS, WAITING_BUILDERS)


# ── links ───────────────────────────────────────────────────────────────


def canonical_url(slug: str, language: str) -> str:
    """The topic's page in the pin's language (guide §7)."""
    lang = _s(language).lower() or "en"
    return f"{LINK_BASE}/topics/{slug}" if lang == "en" else f"{LINK_BASE}/{lang}/topics/{slug}"


def utm_content(fmt: str, pin_id: str) -> str:
    return f"{fmt}-{str(pin_id).replace('-', '')[:8]}"


def utm_for(fmt: str, pin_id: str, language: str) -> dict:
    return {**UTM_STATIC, "utm_content": utm_content(fmt, pin_id), "utm_term": _s(language).lower() or "en"}


def pin_link(slug: str, language: str, fmt: str, pin_id: str) -> str:
    return canonical_url(slug, language) + "?" + urllib.parse.urlencode(utm_for(fmt, pin_id, language))


LINK_OK, LINK_DEAD, LINK_UNKNOWN = "ok", "dead", "unknown"
_HEAD_REFUSED = (403, 405, 501)


def link_status(url: str, *, timeout: int = LINK_TIMEOUT, http=None) -> str:
    """``ok`` (200 after redirects), ``dead`` (the page answers, and not with
    200: a 404, a 410, another 4xx) or ``unknown`` (no answer, a timeout, a
    5xx, a 429 — nothing to hold against the pin). A host that refuses HEAD
    (403 / 405 / 501) is asked again with GET."""
    try:
        if http is None:
            import requests as http  # noqa: N813
        r = http.head(url, timeout=timeout, allow_redirects=True)
        code = int(getattr(r, "status_code", 0) or 0)
        if code in _HEAD_REFUSED:
            r = http.get(url, timeout=timeout, allow_redirects=True, stream=True)
            code = int(getattr(r, "status_code", 0) or 0)
            close = getattr(r, "close", None)
            if callable(close):
                close()
    except Exception as exc:  # noqa: BLE001
        log.info("pinterest: link check could not reach %s: %s", url.split("?")[0], type(exc).__name__)
        return LINK_UNKNOWN
    if code == 200:
        return LINK_OK
    if code == 0 or code >= 500 or code == 429:
        return LINK_UNKNOWN
    return LINK_DEAD


def link_verdict(answer) -> str:
    """A link checker's answer read as ok / dead / unknown: a checker answers
    with a verdict string or a plain bool (True ok, False dead)."""
    if answer is True or answer == LINK_OK:
        return LINK_OK
    if answer is None or answer == LINK_UNKNOWN:
        return LINK_UNKNOWN
    return LINK_DEAD


def link_ok(url: str, *, timeout: int = LINK_TIMEOUT) -> str:
    """The default checker: a verdict (``link_status``)."""
    return link_status(url, timeout=timeout)


LinkCheck = Callable[[str], bool]


def short_hash(text: str, n: int = 8) -> str:
    return hashlib.sha256(_s(text).encode("utf-8")).hexdigest()[:n]


def audit(sb, actor: Optional[str], action: str, target_id: Optional[str], detail: dict) -> None:
    """One ``platform_audit_log`` row; never blocks the work, never holds a token."""
    try:
        sb.table(AUDIT).insert({"actor_id": actor or None, "action": action, "target_kind": "pinterest_pin",
                                "target_id": target_id or None, "detail": detail}).execute()
    except Exception as exc:  # noqa: BLE001
        log.warning("pinterest: audit row %s not written: %s", action, exc)
