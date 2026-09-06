"""``topic_harvest`` — a textbook's chapter and section HEADINGS become
``topic_candidates`` rows. Names only. Never a sentence of book text.

Job shape: ``{id, type: 'topic_harvest', book_id, generation_id: None}``. It
is an OBSERVER job (``worker.client.OBSERVER_JOB_TYPES``): it owns no
generation, so nothing here — and nothing in run.py on its behalf — may write
``generations.status``. It finishes its OWN job row, done or error, the way a
tutor sketch does; run.py only dispatches.

What is written, per heading that survives ``is_heading``:
    topic_candidates {source_kind: 'book', book_id, raw_title, normalized,
                      suggested_topic_id}
``raw_title`` is the heading with its whitespace collapsed and any leading
numbering token removed ("3.2 Cells" → "Cells", see ``strip_numbering``), at
most 120 characters; ``normalized`` is ``canonical_key(raw_title)``; and
``suggested_topic_id`` is the topic whose alias already carries that key, when
one does. The unique index on (source_kind, book, node, normalized) makes a
re-harvest a no-op for every heading it has already filed.

THE GUARANTEE. The catalogue's founding rule (plan §1.1: "Textbooks are used
only to harvest topic names. No page spans, no book text") is enforced in TWO
pure layers, both exercised directly by the tests:

  * ``headings_from_structured`` reads only what the structurer derived from
    a FONT-SIZE-DISTINCT level — chapter titles and ``section_type ==
    'heading'`` sections. The PyMuPDF extractor (prod's path) labels EVERY
    bold span at body size a level-3 heading, so a bold sentence in the body
    becomes a subsection or, with no section above it yet, an implicit
    'subheading' section; neither is read. Measured 2026-09-06 before this
    layer existed: a real PDF with two bold body-size sentences yielded both
    as candidates.
  * ``is_heading`` gates every string before it can be written — a
    200-character sentence, "Fig. 3 shows the cell", a running header, an
    imprint line, an eight-word sentence with no full stop and a heading WITH
    a trailing period are all in the tests.

The harvest reads the whole PDF (it must, to find the headings), but the only
strings that leave this module are the ones both layers accept.

Why a plain INSERT-the-new-ones rather than an upsert with
``ignore_duplicates``: the uniqueness that guards this table is an EXPRESSION
index — ``(source_kind, coalesce(book_id, zero), coalesce(node_id, zero),
normalized)`` — and PostgREST's ``on_conflict`` can only name plain columns,
so the client's upsert cannot target it (the request would 409 on the first
duplicate). The existing keys are read for the book first; a racing second
harvest is caught by the per-row fallback below.
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Iterable, Optional

from catalogue.key import canonical_key
from worker import client as db

log = logging.getLogger("worker.harvest")

JOB_TYPE = "topic_harvest"

# A heading longer than this is a paragraph that happened to be bold.
MAX_HEADING_CHARS = 120
# A name is short. "Scientific enquiry: analysis, evaluation and conclusions"
# is six words; "Plants make their own food using sunlight, water and carbon
# dioxide" is eleven, and a sentence. A word is a whitespace token carrying a
# letter or digit, so the dash in "Light — Reflection" is not one.
MAX_HEADING_WORDS = 10
# This many words ENDING in a full stop is a sentence whatever its shape
# ("All living things are made of cells."); fewer ("1.2 Cells.") is a heading
# the book printed with a period.
_PERIOD_SENTENCE_WORDS = 4
# "The cell is the basic unit of life": eight words, no terminal mark, still
# a sentence. A determiner opening a run of six or more words is prose; "The
# Cell", "The Solar System" and "The cell membrane" stay.
_DETERMINERS = frozenset({"the", "a", "an", "this", "these"})
_DETERMINER_RUN = 5  # words AFTER the determiner that make it prose
# Sentence: a terminal mark followed by whitespace and MORE TEXT. A trailing
# period on its own ("1.2 Cells.") is a heading with a full stop, not prose.
_SENTENCE = re.compile(r"[.!?]\s+\S")
# "Page 12", "p. 12", "pg 12", "12 / 240" — running folios, not topics.
_PAGE_LIKE = re.compile(r"^(?:page|pages|p|pg|pp)\.?\s*\d+(?:\s*[-–/]\s*\d+)?$", re.I)
# "CHAPTER 3 CELLS 47" — a running header is the chapter name in capitals with
# the folio on the end. Only the COMBINATION is rejected: "CO2" has no separate
# trailing number and "Cambridge Lower Secondary Science 7" is not all capitals.
_TRAILING_NUMBER = re.compile(r"\s\d+$")
# "(c) Cambridge University Press 2021", "ISBN 978-…": imprint lines.
_IMPRINT = re.compile(r"©|\(c\)|\bisbn\b", re.I)
# A leading numbering token: "3.2 ", "1.1.4 ", "Chapter 3 ", "Unit 5: ",
# "Section 2) " — and the dash or colon a book puts after it ("2.1 – Indicators").
# Stripped by the harvest ONLY, never by canonical_key: the Cambridge code
# "7Bs.01" must keep its key, and the harvest never sees a curriculum code.
_NUMBERING = re.compile(
    r"^(?:(?:chapter|unit|lesson|section|topic|part)\s*)?\d+(?:\.\d+)*[.):]?\s+(?:[-–—:]\s+)?",
    re.I,
)
_WS = re.compile(r"\s+")
# A queue nobody can read is a queue nobody reads. Chapter titles come first,
# then section headings, so the cap trims the finer grain.
MAX_CANDIDATES_PER_BOOK = 400
# PostgREST filters travel in the URL; keep an IN (...) list well under 8 KB.
_QUERY_CHUNK = 150
_INSERT_CHUNK = 200


def clean_heading(text: object) -> str:
    """Collapse whitespace (including newlines a PDF span break leaves) and
    trim. Pure."""
    if text is None:
        return ""
    return _WS.sub(" ", str(text)).strip()


def _words(s: str) -> list[str]:
    """Whitespace tokens that carry a letter or digit — a lone dash or
    ampersand between words is not a word."""
    return [w for w in s.split() if any(ch.isalnum() for ch in w)]


def is_heading(text: object) -> bool:
    """The gate. True only for a string that can be a topic NAME.

    Rejected: empty; longer than MAX_HEADING_CHARS; a sentence (a . ! or ?
    followed by whitespace and another word); fewer than two letters; purely
    numeric or a page folio; more than MAX_HEADING_WORDS words; four or more
    words ending in a full stop; a determiner (the/a/an/this/these) opening
    six or more words; all capitals with a trailing number (a running header);
    an imprint line (©, (c), ISBN). Question-style headings ("What happens
    when we heat ice?") are real in textbooks and pass. Pure — no I/O, no
    state — so the tests can throw book prose at it directly.
    """
    s = clean_heading(text)
    if not s or len(s) > MAX_HEADING_CHARS:
        return False
    if _SENTENCE.search(s):
        return False
    if sum(1 for ch in s if ch.isalpha()) < 2:
        return False
    if _PAGE_LIKE.match(s):
        return False
    words = _words(s)
    if len(words) > MAX_HEADING_WORDS:
        return False
    if s.endswith(".") and len(words) >= _PERIOD_SENTENCE_WORDS:
        return False
    if words and words[0].lower() in _DETERMINERS and len(words) > _DETERMINER_RUN:
        return False
    if _TRAILING_NUMBER.search(s) and s == s.upper():
        return False
    if _IMPRINT.search(s):
        return False
    return True


def strip_numbering(text: object) -> str:
    """Drop ONE leading numbering token — "3.2 Cells" → "Cells", "Chapter 3
    Cells" → "Cells", "1.1 The cell membrane" → "The cell membrane" — so a
    numbered heading keys like the alias a curator typed ("cell", never
    "3_2_cell"). Pure. Harvest-only: ``canonical_key`` must never do this,
    because a curriculum code ("7Bs.01" → "7bs_01") keeps its digits, and the
    harvest never sees one. A string the token would swallow whole comes back
    as it came."""
    s = clean_heading(text)
    return _NUMBERING.sub("", s, count=1) or s


def headings_from_structured(structured: dict) -> list[str]:
    """Chapter titles, then the section headings the structurer derived from
    a FONT-SIZE-DISTINCT level, from a ``StructuredBook.model_dump()``.
    Unfiltered — ``build_candidates`` gates.

    The discriminator is ``section_type``. In
    ``structurer._build_sections_from_items`` a Section is ``'heading'`` only
    when it came from a level-2 DocItem, and the PyMuPDF extractor (prod's
    path) assigns level 2 only to spans at the second-largest recurring font
    size above the body size — a size the book reserved for headings. Level 1
    (the largest) is the chapter title, which ``chapters[].title`` already
    carries. Level 3 is "bold at body size or larger": every bold sentence,
    key term and emphasised phrase in the body. A level-3 item becomes a
    Subsection under the current section or, with no section open yet, an
    implicit Section typed ``'subheading'``. Docling maps SECTION_HEADER tree
    depth onto the same 1/2/3, so the rule holds for that backend too. Hence:

      * ``'heading'``    — read;
      * ``'subheading'`` — skipped (an orphan bold span);
      * ``'body'``       — skipped (the structurer's own "Content" placeholder
                           for a chapter with no detectable headings — a label
                           the code made up, not one the book printed);
      * ``subsections``  — never read (all level 3).

    Measured before this rule (2026-09-06): a real PDF with two bold body-size
    sentences yielded "All living things are made of cells." and "A cell
    membrane controls what enters and leaves the cell" as candidates.
    """
    titles: list[str] = []
    sections: list[str] = []
    for ch in structured.get("chapters") or []:
        if not isinstance(ch, dict):
            continue
        titles.append(clean_heading(ch.get("title")))
        for sec in ch.get("sections") or []:
            if not isinstance(sec, dict) or sec.get("section_type") != "heading":
                continue
            sections.append(clean_heading(sec.get("section_title")))
    return titles + sections


def headings_from_book_row(book: dict) -> list[str]:
    """``books.chapters[].title`` — the indexed (and possibly vision-healed)
    titles, which beat a re-detection. Unfiltered."""
    out: list[str] = []
    for ch in book.get("chapters") or []:
        if isinstance(ch, dict):
            out.append(clean_heading(ch.get("title")))
    return out


def build_candidates(headings: Iterable[str], limit: int = MAX_CANDIDATES_PER_BOOK,
                     stats: Optional[dict] = None) -> list[dict]:
    """Strip numbering, gate, key, and dedupe (first spelling of a key wins) —
    in input order, so callers put the coarse headings first and the cap trims
    the fine ones. Returns ``[{raw_title, normalized}]``; nothing here has
    touched a database.

    ``raw_title`` is the heading AFTER ``strip_numbering``, so every row keeps
    ``normalized == canonical_key(raw_title)``; the remainder must pass
    ``is_heading`` on its own ("3.2 All living things are made of cells." is
    still a sentence). When ``stats`` is a dict it receives the drop counts —
    ``not_heading`` (failed the gate) and ``no_key`` (passed it but carries no
    key material: an Arabic-only title, a bare article) — so a curator can read
    WHY a book yielded nothing.
    """
    seen: set[str] = set()
    out: list[dict] = []
    not_heading = no_key = 0
    for h in headings:
        raw = strip_numbering(h)
        if not is_heading(raw):
            not_heading += 1
            continue
        key = canonical_key(raw)
        if not key:
            no_key += 1
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append({"raw_title": raw[:MAX_HEADING_CHARS], "normalized": key})
        if len(out) >= limit:
            break
    if stats is not None:
        stats["not_heading"] = not_heading
        stats["no_key"] = no_key
    return out


# ── database edges ─────────────────────────────────────────────────────


def _chunks(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def lookup_alias_topics(sb, keys: list[str]) -> dict[str, str]:
    """``normalized → topic_id`` for every key that already has an alias.
    One IN-query per chunk of keys; nothing is written."""
    found: dict[str, str] = {}
    for chunk in _chunks(list(dict.fromkeys(keys)), _QUERY_CHUNK):
        if not chunk:
            continue
        res = sb.table("topic_aliases").select("topic_id,normalized").in_("normalized", chunk).execute()
        for row in getattr(res, "data", None) or []:
            key, tid = row.get("normalized"), row.get("topic_id")
            if key and tid and key not in found:
                found[key] = tid
    return found


def existing_book_keys(sb, book_id: str) -> set[str]:
    """The keys already filed for this book (any status): a re-harvest must
    not re-open a candidate a curator has merged or dismissed."""
    res = (sb.table("topic_candidates").select("normalized")
           .eq("source_kind", "book").eq("book_id", book_id).execute())
    return {r.get("normalized") for r in (getattr(res, "data", None) or []) if r.get("normalized")}


def _is_duplicate_error(exc: BaseException) -> bool:
    msg = str(exc)
    code = getattr(exc, "code", None)
    return str(code) == "23505" or "23505" in msg or "duplicate key" in msg.lower()


def insert_candidates(sb, rows: list[dict]) -> int:
    """Insert in chunks; a chunk that hits the unique index (a racing
    harvest of the same book) is retried row by row with the duplicates
    skipped. Returns the number of rows actually inserted."""
    inserted = 0
    for chunk in _chunks(rows, _INSERT_CHUNK):
        try:
            sb.table("topic_candidates").insert(chunk).execute()
            inserted += len(chunk)
        except Exception as exc:  # noqa: BLE001
            if not _is_duplicate_error(exc):
                raise
            for row in chunk:
                try:
                    sb.table("topic_candidates").insert(row).execute()
                    inserted += 1
                except Exception as exc2:  # noqa: BLE001
                    if not _is_duplicate_error(exc2):
                        raise
    return inserted


# ── the PDF half ───────────────────────────────────────────────────────


def _headings_from_pdf(pdf_path: Path, book: dict) -> list[str]:
    """Run the SAME extraction and structuring indexing ran, without a model:
    the stored chapter map (num/title/start_page/end_page) is passed as
    ``known_chapters`` so the split is identical and no Claude/vision call is
    made — a harvest costs a download and CPU, never quota. Returns unfiltered
    headings (chapter titles and font-size-distinct section headings — see
    ``headings_from_structured`` for why sub-sections are not among them)."""
    from agent1_ingestion.extractor import extract_pdf
    from agent1_ingestion.structurer import structure_book

    known = [c for c in (book.get("chapters") or [])
             if isinstance(c, dict) and "start_page" in c and "end_page" in c]
    extraction = extract_pdf(str(pdf_path))
    structured = structure_book(
        book_id=str(book.get("id") or ""), title=book.get("title") or "Untitled",
        author=book.get("author") or "Unknown", isbn=None,
        extraction=extraction, images=[], pdf_path=str(pdf_path), client=None,
        known_chapters=known or None,
    ).model_dump()
    return headings_from_structured(structured)


# ── the job ────────────────────────────────────────────────────────────


def harvest_book(sb, job_id: str, book: dict, pdf_headings: Optional[list[str]] = None) -> dict:
    """The harvest proper, given a loaded book row. ``pdf_headings`` lets a
    caller (or a test) supply the PDF's headings instead of downloading; when
    None, the PDF is downloaded and structured here. Returns the summary that
    is also written to ``jobs.stage``."""
    book_id = book["id"]
    source = "chapters_only"
    pdf_error: Optional[str] = None

    db.set_stage(sb, job_id, {"phase": "harvest", "step": "download"})
    db.set_progress(sb, job_id, 5)

    if pdf_headings is None:
        pdf_headings = []
        storage_path = book.get("storage_path")
        if storage_path:
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    pdf_path = db.download_book(sb, storage_path, Path(tmp) / "book.pdf")
                    db.set_progress(sb, job_id, 25)
                    db.set_stage(sb, job_id, {"phase": "harvest", "step": "structure"})
                    pdf_headings = _headings_from_pdf(pdf_path, book)
                    source = "pdf+chapters"
            except Exception as exc:  # noqa: BLE001 — the stored titles still harvest
                pdf_error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("harvest %s: PDF headings unavailable (%s); chapter titles only",
                            book_id, pdf_error)
        else:
            pdf_error = "book has no storage_path"
    else:
        source = "pdf+chapters"

    db.set_progress(sb, job_id, 60)
    # Stored chapter titles FIRST (the indexed truth), then what the PDF yields.
    headings = headings_from_book_row(book) + list(pdf_headings)
    drops: dict = {}
    candidates = build_candidates(headings, stats=drops)

    db.set_stage(sb, job_id, {"phase": "harvest", "step": "match"})
    keys = [c["normalized"] for c in candidates]
    suggestions = lookup_alias_topics(sb, keys) if keys else {}
    already = existing_book_keys(sb, book_id) if keys else set()
    db.set_progress(sb, job_id, 80)

    rows = [
        {
            "source_kind": "book",
            "book_id": book_id,
            "raw_title": c["raw_title"],
            "normalized": c["normalized"],
            "suggested_topic_id": suggestions.get(c["normalized"]),
        }
        for c in candidates if c["normalized"] not in already
    ]
    inserted = insert_candidates(sb, rows) if rows else 0

    summary = {
        "phase": "harvest",
        "step": "done",
        "source": source,
        "headings_seen": len(headings),
        "candidates": len(candidates),
        # Why a book yielded few or none: prose the gate refused, and titles
        # that passed it but carry no key material (an Arabic-only heading).
        "dropped_not_heading": drops.get("not_heading", 0),
        "dropped_no_key": drops.get("no_key", 0),
        "inserted": inserted,
        "existing": len(candidates) - len(rows),
        "suggested": sum(1 for r in rows if r["suggested_topic_id"]),
    }
    if pdf_error:
        summary["pdf_error"] = pdf_error
    return summary


def run_harvest_job(sb, job: dict) -> Optional[dict]:
    """Entry point for run.py. Self-contained: finishes the job row itself
    (done with the summary in ``stage``; error with the message) and never
    raises, so the generic failure path in run.py — which files support
    issues for BUILDER jobs — has nothing to do. Returns the summary, or None
    on failure."""
    job_id = job["id"]
    try:
        book_id = job.get("book_id")
        if not book_id:
            raise RuntimeError("topic_harvest job without book_id")
        book = db.get_book(sb, book_id)
        if not book:
            raise RuntimeError(f"book {book_id} not found")
        if book.get("removed_at"):
            raise RuntimeError("content removed")
        book.setdefault("id", book_id)
        summary = harvest_book(sb, job_id, book)
        db.set_stage(sb, job_id, summary)
        db.finish_job(sb, job_id)  # no generation: an observer job owns none
        log.info("harvest %s: %s", book_id, summary)
        return summary
    except Exception as exc:  # noqa: BLE001
        log.error("harvest job %s failed: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=f"{type(exc).__name__}: {exc}"[:4000])
        except Exception as exc2:  # noqa: BLE001
            log.error("harvest job %s: could not record the failure: %s", job_id, exc2)
        return None


__all__ = [
    "JOB_TYPE", "MAX_HEADING_CHARS", "MAX_HEADING_WORDS", "MAX_CANDIDATES_PER_BOOK",
    "clean_heading", "is_heading", "strip_numbering", "headings_from_structured",
    "headings_from_book_row",
    "build_candidates", "lookup_alias_topics", "existing_book_keys", "insert_candidates",
    "harvest_book", "run_harvest_job",
]
