"""The ``deck_pages`` job: render an EXISTING deck's pages for the Board.

Decks built before the export existed (and any whose best-effort export at
build time was skipped) get their pages from this job. It is an OBSERVER
(worker.client.OBSERVER_JOB_TYPES): it reads a finished generation's
``deck_pptx`` file(s) and writes only that generation's ``slide_png`` /
``deck_pages_json`` rows and the files under ``deck_pages/``. It never
writes ``generations.status``, never clears other artifacts, and never
rebuilds anything — a deck_pages row reaching ``process_generation`` would
rebuild the whole deck by ``generations.kind``, which is why run.py
dispatches it by type before that fallback.

Job shape: ``{"type": "deck_pages", "params": {"generation_id": …}}``
(``jobs.generation_id`` is read as a fallback). The generation must be a
``deck``, or a ``presentation`` carrying an embedded ``deck*.pptx`` (older
kits). Self-contained: it finishes its own row, done or error, and never
raises.

Repeat presses (2026-10-29). "Load the deck as pages" on the Board inserts a
job per press, and there is no unique index (nor a migration for one), so two
quick presses queue two jobs for one deck. Before rendering, the job looks at
what is already there and finishes as DONE without rendering when either

* the deck's newest ``slide_png`` row was written AFTER this job was queued —
  a render (another job's, or the deck build's own best-effort export in
  worker.process._deck_pages_after_build) finished since the press, so the
  pages this job would make are already on the Board; or
* another ``deck_pages`` job for the same generation is ``processing`` and
  was claimed BEFORE this one (claimed_at, then created_at, then id): it is
  rendering the same deck right now. Ordering by claim time means the first
  of two simultaneous claims never yields to the second, so two jobs cannot
  both skip and leave the deck with no pages.

The skip is noted on the job (``jobs.stage``) and in the log; it never
touches ``generations.status``. If those reads fail, the job renders: a
second render is harmless (publish_deck_pages replaces the rows), a skipped
one is not.
"""

from __future__ import annotations

import logging
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from worker import client as db

logger = logging.getLogger("worker")

JOB_TYPE = "deck_pages"
DECK_KINDS = ("deck", "presentation")


def _generation_id(job: dict) -> str:
    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    return str((params or {}).get("generation_id") or job.get("generation_id") or "").strip()


def _deck_rows(sb, generation_id: str) -> list[tuple[int, str]]:
    res = (sb.table("artifacts").select("kind,storage_path").eq("generation_id", generation_id)
           .eq("kind", "deck_pptx").execute())
    from agent5_slides.deck_pages import deck_part_of

    parts: dict[int, str] = {}
    for row in getattr(res, "data", None) or []:
        path = str(row.get("storage_path") or "")
        part = deck_part_of(path)
        if part is not None:
            parts[part] = path
    return sorted(parts.items())


def _when(value) -> Optional[datetime]:
    """A PostgREST timestamptz (``2026-10-29T10:00:00.12345+00:00``, ``…Z``)
    as an aware UTC datetime; None when absent or unreadable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip().replace(" ", "T", 1)
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        # fromisoformat wants 0, 3 or 6 fractional digits on older Pythons
        s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _newest_render(sb, generation_id: str) -> tuple[Optional[datetime], str]:
    """When this generation's pages were last published: the newest
    ``slide_png`` row (publish_deck_pages deletes and re-inserts them, so a
    row's created_at is the time of the render that made it)."""
    from agent5_slides.deck_pages import KIND_PNG

    res = (sb.table("artifacts").select("created_at").eq("generation_id", generation_id)
           .eq("kind", KIND_PNG).execute())
    best, raw = None, ""
    for row in getattr(res, "data", None) or []:
        when = _when(row.get("created_at"))
        if when is not None and (best is None or when > best):
            best, raw = when, str(row.get("created_at"))
    return best, raw


def _claim_order(job: dict) -> tuple:
    far = datetime.max.replace(tzinfo=timezone.utc)
    return (_when(job.get("claimed_at")) or _when(job.get("created_at")) or far,
            _when(job.get("created_at")) or far, str(job.get("id") or ""))


def skip_reason(sb, job: dict) -> Optional[str]:
    """Why this job need not render, or None to render. Never raises: a read
    that fails means render (see the module docstring)."""
    generation_id = _generation_id(job)
    if not generation_id:
        return None
    try:
        queued = _when(job.get("created_at"))
        if queued is not None:
            rendered, raw = _newest_render(sb, generation_id)
            if rendered is not None and rendered > queued:
                return f"skipped: rendered at {raw} after this job was queued"
        res = (sb.table("jobs").select("id,params,generation_id,created_at,claimed_at")
               .eq("type", JOB_TYPE).eq("status", "processing").execute())
        mine = _claim_order(job)
        for other in getattr(res, "data", None) or []:
            if str(other.get("id")) == str(job.get("id")):
                continue
            if _generation_id(other) != generation_id:
                continue
            if _claim_order(other) < mine:
                return "skipped: another render in progress"
    except Exception as exc:  # noqa: BLE001 — rendering twice is harmless
        logger.warning("deck_pages job %s: repeat-press check failed, rendering anyway (%s)",
                       job.get("id"), str(exc)[:200])
    return None


def run_deck_pages(sb, job: dict, renderer=None) -> dict:
    """The work, raising on any fault with a message that says what was
    measured. ``renderer`` is injectable for tests."""
    from agent5_slides import deck_pages as DP

    generation_id = _generation_id(job)
    if not generation_id:
        raise ValueError("deck_pages: no params.generation_id on the job")
    res = sb.table("generations").select("id,kind,owner_id").eq("id", generation_id).execute()
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError(f"deck_pages: generation {generation_id} not found")
    gen = rows[0]
    kind = str(gen.get("kind") or "")
    if kind not in DECK_KINDS:
        raise ValueError(f"deck_pages: generation {generation_id} is a {kind or 'kind-less'} "
                         f"generation, not a deck")
    owner_id = str(gen.get("owner_id") or "")
    if not owner_id:
        raise ValueError(f"deck_pages: generation {generation_id} has no owner_id")
    decks = _deck_rows(sb, generation_id)
    if not decks:
        raise ValueError(f"deck_pages: generation {generation_id} ({kind}) has no deck_pptx artifact")
    db.set_progress(sb, job["id"], 10)
    with tempfile.TemporaryDirectory(prefix="deck_pages_") as tmp:
        local = []
        for part, path in decks:
            dest = Path(tmp) / "in" / f"deck_part{part}.pptx"
            db.download_artifact(sb, path, dest)
            local.append((part, dest))
        db.set_progress(sb, job["id"], 30)
        result = DP.export_deck_parts(local, Path(tmp) / "out", renderer, timeout=DP.JOB_RENDER_SECONDS)
        db.set_progress(sb, job["id"], 80)
        summary = DP.publish_deck_pages(sb, generation_id, owner_id, [p for _, p in decks], result)
    summary.update({"generation_id": generation_id, "parts": len(decks), "renderer": result["renderer"],
                    "seconds": result.get("seconds")})
    return summary


def process_deck_pages(sb, job: dict, renderer=None) -> None:
    """run.py's handler. Finishes the JOB row only (no generation id is ever
    passed to finish_job, so the deck's own status is never relabelled).
    A repeat press (skip_reason) finishes DONE without rendering."""
    note = skip_reason(sb, job)
    if note:
        logger.info("deck_pages job %s (%s): %s", job.get("id"), _generation_id(job), note)
        db.set_stage(sb, job["id"], {"phase": "skipped", "note": note[:200]})
        try:
            db.finish_job(sb, job["id"])
        except Exception as exc:  # noqa: BLE001 — never raises
            logger.error("deck_pages job %s: skip not recorded: %s", job.get("id"), exc)
        return
    try:
        summary = run_deck_pages(sb, job, renderer)
    except Exception as exc:  # noqa: BLE001 — recorded on the job, never raised
        msg = f"{type(exc).__name__}: {exc}"[:1500]
        logger.error("deck_pages job %s failed: %s", job.get("id"), msg)
        try:
            db.finish_job(sb, job["id"], None, error=msg)
        except Exception as exc2:  # noqa: BLE001
            logger.error("deck_pages job %s: error not recorded: %s", job.get("id"), exc2)
        return
    logger.info("deck_pages job %s: %d slide(s) for %s in %ss (%s)%s", job.get("id"), summary["slides"],
                summary["generation_id"], summary.get("seconds"), summary.get("renderer"),
                "" if summary.get("map_registered") else " — map NOT registered (0166 not applied)")
    db.finish_job(sb, job["id"])
