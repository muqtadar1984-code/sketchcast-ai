"""The AUTO TRANSLATE AGENT — plan "SketchCast Feature Plan - Auto Translate
Agent.md", Revision 3 (founder, 2026-10-20).

Every approved English catalogue kit that is PUBLIC on the English YouTube
channel is translated into every lesson language, ONE translation at a time,
continuously — Jawi (ms-arab) from the topic's approved Malay kit, last. The
agent only QUEUES: the translation itself is the ordinary ``kit_translate``
job (catalogue/translate.py), every kit lands IN REVIEW and nothing is ever
approved or posted from here.

WHEN IT RUNS. ``maybe_sweep(sb)`` rides the worker's reaper tick (worker/
run.py ``_serve``, every 60 s, beside the YouTube stats poll). It is inert
unless BOTH switches are on:

  * env ``AUTO_TRANSLATE=1`` on the worker (the code path), and
  * ``platform_settings.auto_translate.enabled`` (the founder's switch; a
    Library button, read every tick — no redeploy).

``dry_run`` (seeded true) runs every decision and records what it WOULD
queue in ``platform_settings.auto_translate_state`` without inserting.

WHAT HOLDS IT (§2.2). A translation queues only when:

  1. the translation lane is free — no kit_translate queued or processing
     and no translated kit still ``generating`` (catalogue/translate.py's
     ``translation_in_flight``; the database re-checks under a lock);
  2. no CLIENT job that needs Bedrock is live, when the translation itself
     would use Bedrock (founder: "Normal client generation on Vertex should
     not hold back auto-translation on Bedrock"). Routing is the worker's
     own decision, never a list kept here: ``shared.model_routing.
     provider_for`` on the job's lesson and book language, ``index_book``
     (indexing always runs on Claude), and a future ``params.provider``
     stamp, read first;
  3. it is not paused (the founder's development/testing hold), not stopped
     by the circuit breaker, and not backing off after a throttle.

A translation already running is never killed: user lanes are claimed
first anyway, and its text calls retry their 429s.

FAILURES (§2.3, §3). A failed or rejected translation is never retried by
the agent (``auto_translate_candidates`` holds the pair until a person
deletes it or translates it by hand). A THROTTLE failure backs off — 30 min,
doubling to 4 h — and gets one retry. Two non-throttle failures in a row trip
the breaker: the agent pauses and emails the founder at once.

COST (§4). After each kit, its unit cost — the kit's piece jobs plus its
translate job, the console's kit-cost.ts rule — over ``cost_warn_usd`` ($2)
is recorded as a warning for the daily email. A warning never pauses.

The SQL lives in app migration 0147 (``auto_translate_candidates``,
``auto_translate_enqueue``). Everything here fails CLOSED: an unreadable
database queues nothing.
"""

from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

ENV = "AUTO_TRANSLATE"
SETTINGS_KEY = "auto_translate"
STATE_KEY = "auto_translate_state"
TICK_SECONDS = 60
DEFAULT_LANGUAGES = ("ar", "ms", "hi", "te", "fr", "es", "pt", "ur", "mr", "ms-arab")
BACKOFF_FIRST_MIN = 30
BACKOFF_MAX_MIN = 240
BREAKER_FAILURES = 2
COST_WARN_DEFAULT = 2.0
TTS_ESTIMATE_USD = 0.21          # ≈ 7,000 characters at $30/M until jobs.usage carries tts_usd (worker #297)
SAMPLE_PER_LANGUAGE = 3
REPEATED_BREACHES = 3            # warnings in 7 days that make the email lead with "investigate"
LIBRARY_URL_ENV = "LIBRARY_BASE_URL"
LIBRARY_URL_DEFAULT = "https://library.sketchcast.app"
FROM_ADDRESS = "SketchCast AI <noreply@sketchcast.app>"

_THROTTLE_RE = re.compile(r"throttl|\b429\b|resource[_ ]exhausted|rate.?limit|too many requests|"
                          r"service ?unavailable|overloaded|\b529\b", re.I)


def throttle_error(text: object) -> bool:
    return bool(_THROTTLE_RE.search(str(text or "")))


def enabled_by_env() -> bool:
    return str(os.getenv(ENV, "") or "").strip().lower() in ("1", "true", "yes", "on")


def _s(v: object) -> str:
    return "" if v is None else str(v)


def _rows(res) -> list[dict]:
    data = getattr(res, "data", None)
    return [r for r in (data or []) if isinstance(r, dict)] if isinstance(data, list) else []


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _when(v: object) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(_s(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ── the pure decision ───────────────────────────────────────────────────────


@dataclass
class Decision:
    action: str                       # "off" | "wait" | "queue"
    why: str = ""
    candidate: Optional[dict] = None
    retry: bool = False


def retry_allowed(c: dict) -> bool:
    """A ``job_failed`` hold the agent may lift ONCE: exactly one failed job
    in the window, and it failed on a throttle."""
    detail = _s(c.get("hold_detail"))
    return _s(c.get("hold")) == "job_failed" and detail.startswith("1 failed job") and throttle_error(detail)


def pick(candidates: list[dict], settings: dict) -> Optional[tuple[dict, bool]]:
    """The first candidate the agent may queue, in the SQL's rank order, and
    whether it is the one throttle retry. Retranslations of superseded kits
    follow ``settings.retranslate`` (founder: both blocked and stale)."""
    allowed = {_s(r) for r in (settings.get("retranslate") or ["blocked", "stale"])}
    for c in sorted(candidates, key=lambda r: int(r.get("rank") or 0)):
        reason = _s(c.get("reason"))
        if reason in ("blocked", "stale") and reason not in allowed:
            continue
        if c.get("hold") is None:
            return c, False
        if retry_allowed(c):
            return c, True
    return None


def decide(settings: Optional[dict], state: dict, *, now: datetime, lane_busy: Optional[str],
           bedrock_busy: Optional[str], candidates: list[dict], uses_bedrock=lambda lang: True) -> Decision:
    """What this tick does. Pure: the reads are passed in."""
    if not settings or not settings.get("enabled"):
        return Decision("off", "switched off")
    if settings.get("paused"):
        return Decision("wait", "paused by you")
    if state.get("breaker"):
        return Decision("wait", f"breaker: {state.get('breaker_reason') or 'failures in a row'}")
    until = _when(state.get("paused_until"))
    if until and until > now:
        return Decision("wait", f"throttled until {until.strftime('%H:%M')} UTC")
    if lane_busy:
        return Decision("wait", lane_busy)
    chosen = pick(candidates, settings)
    if chosen is None:
        return Decision("wait", "nothing left to translate")
    c, retry = chosen
    if bedrock_busy and uses_bedrock(_s(c.get("language"))):
        return Decision("wait", bedrock_busy, c, retry)
    return Decision("queue", "", c, retry)


def judge(state: dict, job: Optional[dict], kit: Optional[dict], *, now: datetime) -> Optional[str]:
    """Record the outcome of the last translation the agent queued, once.
    Returns "success", "throttle", "failure", "breaker" or None (still
    running / nothing to judge). Mutates ``state``."""
    job_id = _s(state.get("last_job_id"))
    if not job_id or _s(state.get("judged_job_id")) == job_id or job is None:
        return None
    if _s(job.get("status")) in ("queued", "processing"):
        return None
    if kit is not None and _s(kit.get("status")) == "generating":
        return None
    err = ""
    if _s(job.get("status")) == "error":
        err = _s(job.get("error")) or "translation job failed"
    elif kit is None:
        err = "the translation left no kit"
    elif _s(kit.get("status")) in ("failed", "rejected") or _s(kit.get("translation_status")) == "failed":
        err = _s(kit.get("notes")) or f"kit {kit.get('id')} {kit.get('status')}"
    state["judged_job_id"] = job_id
    if not err:
        state.update(consecutive_failures=0, backoff_min=0, paused_until=None)
        return "success"
    state["last_failure"] = {"at": _iso(now), "job_id": job_id, "error": err[:500]}
    if throttle_error(err):
        backoff = min(BACKOFF_MAX_MIN, max(BACKOFF_FIRST_MIN, int(state.get("backoff_min") or 0) * 2))
        state.update(backoff_min=backoff, paused_until=_iso(now + timedelta(minutes=backoff)))
        return "throttle"
    state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
    if state["consecutive_failures"] >= BREAKER_FAILURES:
        state.update(breaker=True, breaker_at=_iso(now),
                     breaker_reason=f"{state['consecutive_failures']} failures in a row; last: {err[:200]}")
        return "breaker"
    return "failure"


def breaker_cleared(settings: dict, state: dict) -> bool:
    """The Library's Resume writes ``settings.breaker_cleared_at``; a clear
    newer than the trip lifts the breaker (the agent owns the state row)."""
    cleared, tripped = _when(settings.get("breaker_cleared_at")), _when(state.get("breaker_at"))
    return bool(state.get("breaker") and cleared and (tripped is None or cleared > tripped))


# ── cost (the console's kit-cost.ts rule) ───────────────────────────────────


def job_spend(usage: object) -> Optional[dict]:
    """``total_usd``, else ``cost_usd + images_usd + tts_usd`` — kit-cost.ts
    ``jobSpend``. ``all_in`` only when the row says what pictures and voice
    cost too."""
    if not isinstance(usage, dict):
        return None

    def num(k):
        v = usage.get(k)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    text, total, images, tts = num("cost_usd"), num("total_usd"), num("images_usd"), num("tts_usd")
    if text is None and total is None:
        return None
    usd = total if total is not None else (text or 0.0) + (images or 0.0) + (tts or 0.0)
    return {"usd": usd, "text": text if text is not None else usd - (images or 0.0) - (tts or 0.0),
            "images": images or 0.0, "tts": tts or 0.0,
            "all_in": total is not None or (images is not None and tts is not None)}


def kit_unit_cost(jobs: list[dict]) -> dict:
    """A translated kit's cost from its jobs (each counted once): measured
    spend, and — while ``jobs.usage`` is text-only (before worker #297) — a
    labelled TTS estimate so the $2 test is not blind to narration."""
    seen, usd, text, images, tts, all_in, any_row = set(), 0.0, 0.0, 0.0, 0.0, True, False
    for j in jobs:
        if _s(j.get("id")) in seen:
            continue
        seen.add(_s(j.get("id")))
        s = job_spend(j.get("usage"))
        if not s:
            continue
        any_row = True
        usd += s["usd"]
        text += s["text"]
        images += s["images"]
        tts += s["tts"]
        all_in &= s["all_in"]
    estimate = 0.0 if (any_row and all_in) or tts > 0 else TTS_ESTIMATE_USD
    return {"measured_usd": round(usd, 4), "text_usd": round(text, 4), "images_usd": round(images, 4),
            "tts_usd": round(tts, 4), "tts_estimate_usd": estimate, "total_usd": round(usd + estimate, 4),
            "basis": "measured" if estimate == 0.0 else "measured text + estimated TTS"}


# ── reads and writes ────────────────────────────────────────────────────────


def read_setting(sb, key: str) -> Optional[dict]:
    rows = _rows(sb.table("platform_settings").select("value").eq("key", key).limit(1).execute())
    value = rows[0].get("value") if rows else None
    return value if isinstance(value, dict) else None


def write_state(sb, state: dict, now: datetime) -> None:
    try:
        sb.table("platform_settings").upsert({"key": STATE_KEY, "value": state, "updated_at": _iso(now)},
                                             on_conflict="key").execute()
    except Exception as exc:  # noqa: BLE001 — the state row is a report, never a gate
        logger.warning("auto-translate: state not written (%s)", exc)


def lane_busy(sb) -> Optional[str]:
    """Why the translation lane is not free, or None."""
    from catalogue.translate import translation_in_flight, translations_queued

    if translations_queued(sb):
        return "a translation is queued"
    return translation_in_flight(sb)


def agent_uses_bedrock(language: str) -> bool:
    """Whether a translation into ``language`` takes its text from Claude on
    Bedrock: the catalogue's own provider switch, else the routing rule."""
    from shared.claude_client import llm_backend
    from shared.llm import catalogue_text_provider
    from shared.model_routing import ANTHROPIC, provider_for

    if llm_backend() != "bedrock":
        return False
    return catalogue_text_provider() == ANTHROPIC or provider_for(language) == ANTHROPIC


def bedrock_client_work(sb, now: Optional[datetime] = None) -> Optional[str]:
    """Why a CLIENT job needs Bedrock right now, or None. A client job is a
    live (queued or processing) builder that is not catalogue work and not
    an observer, and has not stalled — catalogue.figures.builder_queued's
    reading. It needs Bedrock when its provider stamp says so, when it is an
    index_book (indexing always runs on Claude), or when its lesson or book
    language routes to Claude (``provider_for``) — with LLM_BACKEND=bedrock."""
    from catalogue.figures import BUILDER_LIVE_STATUSES, BUILDER_PROBE_LIMIT, builder_stalled
    from shared.claude_client import llm_backend
    from shared.model_routing import ANTHROPIC, provider_for
    from worker import client as db

    if llm_backend() != "bedrock":
        return None
    rows = _rows(sb.table("jobs").select("id,type,status,params,generation_id,book_id,updated_at")
                 .in_("status", list(BUILDER_LIVE_STATUSES)).not_.in_("type", sorted(db.OBSERVER_JOB_TYPES))
                 .limit(BUILDER_PROBE_LIMIT).execute())
    live = [r for r in rows if not db.is_catalogue_params(r.get("params")) and not builder_stalled(r, now)]
    if not live:
        return None
    for r in live:
        params = r.get("params") if isinstance(r.get("params"), dict) else {}
        if _s(params.get("provider")).lower() == ANTHROPIC:
            return f"a client {r.get('type')} job is using Bedrock"
        if _s(r.get("type")) == "index_book":
            return "a client book is being indexed on Bedrock"
        if provider_for(_s(params.get("language")) or None) == ANTHROPIC and params.get("language"):
            return f"a client {r.get('type')} job in {params.get('language')} is using Bedrock"
    gen_ids = sorted({_s(r.get("generation_id")) for r in live if r.get("generation_id")})
    book_ids = sorted({_s(r.get("book_id")) for r in live if r.get("book_id")})
    langs: list[str] = []
    if gen_ids:
        for g in _rows(sb.table("generations").select("id,language:params->>language").in_("id", gen_ids).execute()):
            langs.append(_s(g.get("language")))
    if book_ids:
        for b in _rows(sb.table("books").select("id,language").in_("id", book_ids).execute()):
            langs.append(_s(b.get("language")))
    for lang in langs:
        if lang and provider_for(lang) == ANTHROPIC:
            return f"a client generation in {lang} is using Bedrock"
    return None


def candidates(sb, languages: list[str]) -> list[dict]:
    return _rows(sb.rpc("auto_translate_candidates", {"p_languages": languages}).execute())


def enqueue(sb, c: dict, dry_run: bool) -> dict:
    res = sb.rpc("auto_translate_enqueue", {"p_source_kit_id": c["source_kit_id"], "p_language": c["language"],
                                            "p_dry_run": bool(dry_run)}).execute()
    data = getattr(res, "data", None)
    return data if isinstance(data, dict) else {"queued": False, "why": "no answer"}


def last_outcome_rows(sb, state: dict) -> tuple[Optional[dict], Optional[dict]]:
    """The agent's last job and the newest translation kit of its pair."""
    job_id = _s(state.get("last_job_id"))
    if not job_id or _s(state.get("judged_job_id")) == job_id:
        return None, None
    jobs = _rows(sb.table("jobs").select("id,status,error,params,created_at").eq("id", job_id).limit(1).execute())
    if not jobs:
        return {"id": job_id, "status": "error", "error": "the job row is gone"}, None
    job = jobs[0]
    p = job.get("params") or {}
    kits = _rows(sb.table("topic_kits").select("id,status,translation_status,notes,created_at,topic_id,language,"
                                               "presentation_generation_id,doc_generation_ids")
                 .eq("source_kit_id", _s(p.get("source_kit_id"))).eq("language", _s(p.get("language")))
                 .order("created_at", desc=True).limit(1).execute())
    return job, (kits[0] if kits else None)


def kit_jobs(sb, kit: dict) -> list[dict]:
    """Every job that built ``kit``: its translate job(s) and its pieces'."""
    out = _rows(sb.table("jobs").select("id,usage").eq("type", "kit_translate")
                .eq("stage->>kit_id", _s(kit.get("id"))).execute())
    gens = [_s(kit.get("presentation_generation_id"))] + [
        _s(v) for v in (kit.get("doc_generation_ids") or {}).values() if v]
    gens = [g for g in gens if g]
    if gens:
        out += _rows(sb.table("jobs").select("id,usage").in_("generation_id", gens).execute())
    return out


# ── email ───────────────────────────────────────────────────────────────────


def send_email(to: str, subject: str, text: str) -> bool:
    key = os.getenv("RESEND_API_KEY")
    if not key or not to:
        logger.warning("auto-translate email SKIPPED (%s): %s", "no RESEND_API_KEY" if not key else "no address", subject)
        return False
    try:
        import requests

        r = requests.post("https://api.resend.com/emails",
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json={"from": FROM_ADDRESS, "to": [to], "subject": subject, "text": text}, timeout=15)
        return 200 <= getattr(r, "status_code", 0) < 300
    except Exception as exc:  # noqa: BLE001
        logger.warning("auto-translate email failed: %s", exc)
        return False


def library_url(path: str = "/library/translations") -> str:
    return (os.getenv(LIBRARY_URL_ENV) or LIBRARY_URL_DEFAULT).rstrip("/") + path


def summary_due(settings: dict, state: dict, now: datetime) -> bool:
    hour = float(settings.get("summary_hour_utc", 3.5) or 0)
    at = now.replace(hour=int(hour), minute=int(round((hour % 1) * 60)), second=0, microsecond=0)
    return now >= at and _s(state.get("summary_date")) != now.date().isoformat()


def build_summary(*, since: datetime, now: datetime, queued: list[dict], landed: list[dict], failed: list[dict],
                  held: list[dict], in_review: dict[str, int], left: dict[str, int], jawi_waiting: int,
                  warnings: list[dict], state: dict, rng: Optional[random.Random] = None) -> Optional[tuple[str, str]]:
    """The daily email: (subject, text), or None on a day with nothing to say."""
    if not (queued or landed or failed or warnings or state.get("breaker") or any(in_review.values())):
        return None
    rng = rng or random.Random(now.date().isoformat())
    lines = [f"Auto Translate Agent — {since.strftime('%d %b %H:%M')} to {now.strftime('%d %b %H:%M')} UTC", ""]
    recent = [w for w in (state.get("cost_warnings") or []) if (_when(w.get("at")) or now) > now - timedelta(days=7)]
    if len(recent) >= REPEATED_BREACHES:
        lines += [f"⚠ REPEATED COST BREACHES — {len(recent)} kits over the warning in 7 days: investigate.", ""]
    if state.get("breaker"):
        lines += [f"⏸ PAUSED BY THE BREAKER: {state.get('breaker_reason')}", "  Resume from the Library when fixed.", ""]
    by = lambda rows: ", ".join(f"{k} {v}" for k, v in sorted(_count(rows).items())) or "none"  # noqa: E731
    lines += [f"Queued: {len(queued)} ({by(queued)})", f"Landed in review: {len(landed)} ({by(landed)})",
              f"Failed: {len(failed)}"]
    for f in failed:
        lines.append(f"  · {f.get('language')} — {f.get('topic_title') or f.get('topic_id')}: {_s(f.get('error'))[:160]}")
    lines += ["", "Waiting for your review (Approve all clean on the Translations tab):"]
    lines += [f"  {lang}: {n}" for lang, n in sorted(in_review.items()) if n] or ["  none"]
    lines += ["", f"Spot-check (random, {SAMPLE_PER_LANGUAGE} per language):"]
    picked = 0
    for lang in sorted({_s(r.get("language")) for r in landed}):
        rows = [r for r in landed if _s(r.get("language")) == lang]
        for r in rng.sample(rows, min(SAMPLE_PER_LANGUAGE, len(rows))):
            lines.append(f"  · {lang} — {r.get('topic_title') or r.get('topic_id')}: "
                         f"{library_url('/library/topics/' + _s(r.get('topic_id')))}")
            picked += 1
    if not picked:
        lines.append("  nothing landed today")
    lines += ["", "Kits over the cost warning:"]
    lines += [f"  · {w.get('language')} — {w.get('topic_title') or w.get('topic_id')}: ${w.get('total_usd'):.2f} "
              f"(text ${w.get('text_usd', 0):.2f}, TTS ${w.get('tts_usd', 0) or w.get('tts_estimate_usd', 0):.2f}"
              f"{' est.' if w.get('tts_estimate_usd') else ''}, images ${w.get('images_usd', 0):.2f}; {w.get('basis')})"
              for w in warnings] or ["  none"]
    if held:
        lines += ["", "Held for you (never retried by the agent — delete or translate by hand to clear):"]
        lines += [f"  · {h.get('language')} — {h.get('topic_title')}: {h.get('hold')} {_s(h.get('hold_detail'))[:160]}"
                  for h in held]
    lines += ["", "Left to translate: " + (", ".join(f"{k} {v}" for k, v in left.items() if v) or "nothing"),
              f"Jawi waiting for Malay approval: {jawi_waiting}", "", library_url()]
    subject = (f"Auto-translate: {len(landed)} in review, {len(failed)} failed"
               + (" — PAUSED" if state.get("breaker") else "") + (f", {len(warnings)} over cost" if warnings else ""))
    return subject, "\n".join(lines)


def _count(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[_s(r.get("language"))] = out.get(_s(r.get("language")), 0) + 1
    return out


def send_daily_summary(sb, settings: dict, state: dict, cands: list[dict], now: datetime) -> bool:
    since = _when(state.get("summary_since")) or now - timedelta(days=1)
    audit = _rows(sb.table("platform_audit_log").select("detail,created_at").eq("action", "auto_translate_enqueue")
                  .gte("created_at", _iso(since)).execute())
    titles = {_s(c.get("topic_id")): c.get("topic_title") for c in cands}
    queued = [{"language": (a.get("detail") or {}).get("language")} for a in audit]
    kits = _rows(sb.table("topic_kits").select("id,topic_id,language,status,translation_status,notes,updated_at")
                 .neq("language", "en").gte("updated_at", _iso(since)).execute())
    landed = [{**k, "topic_title": titles.get(_s(k.get("topic_id")))} for k in kits
              if _s(k.get("translation_status")) == "in_review"]
    failed = [{**k, "topic_title": titles.get(_s(k.get("topic_id"))), "error": k.get("notes")} for k in kits
              if _s(k.get("status")) == "failed" or _s(k.get("translation_status")) == "failed"]
    review_rows = _rows(sb.table("topic_kits").select("language").eq("translation_status", "in_review").execute())
    in_review = _count(review_rows)
    held = [c for c in cands if _s(c.get("hold")) in ("rejected_or_failed", "job_failed")]
    left: dict[str, int] = {}
    for c in cands:
        if c.get("hold") is None:
            left[_s(c.get("language"))] = left.get(_s(c.get("language")), 0) + 1
    jawi_waiting = sum(1 for c in cands if _s(c.get("hold")) == "malay_not_approved")
    warnings = [w for w in (state.get("cost_warnings") or []) if (_when(w.get("at")) or now) >= since]
    built = build_summary(since=since, now=now, queued=queued, landed=landed, failed=failed, held=held,
                          in_review=in_review, left=left, jawi_waiting=jawi_waiting, warnings=warnings, state=state)
    state["summary_date"] = now.date().isoformat()
    state["summary_since"] = _iso(now)
    if built is None:
        return False
    return send_email(_s(settings.get("summary_email")), *built)


# ── the tick ────────────────────────────────────────────────────────────────

_last_run = 0.0
_lock = threading.Lock()


def maybe_sweep(sb, *, clock=time.monotonic, now: Optional[datetime] = None) -> Optional[Decision]:
    """Called from the reaper tick (every minute). Never raises."""
    global _last_run
    if not enabled_by_env():
        return None
    with _lock:
        if clock() - _last_run < TICK_SECONDS - 1:
            return None
        _last_run = clock()
    try:
        return sweep(sb, now=now or datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 — the agent must never take the reaper down
        logger.error("auto-translate tick failed (nothing queued): %s", exc)
        return None


def sweep(sb, *, now: datetime) -> Decision:
    settings = read_setting(sb, SETTINGS_KEY)
    if not settings or not settings.get("enabled"):
        return Decision("off", "switched off")
    state = read_setting(sb, STATE_KEY) or {}

    if breaker_cleared(settings, state):
        state.update(breaker=False, breaker_reason=None, consecutive_failures=0)
    job, kit = last_outcome_rows(sb, state)
    outcome = judge(state, job, kit, now=now)
    if outcome == "success" and kit is not None:
        cost = kit_unit_cost(kit_jobs(sb, kit))
        warn = float(settings.get("cost_warn_usd") or COST_WARN_DEFAULT)
        if cost["total_usd"] > warn:
            state["cost_warnings"] = ([w for w in (state.get("cost_warnings") or [])
                                       if (_when(w.get("at")) or now) > now - timedelta(days=30)]
                                      + [{"at": _iso(now), "kit_id": kit.get("id"), "topic_id": kit.get("topic_id"),
                                          "language": kit.get("language"), **cost}])
            logger.warning("auto-translate: kit %s (%s) cost $%.2f > $%.2f (%s)", kit.get("id"),
                           kit.get("language"), cost["total_usd"], warn, cost["basis"])
    if outcome == "breaker":
        send_email(_s(settings.get("summary_email")), "Auto-translate PAUSED: failures in a row",
                   f"The Auto Translate Agent paused itself.\n\n{state.get('breaker_reason')}\n\n"
                   f"Nothing more is queued until you press Resume on {library_url()}.")

    languages = [_s(x) for x in (settings.get("languages") or DEFAULT_LANGUAGES) if _s(x)]
    cands = candidates(sb, languages)
    busy = lane_busy(sb)
    bedrock = None if busy else bedrock_client_work(sb, now)
    d = decide(settings, state, now=now, lane_busy=busy, bedrock_busy=bedrock, candidates=cands,
               uses_bedrock=agent_uses_bedrock)

    dry = bool(settings.get("dry_run", True))
    if d.action == "queue":
        res = enqueue(sb, d.candidate, dry)
        if res.get("queued"):
            state.update(last_job_id=res.get("job_id"), last_enqueue_at=_iso(now), finished_notified=False)
            logger.info("auto-translate: queued %s %s (%s) job %s", d.candidate.get("language"),
                        d.candidate.get("topic_title"), d.candidate.get("reason"), res.get("job_id"))
        else:
            d = Decision("wait", "dry run: would queue" if res.get("dry_run") else _s(res.get("why")), d.candidate, d.retry)
    elif d.why == "nothing left to translate" and not state.get("finished_notified"):
        state["finished_notified"] = True
        send_email(_s(settings.get("summary_email")), "Auto-translate: every eligible kit is translated",
                   f"Nothing is left to queue. Review the last kits on {library_url()}.")

    c = d.candidate or {}
    state["last_tick"] = _iso(now)
    state["decision"] = {"at": _iso(now), "action": d.action, "why": d.why, "dry_run": dry,
                         "language": c.get("language"), "topic": c.get("topic_title"),
                         "source_kit_id": c.get("source_kit_id"), "reason": c.get("reason"), "retry": d.retry}
    state["remaining"] = {lang: sum(1 for x in cands if x.get("hold") is None and x.get("language") == lang)
                          for lang in languages}
    state["jawi_waiting"] = sum(1 for x in cands if x.get("hold") == "malay_not_approved")
    if summary_due(settings, state, now):
        try:
            send_daily_summary(sb, settings, state, cands, now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto-translate summary failed: %s", exc)
            state["summary_date"] = now.date().isoformat()
    write_state(sb, state, now)
    return d


__all__ = ["Decision", "build_summary", "decide", "judge", "job_spend", "kit_unit_cost", "maybe_sweep", "pick",
           "retry_allowed", "summary_due", "sweep", "throttle_error"]
