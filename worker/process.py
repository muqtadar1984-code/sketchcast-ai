"""Process one generation: book PDF -> lesson (slides + video + deck) -> Supabase.

Reuses the existing agents end-to-end. Files are processed in a temp dir and the
final deck (.pptx) + video (.mp4) are uploaded to the `artifacts` bucket under
{owner_id}/{generation_id}/.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import uuid
from pathlib import Path

from supabase import Client

from . import client as db

# Book-title cleanup — uploaded PDFs often carry junk filenames
# (e.g. "pdfcoffee.com_cambridge-maths-5-learner-book-pdf-free"). The rules now
# live in shared/book_metadata.py so the app (src/utils/book.ts) has one thing
# to mirror: the two copies drifted apart on 2026-07-12 and the worker's title
# gate has rejected 100% of production books ever since.
from shared import coverage
from shared.book_metadata import (clean_title_fallback as _clean_title_fallback,
                                  looks_like_filename as _looks_like_filename,
                                  pick_book_title, sanitise_author)
from shared.part_label import clean_part_titles, part_label

logger = logging.getLogger("worker")

DEFAULT_LEVEL = "middle_school"


def _coverage_report(analysis: dict, episode: dict | None, text: str, *,
                     kind: str, model: str, part: int | None = None,
                     of: int | None = None, part_scoped: bool = False) -> dict:
    """Measure one artifact's topic coverage and log it. Never raises.

    ``model`` is recorded with the number, and that is the whole reason this
    exists: the point of the gate is to answer "is the cheaper model thinner?"
    empirically after the flip, and a coverage figure with no model attached
    answers nothing. Failure to MEASURE is never a failure of the artifact — a
    measurement bug must not cost a teacher a lesson — so everything here is
    best-effort and an unmeasured artifact simply carries no report.

    ``part_scoped`` marks a params.part job — generated from ONE part's text.
    coverage.measure needs to know, because such a job's single-episode plan
    is indistinguishable from a legacy whole-chapter one, and pooling the
    denominator (correct for the latter) judges the former against every
    concept of the chapter (incident 8b79d4e0).
    """
    try:
        report = coverage.measure(analysis, episode, text, part_scoped=part_scoped)
    except Exception as exc:  # noqa: BLE001
        logger.warning("coverage not measured for %s: %s", kind, exc)
        return {}
    report["kind"] = kind
    report["model"] = model
    if part is not None:
        report["part"], report["of"] = part, of
    log = logger.warning if report.get("verdict") in ("short", "floor") else logger.info
    log(
        "coverage %s%s: %s of %s topics addressed (%s, %s) on %s — missed: %s",
        kind,
        f" part {part}/{of}" if part is not None else "",
        report.get("addressed"), report.get("topics"), report.get("covered"),
        report.get("verdict"), model, ", ".join(report.get("missed") or []) or "nothing",
    )
    return report


def _record_coverage(sb: Client, generation_id: str, reports: list[dict]) -> None:
    """Persist a job's coverage reports to ``generations.params.coverage``.

    params is an existing JSONB column already used for post-hoc telemetry
    (tts_voice_used / tts_voice_downgraded), so this needs NO migration and is
    queryable today:

        select c->>'model' as model, c->>'kind' as kind,
               round(avg((c->>'covered')::numeric), 3) as mean_coverage,
               count(*) as n
        from generations g, jsonb_array_elements(g.params->'coverage') c
        where g.params ? 'coverage' and c->>'covered' is not null
        group by 1, 2 order by 2, 1;

    which is the Sonnet-vs-Haiku answer in one query, per artifact kind. The
    flat ``coverage_model`` key alongside it exists so the same split can be
    taken without unnesting.

    Best-effort: telemetry must never fail a finished lesson.
    """
    reports = [r for r in reports if r]
    if not reports:
        return
    patch: dict = {"coverage": reports}
    # Denormalised alongside the array so the Sonnet-vs-Haiku split can be taken
    # without unnesting; a kit whose parts somehow ran on two models records the
    # pair rather than silently picking one.
    models = sorted({r.get("model") for r in reports if r.get("model")})
    if models:
        patch["coverage_model"] = models[0] if len(models) == 1 else ",".join(models)
    db.merge_generation_params(sb, generation_id, patch)


def _acceptance_report(script_data: dict, video_manifest: dict) -> dict | None:
    """Run the visual-language acceptance check on a finished lesson.

    This exists because the check itself did not run in production — it was
    imported only by tests. Every incident where "the report said PASSED" was
    a LOCAL driver script talking to itself; the worker never asked.

    THE AUDIT AND THE GATE ARE NOT THE SAME THING. `report["passed"]` is the
    full quality audit: every line in it means "look at this". Promoting it
    verbatim to a shipping gate — which is what the first version did —
    destroyed lessons that were fine. Measured: an all-whiteboard lesson
    failed, though video_composer calls that tier a legitimate rung of the
    same visual language; and ONE image failing to generate out of ~30 threw
    away the whole lesson AFTER the script call, all the TTS and every frame.

    So the gate is a deliberately smaller predicate: refuse only what makes
    the artifact not worth delivering, and let the rest ride in the report.

    Returns {"passed", "ship", "summary", "report"} or None when the check is
    not applicable (no scene engine) or itself failed. A validator bug must
    never destroy a lesson that rendered fine, so anything unexpected
    degrades to None; the caller honours `ship`, not `passed`.
    """
    if os.getenv("VIDEO_ENGINE", "").strip().lower() != "scene":
        return None
    try:
        from spike.scene_engine.validate import (format_report,
                                                 validate_visual_language)
        plan = (script_data or {}).get("visual_plan")
        report = validate_visual_language(video_manifest, plan)
        n = max(1, int(report.get("narration_segments") or 0))
        tolerance = max(1, n // 4)

        blocking = []
        # Dead air is the one defect a viewer cannot work around.
        if report.get("mostly_silent"):
            blocking.append("mostly_silent")
        # A lesson with NO pictures at all is not a lesson — but a whiteboard
        # lesson IS one. The whiteboard tier exists precisely so the engine
        # can simplify WITHIN its own visual language rather than fail.
        if report.get("no_scenes_produced") and not report.get(
                "whiteboard_fallback_segments"):
            blocking.append("no_scenes_produced")
        # Proportional: one blank board among thirty is a blemish worth
        # reporting; a third of the lesson blank is a broken lesson.
        for key in ("unresolved_assets", "legacy_renderer_usage"):
            v = report.get(key)
            count = len(v) if isinstance(v, list) else int(v or 0)
            if count > tolerance:
                blocking.append(f"{key}={count}/{n}")

        noted = [k for k in ("no_scenes_produced", "mostly_silent")
                 if report.get(k)]
        for k in ("unresolved_assets", "silent_segments", "overlapping_text"):
            if report.get(k):
                noted.append(f"{k}={len(report[k])}")
        summary = ("BLOCKING: " + ", ".join(blocking) if blocking
                   else (", ".join(noted) if noted else "clean"))
        logger.info("acceptance: audit=%s ship=%s\n%s",
                    "PASSED" if report.get("passed") else "FAILED",
                    "yes" if not blocking else "NO",
                    format_report(report))
        return {"passed": bool(report.get("passed")), "ship": not blocking,
                "summary": summary, "report": report}
    except Exception:  # noqa: BLE001 — never fail a rendered lesson on the checker
        logger.exception("acceptance check itself failed; lesson allowed through")
        return None


def _elevenlabs_enabled() -> bool:
    """Worker-side gate for premium (ElevenLabs) TTS — defense in depth. Premium
    voices run ONLY when the deployment enables the flag AND the key is present,
    no matter what voice the request asked for."""
    import os

    on = os.getenv("ELEVENLABS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
    return on and bool(os.getenv("ELEVENLABS_API_KEY"))


def _chapter_heal_enabled() -> bool:
    """Generation-time chapter self-heal (persisted per-chapter overrides). Turn on
    ONLY after applying app migration 0040 (the heal_* columns) — otherwise every
    override read/write is a best-effort no-op and the expensive relocation would
    re-run on every generation instead of being paid once. Index-time healing does
    not depend on this flag (it writes books.chapters, no new columns)."""
    import os

    return os.getenv("FEATURE_CHAPTER_HEAL", "").strip().lower() in ("1", "true", "yes", "on")


def _title_gate_applies(chapter_title: str, book_title: str | None,
                        n_chapters: int, is_cumulative: bool) -> bool:
    """Whether the generation-time title-vs-content check applies to a chapter.

    The gate protects against WRONG BOUNDARIES in multi-chapter books (printed
    contents-page numbers once misread as physical pages — the original
    mislabel bug). A single whole-book chapter has no boundary to get wrong,
    and its "title" is just the book title (often a raw scanner filename like
    "DocScanner 16 Jun 2026…"), which no page can ever read as — there the
    gate can only false-positive, bricking the book. Cumulative papers have
    synthetic labels — never checked. In MULTI-chapter books the gate always
    applies (even to a chapter that repeats the book title): a wrong stored
    boundary is possible there, and heal — not a skip — is the right remedy.
    (book_title is kept in the signature for call-site clarity/telemetry.)
    """
    del book_title  # multi-chapter books are always checked — see docstring
    return not is_cumulative and n_chapters > 1


def _chapter_check_error(title: str, actual: str) -> str:
    """The loud-fail message when a chapter can't be matched to any pages — shown
    after self-heal has already tried and failed to relocate."""
    return (
        f'Chapter check failed: "{title}" was requested but its pages read as '
        f'"{actual}", and no pages matching the title were found in the book. The '
        "book may be a scan we could not read cleanly — re-upload a text-based PDF "
        "if you have one, or delete and re-upload to re-detect chapters."
    )


def _pick_chapter(chapters: list[dict], chapter_ref: str | None) -> dict:
    real = [c for c in chapters if c.get("chapter_num") is not None]
    pool = real or chapters
    if chapter_ref:
        try:
            want = int(str(chapter_ref).strip())
            for c in pool:
                if int(c.get("chapter_num", -999)) == want:
                    return c
        except ValueError:
            pass
    return pool[0]


def _chapter_plain_text(chapter: dict) -> str:
    """All the readable text of a structured chapter (sections + subsections)."""
    out: list[str] = []
    for s in chapter.get("sections") or []:
        if s.get("content"):
            out.append(str(s["content"]))
        for ss in s.get("subsections") or []:
            if ss.get("content"):
                out.append(str(ss["content"]))
    return "\n".join(out)


def _range_label(nums: list[int]) -> str:
    """Human label for a set of 0-based chapter numbers → 1-based, contiguous
    runs collapsed: [0,1,2,4] → "Chapters 1–3, 5"."""
    ns = sorted(set(int(n) for n in nums))
    if not ns:
        return "selected chapters"
    parts, start, prev = [], ns[0], ns[0]
    for n in ns[1:] + [None]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(f"{start + 1}" if start == prev else f"{start + 1}–{prev + 1}")
        if n is not None:
            start = prev = n
    return ("Chapter " if len(ns) == 1 else "Chapters ") + ", ".join(parts)


# Ceiling on the per-chapter text we STORE (chapter_grounding.source_text).
# Was an inline 60,000 (~43 pages), which silently beheaded any longer chapter.
# Nothing sends this whole string to a model — agent2_analysis chunks it at
# MAX_ANALYSIS_CHARS per part — so the only thing a tight bound bought was
# missing teaching material at the end of long chapters. Kept generous rather
# than unbounded so one pathological book cannot write an unbounded row.
_SOURCE_TEXT_MAX_CHARS = 600_000


def _measured_from_cached_ocr(sb: Client, book_id: str, ch: dict) -> list[dict] | None:
    """A scanned chapter's part map from its ALREADY-CACHED transcription, if any.

    Used at INDEX time. Re-indexing rewrites books.chapters wholesale, and for a
    scanned book the part map falls back to a page-count estimate — so every map
    a generation had measured from real OCR was thrown away on the next index.
    That was self-defeating twice over, because the message a teacher gets when
    the estimate over-offers is "part 8 does not exist. If the book's chapters
    changed, re-index it to refresh the part map" — advice that REVERTED the
    only thing that could fix it.

    The transcription outlives the index, so when it is there the guess is
    unnecessary. Only trusted when the cached text still describes THESE pages:
    chapter_grounding is keyed by chapter_num alone, so a re-index that moved
    chapter 3 would otherwise measure the new range with the old chapter's text.
    """
    try:
        text = db.get_chapter_source_text(sb, book_id, int(ch["num"]))
        if not text:
            return None
        heal = db.get_chapter_heal(sb, book_id, int(ch["num"])) or {}
        hs, he = heal.get("heal_start_page"), heal.get("heal_end_page")
        if hs is not None and he is not None and (
            int(hs) != int(ch["start_page"]) or int(he) != int(ch["end_page"])
        ):
            return None  # the cache belongs to a different span than this map
        from shared.part_label import measured_parts_for

        return measured_parts_for({
            "title": ch.get("title", ""),
            "sections": [{"section_title": "Content", "section_type": "body",
                          "content": text, "page_num": ch.get("start_page", 0),
                          "subsections": []}],
        }, [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("cached-OCR part map skipped for %s ch%s: %s", book_id, ch.get("num"), exc)
        return None


def _persist_measured_parts(sb: Client, book_id: str, chapter_num: int,
                            chapter: dict, book: dict) -> None:
    """Swap a scanned chapter's ESTIMATED part map for the measured one.

    Runs the moment OCR lands, on the very dict generation will chunk, so the
    advertised count and the buildable count cannot disagree. Costs nothing: the
    transcription already happened and the chunker is pure Python. The first
    generation of a chapter fixes its count for everyone afterwards.

    All the judgement lives in shared.part_label.measured_parts_for (pure, and
    therefore testable — worker/ cannot be imported without supabase). Anything
    that goes wrong is logged and swallowed: a part map is a LABEL, and losing
    one must never cost a teacher the generation they paid for.
    """
    try:
        from agent1_ingestion.vision_chapters import transcribe_page_range
        from shared.part_label import measured_parts_for

        start = int(chapter.get("start_page", 0))
        end = int(chapter.get("end_page", start))
        # Never publish a map measured from a PARTIAL read. The runaway guard
        # stops transcription short only on a chapter whose map is already
        # broken, and freezing that into a stored count would trade a LOUD
        # failure ("part 8 does not exist") for a silent one: the book would
        # simply advertise 3 parts forever and never mention the other six.
        if len(transcribe_page_range(start, end)) < (end - start + 1):
            logger.warning(
                "part map NOT measured for %s ch%s — the transcription was partial, "
                "so the count would understate the chapter", book_id, chapter_num,
            )
            return

        stored = next((c for c in (book.get("chapters") or [])
                       if isinstance(c, dict) and str(c.get("num")) == str(chapter_num)), None)
        old = (stored or {}).get("parts") or []
        measured = measured_parts_for(chapter, old)
        if measured and db.set_chapter_parts(sb, book_id, chapter_num, measured,
                                             expect_start=start, expect_end=end):
            logger.info(
                "part map MEASURED from OCR for %s ch%s: %d part(s) (was %d, %s)",
                book_id, chapter_num, len(measured), len(old),
                "estimated" if any(isinstance(p, dict) and p.get("estimated") for p in old) else "unset",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("measured part map skipped for %s ch%s: %s", book_id, chapter_num, exc)


def _combine_chapters(all_chapters: list[dict], wanted: list, sb: Client, book_id: str,
                      cap_per: int = 6000, cap_total: int = 30000) -> dict:
    """Merge the selected chapters into ONE synthetic chapter for a cumulative
    revision paper: each chapter's text (structured sections, else the stored
    source_text) bounded per-chapter and in total so the prompt stays sane.
    chapter_num = -1 marks it synthetic — the caller must NOT persist its
    analysis/grounding to the per-chapter cache."""
    want_nums = []
    for n in wanted:
        try:
            want_nums.append(int(str(n).strip()))
        except (TypeError, ValueError):
            pass
    picked = [c for c in all_chapters if int(c.get("chapter_num", -999)) in set(want_nums)]
    picked.sort(key=lambda c: int(c.get("chapter_num", 0)))
    if not picked:
        picked = all_chapters[:1]

    sections, total = [], 0
    for c in picked:
        cnum = int(c.get("chapter_num", 0))
        text = _chapter_plain_text(c)
        if len(text) < 200:  # thin structured text → fall back to the index-time cache
            text = db.get_chapter_source_text(sb, book_id, cnum) or text
        chunk = text[:cap_per]
        if total + len(chunk) > cap_total:
            chunk = chunk[: max(0, cap_total - total)]
        if chunk:
            sections.append({
                "section_title": c.get("title") or f"Chapter {cnum + 1}",
                "section_type": "body", "content": chunk,
                "page_num": c.get("start_page", 0), "subsections": [],
            })
            total += len(chunk)
        if total >= cap_total:
            break
    return {
        "chapter_num": -1,
        "title": f"Revision — {_range_label([int(c.get('chapter_num', 0)) for c in picked])}",
        "sections": sections,
        "start_page": picked[0].get("start_page", 0) if picked else 0,
        "end_page": picked[-1].get("end_page", 0) if picked else 0,
    }


def _combine_units(all_chapters: list[dict], scope: list, sb: Client, book_id: str,
                   cap_total: int = 30000) -> dict:
    """Merge the chosen {chapter, part} units into ONE synthetic chapter for a
    cumulative EXAM. part 0 = the whole chapter; part N = that part's chunk (the
    same index-time chunker the kit uses). The per-unit character budget is split
    evenly across the selection so a wide exam still samples every unit, not just
    the first few. chapter_num = -1 marks it synthetic (never cached).

    Also carries a `coverage` list of human labels ("Chapter 2: … — Part 1") so
    the paper and the app can name exactly what the exam tested."""
    from agent2_analysis.analyzer import build_chapter_parts

    by_num: dict[int, dict] = {}
    for c in all_chapters:
        try:
            by_num[int(c.get("chapter_num", -999))] = c
        except (TypeError, ValueError):
            pass

    units: list[tuple[int, int]] = []
    for u in scope or []:
        if not isinstance(u, dict):
            continue
        try:
            cn = int(str(u.get("chapter")).strip())
            pt = int(u.get("part") or 0)
        except (TypeError, ValueError):
            continue
        units.append((cn, pt))
    units = sorted(set(units))
    if not units:
        # Defence-in-depth: the DB requires a non-empty scope, but never trust it.
        first = next(iter(by_num.values()), None)
        if first is not None:
            units = [(int(first.get("chapter_num", 0)), 0)]

    # Split the grounding budget across ALL selected units (a low floor so a wide
    # exam still samples every unit, not just the first 20).
    per = max(800, cap_total // max(1, len(units)))
    sections, coverage, total = [], [], 0
    for cn, pt in units:
        c = by_num.get(cn)
        if not c:
            continue
        ctitle = c.get("title") or f"Chapter {cn + 1}"
        if pt >= 1:
            parts = build_chapter_parts(c)
            if pt > len(parts):
                # The teacher TICKED this unit for the paper and it is being
                # dropped. Skipping stays the behaviour — failing a paid
                # generation outright is worse — but it must never be silent:
                # a revision paper that quietly covers fewer units than were
                # asked for is indistinguishable from one that covered them all.
                # Made far rarer by reading a chapter to its own end (the old
                # 18-page transcription cap left a 58-page chapter with ~3
                # buildable parts against the 10 its map offered), but a part map
                # estimated from PAGE count can still outrun the text.
                logger.error(
                    "CUMULATIVE PAPER DROPPED A TICKED UNIT: %r part %d requested, only %d "
                    "part(s) buildable — that unit contributes NOTHING to this paper.",
                    ctitle, pt, len(parts),
                )
                continue
            pk = parts[pt - 1]
            text = pk.get("text", "") or ""
            seg = [t for t in (pk.get("section_titles") or []) if t][:2]
            label = f"Chapter {cn + 1}: {ctitle} — Part {pt}"
            if seg:
                label += f" ({', '.join(seg)})"
        else:
            text = _chapter_plain_text(c)
            if len(text) < 200:  # thin structured text → index-time source cache
                text = db.get_chapter_source_text(sb, book_id, cn) or text
            label = f"Chapter {cn + 1}: {ctitle}"
        chunk = text[:per]
        if total + len(chunk) > cap_total:
            chunk = chunk[: max(0, cap_total - total)]
        # Only a unit that actually contributes text is listed as covered — the
        # coverage line must never claim a unit the exam wasn't grounded on.
        if chunk:
            sections.append({
                "section_title": label, "section_type": "body", "content": chunk,
                "page_num": c.get("start_page", 0), "subsections": [],
            })
            coverage.append(label)
            total += len(chunk)
    return {
        "chapter_num": -1,
        "title": "Exam",
        "sections": sections,
        "coverage": coverage,
        "start_page": 0,
        "end_page": 0,
    }


def process_generation(sb: Client, job: dict, generation_id: str) -> None:
    # Lazy imports shared by every generation kind.
    from agent1_ingestion.extractor import extract_pdf
    from agent1_ingestion.image_extractor import extract_images
    from agent1_ingestion.structurer import structure_book
    from agent2_analysis.analyzer import run_full_analysis
    from shared.claude_client import ClaudeClient
    from shared.llm import client_for
    from worker.branding import load_branding

    # Label every model call made under this job so spend can be attributed
    # to a lesson. Without this the token log carried only counts and a
    # timestamp: "what did this generation cost" was unanswerable from our own
    # data, and so was "is the semantic path cheaper than the legacy one".
    from shared.claude_client import set_usage_context
    set_usage_context(generation_id=generation_id,
                      job_id=str(job.get("id") or ""),
                      engine=os.getenv("VIDEO_ENGINE", "native"),
                      semantic=os.getenv("SEMANTIC_PLAN", "") == "1")

    job_id = job["id"]
    db.set_generation_status(sb, generation_id, "processing")  # so the UI shows progress, not "queued"
    gen = db.get_generation(sb, generation_id)
    book = db.get_book(sb, gen["book_id"])
    # Console takedown racing a queued job: taken-down content must not be
    # (re)generated. Columns arrive with app migration 0015 — .get() is safe
    # either way.
    if gen.get("removed_at") or book.get("removed_at"):
        raise RuntimeError("content removed")
    owner_id = gen["owner_id"]
    book_id = book["id"]
    kind = gen.get("kind") or "presentation"
    # Idempotent re-run: if the reaper requeued this generation after the worker
    # died mid-run, drop any artifact rows it already produced so the re-run can't
    # leave duplicates (deterministic storage paths are overwritten on re-upload).
    db.clear_artifacts(sb, generation_id)
    # A requeued/re-run job must not show the dead run's part stage.
    db.set_stage(sb, job_id, None)

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = db.download_book(sb, book["storage_path"], Path(tmp) / "book.pdf")
        db.set_progress(sb, job_id, 10)

        # Ingestion + analysis, routed by the BOOK's script (shared/model_routing).
        # This has to route, not just authoring: measured, the Sonnet analysis
        # DOMINATES a kit's cost — $2.08 against a modelled $1.15 — because each
        # artifact makes 4-5 calls and only one of them is the authoring call.
        # Moving authoring alone would shift almost none of the spend onto the
        # GCP credits, which is the point of the exercise.
        client = client_for(book.get("language"))

        # Agent 1 — ingest. Chapter boundaries stored at indexing time are reused
        # (known_chapters) so every generation splits the book identically without
        # re-running detection; client+pdf_path enable the Claude fallback for
        # books the heuristics can't read (see structure_book's cascade).
        extraction = extract_pdf(str(pdf_path))
        images = extract_images(str(pdf_path), book_id)
        structured = structure_book(
            book_id=book_id, title=book.get("title") or "Untitled",
            author=book.get("author") or "Unknown", isbn=None, extraction=extraction, images=images,
            pdf_path=str(pdf_path), client=client, known_chapters=book.get("chapters"),
        ).model_dump()
        chapter = _pick_chapter(structured.get("chapters", []), gen.get("chapter_ref"))
        # Cumulative revision paper (worksheet/exam over a GROUP of chapters):
        # merge the selected chapters into one synthetic chapter and generate the
        # paper from it. Marked chapter_num = -1 so its analysis/grounding is
        # never cached as a real chapter's.
        # A cumulative EXAM (kind 'exam', 0062) merges a chosen set of covered
        # {chapter, part} units — part-granular, so unticked parts are excluded.
        _exam_scope = (gen.get("params") or {}).get("scope")
        is_exam = kind == "exam" and isinstance(_exam_scope, list) and len(_exam_scope) > 0
        _rev_chapters = (gen.get("params") or {}).get("chapters")
        _rev_valid = (
            [int(str(n).strip()) for n in _rev_chapters if str(n).strip().lstrip("-").isdigit()]
            if kind in ("worksheet", "exam_paper") and isinstance(_rev_chapters, list) else []
        )
        # A cumulative unit has no single chapter to heal/OCR/verify or cache.
        is_cumulative = is_exam or len(_rev_valid) >= 2
        if is_exam:
            chapter = _combine_units(structured.get("chapters", []), _exam_scope, sb, book_id)
            if not chapter.get("sections"):
                # Every scope unit resolved to no source text (e.g. a re-index
                # shifted chapter numbering). Fail loud — never hand the model an
                # empty chapter and let it hallucinate a whole exam.
                raise RuntimeError(
                    "The selected chapters/parts have no source text to build an exam from. "
                    "Re-index the book, then rebuild the exam."
                )
        elif len(_rev_valid) >= 2:
            chapter = _combine_chapters(structured.get("chapters", []), _rev_chapters, sb, book_id)
        elif _rev_valid and not gen.get("chapter_ref"):
            # A one-chapter revision paper (params.chapters=[N], no chapter_ref):
            # generate from THAT chapter, not the book's first (defence-in-depth
            # — the UI blocks combining a single chapter, but never trust it).
            chapter = _pick_chapter(structured.get("chapters", []), str(_rev_valid[0]))
        chapter_num = int(chapter.get("chapter_num", 0))
        chapter_title = chapter.get("title") or f"Chapter {chapter_num}"

        # Whether the title-vs-content check can validly apply here (see the
        # helper's docstring — single/whole-book chapters would always fail it).
        title_gate_applies = _title_gate_applies(
            chapter_title, book.get("title"),
            len(structured.get("chapters", [])), is_cumulative,
        )

        # Self-heal overlay (behind FEATURE_CHAPTER_HEAL, which the operator turns on
        # only AFTER applying migration 0040 — the heal_* columns): a book indexed
        # before the boundary fix may have wrong pages stored for this chapter. A
        # per-chapter relocation override (persisted the first time we healed it)
        # wins over the stored pages, so the fix is paid once. 'not_found' means an
        # earlier relocation PROVED the topic isn't in this book — fail fast.
        # Cumulative revision papers have no single chapter to heal/OCR — their
        # text is already gathered from the per-chapter cache.
        heal_on = _chapter_heal_enabled() and not is_cumulative
        heal = db.get_chapter_heal(sb, book_id, chapter_num) if heal_on else None
        # A stale 'not_found' verdict must not brick a chapter the gate no longer
        # applies to (e.g. a whole-book chapter titled with the filename).
        if heal and heal.get("status") == "not_found" and title_gate_applies:
            raise RuntimeError(_chapter_check_error(chapter_title, "a different topic"))
        healed_text = None
        if heal and heal.get("status") == "ok" and heal.get("start_page") is not None:
            chapter["start_page"] = int(heal["start_page"])
            chapter["end_page"] = int(heal["end_page"])
            healed_text = heal.get("source_text")

        # Was the chapter's text read WHOLE? Only a complete read may become a
        # stored part count — see _persist_measured_parts. Unknown (cached or
        # heal-supplied text) counts as complete: the alternative is refusing to
        # ever measure a chapter transcribed before this shipped.
        ocr_complete = True

        # Scanned book (no text layer) → the chapter has no content for the
        # pipeline to teach from. Transcribe its pages with Claude vision once,
        # up front, so every generation kind gets real chapter text.
        if healed_text:
            chapter["sections"] = [{
                "section_title": "Content", "section_type": "body",
                "content": healed_text, "page_num": chapter.get("start_page", 0),
                "subsections": [],
            }]
        else:
            section_chars = sum(
                len(s.get("content") or "")
                + sum(len(ss.get("content") or "") for ss in (s.get("subsections") or []))
                for s in (chapter.get("sections") or [])
            )
            if section_chars < 200 and not is_cumulative:
                # Scanned chapter: transcribe the pages with Claude vision ONCE and
                # cache the text (chapter_grounding.source_text, keyed book+chapter).
                # Every later generation of this chapter — any kind, any owner —
                # reuses it instead of re-running the multi-call, ~minutes-long OCR.
                ocr_text = db.get_chapter_source_text(sb, book_id, chapter_num)
                if not ocr_text:
                    from agent1_ingestion.vision_chapters import (
                        chapter_text_vision, transcribe_page_range)

                    _s = int(chapter.get("start_page", 0))
                    _e = int(chapter.get("end_page", 0))
                    # The chapter is read to its own end. The ONLY way it is not
                    # is the runaway guard, which cannot trip on a real chapter —
                    # so if it does, the chapter map is broken and the teacher is
                    # about to be taught from part of a book. Never silent: a
                    # partial read that looked complete is exactly how a
                    # six-artifact kit, exam included, once shipped from 19% of a
                    # 54-page chapter.
                    _read = transcribe_page_range(_s, _e)
                    if len(_read) < (_e - _s + 1):
                        logger.error(
                            "PARTIAL TRANSCRIPTION book=%s ch=%s: pages %d-%d requested, %d-%d read "
                            "(%d of %d). The chapter map is almost certainly wrong — a real chapter "
                            "is not this long. Everything past page %d is absent from every artifact "
                            "generated for this chapter.",
                            book_id, chapter_num, _s + 1, _e + 1, _read.start + 1, _read.stop,
                            len(_read), _e - _s + 1, _read.stop,
                        )
                    _report: dict = {}
                    ocr_text = chapter_text_vision(str(pdf_path), _s, _e, client, report=_report)
                    # The bounds check above catches the runaway clamp. It cannot
                    # catch the OTHER way this comes back short: on an API failure
                    # mid-loop the transcriber returns the chunks it already has
                    # rather than losing them, so a half-read chapter looks like a
                    # whole one. Only the transcriber knows, so it now says.
                    _done = int(_report.get("pages_done", 0))
                    _want = int(_report.get("pages_requested", 0))
                    if _want and _done < _want:
                        ocr_complete = False
                        logger.error(
                            "PARTIAL TRANSCRIPTION book=%s ch=%s: %d of %d pages transcribed — "
                            "the artifacts for this chapter will be built from part of it",
                            book_id, chapter_num, _done, _want,
                        )
                    if len(_read) < (_e - _s + 1):
                        ocr_complete = False
                    if ocr_text:
                        db.set_chapter_source_text(sb, book_id, chapter_num, ocr_text)
                if ocr_text:
                    chapter["sections"] = [{
                        "section_title": "Content", "section_type": "body",
                        "content": ocr_text, "page_num": chapter.get("start_page", 0),
                        "subsections": [],
                    }]
                    # Scanned books skip index-time language detection (no text
                    # layer) — the first OCR'd chapter fills the gap here.
                    if not book.get("language"):
                        from shared.languages import detect_language

                        _det = detect_language(ocr_text)
                        if _det:
                            db.set_book_language(sb, book_id, _det)
                            book["language"] = _det  # this job resolves with it too

        # Guard: the sliced text must actually belong to the requested chapter.
        # On mismatch, SELF-HEAL — find the pages that actually teach this title,
        # transcribe + strict-confirm them, generate from those, and persist the
        # correction so it is paid once. Only a topic genuinely absent from the
        # book fails loud (and is remembered, so it fails fast next time).
        from agent1_ingestion.chapter_check import verify_chapter_content

        relocated_now = False

        def _sample_text() -> str:
            return " ".join(
                (s.get("content") or "") + " "
                + " ".join(ss.get("content") or "" for ss in (s.get("subsections") or []))
                for s in (chapter.get("sections") or [])
            )

        # A cumulative revision paper's title ("Revision — Chapters 1–3") names
        # no single topic, and a whole-book/book-titled chapter has no
        # chapter-specific topic either — the check applies only where a wrong
        # boundary is possible (see title_gate_applies above). Skipping also
        # saves the Claude call per paper on the synthetic labels.
        ok, actual = (True, None) if not title_gate_applies else verify_chapter_content(chapter_title, _sample_text(), client)
        if not ok:
            # Without the flag/migration, keep the pre-heal behavior: fail loud.
            if not heal_on:
                raise RuntimeError(_chapter_check_error(chapter_title, actual))
            result = {"status": "incomplete"}
            try:
                from agent1_ingestion.vision_chapters import relocate_chapter_for_generation

                result = relocate_chapter_for_generation(
                    str(pdf_path), extraction, chapter, client,
                )
            except Exception as exc:  # noqa: BLE001 — relocation is best-effort
                logger.warning("relocation failed for %s ch%s: %s", book_id, chapter_num, exc)
            if result.get("status") == "ok":
                relocated_now = True  # pages just moved — any cached analysis is stale
                chapter["start_page"] = result["start_page"]
                chapter["end_page"] = result["end_page"]
                chapter["sections"] = [{
                    "section_title": "Content", "section_type": "body",
                    "content": result["source_text"],
                    "page_num": result["start_page"], "subsections": [],
                }]
                db.set_chapter_heal(
                    sb, book_id, chapter_num, result["start_page"],
                    result["end_page"], result["source_text"], "ok",
                )
                logger.info(
                    "self-healed chapter %s of book %s → pages %d-%d (%s)",
                    chapter_num, book_id, result["start_page"],
                    result["end_page"], result.get("actual_topic") or "",
                )
            elif result.get("status") == "absent":
                # We searched properly and the topic isn't in the book — remember it
                # so the next click fails fast instead of re-running a vision sweep.
                db.set_chapter_heal(sb, book_id, chapter_num, None, None, None, "not_found")
                raise RuntimeError(_chapter_check_error(chapter_title, actual))
            else:
                # Could NOT prove absence (vision outage, empty OCR, error). Fail
                # loud but DO NOT persist — a transient blip must not brick a chapter
                # that is actually present; the next run can recover.
                raise RuntimeError(_chapter_check_error(chapter_title, actual))
        db.set_progress(sb, job_id, 20)

        # The chapter's text is final HERE and not before: the OCR block above
        # is only one of three ways sections get filled — a persisted heal
        # override installs its own text without ever entering it, and a
        # relocation REPLACES both the text and the pages afterwards. Measuring
        # at the OCR site therefore missed exactly the chapters whose estimates
        # were worst, and could be stale for the third. Measuring here covers
        # all three and cannot go stale.
        if ocr_complete and not is_cumulative:
            _persist_measured_parts(sb, book_id, chapter_num, chapter, book)


        # ── ON-DEMAND SINGLE PART (params.part = k, 2026-07-18) ──────────────
        # The part map is computed at INDEX time with the same chunker, so a
        # teacher can generate Part 3's lesson (or worksheet, or exam…) alone.
        # The chapter is narrowed to that part's text BEFORE analysis: every
        # artifact of a part job is grounded on the part, and the chapter-level
        # analysis cache/grounding is neither read nor written (it describes
        # the WHOLE chapter; a part must not impersonate it).
        part_ref: int | None = None
        part_total = 1
        forced_part_info: dict | None = None
        _raw_part = (gen.get("params") or {}).get("part")
        if _raw_part is not None:
            try:
                part_ref = int(_raw_part)
            except (TypeError, ValueError):
                raise RuntimeError(f"Invalid part {_raw_part!r} — expected a part number.")
        if part_ref is not None:
            from agent2_analysis.analyzer import build_chapter_parts

            all_parts = build_chapter_parts(chapter)
            part_total = len(all_parts)
            if part_ref < 1 or part_ref > part_total:
                raise RuntimeError(
                    f"'{chapter_title}' has {part_total} part(s) — part {part_ref} does not exist. "
                    "If the book's chapters changed, re-index it to refresh the part map."
                )
            pk = all_parts[part_ref - 1]
            if part_total > 1:
                forced_part_info = {
                    "part": part_ref,
                    "total": part_total,
                    "prev_sections": [t for p in all_parts[: part_ref - 1] for t in (p.get("section_titles") or [])],
                    "next_sections": (all_parts[part_ref].get("section_titles") or []) if part_ref < part_total else [],
                }
            # Narrowed chapter for docgen + image analysis; the ANALYSIS gets
            # the chunk verbatim via chunks_override (re-chunking an at-budget
            # chunk would split off a junk tail episode — review-caught).
            part_chunk = dict(pk)
            chapter = {
                "chapter_num": chapter.get("chapter_num"),
                "title": chapter.get("title"),
                "sections": [{
                    "section_title": (pk.get("section_titles") or [chapter_title])[0],
                    "content": pk.get("text", ""),
                    "subsections": [],
                }],
                "key_boxes": [],
                # The chapter's figures still feed the visual pipeline — a part
                # lesson must not silently lose all image analysis.
                "images": chapter.get("images", []),
            }

        # ── Lesson language (2026-07-18) ─────────────────────────────────────
        # params.language (teacher's explicit choice) → books.language (detected
        # at indexing) → English. One value drives the prompts (analysis,
        # scripts, documents), the layout direction (Arabic = RTL) and the
        # default narration voice.
        from shared.languages import get_language

        _lang_obj = get_language((gen.get("params") or {}).get("language") or book.get("language"))
        lesson_lang = _lang_obj.code
        lesson_dir = _lang_obj.direction

        # Jawi (Malay in the Arabic script, RTL). Documents author it directly.
        # The VIDEO is DUAL-SCRIPT: the slides/deck show Jawi (Noto Sans Arabic +
        # the Arabic RTL layout), while the narration is spoken Malay — the
        # script generator writes narration in Rumi (Malay TTS can't read the
        # Arabic script) and the on-screen text in Jawi; the same Malay words in
        # two scripts. The voice resolves to Malay (default_voice_id_for maps
        # ms-arab → ms).
        jawi = lesson_lang == "ms-arab"

        # Agent 2 — analysis (shared by every kind). The FULL analysis is
        # persisted per (book, chapter) in chapter_grounding.concepts, so the
        # 2nd..Nth artifact of the same chapter REUSES it instead of paying the
        # analysis call(s) again — the single biggest per-job cost (a full-kit
        # chapter previously re-analysed 6 times). Reuse is refused for v1
        # (pre-chunking) rows, immediately after a relocation in THIS job, and
        # for single-part jobs (the cache describes the whole chapter).
        analysis = None if (relocated_now or part_ref is not None or is_cumulative) else db.get_chapter_analysis(sb, book_id, chapter_num)
        if analysis is not None and (analysis.get("language") or "en") != lesson_lang:
            # The cache is language-stamped: a Malay analysis must not ground an
            # English lesson (or vice versa) — regenerate in the right language.
            logger.info(
                "generation %s: cached analysis is %s, lesson is %s — re-analysing",
                generation_id, analysis.get("language") or "en", lesson_lang,
            )
            analysis = None
        if analysis is not None:
            logger.info("generation %s: reusing cached analysis for %s ch%s", generation_id, book_id, chapter_num)
        else:
            # Fresh multi-part analysis is the LONGEST phase — advance 20→43
            # per chunk (and label the stage) so a 4-part chapter never looks
            # "stuck at 20%".
            def _analysis_tick(part: int, total: int) -> None:
                db.set_progress(sb, job_id, 20 + round(23 * part / total))
                if total > 1:
                    db.set_stage(sb, job_id, {"phase": "analysis", "part": part, "total": total, "part_pct": 100})

            analysis = run_full_analysis(
                book_id=book_id, chapter_content=chapter, level=DEFAULT_LEVEL, client=client,
                on_progress=_analysis_tick,
                # Part jobs analyze THEIR chunk verbatim — never re-chunked.
                chunks_override=[part_chunk] if part_ref is not None else None,
                language=lesson_lang,
            ).model_dump()
            analysis["language"] = lesson_lang  # stamps the cache (see reuse guard above)
        db.set_progress(sb, job_id, 45)
        # Analysis stage is over for every kind; the presentation loop writes
        # its own per-part stages, doc kinds stay stage-less.
        db.set_stage(sb, job_id, None)

        # Persist chapter grounding for the AI Tutor — the concept analysis now
        # (covers every kind); the narrated lesson's script is added below for
        # the presentation kind. This is the tutor's curriculum fence. Best-effort.
        # Part jobs skip it: their analysis covers ONE part, and writing it here
        # would poison the whole-chapter cache and the tutor's fence.
        if part_ref is None and not is_cumulative:
            db.set_chapter_grounding(sb, book_id, chapter_num, chapter_title, analysis)

        # School branding (templates + derived accent/logo) — falls back to defaults.
        branding = load_branding(sb, owner_id, Path(tmp))
        base = f"{owner_id}/{generation_id}"

        if kind == "presentation":
            # Narrated deck + video — PART-MAJOR (2026-07-18): each part runs
            # script → slides → video → upload END TO END before the next part
            # starts. Part 1 is finished (and its artifacts uploaded) while
            # Part 2's script is still being written, and the job's stage
            # reads "part 2/4 · 35%" instead of one opaque global bar.
            # Sequential on purpose — the per-chapter slide/video segment dirs
            # are shared, so parts must not interleave. Part 1 keeps the legacy
            # lesson.mp4 / deck.pptx names; later parts get _part{k} suffixes
            # (the app sorts video artifacts by part number → Part 1 first).
            from datetime import datetime
            from agent3_scripts.script_generator import generate_episode_script, save_script
            from agent5_slides.figures import (attach_figures_to_segments,
                                               load_chapter_figures, textbook_figures_enabled)
            from agent5_slides.slide_generator import generate_episode_slides
            from agent6_animation.video_composer import compose_episode_videos
            from agent8_render.renderer import render_final_video

            params = gen.get("params") or {}
            narration_style = params.get("narration_style") or "socratic"
            tts_voice = params.get("tts_voice")  # voice-registry id; None → free Edge default
            if lesson_lang != "en":
                from shared.tts.registry import default_voice_id_for, get_voice

                _v = get_voice(tts_voice)
                if not tts_voice:
                    # No explicit pick: the matching free Edge voice — an
                    # English voice reading Malay/Arabic is wrong.
                    tts_voice = default_voice_id_for(lesson_lang)
                elif (
                    _v is not None
                    and _v.provider == "edge"
                    and _v.lang != lesson_lang
                    and not (gen.get("params") or {}).get("language")
                ):
                    # Stale pre-language params (e.g. edge-aria pinned before
                    # the book's language was detected) being REGENERATED under
                    # the book-language fallback: remap to the matching voice.
                    # An EXPLICIT params.language choice always keeps its voice.
                    tts_voice = default_voice_id_for(lesson_lang)
            allow_premium = _elevenlabs_enabled()

            # Avatar casting: teacher matches the narration VOICE, student
            # matches the book's GRADE band (auto-detected at upload,
            # teacher-editable), gender seeded by generation id so retries
            # render the identical character.
            from shared.tts.registry import default_voice
            from spike.scene_engine.whiteboard import (
                student_avatar_for_grade, teacher_avatar_for_voice)
            # cast against the voice that will actually SPEAK — with no
            # explicit pick, that is the registry default (Aria, female),
            # not "no voice" (which once cast a male teacher over her)
            effective_voice = tts_voice or default_voice().voice_id
            avatars = {
                "teacher": teacher_avatar_for_voice(effective_voice),
                "student": student_avatar_for_grade(book.get("grade"),
                                                    seed=generation_id),
            }

            episodes_plan = (analysis.get("episodes") or {}).get("episodes") or []
            n_parts = max(len(episodes_plan), 1)
            voice_report: dict = {}
            uploaded_videos = 0
            script_dicts: list[dict] = []
            coverage_reports: list[dict] = []
            ep_title = chapter_title

            # Phase 3 (gated): detect + crop this chapter's real textbook figures
            # ONCE, then hand each part the ones that best match its segments. The
            # crops live under the job tmp — embedded into the deck + composited
            # into the video before cleanup. Best-effort: never breaks a lesson.
            chapter_figures: list[dict] = []
            used_figures: set[int] = set()
            _figures_on = textbook_figures_enabled()
            logger.info("textbook figures flag: %s", _figures_on)  # is the env var live on THIS worker?
            if _figures_on:
                try:
                    # Figures were detected at INDEX time (page ranges are reliable
                    # there; gen time has none) and stored on book.chapters — just
                    # crop them from the downloaded PDF here.
                    chapter_figures = load_chapter_figures(
                        book, chapter_num, pdf_path, Path(tmp) / "figures"
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("figure load failed: %s", exc)

            for part_idx, episode in enumerate(episodes_plan, start=1):
                # This part's slice of the overall bar: 45 → 96 split evenly.
                span = 51.0 / n_parts
                base_p = 45 + span * (part_idx - 1)
                db.set_stage(sb, job_id, {"phase": "video", "part": part_idx, "total": n_parts, "part_pct": 5})
                db.set_progress(sb, job_id, round(base_p))

                # Same part framing the chapter-level generator builds: recap
                # what earlier parts covered, preview what the next part will.
                # A single-part job carries its framing in forced_part_info
                # (its analysis holds ONE episode, but the chapter has more).
                part_info = forced_part_info
                if part_info is None and n_parts > 1:
                    part_info = {
                        "part": part_idx,
                        "total": n_parts,
                        "prev_sections": [
                            s for e in episodes_plan[: part_idx - 1] for s in e.get("sections_covered", [])
                        ],
                        "next_sections": episodes_plan[part_idx].get("sections_covered", [])
                        if part_idx < n_parts
                        else [],
                    }
                script = generate_episode_script(
                    episode, analysis, chapter_num, client, narration_style, part_info=part_info,
                    language=lesson_lang, avatars=avatars,
                    subject=book.get("subject"), curriculum=book.get("curriculum"),
                    learner_age=book.get("grade"),
                )
                script_dict = script.model_dump()

                # ── Coverage gate (shared/coverage.py) ───────────────────────
                # Until now nothing compared the reply back to the sections and
                # concepts THIS episode was built from, so a script that taught
                # a third of the part finished as a clean success. Measured here
                # — after the script, before slides/TTS/render — so the retry
                # below costs one script call and a hard failure wastes nothing
                # that has been rendered.
                report = _coverage_report(
                    analysis, episode, coverage.script_text(script_dict),
                    kind="presentation", model=client.model, part=part_idx, of=n_parts,
                    part_scoped=part_ref is not None,
                )
                if coverage.should_retry(report):
                    # Retry ONCE, naming what was dropped, and keep whichever
                    # draft measures higher — a retry can be worse, and the
                    # teacher should never get the worse of two scripts we paid
                    # for. Bounded by should_retry: the same bar as the hard
                    # failure below, so it only ever fires on a job that would
                    # otherwise have failed outright — and NEVER on a pooled
                    # part-scoped report, whose missed list is other parts'
                    # topics (a must_cover built from it orders the model to
                    # teach material this part does not contain — the harmful
                    # half of incident 8b79d4e0).
                    first = report
                    retry = generate_episode_script(
                        episode, analysis, chapter_num, client, narration_style,
                        part_info=part_info, language=lesson_lang,
                        must_cover=first.get("missed") or [], avatars=avatars,
                        subject=book.get("subject"), curriculum=book.get("curriculum"),
                        learner_age=book.get("grade"),
                    )
                    retry_dict = retry.model_dump()
                    retry_report = _coverage_report(
                        analysis, episode, coverage.script_text(retry_dict),
                        kind="presentation", model=client.model, part=part_idx, of=n_parts,
                        part_scoped=part_ref is not None,
                    )
                    if (retry_report.get("covered") or 0) > (first.get("covered") or 0):
                        script, script_dict, report = retry, retry_dict, retry_report
                    # Both numbers are kept: whether naming the missed topics
                    # actually repairs a thin draft is itself a thing the
                    # founder will want to query after the model flip.
                    report["retried_from"] = first.get("covered")
                    if coverage.should_fail(report):
                        coverage_reports.append(report)
                        _record_coverage(sb, generation_id, coverage_reports)
                        raise RuntimeError(
                            f"lesson script covers only {report['addressed']} of "
                            f"{report['topics']} topics this chapter's analysis "
                            f"lists (part {part_idx}/{n_parts}, model {client.model}) "
                            f"— never mentioned: {', '.join(report['missed'])}"
                        )
                coverage_reports.append(report)
                save_script(script)
                # Attach matched textbook figures to this part's segments (semantic
                # match via the model, keyword fallback).
                if chapter_figures:
                    attach_figures_to_segments(
                        script_dict.get("segments", []), chapter_figures, used_figures, client
                    )
                script_dicts.append(script_dict)
                if part_idx == 1:
                    ep_title = script_dict.get("episode_title") or chapter_title
                db.set_stage(sb, job_id, {"phase": "video", "part": part_idx, "total": n_parts, "part_pct": 35})
                db.set_progress(sb, job_id, round(base_p + 0.35 * span))

                part_scripts = {
                    "book_id": book_id,
                    "chapter_num": chapter_num,
                    "chapter_title": chapter_title,
                    "total_episodes": 1,
                    "generated_at": datetime.now().isoformat(),
                    "episodes": [script_dict],
                }
                slides = generate_episode_slides(
                    script_data=part_scripts, branding=branding, direction=lesson_dir,
                ).model_dump()

                video = compose_episode_videos(
                    script_data=part_scripts, slide_manifest=slides, branding=branding,
                    tts_voice=tts_voice, allow_premium=allow_premium, voice_report=voice_report,
                    direction=lesson_dir,
                ).model_dump()

                final = render_final_video(video_manifest=video).model_dump()

                # ACCEPTANCE. Until now this ran only in tests: the engine
                # computed a full per-lesson quality audit on every render and
                # nothing in production ever read it, so blank boards, silent
                # lessons and text written over text all shipped while the
                # report said PASSED — to a local driver script nobody ran.
                # This is the one place that knows the lesson is finished and
                # can still refuse it.
                _accept = _acceptance_report(part_scripts, video)
                if _accept is not None:
                    db.set_stage(sb, job_id, {"phase": "video", "part": part_idx,
                                              "total": n_parts, "part_pct": 99,
                                              "acceptance": _accept["summary"]})
                    if not _accept["ship"]:
                        raise RuntimeError(
                            f"lesson failed acceptance: {_accept['summary']}")

                suffix = "" if part_idx == 1 else f"_part{part_idx}"
                deck_path = slides.get("deck_path")
                if deck_path and Path(deck_path).exists():
                    db.upload_artifact(sb, deck_path, f"{base}/deck{suffix}.pptx")
                    db.add_artifact_row(sb, generation_id, "deck_pptx", f"{base}/deck{suffix}.pptx")
                final_video = final.get("final_video_path")
                if final_video and Path(final_video).exists():
                    db.upload_artifact(sb, final_video, f"{base}/lesson{suffix}.mp4")
                    db.add_artifact_row(sb, generation_id, "video_mp4", f"{base}/lesson{suffix}.mp4")
                    uploaded_videos += 1
                db.set_stage(sb, job_id, {"phase": "video", "part": part_idx, "total": n_parts, "part_pct": 100})
                db.set_progress(sb, job_id, round(base_p + span))

            if uploaded_videos == 0:
                raise RuntimeError("no video parts were produced")
            db.set_stage(sb, job_id, None)  # stage is per-part; clear it once all parts are done
            _record_coverage(sb, generation_id, coverage_reports)

            # Enrich the tutor grounding with the lesson's own narration text —
            # the best source for answers that "sound like the lesson". Skipped
            # for single-part jobs: the whole-chapter grounding is not theirs
            # to overwrite (the assistant still answers from source_text).
            if part_ref is None:
                _script_text = " ".join(
                    (seg.get("text") or "")
                    for ep in script_dicts
                    for seg in (ep.get("segments") or [])
                ).strip()
                db.set_chapter_grounding(sb, book_id, chapter_num, chapter_title, analysis, _script_text)

                # Warm the AI Tutor cache: pre-compute the questions a student is
                # most likely to ask so the first "Ask Coach" is instant + $0.
                # Gated (TUTOR_WARM_CACHE) and best-effort.
                from worker.tutor_warm import warm_tutor_cache
                warm_tutor_cache(sb, client, book_id, chapter_num, chapter_title, analysis, _script_text)

            # Record the voice that ACTUALLY rendered so a silent premium→free
            # downgrade (ElevenLabs not enabled/keyed on the worker, spend cap, or
            # an API failure) is visible in the app — not discovered by listening.
            if voice_report:
                db.merge_generation_params(sb, generation_id, {
                    "tts_voice_used": (voice_report.get("used") or [None])[0],
                    "tts_voice_downgraded": bool(voice_report.get("downgraded")),
                })
                if voice_report.get("downgraded"):
                    logger.warning(
                        "generation %s: requested premium voice %r but rendered %s — "
                        "set ELEVENLABS_ENABLED=true + ELEVENLABS_API_KEY on the worker.",
                        generation_id, tts_voice, voice_report.get("used"),
                    )
            if part_ref is not None:
                # The chapter name and the part's position always travel
                # together, plus the part's own section heading when it has a
                # real one — this string is rendered verbatim by the parent
                # portal, the three diary views and the staff console, so
                # "Part 1" on its own tells those readers nothing.
                title = (
                    f"{book.get('title', 'Lesson')} · "
                    f"{part_label(chapter_title, part_ref, part_total, pk.get('section_titles'))}"
                )
            else:
                # Every chapter-level render reports its delivered part count,
                # including 1 — app migration 0089 seeds the credit ledger from
                # the book's part-map at insert, and the sync trigger can only
                # correct that estimate (down as well as up) on a value the
                # worker actually wrote. Nothing in the app reads this key.
                db.merge_generation_params(sb, generation_id, {"video_parts": uploaded_videos})
                if n_parts > 1:
                    title = f"{book.get('title', 'Lesson')} · {chapter_title} ({uploaded_videos} parts)"
                else:
                    title = f"{book.get('title', 'Lesson')} · {ep_title}"

        elif kind in ("lesson_plan", "activity", "exam_paper", "worksheet", "case_study"):
            # Claude-authored teacher document → editable .docx.
            from docgen import generate_document
            from shared.claude_client import artifact_model

            # Authoring runs on the cheaper artifact model (Haiku by default); the
            # ingestion/analysis above stay on Sonnet. Fold its spend into the job so
            # jobs.usage still reflects the full per-lesson cost.
            # Jawi is the exception: Haiku's Jawi orthography is unreliable
            # (verified 2026-07-19), so Jawi documents author on the stronger
            # analysis model.
            # Authoring, routed by the LESSON's script. Jawi keeps its exception:
            # ms-arab routes to Claude on script, but Haiku's Jawi orthography is
            # unreliable (verified 2026-07-19), so kind=None asks for the stronger
            # general model instead of the per-kind Haiku default.
            gen_client = client_for(lesson_lang, kind=None if jawi else kind)
            out_path = generate_document(
                kind=kind, book=book, chapter=chapter, analysis=analysis,
                client=gen_client, params=gen.get("params") or {}, out_dir=Path(tmp),
                template=branding.get("docx_template"),
                language=lesson_lang,
            )
            # Student/teacher split (2026-08-18): exam_paper/worksheet/activity/
            # case_study now return [student_document, answer_key] like the
            # cumulative exam; lesson_plan still returns a single Path.
            paths = out_path if isinstance(out_path, list) else [out_path]
            for _k, _v in gen_client.session_usage.items():
                client.session_usage[_k] = client.session_usage.get(_k, 0) + _v

            # ── Coverage: MEASURED and RECORDED for documents, never gated ────
            # These five kinds are exactly the ones Stage 1 moves to Haiku, so
            # they are the ones whose before/after numbers matter most — but a
            # document is not a lesson and must not be judged like one. A
            # 10-question worksheet cannot mention 23 concepts; its expected
            # coverage is set by its own length, not by the chapter's size, and
            # it differs again for a lesson plan (which does claim the whole
            # chapter) and a case study (which is deliberately one scenario). No
            # absolute threshold is defensible for them today because no
            # baseline exists for any of them. What IS defensible is recording
            # the number per kind and per model, which is precisely what the
            # Sonnet-vs-Haiku comparison needs — a per-kind threshold becomes a
            # one-line addition once a week of Sonnet rows is in the table.
            # Episode is None: documents are generated per CHAPTER, never per
            # part, so they are measured against the whole chapter's topics.
            # Only the STUDENT document is measured (paths[0]) — same reasoning
            # as the cumulative exam: the answer key restates the paper.
            try:
                _doc_report = _coverage_report(
                    analysis, None, coverage.docx_text(paths[0]),
                    kind=kind, model=gen_client.model,
                )
                _record_coverage(sb, generation_id, [_doc_report])
            except Exception as exc:  # noqa: BLE001
                logger.warning("coverage not recorded for %s: %s", kind, exc)

            db.set_progress(sb, job_id, 90)
            dest = f"{base}/{kind}.docx"
            db.upload_artifact(sb, str(paths[0]), dest)
            db.add_artifact_row(sb, generation_id, "docx", dest)
            if len(paths) > 1:
                key_dest = f"{base}/answer_key.docx"
                db.upload_artifact(sb, str(paths[1]), key_dest)
                # answer_key_docx artifact_kind exists since app migration 0062;
                # a not-yet-applied migration must not fail the generation
                # (mirrors the cumulative exam block below). The presence of
                # this sibling row is the app's proof the student 'docx' above
                # is key-free and safe to hand to a learner.
                try:
                    db.add_artifact_row(sb, generation_id, "answer_key_docx", key_dest)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("answer_key_docx row skipped for %s: %s", generation_id, exc)

            # Structured questions for the interactive quiz player (worksheet/exam).
            # Additive + best-effort: a missing 'questions_json' enum value (migration
            # not yet applied) must not fail the generation.
            qpath = Path(tmp) / "questions.json"
            if kind in ("worksheet", "exam_paper") and qpath.exists():
                try:
                    qdest = f"{base}/{kind}_questions.json"
                    db.upload_artifact(sb, str(qpath), qdest)
                    db.add_artifact_row(sb, generation_id, "questions_json", qdest)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("questions_json upload skipped for %s: %s", generation_id, exc)

            db.set_progress(sb, job_id, 96)
            label = {
                "lesson_plan": "Lesson plan", "activity": "Activities", "exam_paper": "Test paper",
                "worksheet": "Worksheet", "case_study": "Case study",
            }[kind]
            # Same composer as the presentation path — a worksheet for part 3
            # used to be titled "· Part 3" with no total and no heading, so
            # part 1's and part 3's documents were indistinguishable.
            _unit = (
                part_label(chapter_title, part_ref, part_total, pk.get("section_titles"))
                if part_ref is not None else chapter_title
            )
            title = f"{book.get('title', 'Document')} · {_unit} · {label}"

        elif kind == "exam":
            # Cumulative exam (0062): one model call → TWO documents (the exam
            # paper and a SEPARATE answer key), grounded on the chosen covered
            # units (chapter = _combine_units above). The answer key is uploaded
            # under its OWN artifact kind so it never rides a student's download.
            from docgen import generate_document
            from shared.claude_client import artifact_model

            # Authoring, routed by the LESSON's script. Jawi keeps its exception:
            # ms-arab routes to Claude on script, but Haiku's Jawi orthography is
            # unreliable (verified 2026-07-19), so kind=None asks for the stronger
            # general model instead of the per-kind Haiku default.
            gen_client = client_for(lesson_lang, kind=None if jawi else kind)
            out_paths = generate_document(
                kind="exam", book=book, chapter=chapter, analysis=analysis,
                client=gen_client, params=gen.get("params") or {}, out_dir=Path(tmp),
                template=branding.get("docx_template"),
                language=lesson_lang,
            )
            for _k, _v in gen_client.session_usage.items():
                client.session_usage[_k] = client.session_usage.get(_k, 0) + _v
            db.set_progress(sb, job_id, 90)
            paths = out_paths if isinstance(out_paths, list) else [out_paths]
            # Recorded, not gated — same reasoning as the single-chapter
            # documents above, and more so: a cumulative exam is measured
            # against EVERY covered unit's topics, so a low number here is the
            # exam being an exam, not the model being thin. Only the paper is
            # measured; the answer key restates it.
            try:
                _record_coverage(sb, generation_id, [_coverage_report(
                    analysis, None, coverage.docx_text(paths[0]),
                    kind="exam", model=gen_client.model,
                )])
            except Exception as exc:  # noqa: BLE001
                logger.warning("coverage not recorded for exam: %s", exc)
            paper_dest = f"{base}/exam.docx"
            db.upload_artifact(sb, str(paths[0]), paper_dest)
            db.add_artifact_row(sb, generation_id, "docx", paper_dest)
            if len(paths) > 1:
                key_dest = f"{base}/exam_answer_key.docx"
                db.upload_artifact(sb, str(paths[1]), key_dest)
                # answer_key_docx is a new artifact_kind (app migration 0062); a
                # not-yet-applied migration must not fail the exam.
                try:
                    db.add_artifact_row(sb, generation_id, "answer_key_docx", key_dest)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("answer_key_docx row skipped for %s: %s", generation_id, exc)
            db.set_progress(sb, job_id, 96)
            _exam_title = str((gen.get("params") or {}).get("title") or "").strip()
            title = f"{book.get('title', 'Document')} · {_exam_title or 'Exam'}"

        else:
            raise RuntimeError(f"Unsupported generation kind: {kind}")

        db.set_generation_title(sb, generation_id, title)

    # Attribute this job's Claude spend to the row (unit economics per lesson).
    db.set_job_usage(sb, job_id, client.session_usage)
    db.finish_job(sb, job_id, generation_id)
    logger.info("Generation %s (%s) done — %s", generation_id, kind, client.session_usage)


def index_book(sb: Client, job: dict) -> None:
    """Extract a book's chapter list (Agent 1) and store it on the book.

    Runs once per uploaded book so the dashboard can offer a lesson per chapter.
    Reuses the same chapter detection the generator uses (so `num` here matches
    what `_pick_chapter` selects), but skips image extraction — only chapter
    numbers + titles are needed for the list, which keeps indexing fast.
    """
    from agent1_ingestion.extractor import extract_pdf
    from agent1_ingestion.structurer import structure_book

    book_id = job["book_id"]
    book = db.get_book(sb, book_id)
    if book.get("removed_at"):
        raise RuntimeError("content removed")

    # Claude enables chapter detection for books the text heuristics can't read
    # (scanned pages, unconventional labels). Best-effort: without a key,
    # indexing still works for conventional books.
    #
    # NOT script-routed, deliberately. This runs BEFORE the book's language is
    # known — detection is what establishes books.language further down this
    # same function — so there is no script to route on yet. It also stays on
    # Claude because it is the escalation path for the books nothing else could
    # read, and indexing is cheap ($0.03 median) so little credit is left on the
    # table by the exception.
    try:
        from shared.claude_client import ClaudeClient
        client = ClaudeClient()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Claude unavailable for indexing (%s) — heuristics only", exc)
        client = None

    cover_dest = None
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = db.download_book(sb, book["storage_path"], Path(tmp) / "book.pdf")
        extraction = extract_pdf(str(pdf_path))
        structured = structure_book(
            book_id=book_id, title=book.get("title") or "Untitled",
            author=book.get("author") or "Unknown", isbn=None,
            extraction=extraction, images=[],
            pdf_path=str(pdf_path), client=client,
        ).model_dump()

        # Audit the detected list BEFORE it becomes the book's stored truth, and
        # SELF-HEAL it: Claude reads each chapter's opening page (vision, so a
        # scanned book is no longer a blind spot), fixes garbled titles, and moves
        # any chapter whose pages don't match its title to the pages that do —
        # preserving chapter_num, validate-or-revert so a bad heal can't corrupt
        # storage. Best-effort: any failure keeps the heuristic result.
        chapter_defs = structured.get("chapters", [])
        relocated_nums: list[int] = []
        if client is not None and len(chapter_defs) > 1:
            try:
                from agent1_ingestion.vision_chapters import heal_chapter_boundaries

                healed, relocated_nums = heal_chapter_boundaries(
                    str(pdf_path), extraction, chapter_defs, client
                )
                structured["chapters"] = healed
            except Exception as exc:  # noqa: BLE001 — healing must never block indexing
                logger.warning("chapter heal skipped for %s: %s", book_id, exc)

        # Phase 3 (gated): detect each chapter's teaching figures ONCE, HERE — where
        # the page range is reliable and the PDF is on disk. Only lightweight specs
        # (page + bbox + caption) are kept, on the chapter; generation crops them from
        # the PDF. This is the ONLY place with reliable page ranges — at generation
        # time the chapter carries none (start_page=None). Best-effort: never blocks.
        figure_specs: dict[int, list] = {}
        try:
            from agent5_slides.figures import detect_chapter_figure_specs, textbook_figures_enabled

            if textbook_figures_enabled() and client is not None:
                for c in structured.get("chapters", []):
                    try:
                        specs = detect_chapter_figure_specs(str(pdf_path), c, client)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("figure detect skipped for %s ch%s: %s", book_id, c.get("chapter_num"), exc)
                        continue
                    if specs:
                        figure_specs[int(c.get("chapter_num", -1))] = specs
        except Exception as exc:  # noqa: BLE001 — figures must never block indexing
            logger.warning("index figure detection skipped for %s: %s", book_id, exc)

        # Junk-upload gate, part 1: what KIND of document is this? One cheap,
        # bounded Claude call over the opening text (or the rendered opening
        # pages of a scan). The decision itself is computed in book_health and
        # stamped as health.gate — SOFT: the app asks "Generate anyway?",
        # nothing here blocks. Fail-open: any trouble → "unknown" → only the
        # volume rules gate. (A 1-page scanned class roster scored 55 and still
        # burned 6 generations, because nothing marked it.)
        doc_type = None
        try:
            from agent1_ingestion.doc_type import classify_doc_type

            doc_type = classify_doc_type(str(pdf_path), extraction, client)
        except Exception as exc:  # noqa: BLE001 — never block indexing
            logger.warning("doc_type classification skipped for %s: %s", book_id, exc)

        # Book Health Score — a predictive read from signals we already have,
        # shown the moment indexing finishes so a bad scan is caught before it
        # generates failed lessons. Best-effort: never block indexing.
        health = None
        try:
            from agent1_ingestion.book_health import compute_book_health

            # structured["apparatus"] (founder decision 2026-08-24): the
            # cover/contents/glossary/index ranges the structurer EXCLUDED from
            # the chapter list. Health stamps them into facts.apparatus and
            # subtracts their pages from the unmapped-pages guardrail, so the
            # deliberate exclusion is never reported as a detection hole — and
            # because they are not chapters, they are never part-split into
            # credit rows below either.
            health = compute_book_health(
                extraction, structured.get("chapters", []), doc_type=doc_type,
                apparatus=structured.get("apparatus") or [],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("book health skipped for %s: %s", book_id, exc)

        # Cover thumbnail (page 0) for the library UI — best-effort.
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(pdf_path))
            pix = doc.load_page(0).get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
            cover_png = Path(tmp) / "cover.png"
            pix.save(str(cover_png))
            doc.close()
            cover_dest = f"{book['owner_id']}/covers/{book_id}.png"
            db.upload_artifact(sb, str(cover_png), cover_dest)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cover render failed for %s: %s", book_id, exc)
            cover_dest = None

    # Persist page boundaries too, so generations reuse the exact same split
    # without re-running detection (crucial for scanned books, where detection
    # is a vision pass).
    chapters = [
        {"num": int(c["chapter_num"]),
         "title": (c.get("title") or f"Chapter {c['chapter_num']}").strip(),
         "start_page": int(c.get("start_page", 0)),
         "end_page": int(c.get("end_page", 0)),
         # Textbook figure specs (Phase 3) detected above, where the PDF + page range
         # were available; generation crops them. Present only when figures were found.
         **({"figures": figure_specs[int(c["chapter_num"])]}
            if int(c.get("chapter_num", -1)) in figure_specs else {}),
         # The heal's low-confidence marker SURVIVES to storage. It used to be
         # dropped right here — heal_chapter_boundaries stamps
         # relocation="suspect" on a chapter whose pages don't match its title
         # and couldn't be repaired, and this trim was the only place the one
         # suspicion signal the pipeline produced went to die (Sara Junaidi's
         # book, 2026-08-23: every mismatched-but-unrelocatable chapter stored
         # clean). book_health reads it from structured chapters; the app can
         # now read it from books.chapters too.
         **({"suspect": True} if c.get("relocation") == "suspect" else {})}
        for c in structured.get("chapters", [])
    ]

    # Auto-detect grade + subject + a clean title/author from the (often filename-derived)
    # title and chapter list (best-effort; never block indexing). Identified for the teacher.
    grade = subject = None
    detected_title = detected_author = detected_language = None
    try:
        if client is None:
            raise RuntimeError("Claude unavailable")
        from shared.languages import LANGUAGES

        sample = "\n".join(c["title"] for c in chapters[:25])
        prompt = (
            "From this textbook's filename-derived title and chapter list, identify metadata. "
            "Respond ONLY as JSON with keys: "
            "\"grade\" (short canonical, e.g. \"Grade 5\"), "
            "\"subject\" (canonical, e.g. \"Mathematics\", \"Science\", \"History\"), "
            "\"title\" (a clean, human-readable book title: REMOVE download-site names, file "
            "extensions, slug dashes and any stray characters the scan glued on — e.g. "
            "\"pdfcoffee.com_cambridge-maths-5-learner-book-pdf-free\" becomes "
            "\"Cambridge Primary Mathematics Learner's Book 5\" if you recognise it, else a tidied "
            "version like \"Cambridge Maths 5 Learner Book\"), "
            "\"author\" (the book's author OR publisher only if clearly identifiable, e.g. "
            "\"Cambridge University Press\"; give the BARE name in nominative form — strip any "
            "possessive ending, and never put the book title in this field: if the cover reads "
            "\"X's <Title>\", return \"X\". Use null if you are not reasonably sure — do NOT "
            "invent a person's name), "
            "and \"language\" (the ISO 639-1 code of the language the book is WRITTEN in, e.g. "
            "\"en\", \"ms\", \"ar\", \"hi\"; null if you cannot tell). "
            "Best guess for grade/subject if unsure.\n\n"
            f"Filename-derived title: {book.get('title') or 'Unknown'}\n\nChapters:\n{sample}"
        )
        data = client.analyze(prompt, max_tokens=300).get("data", {}) or {}
        grade = (str(data.get("grade") or "").strip() or None)
        subject = (str(data.get("subject") or "").strip() or None)
        detected_title = (str(data.get("title") or "").strip() or None)
        detected_author = sanitise_author(data.get("author"), detected_title or "")
        # Costs nothing — the call is already made. It is the only route to a
        # language for a book whose text layer defeats the stopword heuristic.
        # ms-arab (Jawi) is excluded on purpose: books are written in Rumi and
        # Jawi is an OUTPUT the teacher selects, never a detection target.
        _lang = str(data.get("language") or "").strip().lower()
        detected_language = _lang if _lang in LANGUAGES and _lang != "ms-arab" else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("metadata detection failed for %s: %s", book_id, exc)

    # Replace the title only when the stored one shows a junk signature, and only
    # with a detected title that shares a word with it — never clobber a title
    # the teacher deliberately typed, and never let a hallucination rename a
    # book. The old gate asked only "does this look like a filename?", which the
    # app's own upload-time pre-cleaning made permanently false: it returned
    # False for 19 of 19 production books, so the detection this job pays for
    # was discarded every single time and "文字BUSINESS DYNAMICS" survived 17
    # generations. Fill author only when it is currently empty.
    current_title = (book.get("title") or "").strip()
    new_title = pick_book_title(current_title, detected_title)
    if new_title == current_title:
        new_title = None
    new_author = detected_author if (not (book.get("author") or "").strip() and detected_author) else None

    db.set_book_meta(sb, book_id, grade, subject, title=new_title, author=new_author)
    if cover_dest:
        db.set_book_cover(sb, book_id, cover_dest)
    if health is not None:
        db.set_book_health(sb, book_id, health)
    if client is not None:
        db.set_job_usage(sb, job["id"], client.session_usage)
    # Any chapter whose page range CHANGED versus the previously stored list has
    # stale cached OCR (source_text is keyed only by chapter_num, not pages) — clear
    # it so the new pages are transcribed fresh. Covers heal relocations AND plain
    # re-detection drift, not just relocated_nums.
    prev = {int(c.get("num", c.get("chapter_num", -1))): c for c in (book.get("chapters") or [])}
    changed = [
        c["num"] for c in chapters
        if (old := prev.get(c["num"])) is None
        or int(old.get("start_page", -1)) != c["start_page"]
        or int(old.get("end_page", -1)) != c["end_page"]
    ]

    # Part map: run the SAME chunker generation uses on the SAME structured
    # chapters, so "Part 1..N" is known (and exact) the moment indexing ends —
    # teachers generate any part on demand. Best-effort: a chapter without a
    # part map just renders the classic whole-chapter controls.
    try:
        from agent2_analysis.analyzer import MAX_PART_WORDS, build_chapter_parts

        # Words per page, for a chapter whose text could not be measured.
        #
        # A SCANNED PDF has no text layer, so Docling's structured output is
        # nearly empty and build_chapter_parts returns its documented fallback —
        # a single chunk of 0 words. The chapter then renders as ONE part with
        # ONE kit, while generation, working from the OCR'd text, finds enough
        # for three videos. That is exactly what happened to a teacher's 12-page
        # scanned chapter: 3 videos and 1 worksheet, where the same chapter in a
        # digital book would have split into 4 parts with 4 of each. The videos
        # covered the chapter; the documents covered a quarter of it.
        #
        # So when there is no word count to split on, estimate from PAGES.
        # Calibrated against a book that COULD be measured rather than guessed:
        # Cambridge Primary Science Y7 chapter 1 is 20 pages and really splits
        # into 4 parts (389/1660/1229/991 words). 250 w/page reproduces that 4,
        # and gives the 13-page scanned chapter 3 — which is independently what
        # generation found when it chunked that chapter into 3 videos.
        # 250 is the LOWEST value matching both, so the estimate errs toward
        # fewer parts: an over-estimate would charge a teacher for kits the
        # chapter does not contain.
        EST_WORDS_PER_PAGE = 250
        MAX_ESTIMATED_PARTS = 12  # a mis-detected chapter must not bill 40 kits

        # A page-count estimate is only as real as the page range under it, and
        # a gated book's ranges are exactly what health just called suspect. On
        # Sara Junaidi's book (2026-08-23) two junk bookmark "chapters" of 66
        # and 85 pages estimated 11 and 12 parts — 59 across the book, one
        # credit each behind "Generate all". The gate dialog now fronts every
        # insert surface, and each estimated part additionally carries
        # low_confidence so the app/console can tell a measured "Part 4 of 12"
        # from fiction. (The app ignores unknown keys — no app change needed.)
        low_confidence_map = bool(health and health.get("gate") == "confirm")

        def parts_from_pages(ch: dict) -> list[dict]:
            try:
                start = int(ch.get("start_page", 0))
                end = int(ch.get("end_page", start))
            except (TypeError, ValueError):
                return []
            pages = max(1, end - start + 1)
            n = -(-pages * EST_WORDS_PER_PAGE // MAX_PART_WORDS)  # ceil division
            n = max(1, min(MAX_ESTIMATED_PARTS, n))
            if n < 2:
                return []  # one part is what a missing map already renders
            per = pages * EST_WORDS_PER_PAGE // n
            # `estimated` is not read by the app — it marks these as inferred so
            # a later audit can tell them from measured ones.
            return [{"titles": [], "words": per, "estimated": True,
                     **({"low_confidence": True} if low_confidence_map else {})}
                    for _ in range(n)]

        s_by_num = {}
        for c in structured.get("chapters", []):
            try:
                if c.get("chapter_num") is not None:
                    s_by_num[int(c["chapter_num"])] = c
            except (TypeError, ValueError):
                continue
        for ch in chapters:
            # Per-chapter best-effort: one odd chapter must not cost the rest
            # of the book its part map.
            try:
                sc = s_by_num.get(int(ch["num"]))
                # Placeholder headings are dropped HERE, at the write, so they
                # never reach storage: the app renders `titles[0] || <ordinal>`,
                # so a stored "Content" (the structurer's no-headings-found
                # filler — 47% of every part in production) renders as the
                # part's NAME, while an empty list correctly degrades to
                # "Part 3 of 22".
                measured = (
                    [
                        {"titles": clean_part_titles(p.get("section_titles"), ch.get("title", "")),
                         "words": int(p.get("words", 0))}
                        for p in build_chapter_parts(sc)
                    ]
                    if sc
                    else []
                )
                # Trust any real measurement, however small. Only estimate when
                # the chapter yielded no words at all.
                if any(p["words"] for p in measured):
                    ch["parts"] = measured
                else:
                    # A SCANNED chapter has no text here, so this used to fall
                    # straight to the page estimate — which silently UNDID every
                    # map a generation had already measured from the real OCR,
                    # because indexing rewrites books.chapters wholesale. Worse,
                    # the error a teacher sees when the estimate over-offers
                    # ("part 8 does not exist. If the book's chapters changed,
                    # re-index it") advises the very action that reverts the fix.
                    # If this chapter has been transcribed, that transcription is
                    # still the truth: measure from it and never guess again.
                    cached = _measured_from_cached_ocr(sb, book_id, ch)
                    if cached:
                        ch["parts"] = cached
                        logger.info(
                            "part map MEASURED from cached OCR for %s ch%s: %d part(s) "
                            "(re-index kept the measurement)", book_id, ch.get("num"), len(cached),
                        )
                        continue
                    estimated = parts_from_pages(ch)
                    if estimated:
                        ch["parts"] = estimated
                        logger.info(
                            "part map ESTIMATED from pages for %s ch%s: %d parts (no text layer)",
                            book_id, ch.get("num"), len(estimated),
                        )
                    elif measured:
                        ch["parts"] = measured
            except Exception as exc:  # noqa: BLE001
                logger.warning("part map skipped for %s ch%s: %s", book_id, ch.get("num"), exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("part map skipped for %s: %s", book_id, exc)

    db.set_book_chapters(sb, book_id, chapters, "ready")
    # This fresh list is now authoritative, so drop any generation-time relocation
    # overrides (consulted before book.chapters) that a prior run persisted.
    db.clear_book_heal(sb, book_id)
    for num in changed:
        db.clear_chapter_source_text(sb, book_id, num)

    # Persist each chapter's RAW TEXT into chapter_grounding.source_text so the
    # AI assistant can answer questions from ANY chapter the moment indexing
    # finishes — no generated lesson required (the app falls back to source_text
    # when concepts/script are absent). MUST run AFTER the changed-chapters
    # clear loop above: on a first index EVERY chapter counts as "changed", and
    # persisting first would be a no-op (the clear would null what was just
    # written). Gated on a REAL text layer (same book-level signal healing
    # uses): a scanned book's junk embedded text (watermarks, CamScanner
    # headers) must never overwrite the paid vision-OCR cache the generation
    # path fills — those books keep the OCR flow untouched. Zero LLM cost;
    # best-effort, never blocks indexing.
    book_language: str | None = None
    try:
        from agent1_ingestion.vision_chapters import extraction_has_text

        if extraction_has_text(extraction):
            by_page: dict[int, list[str]] = {}
            for it in (extraction.items or []):
                t = (getattr(it, "text", "") or "").strip()
                if t and getattr(it, "item_type", "") != "picture":
                    by_page.setdefault(int(getattr(it, "page_num", 0)), []).append(t)
            persisted = 0
            lang_sample: list[str] = []
            for ch in chapters:
                if ch["end_page"] < ch["start_page"]:
                    continue
                text = "\n".join(
                    t for p in range(ch["start_page"], ch["end_page"] + 1) for t in by_page.get(p, [])
                ).strip()
                if len(text) >= 200:  # substantial text only — never clobber OCR with emptiness
                    # The text-layer twin of the OCR cap. 60,000 chars is ~43
                    # pages, so a long chapter was silently beheaded here too:
                    # two chapters of a live Cambridge Primary book (52pp and
                    # 50pp) sit at EXACTLY 60000, i.e. both lost their tail. The
                    # bound was for prompt size, but nothing prompts with this
                    # whole string — analyzer.py chunks it at 15,000 chars per
                    # part, so capping it only deleted the parts that would have
                    # taught the end of the chapter.
                    if len(text) > _SOURCE_TEXT_MAX_CHARS:
                        logger.warning(
                            "book %s ch%s: chapter text %d chars exceeds the %d-char store bound "
                            "— tail dropped; pages %d-%d may be partly untaught",
                            book_id, ch["num"], len(text), _SOURCE_TEXT_MAX_CHARS,
                            ch["start_page"] + 1, ch["end_page"] + 1,
                        )
                    db.set_chapter_source_text(sb, book_id, ch["num"], text[:_SOURCE_TEXT_MAX_CHARS])
                    persisted += 1
                    if len(lang_sample) < 3:
                        lang_sample.append(text[:8000])
            if persisted:
                logger.info("book %s: source_text persisted for %d/%d chapters", book_id, persisted, len(chapters))
            # Language detection ($0, deterministic): drives the generation
            # default, the picker preselection and the book's language chip.
            if lang_sample:
                from shared.languages import detect_language

                book_language = detect_language("\n".join(lang_sample))
    except Exception as exc:  # noqa: BLE001
        logger.warning("chapter source_text persistence skipped for %s: %s", book_id, exc)

    # The heuristic tokenises on whitespace, so a text layer with broken word
    # boundaries yields almost nothing to score and comes back None — and a
    # SCANNED book (or one whose text layer just failed the quality gate) never
    # reaches it at all. 11 of 19 production books sat at language=null,
    # including a MALAY one, and a null language silently narrates every lesson
    # in English with an English voice. The model read this same book in the
    # metadata call above, is not fooled by spacing, and cost nothing extra —
    # so it is the FALLBACK, never the override.
    try:
        if not book_language and detected_language:
            book_language = detected_language
            logger.info("book %s: language from model fallback", book_id)
        if book_language:
            db.set_book_language(sb, book_id, book_language)
            logger.info("book %s: language detected as %s", book_id, book_language)
    except Exception as exc:  # noqa: BLE001
        logger.warning("language detection skipped for %s: %s", book_id, exc)

    logger.info(
        "Indexed book %s: %d chapter(s), %d apparatus, grade=%s subject=%s cover=%s relocated=%s ocr_cleared=%s",
        book_id, len(chapters), len(structured.get("apparatus") or []),
        grade, subject, bool(cover_dest), relocated_nums, changed,
    )
