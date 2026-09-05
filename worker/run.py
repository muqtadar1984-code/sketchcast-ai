"""Generation worker — polls the `jobs` table and processes queued jobs.

Run from the sketchcast repo root:
    python -m worker.run          # poll forever
    python -m worker.run --once   # process one job (or exit if none) — handy for testing

Concurrency: set WORKER_CONCURRENCY>1 to run several jobs at once IN ONE process
(threads — the heavy work is ffmpeg/Claude/TTS, all subprocess/IO-bound, so it
truly overlaps). Default 1 reproduces the historical single-threaded behaviour.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path

# Make the agent packages importable regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")  # worker/.env
load_dotenv()  # also pick up a repo-root .env if present

from worker import client as db  # noqa: E402
from worker.process import index_book, process_generation  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")

POLL_SECONDS = int(os.getenv("WORKER_POLL_SECONDS", "5"))
# How many jobs to run at once in this single process. 1 = the old serial
# behaviour. Size the Railway instance's vCPU/RAM for the number of concurrent
# VIDEO renders you set (docs are light; videos are the heavy ones).
# Video rasterization may run in a process pool (RENDER_PROCESSES, see
# agent6_animation/video_composer.py): ONE pool per worker process, shared by
# every job thread here, so total render CPU is bounded by the pool size and
# concurrent lessons queue behind each other on it.
WORKER_CONCURRENCY = max(1, int(os.getenv("WORKER_CONCURRENCY", "1")))

# Documents (papers / plans / activities / case studies / exams) are fast — one
# model call + a .docx — so they jump AHEAD of long video renders, the same
# "fast lane" idea already used for tutor sketches and support diagnoses. Keeps
# a teacher's test paper from sitting behind a 15-minute lesson render. The
# slide deck ('deck', 2026-09) is the same shape: one authoring call + a .pptx.
DOC_JOB_TYPES = ["lesson_plan", "activity", "worksheet", "exam_paper", "case_study", "exam", "deck"]

# Job ids this process is ACTIVELY running. The crash-reaper must never requeue
# these — with concurrency, a live 'processing' row is not an orphan. (Sketches
# are seconds-long, far under the stale window, so they aren't tracked.)
_inflight_lock = threading.Lock()
_inflight_jobs: set[str] = set()


def _inflight_add(job_id: str) -> None:
    with _inflight_lock:
        _inflight_jobs.add(job_id)


def _inflight_remove(job_id: str) -> None:
    with _inflight_lock:
        _inflight_jobs.discard(job_id)


def _inflight_snapshot() -> set:
    with _inflight_lock:
        return set(_inflight_jobs)


def _support_agent_enabled() -> bool:
    return os.getenv("SUPPORT_AGENT_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


# How much of a failure message survives to the reader. NOT a storage limit:
# platform_issues.context is jsonb and jobs.error is text, neither of them
# bounded — 300 and 500 were budgets we picked, and both were picked too small.
#
# Measured on Sara Hamaydeh's lost lesson (gen eb12963c, issue 24b6cadd,
# 2026-09-05). The script failure builds its message DELIBERATELY long: the
# preamble, then the JSON fault window, then the reply's first 220 and last 160
# characters, then the path of the saved dump — because the reply itself dies
# with the container and this line is all a later reader has. It was 1,018
# characters. jobs.error kept 500, which stopped a few words past the
# malformation; platform_issues.context kept 300, which stopped BEFORE it, on
# "… natural land". The console reader therefore had strictly less than the job
# row, and the incident was diagnosed by inference instead of by reading.
#
# 4,000 holds that whole message with room for a longer fault window, and still
# refuses to let a pathological reply become the row.
_EVIDENCE_CHARS = 4000


def _evidence(error, limit: int = _EVIDENCE_CHARS) -> str:
    """A failure message trimmed to `limit`, from the MIDDLE.

    A head-only cut is the worst possible choice here: this message is built
    head-to-tail as preamble → fault window → reply excerpts → dump path, so
    cutting the tail loses the fault AND the excerpts AND the path, in that
    order. Removing the middle keeps both ends — what broke, and where the
    evidence was saved — and says how much it dropped, so nobody mistakes a
    trimmed message for a short one.
    """
    text = str(error or "")
    if len(text) <= limit:
        return text
    marker = f"\n… [{len(text) - limit} chars elided] …\n"
    if limit <= len(marker):
        return text[:limit]     # no room to say anything; never overrun the budget
    keep = limit - len(marker)
    head = keep // 2
    return text[:head] + marker + text[len(text) - (keep - head):]


def _auto_file_support_issue(sb, job: dict, error: str) -> None:
    """A failed job auto-triggers the support agent: file a console issue for
    the content owner and queue a diagnosis job. Never for support jobs
    themselves (no recursion), never twice for the same generation while an
    issue is still open, and never allowed to break the failure path."""
    try:
        gen_id = job.get("generation_id")
        if not gen_id:
            return  # index failures already surface on the book row
        gen = sb.table("generations").select("owner_id, book_id, kind").eq("id", gen_id).maybe_single().execute()
        gen_d = getattr(gen, "data", None)
        if not gen_d:
            return
        open_q = (
            sb.table("platform_issues")
            .select("id")
            .eq("generation_id", gen_id)
            .eq("trigger_source", "auto")
            .neq("status", "resolved")
            .limit(1)
            .execute()
        )
        if getattr(open_q, "data", None):
            return  # an open auto-issue already covers this generation
        ins = (
            sb.table("platform_issues")
            .insert(
                {
                    "reporter_id": gen_d["owner_id"],
                    "category": "generation_failed",
                    "trigger_source": "auto",
                    "title": f"Generation failed: {gen_d.get('kind') or 'lesson'}",
                    "description": None,
                    "generation_id": gen_id,
                    "book_id": gen_d.get("book_id"),
                    "job_id": job["id"],
                    # The whole point of this row: the reporter is waiting
                    # and a human (or the diagnosis agent) reads this line.
                    "context": {"error": _evidence(error)},
                }
            )
            .execute()
        )
        issue_id = (getattr(ins, "data", None) or [{}])[0].get("id")
        if issue_id:
            sb.table("jobs").insert(
                {"type": "support_diagnose", "status": "queued", "issue_id": issue_id,
                 "generation_id": gen_id, "book_id": gen_d.get("book_id")}
            ).execute()
            log.info("Support agent queued for failed job %s (issue %s)", job["id"], issue_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("support auto-trigger skipped: %s", exc)


def run_once(sb) -> bool:
    """Claim and process one unit of work. Returns True if something was handled.

    Priority (fast/interactive work never sits behind a long batch render):
      1. AI-Tutor sketches — a student is waiting live; tiny SVG→MP4.
      2. Support diagnoses — a reporter is watching an issue's status.
      3. Documents — a teacher's papers/plans; one model call + a .docx.
      4. Everything else — video lessons (presentation), index_book.
    All of 1–3 are bounded/fast, so they can't starve the lesson queue."""
    sketch = db.claim_next_sketch(sb)
    if sketch:
        log.info("Claimed tutor sketch %s (book=%s)", sketch["id"], sketch.get("book_id"))
        try:
            from worker.tutor_sketch import render_sketch

            render_sketch(sb, sketch)  # self-contained: marks its own done/error
        except Exception as exc:  # noqa: BLE001
            log.error("Sketch %s failed: %s", sketch["id"], exc)
            try:
                db.set_sketch_error(sb, sketch["id"], str(exc))
            except Exception:  # noqa: BLE001
                pass
        return True

    job = (
        db.claim_next_job(sb, job_type="support_diagnose")
        or db.claim_next_job(sb, job_type=DOC_JOB_TYPES)
        or db.claim_next_job(sb)
    )
    if not job:
        return False
    job_type = job.get("type")
    gen_id = job.get("generation_id")
    _inflight_add(job["id"])
    log.info("Claimed %s job %s (generation=%s book=%s)", job_type, job["id"], gen_id, job.get("book_id"))
    try:
        if job_type == "index_book":
            index_book(sb, job)
            db.finish_job(sb, job["id"])  # process_generation finishes itself; index_book doesn't
        elif job_type == "support_diagnose":
            from support_agent.agent import run_support_job

            run_support_job(sb, job)
            db.finish_job(sb, job["id"])
        else:
            process_generation(sb, job, gen_id)
    except db.TransientTierError as exc:
        # The account's plan could not be read right now, though the RPC is
        # known good. Rendering FREE would hand a paying customer the wrong
        # voice over a timeout, so the job goes back to the queue instead —
        # under the same attempt cap the reaper uses for poison pills.
        att = int(job.get("attempts") or 0)
        if att >= 3:
            log.error("Job %s: plan tier unresolvable after %d attempts: %r", job["id"], att, exc)
            try:
                db.finish_job(sb, job["id"], gen_id,
                              error=_evidence(f"plan tier unresolvable: {exc!r}"))
            except Exception:  # noqa: BLE001
                pass
            # Same hook the generic failure path gets: a tier outage that
            # exhausts its retries is a console issue, not a quiet log line.
            if _support_agent_enabled():
                _auto_file_support_issue(sb, job, f"plan tier unresolvable: {exc!r}")
        else:
            log.warning("Job %s requeued (attempt %d): %r", job["id"], att + 1, exc)
            try:
                # The status guard is the reaper's: if the console or the
                # reaper moved this row while we ran, we must not overwrite it.
                upd = (sb.table("jobs")
                       .update({"status": "queued", "progress": 0, "attempts": att + 1})
                       .eq("id", job["id"]).eq("status", "processing").execute())
                if upd.data:
                    # Only a BUILDER job may relabel its generation (the
                    # observer-job rule in db.generation_to_mirror). Only
                    # process_generation raises TransientTierError today, so
                    # this is always a builder — routed through the rule so
                    # that stays true if an observer job ever reaches it.
                    db.mirror_generation_status(sb, db.generation_to_mirror(job), "queued")
            except Exception as exc2:  # noqa: BLE001
                log.error("Job %s requeue failed: %s", job["id"], exc2)
    except Exception as exc:  # noqa: BLE001
        log.error("Job %s failed: %s", job["id"], exc)
        log.error(traceback.format_exc())
        try:
            # A support job's generation_id is the REPORTED (possibly healthy,
            # assigned) generation — an agent crash must never flip it to error.
            # One rule for every writer: db.generation_to_mirror decides.
            mirror_gen = db.generation_to_mirror(job)
            # Same budget as the issue row: a 500-char head cut stopped just
            # past the malformation on gen eb12963c and lost the excerpts and
            # the dump path that the message was built to carry.
            db.finish_job(sb, job["id"], mirror_gen, error=_evidence(str(exc)))
        except Exception:  # noqa: BLE001
            pass
        # Stop the UI's "Finding chapters…" spinner if indexing failed.
        if job_type == "index_book" and job.get("book_id"):
            try:
                db.set_book_chapters(sb, job["book_id"], [], "error")
            except Exception:  # noqa: BLE001
                pass
        # A failed generation triggers the diagnosis agent (flag-gated; never
        # for a support job itself — that would recurse).
        if _support_agent_enabled() and job_type not in ("support_diagnose", "index_book"):
            _auto_file_support_issue(sb, job, str(exc))
    finally:
        _inflight_remove(job["id"])
    return True


def _worker_loop(idx: int) -> None:
    """One worker thread — its OWN Supabase client (thread safety), looping
    run_once and sleeping between empty polls."""
    sb = db.admin()
    while True:
        try:
            worked = run_once(sb)
        except Exception as exc:  # noqa: BLE001
            log.error("Poll error (worker %d): %s", idx, exc)
            worked = False
        if not worked:
            time.sleep(POLL_SECONDS)


def main() -> None:
    sb = db.admin()

    # Startup key check: with a privileged (service_role/secret) key this counts
    # ALL jobs; with a non-privileged key, RLS hides them and the count is 0.
    try:
        probe = sb.table("jobs").select("id", count="exact").limit(1).execute()
        total = getattr(probe, "count", None)
        if total and total > 0:
            log.info("KEY CHECK OK: worker can see %s job(s) — privileged key working.", total)
        else:
            log.error(
                "KEY CHECK FAILED: worker sees 0 jobs. The SUPABASE_SERVICE_ROLE_KEY "
                "is NOT privileged (it's the anon/publishable key). Use the secret/"
                "service_role key and redeploy."
            )
    except Exception as exc:  # noqa: BLE001
        log.error("KEY CHECK errored: %s", exc)

    # The premium-voice gate depends on plan_tier(). If this key cannot execute
    # it, every job would quietly resolve to FREE — which looks like a slow
    # day, not an outage. Say so once, loudly, at boot.
    db.probe_plan_tier(sb)
    # And 0105's helper, which decides the premium voice for COMPED accounts.
    # A failure here is survivable (paid tiers still get their voice) but it is
    # the difference between "the migration is pending" and a silent downgrade,
    # so it is said once at boot rather than once per job.
    db.probe_premium_voices_allowed(sb)

    once = "--once" in sys.argv
    if once:
        # No startup reap in --once: it would requeue the live daemon's in-flight
        # job if run against the same project while the daemon is mid-generation.
        handled = run_once(sb)
        log.info("Done (%s).", "processed 1 job" if handled else "no queued jobs")
        return

    # Reaper (startup): a worker restart (deploy / crash / OOM) leaves the job(s)
    # it was running stranded in 'processing', and claims only ever pick 'queued'.
    # Nothing is in flight yet at startup, so every 'processing' row is orphaned —
    # requeue them all for instant recovery.
    rj, rs = db.requeue_stale_jobs(sb), db.requeue_stale_sketches(sb)
    if rj or rs:
        log.warning("Reaper: requeued %d job(s) + %d sketch(es) left 'processing' by a prior run", rj, rs)

    stale_min = int(os.getenv("STALE_JOB_MINUTES", "15"))
    log.info("Worker started; concurrency=%d, polling every %ss (stale reaper %sm)",
             WORKER_CONCURRENCY, POLL_SECONDS, stale_min)

    # Worker threads do the claiming + processing.
    for i in range(WORKER_CONCURRENCY):
        threading.Thread(target=_worker_loop, args=(i,), daemon=True, name=f"worker-{i}").start()

    # Main thread = the windowed crash-reaper. It EXCLUDES the jobs this process
    # is actively running (in-flight), so a long render is never requeued and
    # double-run; only a genuinely orphaned 'processing' row (a failed finish_job
    # write, an old row) is recovered. Runs while the queue is busy.
    while True:
        time.sleep(60)
        try:
            r = db.requeue_stale_jobs(sb, older_than_minutes=stale_min, exclude_ids=_inflight_snapshot())
            db.requeue_stale_sketches(sb, older_than_minutes=stale_min)
            if r:
                log.warning("Reaper: requeued %d stale job(s) (>%sm in 'processing', not in-flight)", r, stale_min)
        except Exception as exc:  # noqa: BLE001
            log.error("Reaper error: %s", exc)


if __name__ == "__main__":
    main()
