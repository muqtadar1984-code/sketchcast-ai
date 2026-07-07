"""Generation worker — polls the `jobs` table and processes queued jobs.

Run from the sketchcast repo root:
    python -m worker.run          # poll forever
    python -m worker.run --once   # process one job (or exit if none) — handy for testing
"""

from __future__ import annotations

import logging
import os
import sys
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


def _support_agent_enabled() -> bool:
    return os.getenv("SUPPORT_AGENT_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


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
                    "context": {"error": error[:300]},
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

    AI-Tutor sketches are claimed FIRST: they're small, interactive (a student is
    waiting live) and drain fast, so they shouldn't sit behind a long batch lesson.
    They're monthly-capped per account, so they can't starve the lesson queue."""
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

    job = db.claim_next_job(sb)
    if not job:
        return False
    job_type = job.get("type")
    gen_id = job.get("generation_id")
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
    except Exception as exc:  # noqa: BLE001
        log.error("Job %s failed: %s", job["id"], exc)
        log.error(traceback.format_exc())
        try:
            # A support job's generation_id is the REPORTED (possibly healthy,
            # assigned) generation — an agent crash must never flip it to error.
            mirror_gen = None if job_type == "support_diagnose" else gen_id
            db.finish_job(sb, job["id"], mirror_gen, error=str(exc)[:500])
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
    return True


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

    once = "--once" in sys.argv
    if once:
        handled = run_once(sb)
        log.info("Done (%s).", "processed 1 job" if handled else "no queued jobs")
        return

    log.info("Worker started; polling every %ss", POLL_SECONDS)
    while True:
        try:
            worked = run_once(sb)
        except Exception as exc:  # noqa: BLE001
            log.error("Poll error: %s", exc)
            worked = False
        if not worked:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
