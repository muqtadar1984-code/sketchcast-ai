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
import signal
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
# On SIGTERM, how long to let in-flight work FINISH before handing back what
# is still held (see _serve). Must sit inside Railway's drain — railway.json
# sets deploy.drainingSeconds = 30 — with room for the hand-back's round trips.
SHUTDOWN_GRACE_SECONDS = float(os.getenv("SHUTDOWN_GRACE_SECONDS", "20"))
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

# Observer jobs (db.OBSERVER_JOB_TYPES) report on or read from content without
# building a generation — and they take OPPOSITE lanes. A support diagnosis is
# one Sonnet call with a teacher waiting on the answer: it is claimed first. A
# topic harvest costs no model or image quota, but it re-downloads and
# re-extracts the whole PDF — CPU and storage egress a real user's render would
# otherwise have — so it is claimed LAST, only when no builder job (documents,
# presentations, decks, index_book, exams) is queued. The generic lane excludes
# every observer type so a harvest can never slip in through it. A topic
# derive (catalogue Phase 2a) shares that last lane: one sequential text call
# per sub-strand (48 for Cambridge 0893), so it too waits for every builder.
# Phase 2b adds two more to the same lane: topic_article (one 16k-token text
# call) and figure_render — the one catalogue job that may make IMAGE calls,
# which is why it also re-checks the queue itself before every generation
# (catalogue/figures.py, builder_queued) and pauses when a builder appears.
# Phase 3 adds topic_questions (one text call drafting a topic's question
# bank, plus one coverage top-up) to the same lane. Phase 4 adds topic_publish:
# no model or image quota at all, but each part is several hundred MB of
# Supabase egress out and up to YouTube, which is bandwidth a teacher's render
# wants — so it takes the last lane too, and re-checks builder_queued between
# parts the way figure_render does before every generation.
OBSERVER_JOB_TYPES = ["support_diagnose", "topic_harvest", "topic_derive", "topic_article", "figure_render",
                      "topic_questions", "topic_publish"]
CATALOGUE_JOB_TYPES = ["topic_harvest", "topic_derive", "topic_article", "figure_render", "topic_questions",
                       "topic_publish"]  # the last lane


def _claim_catalogue_generation(sb):
    """The LAST lane (Phase 3, decision 12): a catalogue KIT generation —
    a presentation, deck or document whose job row carries
    ``params.catalogue = true`` (migration 0115 stamps it at insert). These
    are ordinary builder types, so every user lane above excludes them by
    the flag, and this lane claims them only when

      * no job a real user is waiting on is live (``builder_queued``: queued
        OR processing, any non-observer type, the flag ignored) — a kit's
        image calls share the one Vertex pool, and a batch during a
        teacher's render is a total image outage for that teacher; and
      * the off-peak window is open (``CATALOGUE_WINDOW_UTC``, default
        20:00-05:00 UTC; ``always`` ONLY for a supervised pilot — it lets a
        kit render into users' hours) — and, for a PRESENTATION, stays open
        for ``CATALOGUE_PRESENTATION_MARGIN_MIN`` more minutes (default 60):
        a render claimed at 04:50 would otherwise run through the hours
        teachers use. Documents and decks take a minute each and need no
        margin, so near the window's end only they are claimed.

    Claiming is the BEFORE half of the never-starve rule; the DURING half —
    a builder appearing while a kit renders — is worker/process.py's
    (``catalogue.kit.yield_to_users`` between parts, and the scene engine's
    contention probe before every image call).

    Imported lazily: catalogue.figures pulls the visual library in, and the
    poll loop must stay cheap when the answer is "nothing to do"."""
    from catalogue.figures import builder_queued
    from catalogue.kit import catalogue_window_open, presentation_margin_minutes

    if not catalogue_window_open() or builder_queued(sb):
        return None
    if catalogue_window_open(margin_minutes=presentation_margin_minutes()):
        return db.claim_next_job(sb, exclude_types=OBSERVER_JOB_TYPES, catalogue=True)
    return db.claim_next_job(sb, exclude_types=[*OBSERVER_JOB_TYPES, "presentation"], catalogue=True)


def _is_catalogue_job(job: dict | None) -> bool:
    """The one reading of the kit flag (worker.client.is_catalogue_params) —
    nothing from the catalogue package, so the failure path stays importable
    without its dependencies."""
    return db.is_catalogue_params((job or {}).get("params"))


def _fail_catalogue_kit(sb, job: dict, error: str) -> None:
    """A catalogue kit job that failed anywhere — before its prelude ran
    (the generation row unreadable, taken down, the tier unresolvable) as
    much as inside the build — must leave its kit ``failed`` (guarded), or
    the portal keeps Generate disabled on a kit that reads 'generating'
    forever and offers no Retry. process.py marks it too when it can;
    the guard makes the second write a no-op."""
    if not _is_catalogue_job(job):
        return
    db.mark_kit_failed(sb, db.kit_id_of(job.get("params")),
                       f"{job.get('type') or 'generation'} failed: {_evidence(error, 900)}")

# Work this process is ACTIVELY running, by id — the claimed ROW, not just the
# id, because a graceful shutdown hands each one back (release_held_jobs) and
# a job's requeue is fenced on the row's `attempts`. The crash-reaper must
# never requeue a held job — with concurrency, a live 'processing' row is not
# an orphan. Sketches are held too: they are claimed FIRST every poll, so a
# deploy mid-render is the likely case, and the new container's boot reap of
# sketches has already run by the time the old one is signalled.
_inflight_lock = threading.Lock()
_inflight_jobs: dict[str, dict] = {}
_inflight_sketches: dict[str, dict] = {}

# Set by SIGTERM / SIGINT (install_signal_handlers). Railway's teardown order
# is: the NEW deployment goes active, THEN the old container is sent SIGTERM
# and given RAILWAY_DEPLOYMENT_DRAINING_SECONDS before SIGKILL — and that
# drain DEFAULTS TO 0 (docs.railway.com/variables/reference), i.e. SIGKILL on
# the heels of SIGTERM. Until 2026-09-13 this repo set nothing (nixpacks.toml
# only names the start command; no Procfile), so railway.json now sets
# deploy.drainingSeconds = 30; without it nothing below gets to run. On the
# 13:56 UTC deploy that day the old container outlived the new one's boot by
# ~15 s: the new container's boot reap requeued the job the old one still
# held, the old one RE-CLAIMED it, and was removed with it — 'processing', no
# worker, until the 15-minute reaper. The drain is spent as: up to
# SHUTDOWN_GRACE_SECONDS letting in-flight work finish on its own (a document
# seconds from done is not re-bought by the next container), then two guarded
# UPDATEs per row still held — well inside the 30 s.
_shutdown = threading.Event()


def _inflight_add(job: dict) -> None:
    with _inflight_lock:
        _inflight_jobs[job["id"]] = job


def _inflight_remove(job_id: str) -> None:
    with _inflight_lock:
        _inflight_jobs.pop(job_id, None)


def _inflight_snapshot() -> set:
    with _inflight_lock:
        return set(_inflight_jobs)


def _held_jobs() -> list[dict]:
    with _inflight_lock:
        return list(_inflight_jobs.values())


def _sketch_add(sketch: dict) -> None:
    with _inflight_lock:
        _inflight_sketches[sketch["id"]] = sketch


def _sketch_remove(sketch_id: str) -> None:
    with _inflight_lock:
        _inflight_sketches.pop(sketch_id, None)


def _held_sketches() -> list[dict]:
    with _inflight_lock:
        return list(_inflight_sketches.values())


def _holding_work() -> bool:
    with _inflight_lock:
        return bool(_inflight_jobs or _inflight_sketches)


def _exit_now(signum=None, frame=None) -> None:
    """The SECOND signal: the operator wants out now (double Ctrl-C). Nothing
    here takes a lock, so it cannot deadlock against the first handler; the
    rows still held are the next container's boot reap to recover."""
    os._exit(1)


def request_shutdown(signum=None, frame=None) -> None:
    """The signal handler — sets the flag and nothing else (a handler must
    not touch the database). From here on run_once claims nothing, the
    worker threads fall out of their loops, and the main thread wakes from
    its reaper wait to let in-flight work finish and release the rest
    (_serve). A repeat of the same signal exits at once: Event.set() takes a
    plain Lock, so a second handler re-entering it mid-set would otherwise
    hang the main thread before anything was released."""
    if signum is not None:
        signal.signal(signum, _exit_now)
    if not _shutdown.is_set():
        log.warning("Shutdown requested (signal %s): no new claims; in-flight work may finish "
                    "for up to %ss, then what is still held is released",
                    signum if signum is not None else "programmatic", SHUTDOWN_GRACE_SECONDS)
    _shutdown.set()


def install_signal_handlers() -> None:
    """SIGTERM is what Railway sends; SIGINT is Ctrl-C on a laptop. Both
    take the same graceful path instead of KeyboardInterrupt / instant death."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, request_shutdown)


def release_held_jobs(sb) -> int:
    """Everything this process still holds goes back to 'queued'. Jobs:
    attempts KEPT (the job did nothing wrong; the worker is leaving), the
    generation mirrored 'queued' — through db.requeue_job, the same helper
    the boot and windowed reapers use, so the job/generation pair can never
    disagree; a row the new container has already reaped and re-claimed is
    left alone (the helper's attempts fence). Sketches: db.requeue_sketch.
    Best-effort per row: one failure must not strand the rest. Returns how
    many were released."""
    released = 0
    for job in _held_jobs():
        try:
            if db.requeue_job(sb, job, bump_attempts=False):
                released += 1
                log.warning("Shutdown: %s job %s back in the queue (attempt %s kept)",
                            job.get("type"), job["id"], job.get("attempts") or 0)
            else:
                log.warning("Shutdown: job %s had already moved (reaped or re-claimed); left as is", job["id"])
        except Exception as exc:  # noqa: BLE001
            log.error("Shutdown: job %s could not be requeued: %s", job["id"], exc)
    for sketch in _held_sketches():
        if db.requeue_sketch(sb, sketch):
            released += 1
            log.warning("Shutdown: tutor sketch %s back in the queue", sketch["id"])
        else:
            log.warning("Shutdown: sketch %s had already moved; left as is", sketch["id"])
    return released


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


def _failure_snapshot(sb, gen_id: str, gen_d: dict, job: dict) -> dict:
    """The ids and names a failed generation had at the moment it failed —
    generation, book (id and title), chapter, kind, language, job — as
    plain values for `platform_issues.context`. Best-effort: a book that
    cannot be read simply has no title."""
    params = gen_d.get("params") if isinstance(gen_d.get("params"), dict) else {}
    snap = {
        "generation_id": gen_id,
        "job_id": job.get("id"),
        "job_type": job.get("type"),
        "kind": gen_d.get("kind"),
        "book_id": gen_d.get("book_id"),
        "chapter": gen_d.get("chapter_ref"),
        "language": params.get("language"),
    }
    try:
        if gen_d.get("book_id"):
            book = sb.table("books").select("title").eq("id", gen_d["book_id"]).maybe_single().execute()
            book_d = getattr(book, "data", None) or {}
            if book_d.get("title"):
                snap["book_title"] = str(book_d["title"])[:200]
    except Exception:  # noqa: BLE001 — the snapshot is a convenience, never a gate
        pass
    return {k: v for k, v in snap.items() if v not in (None, "")}


def _auto_file_support_issue(sb, job: dict, error: str) -> None:
    """A failed job auto-triggers the support agent: file a console issue for
    the content owner and queue a diagnosis job. Never for support jobs
    themselves (no recursion), never twice for the same generation while an
    issue is still open, and never allowed to break the failure path."""
    try:
        gen_id = job.get("generation_id")
        if not gen_id:
            return  # index failures already surface on the book row
        gen = (sb.table("generations").select("owner_id, book_id, kind, chapter_ref, params")
               .eq("id", gen_id).maybe_single().execute())
        gen_d = getattr(gen, "data", None)
        if not gen_d:
            return
        # A SNAPSHOT of what failed, kept in `context` where no cascade can
        # reach it. The row's generation_id / book_id columns are foreign
        # keys ON DELETE SET NULL (0020), so a reporter who deletes the book
        # (or the failed lesson) after reporting leaves the console with a
        # job id that no longer exists and nothing else — issue 2cfc1585
        # (2026-09-21) was diagnosed with no book, no chapter and no kind.
        snapshot = _failure_snapshot(sb, gen_id, gen_d, job)
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
                    "context": {"error": _evidence(error), **snapshot},
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
      5. Catalogue observers — topic harvests (download + CPU), topic
         derives (one text call per sub-strand), topic articles (one text
         call), figure renders (image calls, self-pausing) and question
         drafts: only when nothing above is queued.
      6. Catalogue KIT generations (params.catalogue = true): only when no
         user builder is live AND the off-peak window is open — see
         _claim_catalogue_generation. Lanes 1-4 exclude them by the flag.
    All of 1–3 are bounded/fast, so they can't starve the lesson queue."""
    if _shutdown.is_set():
        return False  # leaving: anything claimed now would only be handed straight back
    sketch = db.claim_next_sketch(sb)
    if sketch:
        if _shutdown.is_set():
            # The signal landed inside the claim: nothing else knows this
            # process holds it, so it is handed straight back here.
            db.requeue_sketch(sb, sketch)
            return False
        log.info("Claimed tutor sketch %s (book=%s)", sketch["id"], sketch.get("book_id"))
        _sketch_add(sketch)
        try:
            from worker.tutor_sketch import render_sketch

            render_sketch(sb, sketch)  # self-contained: marks its own done/error
        except Exception as exc:  # noqa: BLE001
            log.error("Sketch %s failed: %s", sketch["id"], exc)
            try:
                db.set_sketch_error(sb, sketch["id"], str(exc))
            except Exception:  # noqa: BLE001
                pass
        finally:
            _sketch_remove(sketch["id"])
        return True

    # catalogue=False on the user lanes: a kit's rows are the same builder
    # TYPES as a teacher's, and only the flag tells them apart.
    job = (
        db.claim_next_job(sb, job_type="support_diagnose", catalogue=False)
        or db.claim_next_job(sb, job_type=DOC_JOB_TYPES, catalogue=False)
        or db.claim_next_job(sb, exclude_types=OBSERVER_JOB_TYPES, catalogue=False)  # every user builder
        or db.claim_next_job(sb, job_type=CATALOGUE_JOB_TYPES)      # harvest / derive / …: only when nothing else waits
        or _claim_catalogue_generation(sb)                          # kits: no live user builder + off-peak
    )
    if not job:
        return False
    if _shutdown.is_set():
        # Claimed in the gap between the check above and the row moving: the
        # signal landed mid-claim. Nothing else knows this process holds it
        # (it is not in-flight yet), so it is handed straight back here —
        # attempt kept — before any work starts.
        try:
            db.requeue_job(sb, job, bump_attempts=False)
        except Exception as exc:  # noqa: BLE001
            log.error("Job %s claimed during shutdown could not be requeued: %s", job["id"], exc)
        return False
    job_type = job.get("type")
    gen_id = job.get("generation_id")
    _inflight_add(job)
    log.info("Claimed %s job %s (generation=%s book=%s)", job_type, job["id"], gen_id, job.get("book_id"))
    try:
        if job_type == "index_book":
            index_book(sb, job)
            db.finish_job(sb, job["id"])  # process_generation finishes itself; index_book doesn't
        elif job_type == "support_diagnose":
            from support_agent.agent import run_support_job

            run_support_job(sb, job)
            db.finish_job(sb, job["id"])
        elif job_type == "topic_harvest":
            from catalogue.harvest import run_harvest_job

            run_harvest_job(sb, job)  # self-contained: finishes its own row, done or error
        elif job_type == "topic_derive":
            from catalogue.derive import run_derive_job

            run_derive_job(sb, job)  # self-contained: finishes its own row, done or error
        elif job_type == "topic_article":
            from catalogue.article import run_article_job

            run_article_job(sb, job)  # self-contained: finishes its own row, done or error
        elif job_type == "figure_render":
            from catalogue.figures import run_figure_render_job

            run_figure_render_job(sb, job)  # self-contained: finishes its own row, done or error
        elif job_type == "topic_questions":
            from catalogue.questions import run_questions_job

            run_questions_job(sb, job)  # self-contained: finishes its own row, done or error
        elif job_type == "topic_publish":
            from catalogue.publish import run_publish_job

            run_publish_job(sb, job)  # self-contained: finishes its own row, done or error
        else:
            process_generation(sb, job, gen_id)
    except db.DeferredJob as exc:
        # The job asked to run later (a deck whose lesson video is still
        # rendering). Back to the queue with a wake-up time; no attempt is
        # spent, nothing is failed, and the generation reads `queued` again
        # (defer_job mirrors it through the one requeue writer) so the
        # dashboard does not show a build that is not happening.
        if db.defer_job(sb, job, exc.seconds, exc.note):
            log.info("Job %s deferred %ds: %s", job["id"], exc.seconds, exc.note)
        else:
            log.warning("Job %s asked to defer but the row had moved; left as is", job["id"])
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
            _fail_catalogue_kit(sb, job, f"plan tier unresolvable: {exc!r}")
            # Same hook the generic failure path gets: a tier outage that
            # exhausts its retries is a console issue, not a quiet log line.
            if _support_agent_enabled() and not _is_catalogue_job(job):
                _auto_file_support_issue(sb, job, f"plan tier unresolvable: {exc!r}")
        else:
            log.warning("Job %s requeued (attempt %d): %r", job["id"], att + 1, exc)
            try:
                # The reaper's writer (db.requeue_job): its status guard — if
                # the console or the reaper moved this row while we ran, we
                # must not overwrite it — and its attempt spent. Only a
                # BUILDER job may relabel its generation (the observer-job
                # rule in db.generation_to_mirror); the helper applies it.
                db.requeue_job(sb, job, bump_attempts=True)
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
        _fail_catalogue_kit(sb, job, str(exc))
        # Stop the UI's "Finding chapters…" spinner if indexing failed.
        if job_type == "index_book" and job.get("book_id"):
            try:
                db.set_book_chapters(sb, job["book_id"], [], "error")
            except Exception:  # noqa: BLE001
                pass
        # A failed generation triggers the diagnosis agent (flag-gated; never
        # for a support job itself — that would recurse — nor for the other
        # generation-less jobs, which have no reporter waiting on a lesson).
        # A catalogue kit's failure is the reviewer's, in the portal (the kit
        # row goes 'failed' with the reason): filing a console issue under
        # the system account would only spend a diagnosis call on nobody.
        if (_support_agent_enabled() and job_type not in ("support_diagnose", "index_book", *CATALOGUE_JOB_TYPES)
                and not _is_catalogue_job(job)):
            _auto_file_support_issue(sb, job, str(exc))
    finally:
        _inflight_remove(job["id"])
    return True


def _worker_loop(idx: int) -> None:
    """One worker thread — its OWN Supabase client (thread safety), looping
    run_once and sleeping between empty polls."""
    sb = db.admin()
    while not _shutdown.is_set():
        try:
            worked = run_once(sb)
        except Exception as exc:  # noqa: BLE001
            log.error("Poll error (worker %d): %s", idx, exc)
            worked = False
        if not worked:
            _shutdown.wait(POLL_SECONDS)  # a sleep that a shutdown cuts short


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
    # Phase 3's lanes read the kit flag from jobs.params, which only migration
    # 0115 stamps. A kit row inserted before 0115 is applied would be claimed
    # by the USER lanes — the never-starve gate bypassed — so say so at boot.
    db.probe_catalogue_job_flags(sb)

    once = "--once" in sys.argv
    if once:
        # No startup reap in --once: it would requeue the live daemon's in-flight
        # job if run against the same project while the daemon is mid-generation.
        handled = run_once(sb)
        log.info("Done (%s).", "processed 1 job" if handled else "no queued jobs")
        return

    # From here on a SIGTERM (Railway's teardown) or SIGINT takes the graceful
    # path: no new claims, held jobs handed back, then exit — see _shutdown.
    install_signal_handlers()

    # Reaper (startup): a worker restart (deploy / crash / OOM) leaves the job(s)
    # it was running stranded in 'processing', and claims only ever pick 'queued'.
    # Nothing is in flight yet at startup, so every 'processing' row is orphaned —
    # requeue them all for instant recovery. (During a ROLLING deploy the prior
    # run is still alive for the drain window; its own shutdown path releases
    # what it holds, and the attempts fence in db.requeue_job keeps the two from
    # pulling one job back and forth.)
    rj, rs = db.requeue_stale_jobs(sb), db.requeue_stale_sketches(sb)
    if rj or rs:
        log.warning("Reaper: requeued %d job(s) + %d sketch(es) left 'processing' by a prior run", rj, rs)

    stale_min = int(os.getenv("STALE_JOB_MINUTES", "15"))
    log.info("Worker started; concurrency=%d, polling every %ss (stale reaper %sm)",
             WORKER_CONCURRENCY, POLL_SECONDS, stale_min)

    # Worker threads do the claiming + processing.
    for i in range(WORKER_CONCURRENCY):
        threading.Thread(target=_worker_loop, args=(i,), daemon=True, name=f"worker-{i}").start()

    _serve(sb, stale_min)
    # Whatever is still running after the grace is a daemon thread mid-render
    # whose row has just been handed back. It must NOT get to finish: a
    # finish_job from here would mark a job 'done' that the next container is
    # about to build. A plain return would also let the render process pool's
    # exit hook wait on it.
    logging.shutdown()
    os._exit(0)


def _serve(sb, stale_min: int, reap_every: float = 60, grace: float | None = None) -> None:
    """Main thread = the windowed crash-reaper, until a shutdown is requested.

    The reaper EXCLUDES the jobs this process is actively running (in-flight),
    so a long render is never requeued and double-run; only a genuinely
    orphaned 'processing' row (a failed finish_job write, an old row) is
    recovered. Runs while the queue is busy.

    On shutdown: wait up to `grace` seconds (SHUTDOWN_GRACE_SECONDS) for the
    in-flight work to finish by itself — the threads already refuse new
    claims, and a job that finishes leaves the held set, so it is never
    handed back — then release what is still held and return; the caller
    ends the process."""
    while not _shutdown.wait(reap_every):
        try:
            r = db.requeue_stale_jobs(sb, older_than_minutes=stale_min, exclude_ids=_inflight_snapshot())
            db.requeue_stale_sketches(sb, older_than_minutes=stale_min)
            if r:
                log.warning("Reaper: requeued %d stale job(s) (>%sm in 'processing', not in-flight)", r, stale_min)
        except Exception as exc:  # noqa: BLE001
            log.error("Reaper error: %s", exc)
        # The YouTube statistics poll rides the same tick (catalogue/
        # youtube_stats.py decides whether one is due; hourly by default,
        # dark without channel credentials). Imported lazily so the loop
        # stays importable without the catalogue package's dependencies.
        try:
            from catalogue.youtube_stats import maybe_poll
            maybe_poll(sb)
        except Exception as exc:  # noqa: BLE001 — never the reaper's problem
            log.error("YouTube stats tick error: %s", exc)
        # Once per boot: the end screen's ask into the descriptions of the
        # videos posted before it existed (catalogue/youtube_backfill.py).
        # Idempotent, so a boot where every description has it costs one read.
        try:
            from catalogue.youtube_backfill import maybe_backfill
            maybe_backfill(sb)
        except Exception as exc:  # noqa: BLE001
            log.error("YouTube CTA backfill tick error: %s", exc)
        # So does the website-traffic poll (catalogue/cloudflare_stats.py):
        # Cloudflare's daily figures for sketchcast.app, hourly, dark
        # without CLOUDFLARE_API_TOKEN / CLOUDFLARE_ZONE_ID.
        try:
            from catalogue.cloudflare_stats import maybe_poll as maybe_poll_cloudflare
            maybe_poll_cloudflare(sb)
        except Exception as exc:  # noqa: BLE001
            log.error("Cloudflare stats tick error: %s", exc)
    budget = SHUTDOWN_GRACE_SECONDS if grace is None else grace
    deadline = time.monotonic() + budget
    while _holding_work() and time.monotonic() < deadline:
        time.sleep(0.2)
    n = release_held_jobs(sb)
    log.warning("Shutdown: %d row(s) released to the queue after a %.0fs grace; exiting", n, budget)


if __name__ == "__main__":
    main()
