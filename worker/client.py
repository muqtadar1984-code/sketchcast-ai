"""Supabase admin client + helpers for the generation worker.

Uses the service_role key, so it bypasses RLS — keep this key server-side only.
"""

from __future__ import annotations

import logging
import os
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

def claim_next_job(sb: Client) -> Optional[dict]:
    """Atomically-ish claim the oldest queued job (sets it to processing)."""
    res = (
        sb.table("jobs")
        .select("*")
        .eq("status", "queued")
        .order("created_at")
        .limit(1)
        .execute()
    )
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


def set_book_chapters(sb: Client, book_id: str, chapters: list[dict], status: str) -> None:
    sb.table("books").update({"chapters": chapters, "status": status}).eq("id", book_id).execute()


def set_book_meta(sb: Client, book_id: str, grade: Optional[str], subject: Optional[str]) -> None:
    sb.table("books").update({"grade": grade, "subject": subject}).eq("id", book_id).execute()


def set_book_cover(sb: Client, book_id: str, cover_path: str) -> None:
    sb.table("books").update({"cover_path": cover_path}).eq("id", book_id).execute()


def set_book_health(sb: Client, book_id: str, health: dict) -> None:
    """Persist the Book Health Score (books.health). Best-effort: a deployment
    whose migration hasn't added the column must not fail indexing."""
    try:
        sb.table("books").update({"health": health}).eq("id", book_id).execute()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("worker").warning("book health not persisted for %s: %s", book_id, exc)


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
