"""Supabase admin client + helpers for the generation worker.

Uses the service_role key, so it bypasses RLS — keep this key server-side only.
"""

from __future__ import annotations

import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from supabase import Client, ClientOptions, create_client


def admin() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    # storage3 defaults to a 20s timeout — far too short for downloading a real
    # textbook PDF or uploading a rendered video over Railway↔Supabase. Give the
    # storage + REST clients generous timeouts so large transfers don't fail with
    # "The read operation timed out".
    options = ClientOptions(
        postgrest_client_timeout=60,
        storage_client_timeout=600,
    )
    return create_client(url, key, options)


# ── jobs / generations ───────────────────────────────────────────────

# Jobs that REPORT ON a generation without building it. Their generation_id
# names the generation under investigation — a support_diagnose job is filed
# against a failed generation (auto) or a healthy, assigned one (a teacher's
# report) — so mirroring THEIR lifecycle onto that row relabels someone else's
# work. Measured in prod 2026-09-03: the worker marked a worksheet 'error' at
# 11:50:12 (storage stream reset; credit refunded), the auto-filed diagnosis job
# was claimed at 11:50:14 and flipped the same row back to 'processing', and
# nothing ever wrote a terminal state again. The Library showed a spinner that
# never ended, the ✕ was inert, and the book could not be deleted. Only the job
# that BUILDS a generation may write its status.
#
# topic_harvest (catalogue, 2026-09) is an observer too: it reads a book's
# headings into topic_candidates and owns NO generation (its generation_id is
# NULL by construction). Listing it here is belt-and-braces — should a harvest
# ever be filed carrying a generation_id, claiming or finishing it must still
# never relabel that row. topic_derive (catalogue Phase 2a) is the same shape:
# it reads a curriculum's nodes (jobs.params.curriculum_id) into grouped
# candidates, owns no generation and no book.
OBSERVER_JOB_TYPES = frozenset({"support_diagnose", "topic_harvest", "topic_derive"})


def generation_to_mirror(job: Optional[dict]) -> Optional[str]:
    """The generation whose status THIS job's lifecycle may write, or None.

    A builder job (presentation, worksheet, …) owns its generation row. An
    observer job (support_diagnose) carries a generation_id it must never
    relabel. index_book carries none, so it is a no-op either way."""
    if not job or job.get("type") in OBSERVER_JOB_TYPES:
        return None
    return job.get("generation_id")


def mirror_generation_status(sb: Client, generation_id, status: str) -> None:
    """Mirror a job's lifecycle onto its generation row. Best-effort, never raises.

    generations.status used to be written ONLY by finish_job, and only ever to
    'done' or 'error' — so a row said "queued" for the entire time it was being
    built, however many minutes that took. Two things went wrong with that.

    The Library reads generations.status (dashboard/page.tsx builds its lesson
    objects with `status: g.status` and `progress: g.jobs[0].progress`), and its
    whole in-flight UI is written against 'processing': the progress ring is
    `pct={status === "processing" ? progress : 0}`, so it sat at ZERO while the
    job reported real progress, and the ETA label never rendered at all.

    Worse, delete-lesson.tsx offers a CANCEL for a 'queued' row and is inert for
    a 'processing' one — so the ✕ invited a teacher to cancel a render that was
    already running, and credit_ledger_void_unconsumed then refused the refund
    because claim_next_job had already set the job's progress to 1. Measured on
    prod 2026-08-25: two teachers lost a credit that way, both on presentations
    (the slow artifact that looks stuck), one of them the founder.
    """
    if not generation_id:
        return
    try:
        sb.table("generations").update({"status": status}).eq("id", generation_id).execute()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").warning(
            "generation %s status not mirrored to %s: %s", generation_id, status, exc
        )


def claim_next_job(sb: Client, job_type=None, exclude_types=None) -> Optional[dict]:
    """Atomically-ish claim the oldest queued job (sets it to processing).
    `job_type` may be a single type or a LIST of types — only those are
    considered; `exclude_types` is a collection of types NOT to consider
    (run.py's generic lane passes OBSERVER_JOB_TYPES, so a topic_harvest —
    cheap on quota, heavy on CPU and egress — is claimed only by its own last
    lane, when nothing else is queued). Lets the loop prioritise small/fast
    work (support diagnoses, then documents) over long batch video renders."""
    q = sb.table("jobs").select("*").eq("status", "queued")
    if isinstance(job_type, (list, tuple, set, frozenset)):
        q = q.in_("type", list(job_type))
    elif job_type:
        q = q.eq("type", job_type)
    if exclude_types:
        q = q.not_.in_("type", list(exclude_types))
    res = q.order("created_at").limit(1).execute()
    if not res.data:
        return None
    job = res.data[0]
    upd = (
        sb.table("jobs")
        .update({"status": "processing", "progress": 1})
        .eq("id", job["id"])
        .eq("status", "queued")  # guard against a racing worker
        .execute()
    )
    if not upd.data:
        return None  # someone else grabbed it
    claimed = upd.data[0]
    # The generation is being BUILT now, and the Library must say so: it is what
    # turns on the progress ring and the ETA, and what makes the ✕ stop offering
    # a cancel that would cost the teacher a credit for nothing. Only a BUILDER
    # job says so — an observer job (support_diagnose) names a generation it is
    # investigating, and claiming one must not relabel that row.
    mirror_generation_status(sb, generation_to_mirror(claimed), "processing")
    return claimed


def requeue_stale_jobs(sb: Client, older_than_minutes: Optional[int] = None, max_attempts: int = 3,
                       exclude_ids: Optional[set] = None) -> int:
    """Return orphaned 'processing' jobs to 'queued' so a live worker re-runs them.

    ``claim_next_job`` only ever claims 'queued' rows, so a job the worker was
    running when it died (a deploy, crash or OOM) is stranded in 'processing'
    forever — the book sits at "Finding chapters…" / a generation freezes. This is
    a SINGLE-worker service, so:

    * at STARTUP, every 'processing' job is orphaned (nothing else is running it) —
      call with ``older_than_minutes=None`` to reap them all and recover instantly;
    * a windowed backstop passes ``older_than_minutes`` so an orphan created without
      a restart (e.g. a failed finish_job write) is eventually recovered.

    Each requeue bumps ``attempts``; a job that has already been retried
    ``max_attempts`` times is marked 'error' instead of requeued, so a poison-pill
    job that keeps hard-crashing the worker can't loop forever and block the queue.

    Returns the number requeued. Best-effort — never raises. (Scaling past one
    replica would require gating the startup reap-all, or a peer's in-flight job
    could be requeued.)"""
    cutoff = (
        (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat()
        if older_than_minutes is not None else None
    )
    try:
        q = sb.table("jobs").select("id,type,book_id,attempts,generation_id").eq("status", "processing")
        if cutoff:
            q = q.lt("updated_at", cutoff)
        rows = q.execute().data or []
        requeued = failed = 0
        for j in rows:
            # Never requeue a job THIS process is actively running (in-process
            # concurrency): its 'processing' row is alive, not orphaned.
            if exclude_ids and j["id"] in exclude_ids:
                continue
            att = int(j.get("attempts") or 0)
            # The generation this job may relabel — None for an observer job
            # (support_diagnose), whose generation_id is the row it REPORTS ON.
            # Auto-failing a stuck diagnosis must not mark a healthy, assigned
            # lesson 'error' (and void its credit); requeuing one must not flip
            # that lesson to 'queued'.
            owned_gen = generation_to_mirror(j)
            # The .eq("status","processing") guard means a job another actor already
            # moved is skipped (returns no rows) — safe under any race.
            if att >= max_attempts:
                sb.table("jobs").update({
                    "status": "error",
                    "error": f"Auto-failed after {att} attempts — the worker kept dying on this job.",
                }).eq("id", j["id"]).eq("status", "processing").execute()
                # Mirror the failure onto the generation. Without this a
                # poison-pill row sat in the Library forever AND kept its credit:
                # credit_ledger_sync only voids on generations.status = 'error',
                # which nothing here ever wrote.
                mirror_generation_status(sb, owned_gen, "error")
                if j.get("type") == "index_book" and j.get("book_id"):
                    try:  # stop the UI's "Finding chapters…" spinner
                        sb.table("books").update({"status": "error"}).eq("id", j["book_id"]).execute()
                    except Exception:  # noqa: BLE001
                        pass
                failed += 1
            else:
                upd = sb.table("jobs").update(
                    {"status": "queued", "progress": 0, "attempts": att + 1}
                ).eq("id", j["id"]).eq("status", "processing").execute()
                if upd.data:
                    # Back in the queue, so the row must not stay "being built" —
                    # a stuck 'processing' would leave the ✕ inert forever.
                    mirror_generation_status(sb, owned_gen, "queued")
                    requeued += 1
        if failed:
            logging.getLogger("worker").error(
                "Reaper: %d job(s) auto-failed after %d attempts (poison pill)", failed, max_attempts
            )
        return requeued
    except Exception as exc:  # noqa: BLE001
        # The 'attempts' column may not exist yet (app migration 0041 not applied) —
        # recovery must still work, so fall back to a plain reap with no cap.
        logging.getLogger("worker").warning("capped requeue failed (%s); plain reap", exc)
        try:
            q = sb.table("jobs").update({"status": "queued", "progress": 0}).eq("status", "processing")
            if cutoff:
                q = q.lt("updated_at", cutoff)
            if exclude_ids:
                q = q.not_.in_("id", list(exclude_ids))
            return len((q.execute().data) or [])
        except Exception as exc2:  # noqa: BLE001
            logging.getLogger("worker").warning("stale-job requeue failed: %s", exc2)
            return 0


def requeue_stale_sketches(sb: Client, older_than_minutes: Optional[int] = None) -> int:
    """Same as ``requeue_stale_jobs`` for the separate tutor_sketch queue (claimed
    FIRST each loop, so a restart is disproportionately likely to strand one mid-
    render and leave a student's coach doodle frozen). No attempt cap: a sketch is
    a tiny SVG→MP4 render, a hard crash on one is very unlikely, and a sketch that
    merely raises is already caught and set 'error'. Best-effort."""
    try:
        q = sb.table("tutor_sketch").update({"status": "queued"}).eq("status", "processing")
        if older_than_minutes is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat()
            q = q.lt("updated_at", cutoff)
        return len((q.execute().data) or [])
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").warning("stale-sketch requeue failed: %s", exc)
        return 0


def set_progress(sb: Client, job_id: str, progress: int) -> None:
    sb.table("jobs").update({"progress": progress}).eq("id", job_id).execute()


def set_book_language(sb: Client, book_id: str, language: Optional[str]) -> None:
    """Persist the detected book language (books.language, 0056). Best-effort:
    a deployment whose migration hasn't added the column must not fail indexing."""
    if not language:
        return
    try:
        sb.table("books").update({"language": language}).eq("id", book_id).execute()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").debug("set_book_language skipped: %s", exc)


def set_stage(sb: Client, job_id: str, stage: Optional[dict]) -> None:
    """Persist the human-facing stage of a multi-part job (jobs.stage, 0053):
    {"phase": "analysis"|"video", "part": k, "total": n, "part_pct": p}.
    Best-effort: a deployment whose migration hasn't added the column must not
    fail the job — stage is presentation, progress is the source of truth."""
    try:
        sb.table("jobs").update({"stage": stage}).eq("id", job_id).execute()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").debug("set_stage skipped: %s", exc)


def set_job_usage(sb: Client, job_id: str, usage: Optional[dict]) -> None:
    """Persist a job's Claude token/cost total (jobs.usage), MERGING additively
    with any existing value — a support-agent run reuses its job id for an
    inline re-index, and the expensive half of the spend must not be clobbered
    by the final write. Best-effort: a deployment whose migration hasn't added
    the column must not fail the job."""
    if not usage or not usage.get("calls"):
        return
    try:
        prev_q = sb.table("jobs").select("usage").eq("id", job_id).maybe_single().execute()
        prev = (getattr(prev_q, "data", None) or {}).get("usage") or {}
        merged = {
            "calls": int(prev.get("calls") or 0) + int(usage.get("calls") or 0),
            "input_tokens": int(prev.get("input_tokens") or 0) + int(usage.get("input_tokens") or 0),
            "output_tokens": int(prev.get("output_tokens") or 0) + int(usage.get("output_tokens") or 0),
            "cost_usd": round(float(prev.get("cost_usd") or 0) + float(usage.get("cost_usd") or 0), 6),
        }
        sb.table("jobs").update({"usage": merged}).eq("id", job_id).execute()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").warning("job usage not persisted for %s: %s", job_id, exc)


def finish_job(sb: Client, job_id: str, generation_id: Optional[str] = None, error: Optional[str] = None) -> None:
    status = "error" if error else "done"
    sb.table("jobs").update(
        {"status": status, "progress": 100 if not error else 0, "error": error}
    ).eq("id", job_id).execute()
    # index_book jobs have no generation — only mirror status when there is one.
    if generation_id:
        sb.table("generations").update({"status": status}).eq("id", generation_id).execute()


def get_generation(sb: Client, generation_id: str) -> dict:
    return (
        sb.table("generations").select("*").eq("id", generation_id).single().execute().data
    )


def get_book(sb: Client, book_id: str) -> dict:
    return sb.table("books").select("*").eq("id", book_id).single().execute().data


def set_generation_title(sb: Client, generation_id: str, title: str) -> None:
    sb.table("generations").update({"title": title}).eq("id", generation_id).execute()


def set_generation_status(sb: Client, generation_id: str, status: str) -> None:
    sb.table("generations").update({"status": status}).eq("id", generation_id).execute()


def merge_generation_params(sb: Client, generation_id: str, patch: dict) -> None:
    """Merge keys into generations.params (read-modify-write). Best-effort: TTS
    telemetry must never fail a finished lesson. Used to record the voice that was
    ACTUALLY rendered (params.tts_voice_used / tts_voice_downgraded) so a silent
    premium→free downgrade is visible in the app instead of only in the audio."""
    if not patch:
        return
    try:
        cur = sb.table("generations").select("params").eq("id", generation_id).maybe_single().execute()
        params = dict((getattr(cur, "data", None) or {}).get("params") or {})
        params.update(patch)
        sb.table("generations").update({"params": params}).eq("id", generation_id).execute()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").warning("generation params not merged for %s: %s", generation_id, exc)




# ── plan tier (premium voice gate) ───────────────────────────────────────────
# The first RPC the worker has ever called. Premium voices used to be gated by
# a deployment flag — everyone or no one. The gate is now per account:
# `plan_tier(uid)` (app migration 0086, security definer, execute revoked from
# `authenticated`, callable by the service role by default privilege) plus the
# console's comp override on profiles.max_books / max_chapters, which the DB's
# own cap functions check FIRST and call 'unlimited'. The founder's account is
# exactly that case: plan_tier says 'trial', the override says premium.

class TransientTierError(RuntimeError):
    """The tier could not be resolved right now, though the RPC is known good
    (the boot probe passed). The job is requeued rather than rendered free: a
    paying customer must not receive the free voice because of a timeout."""


_TIER_TIMEOUT_S = float(os.getenv("PLAN_TIER_TIMEOUT_S", "10"))
_TIER_RETRIES = 3
# Tri-state. True: the RPC is known good (a per-job failure is transient →
# requeue). False: known broken — permission/missing function (proceed free,
# the boot CRITICAL carries the alarm). None: not proven either way, e.g. the
# probe itself timed out — treated as GOOD, so failures still requeue rather
# than a boot-time blip disabling the requeue path for the process lifetime.
_PLAN_TIER_PROBE_OK: Optional[bool] = None
# The same tri-state for 0105's premium_voices_allowed(), kept SEPARATE because
# the two RPCs fail for different reasons and only one of them is allowed to
# requeue a job. False here means the function is missing or unexecutable —
# overwhelmingly "0105 has not been applied yet", which is a legitimate state
# this branch was built to survive — so it must never turn into a requeue loop.
_PREMIUM_PROBE_OK: Optional[bool] = None


class _Timeout(Exception):
    """Raised by _call_with_timeout when the bound is exceeded."""


def _call_with_timeout(fn, seconds: float):
    """Run `fn` on a DAEMON thread with a wall-clock bound. The shared client's
    PostgREST timeout is 60 s (right for uploads, wrong for a 5 ms SQL
    function); three of those mid-job would stall a render for minutes.

    A daemon thread, not a ThreadPoolExecutor: executor workers are joined at
    interpreter exit, so one abandoned 60 s call pinned `--once` and clean
    shutdown for a full minute (measured 66 s). A daemon thread is simply
    dropped."""
    import queue
    import threading
    box: queue.Queue = queue.Queue(maxsize=1)

    def run():
        try:
            box.put(("ok", fn()))
        except BaseException as exc:  # noqa: BLE001 — carried to the caller
            box.put(("err", exc))

    threading.Thread(target=run, daemon=True, name="tier-call").start()
    try:
        kind, val = box.get(timeout=seconds)
    except queue.Empty:
        raise _Timeout(f"no answer within {seconds:g}s") from None
    if kind == "err":
        raise val
    return val


def _is_transient(exc: BaseException) -> bool:
    """Worth retrying: our timeout, or a transport-level failure. A PostgREST
    APIError (missing function, 42501, bad argument) will not get better by
    waiting, and retrying it three times with sleeps is 43 s of dead time."""
    if isinstance(exc, _Timeout):
        return True
    name = type(exc).__name__
    mod = type(exc).__module__ or ""
    if "APIError" in name:
        return False
    return ("httpx" in mod or "httpcore" in mod or "Timeout" in name
            or "Connect" in name or isinstance(exc, (OSError, ConnectionError)))


def _retrying(label: str, owner_id: str, fn):
    """Up to _TIER_RETRIES attempts, sleeping only between attempts and only
    for transient errors. Returns (value, None) or (None, last_exception)."""
    last: Optional[BaseException] = None
    for attempt in range(_TIER_RETRIES):
        try:
            return fn(), None
        except BaseException as exc:  # noqa: BLE001
            last = exc
            if not _is_transient(exc) or attempt == _TIER_RETRIES - 1:
                break
            time.sleep(0.5 * (attempt + 1))
    logging.getLogger("worker").error("%s(%s) failed after %d attempt(s): %s: %r",
                                      label, owner_id, _TIER_RETRIES, type(last).__name__, last)
    return None, last


def plan_tier_for(sb: Client, owner_id: str) -> Optional[str]:
    """`plan_tier(owner_id)` with retries and a timeout. None = could not
    resolve. Never raises. supabase-py returns the scalar as a bare str
    (measured against the live project: 'trial'); SQL NULL arrives as None."""
    def call():
        res = _call_with_timeout(
            lambda: sb.rpc("plan_tier", {"uid": owner_id}).execute(), _TIER_TIMEOUT_S)
        data = getattr(res, "data", None)
        tier = data if isinstance(data, str) else (data[0] if isinstance(data, list) and data else None)
        return tier.strip().lower() if isinstance(tier, str) and tier else None
    val, _ = _retrying("plan_tier", owner_id, call)
    return val


def premium_override_for(sb: Client, owner_id: str) -> Optional[bool]:
    """True when the console comped this account (profiles.max_books or
    max_chapters set). None = could not read. Retried like the RPC — this read
    is what decides for a comped account, so a single failed attempt must not
    quietly turn the founder's account into a trial one. Never raises."""
    def call():
        res = _call_with_timeout(
            lambda: (sb.table("profiles").select("max_books, max_chapters")
                     .eq("id", owner_id).maybe_single().execute()), _TIER_TIMEOUT_S)
        row = getattr(res, "data", None) or {}
        return row.get("max_books") is not None or row.get("max_chapters") is not None
    val, _ = _retrying("premium_override", owner_id, call)
    return val


class _UnreadableAnswer(RuntimeError):
    """premium_voices_allowed replied with something that is not a boolean.
    Not worth a retry (it will reply the same way) and NOT "the function is
    missing" either — so it is reported as unknown, never as a confident no."""


def premium_allowed_for(sb: Client, owner_id: str) -> tuple[Optional[bool], Optional[str]]:
    """The DATABASE's answer to "may this account use the premium voices?".

    Migration 0105 put that question in one place — premium_voices_allowed(uid):
    a paid tier, OR a comp override of at least the threshold that function
    alone carries. The app asks it through my_fair_use().premium_voices; we ask
    it directly. Neither side re-derives it, so neither side can drift.

    Why not read profiles.max_books here and compare? Because then the number
    would be written twice, and the day it moves one of the two halves of the
    product would keep offering a voice the other refuses.

    Returns (allowed, note), never raises:
        (True/False, None)          the database answered
        (None, "unavailable")       a non-transient refusal — 0105 is not
                                    applied yet, or the key may not execute it.
                                    The caller degrades; it must NOT fail a job.
        (None, "unreadable")        the RPC answered with a shape that is not a
                                    boolean. Also permanent — asking again gets
                                    the same reply — so the caller degrades.
        (None, "unread")            a timeout or transport failure. Nothing is
                                    known, and asking again may well work, so
                                    this is the ONLY note a caller may requeue
                                    on. It must never grant premium.

    Review finding: "unreadable" used to be folded into "unread". That was
    harmless while nothing requeued, but resolve_tier now does, and requeueing
    a job whose answer will never parse spends three attempts and then fails
    the lesson. The two are kept apart so the requeue can key on the one case
    that a retry can actually fix.
    """
    def call():
        res = _call_with_timeout(
            lambda: sb.rpc("premium_voices_allowed", {"uid": owner_id}).execute(), _TIER_TIMEOUT_S)
        data = getattr(res, "data", None)
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, bool):
            # A shape we do not understand is not an answer.
            raise _UnreadableAnswer(f"premium_voices_allowed returned {data!r}")
        return data
    val, exc = _retrying("premium_voices_allowed", owner_id, call)
    if exc is None and isinstance(val, bool):
        return val, None
    if isinstance(exc, _UnreadableAnswer):
        return None, "unreadable"
    if exc is not None and not _is_transient(exc):
        return None, "unavailable"
    return None, "unread"


def resolve_tier(sb: Client, owner_id: str) -> dict:
    """Everything the premium gate needs, in one dict:
        {"tier": str|None, "override": bool|None, "paid": bool,
         "premium": bool, "premium_note": str|None, "error": str|None}

    TWO different questions, deliberately kept apart since 0105:

    `paid` = "exempt from the credit gate". Unchanged: an override of ANY size
    still buys unlimited generation, and other code and the job logs read this.

    `premium` = "may hear the premium voices". The DATABASE decides
    (premium_voices_allowed: a paid tier, or a comp override at or above the
    threshold that SQL function alone carries). This is what worker/process.py
    hands to allow_premium. A comped account below the threshold is therefore
    `paid: True, premium: False` — unlimited kits, free voice — which is
    exactly the founder's 2026-09-05 decision.

    `premium_note` is None when the database answered. Otherwise it says why it
    did not, and the three reasons are kept apart because only one of them is
    worth another attempt:
        "unavailable"  0105 not applied, or the key may not execute it.
        "unreadable"   the RPC replied with something that is not a boolean.
        "unread"       a timeout or transport failure.
    The first two are permanent, so premium degrades to the paid tiers alone
    and the job renders with the free voice. The third can be requeued — see
    the raise below for the one account shape that is.

    A read that FAILED is recorded as None, never as False: `override: False`
    means "read it, not comped", and writing that for an unread row would be
    an audit record claiming the account was checked. Raises
    TransientTierError when EITHER read failed to say "paid" and the RPC is
    not known to be broken — the first draft raised only when BOTH failed,
    which let a Pro teacher whose RPC timed out, or the comped founder whose
    profile read timed out, render FREE with error=None. Requeue is cheap;
    a paying customer hearing the free voice over a timeout is not.

    Since the review, the SAME rule covers the premium read: a comped account
    whose premium_voices_allowed call TIMED OUT is requeued too, rather than
    quietly rendered with the free voice. See the note at that raise for why
    only that one case, and only the timeout."""
    if not owner_id:
        raise ValueError("resolve_tier: owner_id is required")
    from shared.tts.registry import PAID_TIERS
    override = premium_override_for(sb, owner_id)
    tier = plan_tier_for(sb, owner_id)

    # The two reads that settle `paid` are asked FIRST, and a job neither of
    # them can answer goes back to the queue before the premium RPC is called
    # at all. Review finding: the premium answer is discarded on this path, so
    # fetching it up front spent another _TIER_RETRIES x _TIER_TIMEOUT_S — about
    # 31 s with production settings — on a job that was already doomed. Nothing
    # about the returned dict changes; only the doomed path gets its time back.
    unread = [n for n, v in (("override", override), ("tier", tier)) if v is None]
    doomed = bool(unread) and not override and tier not in PAID_TIERS
    if doomed and _PLAN_TIER_PROBE_OK is not False:
        raise TransientTierError(
            f"plan tier unresolvable for {owner_id}: {', '.join(unread)} unread (transient)")

    allowed, note = premium_allowed_for(sb, owner_id)

    # `premium` is the DB's answer when we have one. When we do not, degrade to
    # PAID TIERS ONLY — deliberately NOT to `override`, which is the whole
    # point of 0105: before it, any override at all (including the eleven
    # seeded accounts capped at 20 books) counted as premium. Under-offering
    # while the migration is pending is the small wrong; over-granting is the
    # one the founder asked us to stop.
    premium = allowed if allowed is not None else (tier in PAID_TIERS)

    # A COMPED account whose premium read TIMED OUT is requeued, exactly as a
    # Pro teacher whose plan_tier read times out already is. Review finding:
    # without this the two behaved differently for the same failure — the tier
    # read requeued, the premium read silently downgraded — and it is the
    # comped accounts (all seven of them plan_tier='trial' on prod) whose
    # entitlement lives ONLY in the premium answer, so a timeout was the one
    # way to hand the founder's own 100k accounts the free voice with nothing
    # but an ERROR line to show for it.
    #
    # Narrow on purpose, four ways:
    #   * `override is True` only. A paid tier already falls back to premium
    #     through PAID_TIERS below, so nothing is lost there and nothing needs
    #     requeueing; an uncomped free account's answer is False either way.
    #   * note == "unread" only. "unavailable" (0105 not applied, or the key
    #     may not execute it) and "unreadable" (a shape that will never parse)
    #     do not get better on a retry — they degrade to paid-tiers-only, which
    #     is what lets this deploy land BEFORE the migration.
    #   * `tier is not None`, i.e. plan_tier answered on the SAME connection a
    #     moment ago. This one is load-bearing and was found by an existing
    #     test (test_a_comp_needs_no_rpc_to_succeed): when the whole RPC
    #     surface is down, a comped account is supposed to keep rendering — it
    #     needs no RPC to establish `paid` — and requeueing it would spend the
    #     3-attempt cap and then FAIL the lesson. A requeue is only worth it
    #     when the evidence says this one function was slow, not the database.
    #   * both probes not-known-broken, the same guard the tier raise uses.
    # A comp BELOW the threshold still requeues: without the answer we just
    # failed to get, the worker cannot know its size — the threshold lives in
    # the migration alone, which is the point of 0105. That costs those eleven
    # seeded accounts a requeue they did not need, bounded at 3 attempts by
    # worker/run.py, and only while this one RPC is timing out.
    if (override is True and note == "unread" and tier is not None
            and _PLAN_TIER_PROBE_OK is not False and _PREMIUM_PROBE_OK is not False):
        raise TransientTierError(
            f"premium voices unresolvable for {owner_id}: premium_voices_allowed "
            f"unread (transient) for a comped account")

    def _out(**kw) -> dict:
        return {"tier": tier, "premium": premium, "premium_note": note, **kw}

    if override:
        return _out(override=True, paid=True, error=None)
    if tier in PAID_TIERS:
        return _out(override=override, paid=True, error=None)
    if doomed:
        # Only reachable with the probe at False: the RPC is known broken, so
        # requeueing would just burn the attempt cap. Render free, on the record.
        return _out(override=override, paid=False,
                    error="+".join(f"{n}_unread" for n in unread))
    return _out(override=override, paid=False, error=None)


def probe_plan_tier(sb: Client) -> Optional[bool]:
    """Boot-time: can this worker's key execute plan_tier at all?

    Three outcomes, kept apart on purpose. Success → True. An API error
    (permission denied, missing function) → False and a CRITICAL log: every
    account will resolve free until it is fixed, and that must not read as a
    quiet day. A timeout or transport error → None: the RPC is not known to
    be broken, so per-job failures still requeue — a network blip at boot must
    not switch the whole process into silent-free mode for its lifetime."""
    global _PLAN_TIER_PROBE_OK
    try:
        _call_with_timeout(
            lambda: sb.rpc("plan_tier", {"uid": "00000000-0000-0000-0000-000000000000"}).execute(),
            _TIER_TIMEOUT_S)
        _PLAN_TIER_PROBE_OK = True
        logging.getLogger("worker").info("PLAN TIER CHECK OK: worker can execute plan_tier().")
    except BaseException as exc:  # noqa: BLE001
        if _is_transient(exc):
            _PLAN_TIER_PROBE_OK = None
            logging.getLogger("worker").warning(
                "PLAN TIER CHECK inconclusive (%s: %r) — treating the RPC as good; "
                "per-job failures will requeue.", type(exc).__name__, exc)
        else:
            _PLAN_TIER_PROBE_OK = False
            logging.getLogger("worker").critical(
                "PLAN TIER CHECK FAILED: %s: %r — every account will resolve as FREE until "
                "this is fixed. Check that plan_tier(uuid) exists and the service role may "
                "execute it.", type(exc).__name__, exc)
    return _PLAN_TIER_PROBE_OK


def probe_premium_voices_allowed(sb: Client) -> Optional[bool]:
    """Boot-time: can this worker's key execute 0105's premium_voices_allowed?

    The same tri-state as probe_plan_tier, but a FALSE here is a warning and
    not a critical: the overwhelmingly likely cause is that 0105 has not been
    applied yet, which this branch is built to survive — every account degrades
    to paid-tiers-only and the jobs still render. What it buys is (a) one loud
    line at boot instead of one ERROR per job, and (b) the guard that stops
    resolve_tier requeueing a comped account against an RPC that is not there.

    A timeout leaves it None ("not known broken"), so a blip at boot does not
    disable the requeue path for the process lifetime — the same reasoning as
    the plan_tier probe."""
    global _PREMIUM_PROBE_OK
    try:
        _call_with_timeout(
            lambda: sb.rpc("premium_voices_allowed",
                           {"uid": "00000000-0000-0000-0000-000000000000"}).execute(),
            _TIER_TIMEOUT_S)
        _PREMIUM_PROBE_OK = True
        logging.getLogger("worker").info(
            "PREMIUM VOICE CHECK OK: worker can execute premium_voices_allowed().")
    except BaseException as exc:  # noqa: BLE001
        if _is_transient(exc):
            _PREMIUM_PROBE_OK = None
            logging.getLogger("worker").warning(
                "PREMIUM VOICE CHECK inconclusive (%s: %r) — treating the RPC as good.",
                type(exc).__name__, exc)
        else:
            _PREMIUM_PROBE_OK = False
            logging.getLogger("worker").warning(
                "PREMIUM VOICE CHECK FAILED: %s: %r — migration 0105 is probably not applied. "
                "Premium voices fall back to the PAID TIERS alone, so console comps (any size) "
                "will hear the free voice until it is. Jobs still render.",
                type(exc).__name__, exc)
    return _PREMIUM_PROBE_OK


def set_book_chapters(sb: Client, book_id: str, chapters: list[dict], status: str) -> None:
    sb.table("books").update({"chapters": chapters, "status": status}).eq("id", book_id).execute()


def set_chapter_parts(sb: Client, book_id: str, chapter_num: int, parts: list[dict],
                      expect_start: Optional[int] = None,
                      expect_end: Optional[int] = None) -> bool:
    """Replace ONE chapter's part map in books.chapters. Best-effort; never raises.

    Deliberately NOT set_book_chapters, for two reasons. That one also writes
    ``status``, and this runs in the middle of a generation — flipping a book's
    status from under the app is not this function's business. And it takes the
    whole array from a caller that may be holding a stale copy; this re-reads
    immediately before writing so every OTHER chapter is carried forward at its
    current value.

    The remaining race is benign and self-healing. Two chapters of one book can
    be generated concurrently (claim_next_job has no per-book exclusion and
    WORKER_CONCURRENCY may exceed 1), and if both land inside this read-modify-
    write one measurement is lost. What survives is the ESTIMATE for that
    chapter, which the next generation of it measures again — no corruption, no
    wrong number, just one more generation before the map is exact. Paying for a
    migration and an RPC to close that would buy nothing a retry does not.

    Returns True when the row was written.
    """
    try:
        rows = sb.table("books").select("chapters").eq("id", book_id).limit(1).execute().data or []
        chapters = (rows[0].get("chapters") if rows else None) or []
        if not isinstance(chapters, list):
            return False
        hit = False
        for ch in chapters:
            if isinstance(ch, dict) and str(ch.get("num")) == str(chapter_num):
                # The measurement belongs to the pages it was taken from. If the
                # re-read shows different bounds, a re-index or a heal moved this
                # chapter while the generation ran, and the stored map now
                # describes a different span — writing ours would overwrite fresh
                # truth with a stale count.
                if expect_start is not None and expect_end is not None:
                    try:
                        if (int(ch.get("start_page", -1)) != int(expect_start)
                                or int(ch.get("end_page", -1)) != int(expect_end)):
                            logging.getLogger("worker").info(
                                "part map skipped for %s ch%s — pages moved to %s-%s while "
                                "measuring %s-%s", book_id, chapter_num,
                                ch.get("start_page"), ch.get("end_page"), expect_start, expect_end,
                            )
                            return False
                    except (TypeError, ValueError):
                        return False
                ch["parts"] = parts
                hit = True
                break
        if not hit:
            return False
        sb.table("books").update({"chapters": chapters}).eq("id", book_id).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").warning(
            "part map not persisted for %s ch%s: %s", book_id, chapter_num, exc
        )
        return False


def set_book_meta(
    sb: Client,
    book_id: str,
    grade: Optional[str],
    subject: Optional[str],
    title: Optional[str] = None,
    author: Optional[str] = None,
) -> None:
    # grade/subject are always written (detection is best-effort → may be None). title/author
    # are only touched when a value is supplied, so we never wipe a teacher-entered title.
    patch: dict = {"grade": grade, "subject": subject}
    if title is not None:
        patch["title"] = title
    if author is not None:
        patch["author"] = author
    sb.table("books").update(patch).eq("id", book_id).execute()


def set_book_cover(sb: Client, book_id: str, cover_path: str) -> None:
    sb.table("books").update({"cover_path": cover_path}).eq("id", book_id).execute()


def set_book_health(sb: Client, book_id: str, health: dict) -> None:
    """Persist the Book Health Score (books.health). Best-effort: a deployment
    whose migration hasn't added the column must not fail indexing."""
    try:
        sb.table("books").update({"health": health}).eq("id", book_id).execute()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").warning("book health not persisted for %s: %s", book_id, exc)


def set_chapter_grounding(
    sb: Client,
    book_id: str,
    chapter_num: int,
    chapter_title: str,
    concepts: dict,
    script_text: Optional[str] = None,
) -> None:
    """Persist a chapter's grounding (Agent-2 concept analysis + the lesson
    narration text) so the AI Tutor can answer STRICTLY from this chapter.
    Upsert keyed (book_id, chapter_num) — shared across every generation of the
    chapter. Best-effort: a deployment missing app migration 0025 must not fail
    the generation. `script_text` is only written when present, so a docx-only
    generation never wipes a prior lesson's script."""
    try:
        row = {
            "book_id": book_id,
            "chapter_num": int(chapter_num),
            "chapter_title": chapter_title,
            "concepts": concepts,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if script_text:
            row["script_text"] = script_text
        sb.table("chapter_grounding").upsert(row, on_conflict="book_id,chapter_num").execute()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").warning(
            "chapter grounding not persisted for %s ch%s: %s", book_id, chapter_num, exc
        )


def get_chapter_source_text(sb: Client, book_id: str, chapter_num: int) -> Optional[str]:
    """Return a chapter's cached source text (Claude-vision OCR of a scanned book),
    or None. Keyed (book_id, chapter_num) so a scanned chapter is transcribed ONCE
    and reused by every later generation of ANY kind, for ANY owner. Best-effort:
    a deployment missing app migration 0036 just means no cache (re-transcribe)."""
    try:
        res = (
            sb.table("chapter_grounding")
            .select("source_text")
            .eq("book_id", book_id)
            .eq("chapter_num", int(chapter_num))
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return (rows[0].get("source_text") if rows else None) or None
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").warning(
            "chapter source_text lookup failed for %s ch%s: %s", book_id, chapter_num, exc
        )
        return None


def get_chapter_analysis(sb: Client, book_id: str, chapter_num: int) -> Optional[dict]:
    """Return the chapter's cached FULL Agent-2 analysis (set_chapter_grounding
    persists the whole MasterAnalysis dump into chapter_grounding.concepts), or
    None. Reused by later artifact jobs of the SAME chapter so the analysis LLM
    call — the single biggest per-job cost — is paid once per chapter, not once
    per artifact. Only v2 (chunked, full-coverage) analyses with real content
    qualify: a v1 row predates the truncation fix and must be re-analysed."""
    try:
        res = (
            sb.table("chapter_grounding")
            .select("concepts")
            .eq("book_id", book_id)
            .eq("chapter_num", int(chapter_num))
            .limit(1)
            .execute()
        )
        rows = res.data or []
        a = rows[0].get("concepts") if rows else None
        if (
            isinstance(a, dict)
            and a.get("analyzer_version") == 2
            and (a.get("concepts") or {}).get("concepts")
            and (a.get("episodes") or {}).get("episodes")
        ):
            return a
        return None
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").warning(
            "chapter analysis lookup failed for %s ch%s: %s", book_id, chapter_num, exc
        )
        return None


def set_chapter_source_text(sb: Client, book_id: str, chapter_num: int, text: str) -> None:
    """Cache a chapter's transcribed source text (upsert keyed book_id+chapter_num).
    Best-effort: never fails the generation, and does not disturb the concepts/
    script_text columns written later by set_chapter_grounding."""
    try:
        sb.table("chapter_grounding").upsert(
            {
                "book_id": book_id,
                "chapter_num": int(chapter_num),
                "source_text": text,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="book_id,chapter_num",
        ).execute()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").warning(
            "chapter source_text not cached for %s ch%s: %s", book_id, chapter_num, exc
        )


def clear_chapter_source_text(sb: Client, book_id: str, chapter_num: int) -> None:
    """Null a chapter's cached OCR AND its cached analysis — called when its
    pages MOVE (relocation / re-index drift), so the next generation
    re-transcribes and re-analyses the new pages instead of reusing content of
    the old ones. (The analysis cache lives in `concepts`; script_text is left
    alone — an existing lesson's narration is still that lesson's.) Best-effort."""
    try:
        sb.table("chapter_grounding").update(
            {"source_text": None, "concepts": None, "updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("book_id", book_id).eq("chapter_num", int(chapter_num)).execute()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").warning(
            "chapter source_text not cleared for %s ch%s: %s", book_id, chapter_num, exc
        )


def clear_book_heal(sb: Client, book_id: str) -> None:
    """Drop every generation-time relocation override for a book. Called on
    re-index: the freshly detected+healed book.chapters is now authoritative, so a
    stale per-chapter override (which is consulted BEFORE book.chapters) must not
    shadow it. Leaves source_text alone. Best-effort."""
    try:
        sb.table("chapter_grounding").update(
            {"heal_status": None, "heal_start_page": None, "heal_end_page": None}
        ).eq("book_id", book_id).execute()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").warning("book heal not cleared for %s: %s", book_id, exc)


def get_chapter_heal(sb: Client, book_id: str, chapter_num: int) -> Optional[dict]:
    """Read a chapter's generation-time relocation override (chapter_grounding
    heal_* columns), or None. Consulted BEFORE trusting book.chapters so a book
    indexed with wrong pages self-heals once: ``{start_page, end_page, status,
    source_text}`` where status is 'ok' (use these pages) or 'not_found' (the
    topic isn't in the book — fail fast). Best-effort: a deployment missing the
    heal_* columns just means no override."""
    try:
        res = (
            sb.table("chapter_grounding")
            .select("heal_start_page,heal_end_page,heal_status,source_text")
            .eq("book_id", book_id)
            .eq("chapter_num", int(chapter_num))
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows or not rows[0].get("heal_status"):
            return None
        r = rows[0]
        return {
            "start_page": r.get("heal_start_page"),
            "end_page": r.get("heal_end_page"),
            "status": r.get("heal_status"),
            "source_text": r.get("source_text"),
        }
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").warning(
            "chapter heal lookup failed for %s ch%s: %s", book_id, chapter_num, exc
        )
        return None


def set_chapter_heal(
    sb: Client,
    book_id: str,
    chapter_num: int,
    start_page: Optional[int],
    end_page: Optional[int],
    source_text: Optional[str],
    status: str,
) -> None:
    """Persist a per-chapter relocation override (upsert keyed book_id+chapter_num).
    status='ok' with the corrected pages + confirmed OCR, or 'not_found' so a
    genuinely-absent topic fails fast on every later request instead of re-running
    a minutes-long relocation. Writes only the columns it sets, so it never wipes
    the concepts/script_text a prior generation stored. Best-effort."""
    try:
        row: dict = {
            "book_id": book_id,
            "chapter_num": int(chapter_num),
            "heal_status": status,
            "heal_start_page": int(start_page) if start_page is not None else None,
            "heal_end_page": int(end_page) if end_page is not None else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if source_text:
            row["source_text"] = source_text
        sb.table("chapter_grounding").upsert(row, on_conflict="book_id,chapter_num").execute()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").warning(
            "chapter heal not persisted for %s ch%s: %s", book_id, chapter_num, exc
        )


def insert_tutor_seed_qa(
    sb: Client,
    book_id: str,
    chapter_num: int,
    rows: list[dict],
) -> int:
    """Bank pre-computed tutor Q&A (see worker.tutor_warm) into the shared answer
    cache so the FIRST student to ask a common question gets an instant, $0 reply.
    Seed rows are marked is_verified so the conservative serve-rule replays them on
    a fuzzy match. Skips any question already cached for the chapter (idempotent —
    re-generating a lesson won't duplicate). Best-effort: never fails a generation."""
    try:
        existing = (
            sb.table("tutor_qa")
            .select("question_norm")
            .eq("book_id", book_id)
            .eq("chapter_num", int(chapter_num))
            .execute()
        )
        seen = {r.get("question_norm") for r in (existing.data or [])}
        fresh = [
            {
                "book_id": book_id,
                "chapter_num": int(chapter_num),
                "question_text": r["question_text"],
                "question_norm": r["question_norm"],
                "answer_text": r["answer_text"],
                "is_verified": True,  # curated at build time → safe to serve on a fuzzy match
            }
            for r in rows
            if r.get("question_norm") and r["question_norm"] not in seen
        ]
        if not fresh:
            return 0
        sb.table("tutor_qa").insert(fresh).execute()
        return len(fresh)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").warning(
            "tutor seed Q&A not banked for %s ch%s: %s", book_id, chapter_num, exc
        )
        return 0


# ── storage ──────────────────────────────────────────────────────────

# Storage transfers cross Railway → Supabase's edge over HTTP/2 and carry the
# largest payloads the worker moves: a whole textbook down, a rendered lesson
# up. Either can be cut by the peer mid-body. Measured in prod 2026-09-03: six
# threads fetched the same 30 MB PDF at once and the edge reset one stream
# (`<StreamReset stream_id:1, error_code:2, remote_reset:True>`), which failed
# that worksheet at progress 0 — a credit's worth of work lost to a two-second
# blip, with nothing to retry it. Both transfers are idempotent (a download has
# no side effect; uploads are upsert), so a second attempt is always safe.
_TRANSFER_ATTEMPTS = 3
_TRANSFER_BACKOFF_SECONDS = (2.0, 5.0)
# Jitter: each wait is the base plus up to this fraction of it, drawn per
# attempt. The incident was SIX threads reset by the same edge in the same
# second; on a fixed schedule all six re-hit it in lock-step at t+2 s and
# again at t+7 s, which is the shape most likely to be reset again. Additive
# jitter keeps the base as the MINIMUM spacing (the point of the schedule) and
# spreads the retries across [base, 1.5 × base].
_TRANSFER_JITTER_FRACTION = 0.5


def _transfer_delay(attempt: int) -> float:
    """The wait before retry number ``attempt`` (1-based): the schedule's base
    for that attempt plus a random share of it. Pure apart from the draw, so
    a test can pin ``random.random`` and assert the exact figure."""
    base = _TRANSFER_BACKOFF_SECONDS[min(attempt, len(_TRANSFER_BACKOFF_SECONDS)) - 1]
    return base * (1.0 + _TRANSFER_JITTER_FRACTION * random.random())


def _is_transient_transfer_error(exc: BaseException) -> bool:
    """A failure worth one more try: the connection or stream died
    (httpx.TransportError — reset, disconnect, refused, timeout) or the storage
    edge answered 5xx. A 4xx (missing object, forbidden) is not transient and
    must surface at once.

    Classified on what storage3 ACTUALLY raises (2.31.0, file_api._request):
      * a JSON error body → StorageApiError(message, code, status) — ONE string
        arg, the status only on ``.status`` (str or int);
      * a non-JSON 5xx body (an edge HTML page, an empty 504) → the
        json.JSONDecodeError escapes, chained to the httpx.HTTPStatusError;
      * a JSON body without storage-api keys (a gateway's {"message": …}) →
        a KeyError, then an AttributeError from ``resp.text`` — chained twice.
    So the chain (__cause__ / __context__) is walked, and the verdict comes from
    the first link that names a transport failure, a 5xx response, or a 5xx
    ``.status``. A first cut matched ``'statusCode': '503'`` in the message —
    which that message never contains — and was dead code (adversarial review,
    2026-09-03)."""
    try:
        import httpx
    except Exception:  # noqa: BLE001 — httpx is a supabase dependency; be safe anyway
        httpx = None  # type: ignore[assignment]
    seen: set[int] = set()
    e: Optional[BaseException] = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if httpx is not None:
            if isinstance(e, httpx.TransportError):
                return True
            if isinstance(e, httpx.HTTPStatusError):
                resp = getattr(e, "response", None)
                if resp is not None and int(getattr(resp, "status_code", 0) or 0) >= 500:
                    return True
        status = getattr(e, "status", None)  # storage3 StorageApiError
        if status is not None and str(status).startswith("5"):
            return True
        e = e.__cause__ or e.__context__
    return False


def _transfer_with_retry(what: str, op):
    """Run a storage transfer, retrying a transient transport failure
    ``_TRANSFER_ATTEMPTS`` times with backoff. Anything else re-raises at once."""
    log_ = logging.getLogger("worker")
    for attempt in range(1, _TRANSFER_ATTEMPTS + 1):
        try:
            return op()
        except Exception as exc:  # noqa: BLE001
            if attempt >= _TRANSFER_ATTEMPTS or not _is_transient_transfer_error(exc):
                raise
            delay = _transfer_delay(attempt)
            log_.warning("%s failed on attempt %d/%d (%s: %s); retrying in %.1fs",
                         what, attempt, _TRANSFER_ATTEMPTS, type(exc).__name__, exc, delay)
            time.sleep(delay)


def download_book(sb: Client, storage_path: str, dest: str | Path) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = _transfer_with_retry(
        f"download of {storage_path}",
        lambda: sb.storage.from_("uploads").download(storage_path),
    )
    dest.write_bytes(data)
    return dest


_CONTENT_TYPES = {
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".mp4": "video/mp4",
    ".pdf": "application/pdf",
    ".png": "image/png",
}


def upload_artifact(sb: Client, local_path: str | Path, dest_path: str) -> str:
    """Upload a local file to the `artifacts` bucket; return its storage path."""
    local_path = Path(local_path)
    ctype = _CONTENT_TYPES.get(local_path.suffix.lower(), "application/octet-stream")
    payload = local_path.read_bytes()
    # upsert: a retried upload overwrites its own partial predecessor.
    _transfer_with_retry(
        f"upload of {dest_path}",
        lambda: sb.storage.from_("artifacts").upload(
            dest_path,
            payload,
            {"content-type": ctype, "upsert": "true"},
        ),
    )
    return dest_path


def add_artifact_row(sb: Client, generation_id: str, kind: str, storage_path: str) -> None:
    sb.table("artifacts").insert(
        {"generation_id": generation_id, "kind": kind, "storage_path": storage_path}
    ).execute()


def clear_artifacts(sb: Client, generation_id: str) -> None:
    """Delete a generation's existing artifact rows before (re)generating, so a
    re-run — e.g. after the worker was killed mid-generation and the reaper
    requeued the job — doesn't leave DUPLICATE deck/video rows. Storage paths are
    deterministic and overwritten on re-upload, so only the rows need clearing.
    Best-effort (a first run just deletes nothing)."""
    try:
        sb.table("artifacts").delete().eq("generation_id", generation_id).execute()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").warning("artifact rows not cleared for %s: %s", generation_id, exc)


# ── AI Tutor sketch queue (Phase 2) ──────────────────────────────────
# tutor_sketch is its OWN lightweight queue + cache (app migration 0028), kept off
# the generations/jobs rail so coach doodles never clutter the teacher's library.

def claim_next_sketch(sb: Client) -> Optional[dict]:
    """Atomically-ish claim the oldest queued sketch (queued → processing)."""
    res = (
        sb.table("tutor_sketch")
        .select("*")
        .eq("status", "queued")
        .order("created_at")
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    row = res.data[0]
    upd = (
        sb.table("tutor_sketch")
        .update({"status": "processing", "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", row["id"])
        .eq("status", "queued")  # guard against a racing worker
        .execute()
    )
    if not upd.data:
        return None  # lost the race
    return row


def set_sketch_done(sb: Client, sketch_id: str, storage_path: str) -> None:
    sb.table("tutor_sketch").update(
        {"status": "done", "storage_path": storage_path, "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", sketch_id).execute()


def set_sketch_error(sb: Client, sketch_id: str, error: str) -> None:
    sb.table("tutor_sketch").update(
        {"status": "error", "error": (error or "")[:500], "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", sketch_id).execute()


def upload_sketch(sb: Client, local_path: str | Path, dest_path: str) -> str:
    """Upload a rendered sketch MP4 to the private `tutor-sketch` bucket."""
    sb.storage.from_("tutor-sketch").upload(
        dest_path,
        Path(local_path).read_bytes(),
        {"content-type": "video/mp4", "upsert": "true"},
    )
    return dest_path
