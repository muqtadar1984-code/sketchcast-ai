"""One agent2 analysis per UNIT, shared by every piece of a kit — a catalogue
article (worker migration 0140, ``topic_article_analyses``) or a book chapter
part (0144, ``book_unit_analyses``).

Why. A catalogue article has no books row, so the chapter_grounding cache
that lets a book's 2nd..Nth artifact reuse the chapter's analysis never
applied to a kit (decision 14): the presentation, the deck, the activity, the
case study and the lesson plan each ran the combined analysis of the SAME
article. On the Arabic Cells kit 427aa1ac (2026-10-17) that was four
analyses of 248 s, 566 s, 540 s and 578 s — ~$0.3–0.6 each on an article the
English kit analysed in 17–40 s — and three of the four jobs were CLAIMED
within one second of each other, so a cache written after the first analysis
and read by the next job would have saved only the lesson plan's.

A book chapter PART had the same gap for a different reason: the
chapter_grounding cache describes the WHOLE chapter (and is the AI Tutor's
fence), so worker.process neither reads nor writes it for a part job — every
piece of a part kit analysed the part again. On the Bedrock cut-over sample
kit (2026-10-18, founder's book, ch1 part 1, Arabic) the worksheet, activity,
case study, lesson plan and deck each paid $0.43–0.54, all claimed within one
second. A whole-chapter kit hit the same race whenever its pieces started
together, before the first had written chapter_grounding.

What. One row per unit is both the cache and a lease:

* ``claim`` — the first piece to arrive INSERTS the row as ``analysing`` and
  analyses (``Verdict.action == "analyse"``, ``held``); a piece that finds a
  fresh lease held by a sibling is told to ``wait`` (the worker defers the
  job, as a deck waits for its video, and asks again in ``poll_seconds``); a
  piece that finds a ``ready`` row for the same language, analyzer version,
  source text (and, for an article, revision) reuses it (``reuse``). A stale
  lease (its holder crashed), the caller's own lease (its earlier attempt
  died) or any mismatch is taken over and re-analysed. The take-over is an
  UPDATE guarded by the row's ``updated_at`` (optimistic), the insert by the
  primary key, so two pieces racing for an empty or stale row cannot both
  believe they hold it. A piece that has already waited ``lease_seconds`` in
  total analyses on its own rather than wait forever.
* ``store`` — the holder writes the finished analysis (``ready``); only the
  holder's write lands.
* ``release`` — the holder's analysis failed: the lease is dropped so a
  waiting sibling takes over now instead of after the lease expires.

Every call is best-effort: a database without the table (a worker deployed
before the migration is applied), a transport error or a malformed row all
degrade to "analyse, untracked" — exactly what every piece did before.
``CATALOGUE_SHARED_ANALYSIS=0`` / ``BOOK_SHARED_ANALYSIS=0`` are the rollback
levers. The translated presentation never reaches this module
(worker.process: the ``override`` branch makes no analysis call).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("worker")

TABLE = "topic_article_analyses"
BOOK_TABLE = "book_unit_analyses"


def enabled() -> bool:
    return os.getenv("CATALOGUE_SHARED_ANALYSIS", "1").strip() != "0"


def book_enabled() -> bool:
    return os.getenv("BOOK_SHARED_ANALYSIS", "1").strip() != "0"


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


@dataclass(frozen=True)
class Unit:
    """WHAT is analysed once: the table, its key column and value, the extra
    columns a fresh row carries, and (for an article) the revision column a
    ready row must not predate."""
    table: str
    key_col: str
    key: str
    label: str                                     # for logs and the wait note
    insert_fields: dict = field(default_factory=dict)
    revision_col: Optional[str] = None
    revision: Optional[str] = None                 # the source's own timestamptz
    on: bool = True                                # its rollback lever, read at construction


def article_unit(article_id: str, article: Optional[dict] = None) -> Unit:
    return Unit(table=TABLE, key_col="article_id", key=str(article_id), label=f"article {article_id}",
                revision_col="article_updated_at", revision=(article or {}).get("updated_at"),
                on=enabled())


def book_unit(book_id: str, chapter_num: int, part: Optional[int], language: str) -> Unit:
    """A book chapter part (``part`` k) or the whole chapter (``part`` None),
    in one lesson language. Language is in the KEY, not only matched: an
    English and an Arabic kit of the same part run side by side without
    taking each other's lease over."""
    p = "all" if part is None else str(int(part))
    return Unit(table=BOOK_TABLE, key_col="unit_key", key=f"{book_id}:{int(chapter_num)}:{p}:{language}",
                label=f"book {book_id} ch{chapter_num} " + ("whole chapter" if part is None else f"part {p}"),
                insert_fields={"book_id": str(book_id), "chapter_num": int(chapter_num),
                               "part": None if part is None else int(part)},
                on=book_enabled())


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


def book_source_hash(*, language: str, level: str, chunks: list[dict], chapter: Optional[dict]) -> str:
    """``source_hash`` for a book unit. A part job hands the analyzer its chunk
    verbatim (hashed exactly as an article's are); a whole-chapter job hands
    the chapter, and there the figure list is left OUT: its entries can carry
    per-job local paths, which would give every sibling a different key and
    no reuse at all, while the analysis is computed from the text."""
    ch = {k: v for k, v in (chapter or {}).items() if k != "images"} if chapter else chapter
    return source_hash(language=language, level=level, chunks=chunks, chapter=ch)


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


def _read(sb, unit: Unit) -> Optional[dict]:
    res = sb.table(unit.table).select("*").eq(unit.key_col, unit.key).limit(1).execute()
    rows = getattr(res, "data", None) or []
    return dict(rows[0]) if rows else None


def _matches(row: dict, unit: Unit, *, language: str, version: int, h: str) -> bool:
    if str(row.get("language") or "") != language:
        return False
    if int(row.get("analyzer_version") or 0) != version:
        return False
    if str(row.get("source_hash") or "") != h:
        return False
    revised = _parse(unit.revision) if unit.revision_col else None
    if revised is not None:
        cached_for = _parse(row.get(unit.revision_col))
        if cached_for is None or cached_for < revised:
            return False   # the source moved on since this analysis
    return True


# ── the protocol ────────────────────────────────────────────────────────
def claim_unit(sb, unit: Unit, *, generation_id: str, language: str, source_hash: str,
               waited_seconds: float = 0.0) -> Verdict:
    """See the module doc. Never raises."""
    if not unit.on:
        return Verdict("analyse", reason="shared analysis switched off")
    if not unit.key:
        return Verdict("analyse", reason="no unit")
    h = source_hash
    try:
        version = analyzer_version()
        lease = {
            "language": language, "analyzer_version": version, "source_hash": h,
            "status": "analysing", "holder_generation_id": str(generation_id),
            "lease_started_at": _now().isoformat(), "analysis": None, "analysed_at": None,
            "updated_at": _now().isoformat(),
        }
        if unit.revision_col:
            lease[unit.revision_col] = unit.revision
        row = _read(sb, unit)
        for _attempt in range(2):
            if row is None:
                try:
                    sb.table(unit.table).insert({unit.key_col: unit.key, **unit.insert_fields, **lease}).execute()
                    return Verdict("analyse", held=True, reason=f"first piece of this {unit.label.split(' ')[0]}")
                except Exception as exc:  # noqa: BLE001 — a sibling inserted first (23505), or worse
                    row = _read(sb, unit)
                    if row is None:
                        raise exc
                    continue
            same = _matches(row, unit, language=language, version=version, h=h)
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
            res = (sb.table(unit.table).update(lease)
                   .eq(unit.key_col, unit.key).eq("updated_at", row.get("updated_at"))
                   .execute())
            if getattr(res, "data", None):
                why = ("stale lease" if status == "analysing" and holder != str(generation_id)
                       else "own lease" if status == "analysing"
                       else f"{row.get('language')}/v{row.get('analyzer_version')} does not match")
                return Verdict("analyse", held=True, reason=f"took over ({why})")
            row = _read(sb, unit)   # lost the race — look at what won
        return Verdict("analyse", reason="contended twice; analysing untracked")
    except Exception as exc:  # noqa: BLE001 — the cache must never fail a build
        logger.warning("%s: shared analysis unavailable (%s); analysing", unit.label, exc)
        return Verdict("analyse", reason=f"cache unavailable: {exc}")


def store_unit(sb, unit: Unit, *, generation_id: str, analysis: dict) -> bool:
    """The holder's finished analysis → ``ready``. False when the lease had
    moved (taken over as stale) — then the taker's analysis is the one kept."""
    try:
        now = _now().isoformat()
        res = (sb.table(unit.table)
               .update({"status": "ready", "analysis": analysis, "analysed_at": now, "updated_at": now})
               .eq(unit.key_col, unit.key).eq("holder_generation_id", str(generation_id))
               .eq("status", "analysing").execute())
        ok = bool(getattr(res, "data", None))
        if not ok:
            logger.info("%s: analysis lease moved from %s; not stored", unit.label, generation_id)
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: analysis not stored (%s)", unit.label, exc)
        return False


def release_unit(sb, unit: Unit, *, generation_id: str) -> None:
    """The holder's analysis failed: drop its lease so a waiting sibling
    analyses now. Only this generation's live lease is touched."""
    try:
        (sb.table(unit.table).delete()
         .eq(unit.key_col, unit.key).eq("holder_generation_id", str(generation_id))
         .eq("status", "analysing").execute())
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: analysis lease not released (%s)", unit.label, exc)


# ── the article API (catalogue kits; unchanged signatures) ──────────────
def claim(sb, *, article_id: str, article: Optional[dict], generation_id: str, language: str,
          level: str, chunks: list[dict], chapter: Optional[dict], waited_seconds: float = 0.0) -> Verdict:
    if not enabled():
        return Verdict("analyse", reason="CATALOGUE_SHARED_ANALYSIS=0")
    if not article_id:
        return Verdict("analyse", reason="no article")
    return claim_unit(sb, article_unit(article_id, article), generation_id=generation_id, language=language,
                      source_hash=source_hash(language=language, level=level, chunks=chunks, chapter=chapter),
                      waited_seconds=waited_seconds)


def store(sb, *, article_id: str, generation_id: str, analysis: dict) -> bool:
    return store_unit(sb, article_unit(article_id), generation_id=generation_id, analysis=analysis)


def release(sb, *, article_id: str, generation_id: str) -> None:
    release_unit(sb, article_unit(article_id), generation_id=generation_id)
