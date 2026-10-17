"""One agent2 analysis per catalogue article, shared by every piece of its
kits (worker migration 0140, ``topic_article_analyses``).

Why. A catalogue article has no books row, so the chapter_grounding cache
that lets a book's 2nd..Nth artifact reuse the chapter's analysis never
applied to a kit (decision 14): the presentation, the deck, the activity, the
case study and the lesson plan each ran the combined analysis of the SAME
article. On the Arabic Cells kit 427aa1ac (2026-10-17) that was four
analyses of 248 s, 566 s, 540 s and 578 s — ~$0.3–0.6 each on an article the
English kit analysed in 17–40 s — and three of the four jobs were CLAIMED
within one second of each other, so a cache written after the first analysis
and read by the next job would have saved only the lesson plan's.

What. One row per article is both the cache and a lease:

* ``claim`` — the first piece to arrive INSERTS the row as ``analysing`` and
  analyses (``Verdict.action == "analyse"``, ``held``); a piece that finds a
  fresh lease held by a sibling is told to ``wait`` (the worker defers the
  job, as a deck waits for its video, and asks again in ``poll_seconds``); a
  piece that finds a ``ready`` row for the same language, analyzer version,
  source text and article revision reuses it (``reuse``). A stale lease (its
  holder crashed), the caller's own lease (its earlier attempt died) or any
  mismatch is taken over and re-analysed. The take-over is an UPDATE guarded
  by the row's ``updated_at`` (optimistic), the insert by the primary key, so
  two pieces racing for an empty or stale row cannot both believe they hold
  it. A piece that has already waited ``lease_seconds`` in total analyses on
  its own rather than wait forever.
* ``store`` — the holder writes the finished analysis (``ready``); only the
  holder's write lands.
* ``release`` — the holder's analysis failed: the lease is dropped so a
  waiting sibling takes over now instead of after the lease expires.

Every call is best-effort: a database without the table (a worker deployed
before 0140 is applied), a transport error or a malformed row all degrade to
"analyse, untracked" — exactly what every piece did before.
``CATALOGUE_SHARED_ANALYSIS=0`` is the rollback lever. The translated
presentation never reaches this module (worker.process: the ``override``
branch makes no analysis call).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("worker")

TABLE = "topic_article_analyses"


def enabled() -> bool:
    return os.getenv("CATALOGUE_SHARED_ANALYSIS", "1").strip() != "0"


def lease_seconds() -> int:
    """How long a lease stays fresh, and the most a piece waits in total. The
    longest Arabic analysis measured was 578 s with its truncation retry;
    30 min is generous without parking a kit for an hour on a crashed holder."""
    return max(60, int(os.getenv("ARTICLE_ANALYSIS_LEASE_S", "1800")))


def poll_seconds() -> int:
    return max(15, int(os.getenv("ARTICLE_ANALYSIS_POLL_S", "60")))


def analyzer_version() -> int:
    from agent2_analysis.analyzer import ANALYZER_VERSION

    return int(ANALYZER_VERSION)


@dataclass
class Verdict:
    action: str                       # "reuse" | "analyse" | "wait"
    analysis: Optional[dict] = None   # for "reuse"
    held: bool = False                # "analyse" with the lease: store()/release() apply
    holder: Optional[str] = None      # for "wait": the generation analysing
    reason: str = ""


# ── keys ────────────────────────────────────────────────────────────────
def source_hash(*, language: str, level: str, chunks: list[dict], chapter: Optional[dict]) -> str:
    """What the analysis was computed FROM: the article's chunked text (the
    analyzer's ``chunks_override``, verbatim), the level and the language. An
    article edited in place changes it; so does a different depth node."""
    texts = [str((c or {}).get("text") or "") for c in (chunks or [])]
    if not texts:
        texts = [json.dumps(chapter or {}, sort_keys=True, ensure_ascii=False, default=str)]
    body = json.dumps({"language": language, "level": level, "chunks": texts},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts) -> Optional[datetime]:
    """A PostgREST timestamptz (``2026-10-17T06:54:52.61677+00:00``), the
    ``+00`` Postgres prints, a ``Z``, or a naive stamp (taken as UTC)."""
    if not ts:
        return None
    s = str(ts).strip().replace(" ", "T", 1)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if len(s) >= 3 and s[-3] in "+-" and s[-2:].isdigit():
        s = s + ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _usable(analysis) -> bool:
    """The same bar chapter_grounding's cache sets (worker.client.
    get_chapter_analysis): a dict with real concepts and an episode plan."""
    return (isinstance(analysis, dict)
            and bool((analysis.get("concepts") or {}).get("concepts"))
            and bool((analysis.get("episodes") or {}).get("episodes")))


def _read(sb, article_id: str) -> Optional[dict]:
    res = sb.table(TABLE).select("*").eq("article_id", article_id).limit(1).execute()
    rows = getattr(res, "data", None) or []
    return dict(rows[0]) if rows else None


def _matches(row: dict, *, language: str, version: int, h: str, article_updated: Optional[datetime]) -> bool:
    if str(row.get("language") or "") != language:
        return False
    if int(row.get("analyzer_version") or 0) != version:
        return False
    if str(row.get("source_hash") or "") != h:
        return False
    if article_updated is not None:
        cached_for = _parse(row.get("article_updated_at"))
        if cached_for is None or cached_for < article_updated:
            return False   # the article moved on since this analysis
    return True


# ── the protocol ────────────────────────────────────────────────────────
def claim(sb, *, article_id: str, article: Optional[dict], generation_id: str, language: str,
          level: str, chunks: list[dict], chapter: Optional[dict], waited_seconds: float = 0.0) -> Verdict:
    """See the module doc. Never raises."""
    if not enabled():
        return Verdict("analyse", reason="CATALOGUE_SHARED_ANALYSIS=0")
    if not article_id:
        return Verdict("analyse", reason="no article")
    h = source_hash(language=language, level=level, chunks=chunks, chapter=chapter)
    try:
        version = analyzer_version()
        article_updated = _parse((article or {}).get("updated_at"))
        lease = {
            "language": language, "analyzer_version": version, "source_hash": h,
            "status": "analysing", "holder_generation_id": str(generation_id),
            "lease_started_at": _now().isoformat(), "analysis": None, "analysed_at": None,
            "article_updated_at": (article or {}).get("updated_at"),
            "updated_at": _now().isoformat(),
        }
        row = _read(sb, article_id)
        for _attempt in range(2):
            if row is None:
                try:
                    sb.table(TABLE).insert({"article_id": article_id, **lease}).execute()
                    return Verdict("analyse", held=True, reason="first piece of this article")
                except Exception as exc:  # noqa: BLE001 — a sibling inserted first (23505), or worse
                    row = _read(sb, article_id)
                    if row is None:
                        raise exc
                    continue
            same = _matches(row, language=language, version=version, h=h, article_updated=article_updated)
            status = str(row.get("status") or "")
            holder = str(row.get("holder_generation_id") or "")
            if status == "ready" and same and _usable(row.get("analysis")):
                return Verdict("reuse", analysis=row["analysis"], holder=holder or None, reason="ready")
            if status == "analysing" and same and holder and holder != str(generation_id):
                started = _parse(row.get("lease_started_at"))
                fresh = started is not None and (_now() - started).total_seconds() < lease_seconds()
                if fresh:
                    if waited_seconds < lease_seconds():
                        return Verdict("wait", holder=holder, reason="a sibling is analysing")
                    # waited the whole lease out: the holder may still finish,
                    # so its lease is left alone and this piece analyses itself
                    return Verdict("analyse", reason=f"waited {waited_seconds:.0f}s; analysing untracked")
            # take over: a stale lease, this generation's own earlier lease, a
            # ready row for another language / version / text, or a bad blob
            res = (sb.table(TABLE).update(lease)
                   .eq("article_id", article_id).eq("updated_at", row.get("updated_at"))
                   .execute())
            if getattr(res, "data", None):
                why = ("stale lease" if status == "analysing" and holder != str(generation_id)
                       else "own lease" if status == "analysing"
                       else f"{row.get('language')}/v{row.get('analyzer_version')} does not match")
                return Verdict("analyse", held=True, reason=f"took over ({why})")
            row = _read(sb, article_id)   # lost the race — look at what won
        return Verdict("analyse", reason="contended twice; analysing untracked")
    except Exception as exc:  # noqa: BLE001 — the cache must never fail a build
        logger.warning("article %s: shared analysis unavailable (%s); analysing", article_id, exc)
        return Verdict("analyse", reason=f"cache unavailable: {exc}")


def store(sb, *, article_id: str, generation_id: str, analysis: dict) -> bool:
    """The holder's finished analysis → ``ready``. False when the lease had
    moved (taken over as stale) — then the taker's analysis is the one kept."""
    try:
        now = _now().isoformat()
        res = (sb.table(TABLE)
               .update({"status": "ready", "analysis": analysis, "analysed_at": now, "updated_at": now})
               .eq("article_id", article_id).eq("holder_generation_id", str(generation_id))
               .eq("status", "analysing").execute())
        ok = bool(getattr(res, "data", None))
        if not ok:
            logger.info("article %s: analysis lease moved from %s; not stored", article_id, generation_id)
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning("article %s: analysis not stored (%s)", article_id, exc)
        return False


def release(sb, *, article_id: str, generation_id: str) -> None:
    """The holder's analysis failed: drop its lease so a waiting sibling
    analyses now. Only this generation's live lease is touched."""
    try:
        (sb.table(TABLE).delete()
         .eq("article_id", article_id).eq("holder_generation_id", str(generation_id))
         .eq("status", "analysing").execute())
    except Exception as exc:  # noqa: BLE001
        logger.warning("article %s: analysis lease not released (%s)", article_id, exc)
