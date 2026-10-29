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
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

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
    passed to finish_job, so the deck's own status is never relabelled)."""
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
