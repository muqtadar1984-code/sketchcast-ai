"""Process one generation: book PDF -> lesson (slides + video + deck) -> Supabase.

Reuses the existing agents end-to-end. Files are processed in a temp dir and the
final deck (.pptx) + video (.mp4) are uploaded to the `artifacts` bucket under
{owner_id}/{generation_id}/.
"""

from __future__ import annotations

import logging
import re
import tempfile
import uuid
from pathlib import Path

from supabase import Client

from . import client as db

# Book-title cleanup — uploaded PDFs often carry junk filenames
# (e.g. "pdfcoffee.com_cambridge-maths-5-learner-book-pdf-free"). Mirrors the app's
# src/utils/book.ts so worker and UI agree on what a "filename-like" title is.
_DOMAIN_HEAD = re.compile(r"^[a-z0-9-]+\.(?:com|net|org|pub|io|in|co|info|xyz)[._-]+", re.I)
_JUNK_TAIL = re.compile(r"[\s._-]*(pdf[\s._-]*free|free[\s._-]*pdf|ebook|pdf|free|download)\s*$", re.I)


def _looks_like_filename(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return True
    return (
        " " not in s
        or s.lower().endswith(".pdf")
        or bool(_DOMAIN_HEAD.search(s))
        or bool(re.search(r"[\s._-](pdf|free)", s, re.I))
    )


def _clean_title_fallback(s: str) -> str:
    t = re.sub(r"\.pdf$", "", s or "", flags=re.I)
    t = _DOMAIN_HEAD.sub("", t)
    t = _JUNK_TAIL.sub("", _JUNK_TAIL.sub("", t))
    t = re.sub(r"[_-]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t.title() if t else "Untitled book"

logger = logging.getLogger("worker")

DEFAULT_LEVEL = "middle_school"


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
    from worker.branding import load_branding

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

        client = ClaudeClient()

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
        if heal and heal.get("status") == "not_found":
            raise RuntimeError(_chapter_check_error(chapter_title, "a different topic"))
        healed_text = None
        if heal and heal.get("status") == "ok" and heal.get("start_page") is not None:
            chapter["start_page"] = int(heal["start_page"])
            chapter["end_page"] = int(heal["end_page"])
            healed_text = heal.get("source_text")

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
                    from agent1_ingestion.vision_chapters import chapter_text_vision

                    ocr_text = chapter_text_vision(
                        str(pdf_path), int(chapter.get("start_page", 0)),
                        int(chapter.get("end_page", 0)), client,
                    )
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
        # no single topic, so the topic-match check doesn't apply — skip it (it
        # would otherwise pay a Claude call per paper and could fail loud on the
        # synthetic label).
        ok, actual = (True, None) if is_cumulative else verify_chapter_content(chapter_title, _sample_text(), client)
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
                                               detect_and_crop_figures, textbook_figures_enabled)
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

            episodes_plan = (analysis.get("episodes") or {}).get("episodes") or []
            n_parts = max(len(episodes_plan), 1)
            voice_report: dict = {}
            uploaded_videos = 0
            script_dicts: list[dict] = []
            ep_title = chapter_title

            # Phase 3 (gated): detect + crop this chapter's real textbook figures
            # ONCE, then hand each part the ones that best match its segments. The
            # crops live under the job tmp — embedded into the deck + composited
            # into the video before cleanup. Best-effort: never breaks a lesson.
            chapter_figures: list[dict] = []
            used_figures: set[int] = set()
            if textbook_figures_enabled():
                try:
                    chapter_figures = detect_and_crop_figures(
                        pdf_path, chapter, client, Path(tmp) / "figures"
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("figure detection failed: %s", exc)

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
                    language=lesson_lang,
                )
                save_script(script)
                script_dict = script.model_dump()
                # Drop matched textbook figures onto this part's bullet segments.
                if chapter_figures:
                    attach_figures_to_segments(
                        script_dict.get("segments", []), chapter_figures, used_figures
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
                title = f"{book.get('title', 'Lesson')} · {chapter_title} · Part {part_ref} of {part_total}"
            elif n_parts > 1:
                db.merge_generation_params(sb, generation_id, {"video_parts": uploaded_videos})
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
            gen_client = ClaudeClient() if jawi else ClaudeClient(model=artifact_model(kind))
            out_path = generate_document(
                kind=kind, book=book, chapter=chapter, analysis=analysis,
                client=gen_client, params=gen.get("params") or {}, out_dir=Path(tmp),
                template=branding.get("docx_template"),
                language=lesson_lang,
            )
            for _k, _v in gen_client.session_usage.items():
                client.session_usage[_k] = client.session_usage.get(_k, 0) + _v
            db.set_progress(sb, job_id, 90)
            dest = f"{base}/{kind}.docx"
            db.upload_artifact(sb, str(out_path), dest)
            db.add_artifact_row(sb, generation_id, "docx", dest)

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
            title = f"{book.get('title', 'Document')} · {chapter_title} · {label}"
            if part_ref is not None:
                title += f" · Part {part_ref}"

        elif kind == "exam":
            # Cumulative exam (0062): one model call → TWO documents (the exam
            # paper and a SEPARATE answer key), grounded on the chosen covered
            # units (chapter = _combine_units above). The answer key is uploaded
            # under its OWN artifact kind so it never rides a student's download.
            from docgen import generate_document
            from shared.claude_client import artifact_model

            gen_client = ClaudeClient() if jawi else ClaudeClient(model=artifact_model(kind))
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

        # Book Health Score — a predictive read from signals we already have,
        # shown the moment indexing finishes so a bad scan is caught before it
        # generates failed lessons. Best-effort: never block indexing.
        health = None
        try:
            from agent1_ingestion.book_health import compute_book_health

            health = compute_book_health(extraction, structured.get("chapters", []))
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
         "end_page": int(c.get("end_page", 0))}
        for c in structured.get("chapters", [])
    ]

    # Auto-detect grade + subject + a clean title/author from the (often filename-derived)
    # title and chapter list (best-effort; never block indexing). Identified for the teacher.
    grade = subject = None
    detected_title = detected_author = None
    try:
        if client is None:
            raise RuntimeError("Claude unavailable")
        sample = "\n".join(c["title"] for c in chapters[:25])
        prompt = (
            "From this textbook's filename-derived title and chapter list, identify metadata. "
            "Respond ONLY as JSON with keys: "
            "\"grade\" (short canonical, e.g. \"Grade 5\"), "
            "\"subject\" (canonical, e.g. \"Mathematics\", \"Science\", \"History\"), "
            "\"title\" (a clean, human-readable book title: REMOVE download-site names, file "
            "extensions and slug dashes — e.g. "
            "\"pdfcoffee.com_cambridge-maths-5-learner-book-pdf-free\" becomes "
            "\"Cambridge Primary Mathematics Learner's Book 5\" if you recognise it, else a tidied "
            "version like \"Cambridge Maths 5 Learner Book\"), "
            "and \"author\" (the book's author OR publisher only if clearly identifiable, e.g. "
            "\"Cambridge University Press\"; use null if you are not reasonably sure — do NOT invent "
            "a person's name). Best guess for grade/subject if unsure.\n\n"
            f"Filename-derived title: {book.get('title') or 'Unknown'}\n\nChapters:\n{sample}"
        )
        data = client.analyze(prompt, max_tokens=300).get("data", {}) or {}
        grade = (str(data.get("grade") or "").strip() or None)
        subject = (str(data.get("subject") or "").strip() or None)
        detected_title = (str(data.get("title") or "").strip() or None)
        _a = data.get("author")
        _a_str = str(_a).strip() if _a is not None else ""
        detected_author = _a_str if _a_str and _a_str.lower() not in ("null", "none", "unknown", "n/a") else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("metadata detection failed for %s: %s", book_id, exc)

    # Only replace the title when the stored one looks like a filename — never clobber a title
    # the teacher deliberately typed. Fill author only when it is currently empty.
    current_title = (book.get("title") or "").strip()
    new_title = (detected_title or _clean_title_fallback(current_title)) if _looks_like_filename(current_title) else None
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
        from agent2_analysis.analyzer import build_chapter_parts

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
                if sc:
                    ch["parts"] = [
                        {"titles": (p.get("section_titles") or [])[:3], "words": int(p.get("words", 0))}
                        for p in build_chapter_parts(sc)
                    ]
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
                    db.set_chapter_source_text(sb, book_id, ch["num"], text[:60000])
                    persisted += 1
                    if len(lang_sample) < 3:
                        lang_sample.append(text[:8000])
            if persisted:
                logger.info("book %s: source_text persisted for %d/%d chapters", book_id, persisted, len(chapters))
            # Language detection ($0, deterministic): drives the generation
            # default, the picker preselection and the book's language chip.
            if lang_sample:
                from shared.languages import detect_language

                detected = detect_language("\n".join(lang_sample))
                if detected:
                    db.set_book_language(sb, book_id, detected)
                    logger.info("book %s: language detected as %s", book_id, detected)
    except Exception as exc:  # noqa: BLE001
        logger.warning("chapter source_text persistence skipped for %s: %s", book_id, exc)

    logger.info("Indexed book %s: %d chapter(s), grade=%s subject=%s cover=%s relocated=%s ocr_cleared=%s",
                book_id, len(chapters), grade, subject, bool(cover_dest), relocated_nums, changed)
