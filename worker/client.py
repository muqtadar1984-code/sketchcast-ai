"""Supabase admin client + helpers for the generation worker.

Uses the service_role key, so it bypasses RLS — keep this key server-side only.
"""

from __future__ import annotations

import logging
import os
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

def claim_next_job(sb: Client, job_type: Optional[str] = None) -> Optional[dict]:
    """Atomically-ish claim the oldest queued job (sets it to processing).
    With `job_type`, only that type is considered — lets the loop prioritise
    small interactive work (support diagnoses) over long batch renders."""
    q = sb.table("jobs").select("*").eq("status", "queued")
    if job_type:
        q = q.eq("type", job_type)
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
    return upd.data[0]


def requeue_stale_jobs(sb: Client, older_than_minutes: Optional[int] = None, max_attempts: int = 3) -> int:
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
        q = sb.table("jobs").select("id,type,book_id,attempts").eq("status", "processing")
        if cutoff:
            q = q.lt("updated_at", cutoff)
        rows = q.execute().data or []
        requeued = failed = 0
        for j in rows:
            att = int(j.get("attempts") or 0)
            # The .eq("status","processing") guard means a job another actor already
            # moved is skipped (returns no rows) — safe under any race.
            if att >= max_attempts:
                sb.table("jobs").update({
                    "status": "error",
                    "error": f"Auto-failed after {att} attempts — the worker kept dying on this job.",
                }).eq("id", j["id"]).eq("status", "processing").execute()
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


def set_book_chapters(sb: Client, book_id: str, chapters: list[dict], status: str) -> None:
    sb.table("books").update({"chapters": chapters, "status": status}).eq("id", book_id).execute()


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

def download_book(sb: Client, storage_path: str, dest: str | Path) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = sb.storage.from_("uploads").download(storage_path)
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
    sb.storage.from_("artifacts").upload(
        dest_path,
        local_path.read_bytes(),
        {"content-type": ctype, "upsert": "true"},
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
