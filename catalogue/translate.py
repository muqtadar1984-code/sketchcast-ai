"""The ``kit_translate`` job: an approved ENGLISH catalogue kit becomes the
SAME kit in another lesson language (Kit Translator plan, revision 3,
2026-10-16 — phase T0: article, glossary, video and deck).

Decision D1: translate the finished artifacts, never regenerate. The source
kit's presentation uploaded a ``script_json`` beside every video part; that
script carries the narration, the dialogue, the on-screen text AND the
compiled scene plan (or, for a maths kit, the verified lesson record the
board is compiled from). This job rewrites the WORDS and leaves everything
else — segment ids, speaker turns, asset keys, geometry, the maths — exactly
as it was; the worker then renders the translated script through the
ordinary presentation build with ``script_override`` (worker/process.py),
which skips analysis and script generation and enters at the slides: new
voices, measured timing, the same pictures.

Job shape: ``{id, type: 'kit_translate', params: {source_kit_id, language,
topic_id?}}``, generation_id NULL — an observer job on the catalogue lane
(run.py). Stages, each resumable through the kit row's ``translation.stage``:

  load        the source kit (approved, English), its presentation and the
              script_json of every part, the approved article, the topic;
  kit         the translated ``topic_kits`` row — ``status generating``,
              ``translation_status staged``, ``source_kit_id`` = the source
              (R3 lineage) — created once, found again on a resume;
  glossary    the article's glossary terms, translated FIRST and pinned as a
              directive for every later call (R1); reused across topics by
              (language, subject, sense) from ``topic_term_translations``,
              a reviewer's row never overwritten;
  article     the article's text fields, one call, same shape back; written
              as a ``topic_articles`` row in the language, status ``staged``
              (R2 — promoted by ``approve_topic_kit`` with the kit), with the
              source's ``article_figures`` copied so the deck and the video
              find the same reviewed artwork;
  script      every part: the maths path translates the LESSON RECORD's
              spoken lines and recompiles the board in the language (the
              maths stays maths); the science path translates the segment
              text fields in place;
  worksheet   (T1.5) a maths worksheet's VERIFIED questions, read from the
              source generation's ``questions_json`` artifact: the words of
              each question translated, every number and expression kept
              (check_question), the surviving set staged as a file the
              render side turns into the .docx and its key without a model
              or the verifier (worker/process.py → render_translated_
              worksheet). A question the checks refuse twice is dropped
              with a flag; a sheet with none left is not made.
  check A     catalogue.translate_check — structure, protected expressions,
              numeric equivalence, English leftovers, glossary use. A
              blocking finding retries the part once, then fails the job;
  assets      every picture the scenes reference must already exist — in the
              worker's cache or the visual library — or the job FAILS naming
              the key (reviewer safeguard, revision 3): nothing is ever
              regenerated or substituted;
  generations the translated ``presentation`` and ``deck`` generations
              (owner = the catalogue account, ``params.catalogue``,
              ``params.translation`` naming the staged scripts), the kit
              pointed at them, provenance on ``topic_kits.translation``.

The kit lifecycle (catalogue.kit) then runs unchanged: the presentation
records its timestamps and inserts the lesson plan from the translated
article, the deck follows its video, and the kit moves to ``in_review``
with ``translation_status in_review``. Approval is the reviewer's, through
the RPC's translation branch (migration 0134).

A kit of a NON-ENGLISH source, a source that is not approved, a language
the registry does not know, or the source's own language are refused before
any model call (``TranslateRefused``).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable, Optional

from catalogue import translate_check as check
from catalogue.article import load_article, load_topic
from catalogue.key import canonical_key
from catalogue.kit import (KIT_FAILED, KIT_GENERATING, default_teacher_voice, load_kit, mark_kit_failed,
                           student_voice_for)
from shared.languages import LANGUAGES, get_language
from worker import client as db

log = logging.getLogger("worker.translate")

JOB_TYPE = "kit_translate"
PROMPT_VERSION = 1
SOURCE_LANGUAGE = "en"
TRANSLATION_STAGED = "staged"
TRANSLATION_FAILED = "failed"
ARTICLE_STAGED = "staged"
STAGE_ORDER = ("load", "kit", "glossary", "article", "script", "assets", "generations")
# One translation call carries at most this many strings: a part's segments
# fit, a whole article fits, and a reply that drifts in count is small
# enough to ask for again.
CHUNK = 120
MAX_TOKENS = 16000
TERM_SOURCE_MODEL = "model"
TERM_SOURCE_REVIEWER = "reviewer"

# Text fields of an article row that are WORDS. Everything else (ids,
# figure_keys, covers, codes, specs, asset ids, labels of baked pictures) is
# copied verbatim.
ARTICLE_TEXT_KEYS = frozenset({"title", "heading", "body_md", "text", "definition", "term", "misconception",
                               "correction", "problem", "solution_md", "claim", "statement", "caption",
                               "description", "summary", "why", "name", "depth_rationale"})
ARTICLE_STRING_LISTS = frozenset({"objectives", "misconceptions", "claims", "key_points"})
# A science segment's words.
SEGMENT_TEXT_KEYS = ("text", "slide_heading")
SCENE_TEXT_MAX = 80   # schema.TextElement clamps text at 80 characters

SYSTEM_PROMPT = (
    "You are a careful translator of school lessons. You translate the WORDS of a lesson into the target "
    "language for learners of that language, keeping every number, unit of measurement, variable name, "
    "formula, equation and curriculum code exactly as written. You never add, drop, merge or reorder items. "
    "You reply with JSON only."
)
ITEMS_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
    "required": ["items"],
}


class TranslateRefused(RuntimeError):
    """The job must not proceed; the message says why. ``report`` carries the
    pre-check that refused (when one did) so the kit row keeps the findings
    a reviewer needs to see why."""

    def __init__(self, message: str, report: Optional[dict] = None):
        super().__init__(message)
        self.report = report


def _s(value: object) -> str:
    return str(value or "").strip()


def _rows(res) -> list[dict]:
    return list(getattr(res, "data", None) or [])


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── strings in, strings out ─────────────────────────────────────────────


def _lang_name(language: str) -> str:
    lang = get_language(language)
    return f"{lang.name} ({lang.native})"


def term_directive(terms: list[tuple[str, str]]) -> str:
    if not terms:
        return ""
    lines = "\n".join(f"- {en} → {t}" for en, t in terms[:200])
    return ("Use EXACTLY these translations for these terms, every time they occur "
            "(the glossary this topic was approved with):\n" + lines + "\n")


def _ask_items(client, prompt: str, n: int, *, grace: int = 2) -> list[str]:
    """``{"items": [...]}`` of exactly ``n`` strings, or RuntimeError after
    the grace calls. A reply the client could not read as JSON arrives as
    ``{"raw_text": ...}`` — asked again, as the article job does."""
    last = None
    for attempt in range(grace + 1):
        reply = client.analyze(prompt, system=SYSTEM_PROMPT, max_tokens=MAX_TOKENS, response_schema=ITEMS_SCHEMA)
        data = reply.get("data") if isinstance(reply, dict) and "data" in reply else reply
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list) and len(items) == n and all(isinstance(x, str) for x in items):
            return items
        last = (len(items) if isinstance(items, list) else type(data).__name__)
        log.warning("translate: reply had %s item(s), %d wanted; asking again (%d/%d)", last, n, attempt + 1, grace)
    raise RuntimeError(f"translation reply did not return {n} items (got {last})")


def translate_strings(client, items: list[str], language: str, *, terms: Optional[list[tuple[str, str]]] = None,
                      context: str = "", protect: Optional[Iterable[str]] = None) -> list[str]:
    """Translate ``items`` into ``language`` — a list in, the same list out,
    position for position. Empty strings come back empty without a call."""
    out: list[Optional[str]] = [None] * len(items)
    # Each DISTINCT string is translated once and its translation reused at
    # every position: a term that appears twice in one item (a match item's
    # pairs sit in both ``options`` and ``answer``; a label repeats in a
    # marking point) must read the same twice, or grading by text breaks.
    first: dict[str, int] = {}
    todo: list[tuple[int, str]] = []
    for i, s in enumerate(items):
        if not _s(s):
            out[i] = s
        elif s not in first:
            first[s] = i
            todo.append((i, s))
    protected = [p for p in (protect or []) if _s(p)]
    for start in range(0, len(todo), CHUNK):
        batch = todo[start:start + CHUNK]
        payload = [s for _, s in batch]
        prompt = "\n".join(filter(None, [
            f"Translate each string below into {_lang_name(language)}.",
            context,
            term_directive(terms or []),
            ("These expressions must appear UNCHANGED in the translation: "
             + "; ".join(protected[:60])) if protected else "",
            "Rules: keep every number, unit, variable, formula, equation and curriculum code exactly as written; "
            "keep the register of a teacher speaking to a class; translate the whole string, never summarise; "
            "return exactly one translated string per input string, in the same order, as "
            '{"items": [...]} with ' + f"{len(payload)} items.",
            "INPUT:",
            json.dumps(payload, ensure_ascii=False),
        ]))
        got = _ask_items(client, prompt, len(payload))
        for (i, _), t in zip(batch, got):
            out[i] = t
    for i, s in enumerate(items):
        if out[i] is None and s in first:
            out[i] = out[first[s]]
    return [x if x is not None else "" for x in out]


# ── collecting and applying text fields ─────────────────────────────────


def _walk_strings(obj, want: Callable[[str, object], bool], path: str = "", parent_key: str = ""):
    """(path, string) for every string leaf ``want(key, value)`` accepts.
    ``path`` is a dotted/indexed locator ``apply_strings`` understands."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(v, want, f"{path}.{k}" if path else str(k), str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, want, f"{path}[{i}]", parent_key)
    elif isinstance(obj, str):
        if want(parent_key, obj):
            yield path, obj


_PATH_RE = re.compile(r"\.?([^.\[\]]+)|\[(\d+)\]")


def _set_path(obj, path: str, value) -> None:
    parts = [(m.group(1), m.group(2)) for m in _PATH_RE.finditer(path)]
    cur = obj
    for n, (key, idx) in enumerate(parts):
        last = n == len(parts) - 1
        if idx is not None:
            i = int(idx)
            if last:
                cur[i] = value
            else:
                cur = cur[i]
        else:
            if last:
                cur[key] = value
            else:
                cur = cur[key]


def apply_strings(obj, pairs: Iterable[tuple[str, str]]):
    """A deep copy of ``obj`` with each ``(path, text)`` written back."""
    out = copy.deepcopy(obj)
    for path, text in pairs:
        _set_path(out, path, text)
    return out


def article_text_items(article: dict) -> list[tuple[str, str]]:
    def want(key: str, value: str) -> bool:
        return key in ARTICLE_TEXT_KEYS or key in ARTICLE_STRING_LISTS

    return list(_walk_strings({k: article.get(k) for k in ("title", "objectives", "sections", "glossary",
                                                            "misconceptions", "worked_examples", "claims",
                                                            "depth_rationale") if k in article}, want))


def lesson_text_items(lesson: dict) -> list[tuple[str, str]]:
    """The SPOKEN and WRITTEN words of a maths Lesson record — never its
    maths (translate_check.LESSON_INVARIANT_KEYS)."""
    # the spoken fields, the method card's steps, and the QUESTION itself: a
    # word problem is prose with maths inside, translated as words and
    # checked span by span (translate_check.prose_maths_equivalent)
    text_keys = set(check.LESSON_TEXT_KEYS) | set(check.LESSON_PROSE_MATHS_KEYS) | {"steps"}

    def want(key: str, value: str) -> bool:
        if key in check.LESSON_INVARIANT_KEYS:
            return False
        if key in check.LESSON_PROSE_MATHS_KEYS:
            # a bare equation ("4x + 3 = 19") is typeset, not translated
            return check.has_prose(value)
        return key in text_keys

    items = []
    for path, text in _walk_strings(lesson, want):
        # method.steps are words ("Collect like terms"); an example's steps
        # are objects, whose string leaves were already filtered by key.
        if ".steps[" in path and not path.startswith("method."):
            if not re.search(r"\.(speech|student|explanation|why_wrong)$", path):
                continue
        items.append((path, text))
    return items


def segment_text_items(segment: dict) -> list[tuple[str, str]]:
    """A science segment's words: narration, dialogue lines, heading,
    bullets, the slide visual's words, the scene's text elements."""
    items: list[tuple[str, str]] = []
    for k in SEGMENT_TEXT_KEYS:
        if _s(segment.get(k)):
            items.append((k, str(segment[k])))
    for i, p in enumerate(segment.get("slide_points") or []):
        if _s(p):
            items.append((f"slide_points[{i}]", str(p)))
    for i, d in enumerate(segment.get("dialogue") or []):
        if isinstance(d, dict) and _s(d.get("line")):
            items.append((f"dialogue[{i}].line", str(d["line"])))
    sv = segment.get("slide_visual")
    if isinstance(sv, dict):
        for k in ("heading", "caption", "title"):
            if _s(sv.get(k)):
                items.append((f"slide_visual.{k}", str(sv[k])))
        for i, it in enumerate(sv.get("items") or []):
            if isinstance(it, str) and _s(it):
                items.append((f"slide_visual.items[{i}]", it))
            elif isinstance(it, dict):
                for k in ("text", "label", "caption"):
                    if _s(it.get(k)):
                        items.append((f"slide_visual.items[{i}].{k}", str(it[k])))
    scene = segment.get("scene")
    if isinstance(scene, dict):
        for i, e in enumerate(scene.get("elements") or []):
            if isinstance(e, dict) and e.get("type") == "text" and _s(e.get("text")):
                items.append((f"scene.elements[{i}].text", str(e["text"])))
    return items


def _protected_of_segment(segment: dict) -> list[str]:
    scene = segment.get("scene") if isinstance(segment.get("scene"), dict) else {}
    return [str(e.get("expr")) for e in (scene.get("elements") or [])
            if isinstance(e, dict) and e.get("type") == "math" and _s(e.get("expr"))]


def _finish_segment(seg: dict) -> dict:
    """After the words are written back: the narration IS the dialogue's
    concatenation (what the two voices read), the ElevenLabs copy follows
    the text, scene text keeps the schema's length."""
    dialogue = [d for d in (seg.get("dialogue") or []) if isinstance(d, dict)]
    if dialogue:
        seg["text"] = " ".join(_s(d.get("line")) for d in dialogue if _s(d.get("line")))
    seg["elevenlabs_text"] = seg.get("text") or ""
    scene = seg.get("scene")
    if isinstance(scene, dict):
        for e in scene.get("elements") or []:
            if isinstance(e, dict) and e.get("type") == "text" and isinstance(e.get("text"), str):
                e["text"] = e["text"].strip()[:SCENE_TEXT_MAX]
    seg["estimated_duration_seconds"] = max(int(seg.get("estimated_duration_seconds") or 0),
                                            _spoken_seconds(seg.get("text") or ""))
    return seg


def _spoken_seconds(text: str) -> int:
    # 130 words a minute, as catalogue.kit._spoken_seconds; scripts in
    # non-Latin scripts count characters-per-word about the same
    return max(2, round(len(str(text).split()) / 130 * 60))


# ── the science path ──────────────────────────────────────────────────────


def translate_segments(client, segments: list[dict], language: str, terms, *, context: str) -> list[dict]:
    items: list[tuple[int, str, str]] = []
    protected: list[str] = []
    for i, seg in enumerate(segments):
        protected += _protected_of_segment(seg)
        for path, text in segment_text_items(seg):
            items.append((i, path, text))
    got = translate_strings(client, [t for _, _, t in items], language, terms=terms, context=context,
                            protect=protected)
    out = copy.deepcopy(segments)
    by_seg: dict[int, list[tuple[str, str]]] = {}
    for (i, path, _), t in zip(items, got):
        by_seg.setdefault(i, []).append((path, t))
    for i, pairs in by_seg.items():
        out[i] = _finish_segment(apply_strings(out[i], pairs))
    return out


# ── the maths path ────────────────────────────────────────────────────────


def _lesson_protected(lesson: dict) -> list[str]:
    """The expressions a maths lesson's spoken lines quote — the answers and
    the working — plus the maths SPANS of every question ("π = 3.14",
    "2x + 3 = 11"). Named to the translator so they are left alone while
    the words around them change."""
    out: list[str] = []
    for path, value in check._walk(lesson):
        if not (isinstance(value, str) and _s(value)):
            continue
        key = check._leaf_key(path)
        if key in ("final_answer", "after"):
            if value not in out:
                out.append(value)
        elif key in check.LESSON_PROSE_MATHS_KEYS:
            for span in check.maths_spans(value):
                if span not in out:
                    out.append(span)
    return out[:60]


_QUESTION_RULE = ("A string that is a QUESTION or word problem is translated as words: every number, unit "
                  "symbol, variable letter, equation and expression in it stays exactly as written "
                  "(e.g. 'π = 3.14', '2x + 3 = 11', '6 cm' keeps the 6; the unit word may be translated).")


def translate_lesson(client, lesson: dict, language: str, terms, *, context: str) -> dict:
    items = lesson_text_items(lesson)
    got = translate_strings(client, [t for _, t in items], language, terms=terms,
                            context=(context + " " + _QUESTION_RULE).strip(), protect=_lesson_protected(lesson))
    return apply_strings(lesson, [(p, t) for (p, _), t in zip(items, got)])


def lesson_from_record(lesson: dict):
    """A ``maths.schema.Lesson`` from a STORED record with every example it
    carries. The schema's ``_four`` validator keeps the first four examples
    of a model reply — but the English kit's record was EXTENDED after
    validation (maths.lesson.extend_to_floor appends verified examples to
    reach the length floor), so a rendered lesson may hold eight. Validating
    the dump whole silently drops examples 5..n and the recompiled board
    teaches half the lesson (the Circle pilot, 2026-10-16: 8 examples in the
    record, 4 on the Arabic board, 15 numbers missing). So the record is
    validated with its examples detached, and each example is validated and
    attached on its own."""
    from maths.schema import Lesson, WorkedExample

    body = dict(lesson or {})
    examples = [e for e in (body.pop("examples", None) or []) if isinstance(e, (dict, WorkedExample))]
    built = Lesson.model_validate(body)
    built.examples = [e if isinstance(e, WorkedExample) else WorkedExample.model_validate(e) for e in examples]
    return built


def recompile_maths(script: dict, lesson: dict, language: str, title: str) -> dict:
    """The translated Lesson record back onto the board in the language —
    maths.lesson.to_episode_script, the SAME compiler the English kit used,
    so the board's own words (METHOD, Try it, the reasons) come from
    maths.i18n and the expressions are typeset from the record."""
    from maths.lesson import to_episode_script

    maths = script.get("maths") or {}
    ep = to_episode_script(lesson_from_record(lesson), dict(maths.get("verification") or {}),
                           book_id=str(script.get("book_id") or ""), chapter_num=int(script.get("chapter_num") or -1),
                           episode_num=int(script.get("episode_num") or 1), title=title,
                           avatars=script.get("avatars"), language=language)
    return ep.model_dump()


def _splice_framing(source_segments: list[dict], compiled: list[dict], client, language: str, terms, context: str) -> list[dict]:
    """A multi-part kit's RECAP / OUTRO segments were added by the worker
    around the compiled lesson (catalogue.kit): they are not in the record,
    so they are translated as text and put back where they were."""
    ids = {str(s.get("segment_id") or "") for s in compiled}
    extra = [s for s in source_segments if str(s.get("segment_id") or "") not in ids]
    if not extra:
        return compiled
    translated = translate_segments(client, extra, language, terms, context=context)
    first_id = str((source_segments[0] if source_segments else {}).get("segment_id") or "")
    out = list(compiled)
    for seg in translated:
        if str(seg.get("segment_id") or "") == first_id:
            out.insert(0, seg)       # the recap opened the part
        else:
            out.append(seg)          # the outro closed it
    return out


# ── one part ─────────────────────────────────────────────────────────────


def translate_episode(client, body: dict, language: str, terms, *, title: Optional[str] = None,
                      context: str = "") -> tuple[dict, dict]:
    """``body`` is a script_json artifact; returns (translated script dict,
    pre-check A report). Raises TranslateRefused when the check blocks
    after one retry."""
    src = body.get("script") if isinstance(body.get("script"), dict) else {}
    if not src.get("segments"):
        raise TranslateRefused("the source script has no segments")
    maths = src.get("maths") if isinstance(src.get("maths"), dict) else {}
    lesson = maths.get("lesson") if isinstance(maths.get("lesson"), dict) else None
    ep_title = title or translate_strings(client, [str(src.get("episode_title") or "")], language,
                                          terms=terms, context=context)[0]
    report: dict = {}
    dst: dict = {}
    for attempt in range(2):
        if lesson is not None:
            new_lesson = translate_lesson(client, lesson, language, terms, context=context)
            r_lesson = check.check_maths_lesson(lesson, new_lesson)
            dst = recompile_maths(src, new_lesson, language, ep_title)
            dst["segments"] = _splice_framing(src.get("segments") or [], dst.get("segments") or [], client,
                                              language, terms, context)
            r_script = check.check_script(src, dst, language=language, glossary=terms, strict=False,
                                          board=check.board_numbers(lesson))
            report = check.merge_reports(r_lesson, r_script)
        else:
            dst = copy.deepcopy(src)
            dst["segments"] = translate_segments(client, src.get("segments") or [], language, terms, context=context)
            dst["episode_title"] = ep_title
            report = check.merge_reports(check.check_script(src, dst, language=language, glossary=terms))
        if report.get("ok"):
            break
        log.warning("translate: pre-check A blocked part %s (%d finding(s)); %s", body.get("part"),
                    len(report.get("blocking") or []), "asking again" if attempt == 0 else "giving up")
    if not report.get("ok"):
        heads = "; ".join(f"{f.get('where')}: {f.get('what')}" for f in (report.get("blocking") or [])[:5])
        raise TranslateRefused(f"translation of part {body.get('part')} failed pre-check A: {heads}",
                               report={"part": body.get("part"), **report})
    # The script's own label of its language, read by the renderer's RTL and
    # font choices, and by the deck.
    if isinstance(dst.get("maths"), dict):
        dst["maths"]["language"] = language
    return dst, report


# ── loading the source ───────────────────────────────────────────────────


def _part_of_path(path: str) -> int:
    m = re.search(r"script(?:_part(\d+))?\.json$", str(path or ""))
    return int(m.group(1)) if m and m.group(1) else 1


def load_source(sb, source_kit_id: str) -> dict:
    kit = load_kit(sb, source_kit_id)
    if kit is None:
        raise TranslateRefused(f"source kit {source_kit_id} not found")
    if _s(kit.get("status")) != "approved":
        raise TranslateRefused(f"source kit {source_kit_id} is {kit.get('status') or 'unknown'}, not approved — "
                               "only an approved kit is translated")
    if _s(kit.get("language")).lower() != SOURCE_LANGUAGE:
        raise TranslateRefused(f"source kit {source_kit_id} is in {kit.get('language')}; translations start from English")
    if kit.get("translation_status"):
        raise TranslateRefused(f"source kit {source_kit_id} is itself a translation")
    pres_id = _s(kit.get("presentation_generation_id"))
    if not pres_id:
        raise TranslateRefused(f"source kit {source_kit_id} has no presentation")
    gen_rows = _rows(sb.table("generations").select("id,owner_id,status,params,kind").eq("id", pres_id).limit(1).execute())
    if not gen_rows or _s(gen_rows[0].get("status")) != "done":
        raise TranslateRefused(f"source presentation {pres_id} is not done")
    arts = _rows(sb.table("artifacts").select("kind,storage_path").eq("generation_id", pres_id).eq("kind", "script_json").execute())
    scripts = sorted((_s(a.get("storage_path")) for a in arts if _s(a.get("storage_path"))), key=_part_of_path)
    if not scripts:
        raise TranslateRefused(f"source presentation {pres_id} uploaded no script_json — a kit rendered before "
                               "decision 4 (Phase 3) cannot be translated; regenerate it first")
    article = load_article(sb, _s(kit.get("article_id")))
    if article is None or _s(article.get("status")) != "approved":
        raise TranslateRefused(f"source article {kit.get('article_id')} is not the approved version")
    topic = load_topic(sb, _s(kit.get("topic_id")))
    if topic is None:
        raise TranslateRefused(f"topic {kit.get('topic_id')} not found")
    return {"kit": kit, "presentation": gen_rows[0], "script_paths": scripts, "article": article, "topic": topic}


def download_scripts(sb, paths: list[str], tmp: Path) -> list[dict]:
    bodies = []
    for p in paths:
        dest = db.download_artifact(sb, p, Path(tmp) / Path(p).name)
        body = json.loads(Path(dest).read_text(encoding="utf-8"))
        body["_path"] = p
        body["_sha256"] = _sha(Path(dest).read_bytes())
        body.setdefault("part", _part_of_path(p))
        bodies.append(body)
    bodies.sort(key=lambda b: int(b.get("part") or 1))
    return bodies


# ── the kit row ─────────────────────────────────────────────────────────


def find_translation_kit(sb, source_kit_id: str, language: str) -> Optional[dict]:
    rows = _rows(sb.table("topic_kits").select("*").eq("source_kit_id", source_kit_id).eq("language", language)
                 .eq("status", KIT_GENERATING).execute())
    rows = [r for r in rows if r.get("translation_status") == TRANSLATION_STAGED]
    return rows[0] if rows else None


def create_translation_kit(sb, source: dict, language: str, job_id: str) -> dict:
    kit = source["kit"]
    avatar = _s(kit.get("teacher_avatar")) or "female"
    teacher = default_teacher_voice(language, avatar)
    student, fallback = student_voice_for(teacher, language, None)
    row = {
        "topic_id": kit["topic_id"], "article_id": kit["article_id"], "language": language,
        "source_kit_id": kit["id"], "teacher_avatar": avatar,
        "voice_pair": {"teacher": teacher, "student": student},
        "status": KIT_GENERATING, "translation_status": TRANSLATION_STAGED,
        "translation": {"job_id": job_id, "source_kit_id": kit["id"], "source_language": SOURCE_LANGUAGE,
                        "source_article_id": kit["article_id"],
                        "source_generation_id": source["presentation"]["id"],
                        "student_voice_fallback": bool(fallback), "prompt_version": PROMPT_VERSION,
                        "stage": {}},
    }
    ins = sb.table("topic_kits").insert(row).execute()
    rows = _rows(ins)
    if not rows or not rows[0].get("id"):
        raise RuntimeError("topic_kits insert returned no id")
    return rows[0]


def _save_translation(sb, kit: dict, patch: dict) -> dict:
    tr = dict(kit.get("translation") or {})
    for k, v in patch.items():
        if k == "stage":
            tr["stage"] = {**dict(tr.get("stage") or {}), **v}
        else:
            tr[k] = v
    sb.table("topic_kits").update({"translation": tr}).eq("id", kit["id"]).execute()
    kit["translation"] = tr
    return kit


def _fail_translation(sb, kit: Optional[dict], note: str, report: Optional[dict] = None) -> None:
    """A translation that failed BEFORE it had any piece is not kept
    (founder, 2026-10-16: "remove all the failed translation kits from the
    portal; we don't need to store them"): its staged items, its staged
    article and the kit row are deleted; the job keeps the error, and the
    Translations panel shows that job's line. A kit that already holds
    generations (a failure after the pieces were queued) is marked failed
    instead, so the console's Retry still has something to retry."""
    if not kit:
        return
    kit_id = str(kit.get("id") or "")
    if not kit_id:
        return
    try:
        rows = _rows(sb.table("topic_kits").select("id,article_id,language,presentation_generation_id,doc_generation_ids")
                     .eq("id", kit_id).limit(1).execute())
        row = (rows or [None])[0]
        if row is None:
            return
        docs = row.get("doc_generation_ids") if isinstance(row.get("doc_generation_ids"), dict) else {}
        has_pieces = bool(row.get("presentation_generation_id")) or any(bool(v) for v in docs.values())
        if isinstance(report, dict):
            log.warning("translation kit %s refused: %s", kit_id, json.dumps(report, ensure_ascii=False)[:4000])
        if has_pieces:
            mark_kit_failed(sb, kit_id, note)
            sb.table("topic_questions").update({"status": "retired"}).eq("translation_kit_id", kit_id).eq("status", "staged").execute()
            patch: dict = {"translation_status": TRANSLATION_FAILED}
            if isinstance(report, dict):
                patch["precheck"] = report
            sb.table("topic_kits").update(patch).eq("id", kit_id).execute()
            return
        # Order matters: the kit row references its article
        # (topic_kits.article_id → topic_articles, no cascade), so the kit
        # goes before the article — the first science run (2026-10-17, kit
        # c84faaee) hit 23503 the other way round and the row survived.
        sb.table("topic_questions").delete().eq("translation_kit_id", kit_id).execute()
        sb.table("topic_kits").delete().eq("id", kit_id).execute()
        article_id = _s(row.get("article_id"))
        if article_id and _s(row.get("language")) != SOURCE_LANGUAGE:
            sb.table("topic_articles").delete().eq("id", article_id).eq("status", ARTICLE_STAGED).execute()
        log.info("translation kit %s removed after a failure: %s", kit_id, note[:200])
    except Exception:  # noqa: BLE001
        log.exception("translation kit %s could not be cleared after its failure", kit_id)


# ── glossary (R1) ──────────────────────────────────────────────────────


def _glossary_terms(article: dict) -> list[str]:
    out: list[str] = []
    for g in article.get("glossary") or []:
        term = _s(g.get("term")) if isinstance(g, dict) else _s(g)
        if term and term not in out:
            out.append(term)
    return out


def glossary_stage(sb, client, topic: dict, article: dict, language: str) -> tuple[list[tuple[str, str]], dict]:
    """Term pairs ``[(term_en, term)]`` for the directive, reusing rows for
    the same (language, subject, sense) and writing the new ones."""
    terms = _glossary_terms(article)
    subject = _s(topic.get("subject")).lower() or "general"
    senses = {t: canonical_key(t) for t in terms}
    existing: dict[str, dict] = {}
    if senses:
        rows = _rows(sb.table("topic_term_translations").select("*").eq("language", language).eq("subject", subject)
                     .in_("sense", sorted(set(senses.values()))).execute())
        for r in rows:
            cur = existing.get(r["sense"])
            # a reviewer's row always wins over a model's
            if cur is None or (r.get("source") == TERM_SOURCE_REVIEWER and cur.get("source") != TERM_SOURCE_REVIEWER):
                existing[r["sense"]] = r
    todo = [t for t in terms if senses[t] not in existing]
    got = translate_strings(client, todo, language,
                            context=f"These are glossary TERMS (one to four words each) of a {subject} topic "
                                    f"titled '{_s(topic.get('title'))}'. Give the standard school term.") if todo else []
    written = 0
    for t, tr in zip(todo, got):
        if not _s(tr):
            continue
        row = {"language": language, "subject": subject, "sense": senses[t], "term_en": t, "term": _s(tr),
               "source": TERM_SOURCE_MODEL, "topic_id": topic.get("id")}
        try:
            sb.table("topic_term_translations").insert(row).execute()
            written += 1
            existing[senses[t]] = row
        except Exception as exc:  # noqa: BLE001 — a sibling job wrote it first
            if "23505" not in str(exc) and str(getattr(exc, "code", "")) != "23505":
                raise
            again = _rows(sb.table("topic_term_translations").select("*").eq("language", language)
                          .eq("subject", subject).eq("sense", senses[t]).execute())
            if again:
                existing[senses[t]] = again[0]
    pairs = [(t, _s(existing[senses[t]].get("term"))) for t in terms if senses[t] in existing and _s(existing[senses[t]].get("term"))]
    return pairs, {"terms": len(terms), "reused": len(terms) - len(todo), "written": written}


# ── article (R2) ────────────────────────────────────────────────────────


def _article_structure(article: dict) -> dict:
    return {
        "sections": [(_s(s.get("id")), list(s.get("figure_keys") or []), list(s.get("covers") or []))
                     for s in (article.get("sections") or []) if isinstance(s, dict)],
        "glossary": len(article.get("glossary") or []),
        "misconceptions": len(article.get("misconceptions") or []),
        "worked_examples": len(article.get("worked_examples") or []),
        "claims": len(article.get("claims") or []),
        "objectives": len(article.get("objectives") or []),
    }


def _word_count(sections: list) -> int:
    n = 0
    for s in sections or []:
        if isinstance(s, dict):
            n += len(str(s.get("body_md") or "").split())
    return n


def article_stage(sb, client, source: dict, language: str, terms, kit: dict) -> tuple[str, dict]:
    article = source["article"]
    topic = source["topic"]
    items = article_text_items(article)
    got = translate_strings(client, [t for _, t in items], language, terms=terms,
                            context=f"These are the fields of a knowledge article on '{_s(article.get('title'))}' "
                                    f"({_s(topic.get('subject'))}) for school students. Section bodies are "
                                    "markdown-free prose; keep paragraph breaks.")
    translated = apply_strings({k: article.get(k) for k in ("title", "objectives", "sections", "glossary",
                                                            "misconceptions", "worked_examples", "claims",
                                                            "depth_rationale") if k in article},
                               [(p, t) for (p, _), t in zip(items, got)])
    if _article_structure(article) != _article_structure(translated):
        raise TranslateRefused("the translated article's structure differs from the source")
    versions = _rows(sb.table("topic_articles").select("version").eq("topic_id", article["topic_id"]).eq("language", language).execute())
    version = max([int(v.get("version") or 0) for v in versions] + [0]) + 1
    row = {
        "topic_id": article["topic_id"], "version": version, "language": language,
        "source_article_id": article["id"], "title": translated.get("title") or article.get("title"),
        "objectives": translated.get("objectives") or [], "sections": translated.get("sections") or [],
        "glossary": translated.get("glossary") or [], "misconceptions": translated.get("misconceptions") or [],
        "worked_examples": translated.get("worked_examples") or [], "claims": translated.get("claims") or [],
        "depth_node_id": article.get("depth_node_id"), "depth_rationale": translated.get("depth_rationale") or None,
        "word_count": _word_count(translated.get("sections") or []), "status": ARTICLE_STAGED,
        "author": "model", "notes": f"translated from v{article.get('version')} ({SOURCE_LANGUAGE}) by kit_translate",
    }
    ins = sb.table("topic_articles").insert(row).execute()
    rows = _rows(ins)
    if not rows or not rows[0].get("id"):
        raise RuntimeError("topic_articles insert returned no id")
    new_id = str(rows[0]["id"])
    # The reviewed artwork travels with the article: same specs, same
    # visual assets, the captions in the language.
    figs = _rows(sb.table("article_figures").select("*").eq("article_id", article["id"]).order("sort").execute())
    captions = translate_strings(client, [_s(f.get("caption")) for f in figs], language, terms=terms,
                                 context="Figure captions of the same article.") if figs else []
    for f, cap in zip(figs, captions):
        sb.table("article_figures").insert({
            "article_id": new_id, "figure_key": f.get("figure_key"), "caption": cap or f.get("caption"),
            "spec": f.get("spec") or {}, "visual_asset_id": f.get("visual_asset_id"),
            "labels": f.get("labels") or [], "sort": f.get("sort") or 0, "status": f.get("status") or "draft",
        }).execute()
    sb.table("topic_kits").update({"article_id": new_id}).eq("id", kit["id"]).execute()
    kit["article_id"] = new_id
    return new_id, {"article_id": new_id, "version": version, "figures": len(figs), "title": row["title"]}


# ── assets (revision 3 safeguard) ───────────────────────────────────────


def scene_asset_keys(scripts: list[dict]) -> list[str]:
    keys: list[str] = []
    for script in scripts:
        for k in (script.get("avatars") or {}).values():
            if _s(k) and k not in keys:
                keys.append(str(k))
        for seg in script.get("segments") or []:
            scene = seg.get("scene") if isinstance(seg, dict) and isinstance(seg.get("scene"), dict) else {}
            for e in scene.get("elements") or []:
                if isinstance(e, dict) and e.get("type") == "illustration" and _s(e.get("asset")):
                    k = str(e["asset"])
                    if k not in keys:
                        keys.append(k)
    return keys


def resolve_assets(sb, keys: list[str]) -> tuple[list[dict], list[str]]:
    """Where each picture already IS: the worker's cache (hashed), an
    approved library row (by storage path), or — for a board picture under
    the board-colour pictures switch — the colour SHELF's row for its exact
    colour key (shared.visual_library.find_colour; ``colour: True``, the
    cache key the render will ask for). Missing keys are returned, not
    invented: a board picture the shelf does not hold is a redraw, and a
    flag, as before (translate_kit)."""
    from spike.scene_engine.raster_assets import cache_dir_for, colour_key

    found: list[dict] = []
    missing: list[str] = []
    for key in keys:
        hit = None
        for k in dict.fromkeys([key, colour_key(key)]):
            png = cache_dir_for(k) / "asset.png"
            if png.exists():
                hit = {"key": key, "source": "cache", "cache_key": k, "sha256": _sha(png.read_bytes())}
                break
        if hit is None:
            ck = colour_key(key)
            if ck != key:
                # a board picture under the pictures switch: the render asks
                # for the COLOUR key and the ink library is never consulted
                # for it (raster_assets.generation_forbidden) — only the
                # colour shelf, by exact key, can answer; an ink row here
                # would promise a picture the render cannot use
                hit = _colour_shelf_row(sb, key, ck)
            else:
                hit = _library_row(sb, key)
        if hit is None:
            missing.append(key)
        else:
            found.append(hit)
    return found, missing


def _colour_shelf_row(sb, key: str, ck: str) -> Optional[dict]:
    """The shelf row for a board key's colour key — exact asset_key only,
    through the library's own lookup (so the translate stage and the render
    answer from the same row), with the client this job holds. Off when the
    shelf is off (VISUAL_LIBRARY_COLOUR_SHELF=0): then every board picture
    is a redraw, as before the shelf. A lookup that fails is a MISSING
    picture, never a crashed stage."""
    try:
        from shared.visual_library import find_colour
        row = find_colour(ck, sb=sb)
    except Exception as exc:  # noqa: BLE001
        log.warning("translate: colour shelf lookup failed for %s: %s", ck, exc)
        return None
    if not row:
        return None
    return {"key": key, "source": "library", "colour": True, "cache_key": ck,
            "id": row.get("id"), "storage_path": row.get("storage_path")}


def board_pictures_in_colour() -> bool:
    """Whether the renderer draws board pictures in colour (FEATURE_BOARD_
    COLOUR_PICTURES) — then a translated render redraws every board picture
    the cache does not hold, and the translate stage flags each one."""
    try:
        from spike.scene_engine import colour as _colour
        return bool(_colour.pictures_enabled())
    except Exception:  # noqa: BLE001
        return False


def _library_row(sb, key: str) -> Optional[dict]:
    """The approved library row a scene key resolves to, the way the renderer
    resolves it (shared.visual_library): a plain key by its asset_key or
    canonical key; an avatar FACE key (``avatar_teacher_female__face_c9f95497``,
    the roster key plus the first eight hex characters of the row id) by the
    roster key's canonical key and then the id PREFIX — the suffix is not a
    uuid and must never be compared with the id column (the third Circle
    run, 2026-10-16: ``invalid input syntax for type uuid``). A lookup that
    fails for any reason is a MISSING picture, reported as such, never a
    crashed stage."""
    try:
        # The LIBRARY's normaliser (shared.asset_keys), not the catalogue's
        # topic-key one (catalogue.key): "avatar_teacher_female" is stored as
        # "avatar_female_teacher", "plant_cell" as "cell_plant". The fourth
        # Circle run (2026-10-16) found neither face with the wrong one.
        from shared.asset_keys import canonical_key as asset_key_of
        from shared.visual_library import base_avatar_key, face_id_of

        cols = "id,asset_key,canonical_key,storage_path,status"
        if key.startswith("avatar"):
            fid = (face_id_of(key) or "").lower()
            base = base_avatar_key(key)
            rows = _rows(sb.table("visual_assets").select(cols).eq("status", "approved")
                         .eq("canonical_key", asset_key_of(base)).execute())
            rows = [r for r in rows if _s(r.get("storage_path"))]
            if fid:
                exact = [r for r in rows if str(r.get("id") or "").lower().replace("-", "").startswith(fid)]
                if exact:
                    rows = exact
                elif rows:
                    log.warning("translate: face %s of %s is gone from the library; the oldest face stands in", fid, base)
            if rows:
                r = rows[0]
                return {"key": key, "source": "library", "id": r.get("id"), "storage_path": r.get("storage_path")}
            return None
        for col, val in (("asset_key", key), ("canonical_key", asset_key_of(key))):
            if not val:
                continue
            rows = _rows(sb.table("visual_assets").select(cols).eq(col, val).limit(5).execute())
            rows = [r for r in rows if _s(r.get("status")) in ("approved", "") and _s(r.get("storage_path"))]
            if rows:
                r = rows[0]
                return {"key": key, "source": "library", "id": r.get("id"), "storage_path": r.get("storage_path")}
        return None
    except Exception as exc:  # noqa: BLE001 — a lookup we cannot make is a picture we cannot find
        log.warning("translate: library lookup failed for %s: %s", key, exc)
        return None


# ── the question bank (D4) ───────────────────────────────────────────────

ITEM_TEXT_KEYS = frozenset({"stem", "text", "caption", "label", "left", "right", "point", "why_wrong", "explanation"})
ITEM_FIELDS = ("stem", "options", "answer", "marking_scheme", "distractor_rationale", "explanation")
ITEM_COPY_KEYS = ("topic_id", "objective_ref", "claim_ref", "item_type", "answer_mode", "difficulty",
                  "cognitive_level", "marks", "est_seconds", "tags")
BANK_FLOOR = 10          # under this many staged items the bank is not worth a kit piece
ITEM_RULE = ("Each string is a field of a school test question: translate the words; keep every number, unit "
             "symbol, variable, equation and expression exactly as written; keep option texts DISTINCT from "
             "each other; a one-word answer stays one word.")
# The bank is the translation's OUTPUT-heavy stage: the Cells kit's 289 items
# were ~1,500 distinct strings ≈ 13 calls of 120 strings, run one after the
# other — about 20 of the job's 36 minutes (2026-10-17, job 5c22720b). The
# calls are independent (each is a list of strings in, the same list out), so
# they run side by side; smaller chunks make more of them and keep a drifted
# reply's re-ask small. Both knobs are environment variables so a rate-limit
# squeeze is one variable away from the old serial behaviour (workers=1).
BANK_CHUNK_ENV = "TRANSLATE_BANK_CHUNK"        # strings per bank call
BANK_WORKERS_ENV = "TRANSLATE_BANK_WORKERS"    # bank calls in flight at once
BANK_CHUNK_DEFAULT = 60
BANK_WORKERS_DEFAULT = 3
BANK_WORKERS_MAX = 8
UNUSABLE_REPLY = "translation reply unusable"


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def bank_chunk() -> int:
    return _env_int(BANK_CHUNK_ENV, BANK_CHUNK_DEFAULT, 10, CHUNK)


def bank_workers() -> int:
    return _env_int(BANK_WORKERS_ENV, BANK_WORKERS_DEFAULT, 1, BANK_WORKERS_MAX)


def batch_items(items: list[dict], chunk: int) -> list[list[dict]]:
    """Whole items grouped so that no batch carries more than ``chunk``
    distinct strings — an item is never split across two calls, so a batch
    whose reply cannot be used maps back to exactly its items. An item with
    more strings than ``chunk`` is a batch of its own."""
    batches: list[list[dict]] = []
    current: list[dict] = []
    seen: set[str] = set()
    for it in items:
        words = {t for _, t in item_text_items(it) if _s(t)}
        new = words - seen
        if current and len(seen) + len(new) > chunk:
            batches.append(current)
            current, seen = [], set()
            new = words
        current.append(it)
        seen |= new
    if current:
        batches.append(current)
    return batches


def _model_calls(client) -> Optional[int]:
    usage = getattr(client, "session_usage", None)
    return int(usage.get("calls") or 0) if isinstance(usage, dict) else None


def item_text_items(item: dict) -> list[tuple[str, str]]:
    """The words of a bank item: stem, option texts, text answers, labels,
    pairs, marking-scheme points, distractor rationales, explanation —
    never keys, figure keys, units or numeric values."""
    def want(key: str, value: str) -> bool:
        return key in ITEM_TEXT_KEYS

    body = {k: item.get(k) for k in ITEM_FIELDS if item.get(k) is not None}
    return list(_walk_strings(body, want))


def translated_item(item: dict, pairs: Iterable[tuple[str, str]]) -> dict:
    body = {k: item.get(k) for k in ITEM_FIELDS if item.get(k) is not None}
    return {**item, **apply_strings(body, pairs)}


def _item_hash(row: dict, kit_id: str) -> str:
    """The content_hash of a TRANSLATED item. Not ``catalogue.questions.
    content_hash``: that canonicalises the stem through the catalogue key,
    which keeps only Latin word characters — an Arabic, Hindi or Telugu stem
    reduces to "" and every item of the bank would share one hash under
    the (topic_id, language, content_hash) unique. Instead the stem is
    folded script-agnostically (NFKC, lower-case, punctuation and runs of
    space collapsed) and the hash is scoped to the KIT: a re-translation
    of the same source for a new kit legitimately produces the same
    sentence, and the superseded kit's items keep theirs as ``retired``.
    Within one kit, two source items that translate to one sentence are
    one item (the insert's twin path drops the second with a flag)."""
    itype = str(row.get("item_type") or "")
    stem = unicodedata.normalize("NFKC", str(row.get("stem") or "")).casefold()
    stem = " ".join(_STEM_PUNCT.sub(" ", stem).split())
    material = ["translation", str(kit_id), itype, stem]
    answer = row.get("answer") if isinstance(row.get("answer"), dict) else {}
    if itype == "match" and answer.get("pairs"):
        material.append(",".join(sorted(f"{p.get('left')}={p.get('right')}" for p in answer["pairs"] if isinstance(p, dict))))
    return hashlib.sha1("|".join(material).encode("utf-8")).hexdigest()


_STEM_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def existing_staged(sb, kit_id: str) -> dict[str, str]:
    """``{source_item_id: id}`` of the items already staged for this kit — a
    resume stages nothing twice (the unique index would refuse it anyway)."""
    rows = _rows(sb.table("topic_questions").select("id,source_item_id").eq("translation_kit_id", kit_id).execute())
    return {str(r.get("source_item_id")): str(r.get("id")) for r in rows if r.get("source_item_id")}


def bank_stage(sb, client, source: dict, kit: dict, language: str, terms, article_id: str) -> tuple[dict[str, str], dict]:
    """Translate the topic's APPROVED English items and stage them for the
    kit (status ``staged``, ``source_item_id``, ``translation_kit_id``);
    returns the source → translated id mapping and a summary with the items
    the checks refused (flags, not blocks — a bank short of a few items is
    still a bank; one under BANK_FLOOR is refused)."""
    from catalogue.composer import load_bank

    topic_id = str(kit["topic_id"])
    items = load_bank(sb, topic_id, SOURCE_LANGUAGE)
    if not items:
        return {}, {"source_items": 0, "staged": 0, "reused": 0, "retried": 0, "dropped": []}
    mapping = existing_staged(sb, kit["id"])
    todo = [it for it in items if str(it.get("id")) not in mapping]
    chunk, workers = bank_chunk(), bank_workers()
    calls_before = _model_calls(client)
    tally = {"chunk": chunk, "workers": workers, "batches": 0, "batch_calls": 0,
             "fallback_items": 0, "fallback_failed": 0}

    def one_item(src: dict, context: str) -> tuple[dict, dict, dict]:
        """One item in its own call — the fallback for a batch whose reply
        could not be used. A reply unusable even for one item drops it with
        a finding the reviewer sees (``bank item not staged``)."""
        words = item_text_items(src)
        try:
            got = translate_strings(client, [t for _, t in words], language, terms=terms, context=context) if words else []
        except RuntimeError as exc:
            sid = str(src.get("id") or "item")
            return src, src, {"check": "A-item", "ok": False, "flags": [],
                              "blocking": [{"where": sid, "what": UNUSABLE_REPLY, "error": str(exc)[:200]}]}
        dst = translated_item(src, list(zip([p for p, _ in words], got)))
        return src, dst, check.check_item(src, dst)

    def one_batch(batch: list[dict], context: str) -> tuple[list[tuple[dict, dict, dict]], int]:
        """Translate every text field of ``batch`` in one call and check each
        item: ``(source, translated, report)`` per item, and how many items
        fell back. A reply that cannot be used — the count still drifts after
        the grace re-asks, or the JSON never parses — falls back to one call
        per item for THIS batch only; the other batches are untouched. A
        provider failure propagates as before (the job fails and a resume
        stages nothing twice). Runs on a pool thread: it returns its counts
        rather than touching shared state."""
        flat: list[tuple[int, str, str]] = []
        for i, it in enumerate(batch):
            for path, text in item_text_items(it):
                flat.append((i, path, text))
        try:
            got = translate_strings(client, [t for _, _, t in flat], language, terms=terms, context=context) if flat else []
        except RuntimeError as exc:
            log.warning("translate: bank batch of %d item(s) had no usable reply (%s); translating them one by one",
                        len(batch), exc)
            return [one_item(src, context) for src in batch], len(batch)
        by_item: dict[int, list[tuple[str, str]]] = {}
        for (i, path, _), t in zip(flat, got):
            by_item.setdefault(i, []).append((path, t))
        out = []
        for i, src in enumerate(batch):
            dst = translated_item(src, by_item.get(i, []))
            out.append((src, dst, check.check_item(src, dst)))
        return out, 0

    def attempt(batch: list[dict], context: str) -> list[tuple[dict, dict, dict]]:
        """``one_batch`` over the items in chunks of at most ``chunk`` distinct
        strings, ``workers`` chunks in flight at once; results in item order.
        Nothing here touches the database — the inserts below run in the
        job's own thread once every translation is back — and the tally is
        updated here, in that thread, from what each batch returns."""
        batches = batch_items(batch, chunk)
        if len(batches) <= 1 or workers <= 1:
            results = [one_batch(b, context) for b in batches]
        else:
            with ThreadPoolExecutor(max_workers=min(workers, len(batches)), thread_name_prefix="bank-translate") as pool:
                results = list(pool.map(lambda b: one_batch(b, context), batches))
        rows = [row for got, _ in results for row in got]
        tally["batches"] += len(batches)
        tally["batch_calls"] += len(batches)
        tally["fallback_items"] += sum(n for _, n in results)
        tally["fallback_failed"] += sum(1 for _, _, rep in rows
                                        if any(f.get("what") == UNUSABLE_REPLY for f in rep.get("blocking") or []))
        return rows

    first = attempt(todo, ITEM_RULE)
    # One retry for the items the check refused, told what was wrong: a
    # number the model added, an option it merged, a key it moved. A second
    # refusal drops the item, with both findings on the kit for the reviewer.
    refused = [(src, rep) for src, _, rep in first if not rep["ok"]]
    second: dict[str, tuple[dict, dict]] = {}
    if refused:
        why = "; ".join(sorted({str(f.get("what") or "") for _, rep in refused for f in rep["blocking"]}))
        again = attempt([src for src, _ in refused],
                        ITEM_RULE + f"\nA previous translation of these strings was refused ({why}). Translate the WORDS only: "
                                    "add no number, symbol, unit or explanation that the English does not have; keep every "
                                    "option distinct; a key, a number and a formula stay exactly as written.")
        second = {str(src.get("id")): (dst, rep) for src, dst, rep in again}
    calls_after = _model_calls(client)
    if calls_before is not None and calls_after is not None:
        tally["model_calls"] = calls_after - calls_before

    dropped: list[dict] = []
    staged = 0
    for src, dst, report in first:
        sid = str(src.get("id"))
        if not report["ok"]:
            dst2, report2 = second.get(sid, (None, None))
            if dst2 is None or report2 is None or not report2["ok"]:
                findings = (report["blocking"][:3]) + ([{**f, "where": f"{sid} (retry)"} for f in (report2 or {}).get("blocking", [])[:3]])
                dropped.append({"source_item_id": sid, "findings": findings})
                continue
            dst = dst2
        row = {k: src.get(k) for k in ITEM_COPY_KEYS}
        row.update({
            "article_id": article_id, "language": language, "status": "staged",
            "source_item_id": sid, "translation_kit_id": kit["id"],
            **{k: dst.get(k) for k in ITEM_FIELDS},
        })
        row["content_hash"] = _item_hash(row, str(kit["id"]))
        try:
            ins = _rows(sb.table("topic_questions").insert(row).execute())
            new_id = str((ins or [{}])[0].get("id") or "")
        except Exception as exc:  # noqa: BLE001
            if "23505" not in str(exc) and str(getattr(exc, "code", "")) != "23505":
                raise
            # a twin: the same stem in the language already (two English
            # items that translate to one sentence), or a resume's rerun
            again_rows = _rows(sb.table("topic_questions").select("id").eq("translation_kit_id", kit["id"])
                               .eq("source_item_id", sid).execute())
            new_id = str((again_rows or [{}])[0].get("id") or "")
            if not new_id:
                dropped.append({"source_item_id": sid,
                                "findings": [{"where": sid, "what": "translated stem duplicates another item"}]})
                continue
        if new_id:
            mapping[sid] = new_id
            staged += 1
    total = len(mapping)
    # The floor binds only where the bank is one of the source kit's pieces
    # (the app inserts a question_bank at approval when the topic's bank is
    # at least basic): a kit whose topic has few or no approved English
    # items has no bank to mirror, and its items (if any) simply ride with
    # the kit's approval.
    docs = source["kit"].get("doc_generation_ids") if isinstance(source["kit"].get("doc_generation_ids"), dict) else {}
    if docs.get("question_bank") and total < BANK_FLOOR:
        raise TranslateRefused(f"only {total} bank item(s) survived translation of {len(items)}; "
                               f"the source kit's bank needs at least {BANK_FLOOR}")
    return mapping, {"source_items": len(items), "staged": staged, "reused": len(items) - len(todo),
                     "retried": len(refused), "dropped": dropped, "batching": tally}


def bank_flags(summary: dict) -> list[dict]:
    """The dropped items of a bank summary as pre-check flags: what the
    reviewer sees on the kit, whether or not the job went on to finish."""
    return [{"where": f"item {str(d.get('source_item_id') or '')[:8]}", "what": "bank item not staged",
             "detail": "; ".join(str(f.get("what") or "") for f in (d.get("findings") or []))}
            for d in (summary.get("dropped") or [])]


# ── the quiz set (the explicit-ids rule, revision 3) ─────────────────────


def quiz_stage(sb, source: dict, kit: dict, language: str, mapping: dict[str, str],
               dropped: Optional[dict[str, str]] = None) -> Optional[dict]:
    """The English kit's composed quiz, question for question: its
    question_sets row's ordered ids mapped through ``source_item_id`` to the
    staged items, written as a new question_sets row in the language (same
    blueprint, same seed). None when the source kit has no quiz. A source
    item that did not survive the bank checks fails the stage: the quiz
    must be the same quiz."""
    docs = source["kit"].get("doc_generation_ids") if isinstance(source["kit"].get("doc_generation_ids"), dict) else {}
    quiz_gen_id = _s(docs.get("quiz"))
    if not quiz_gen_id:
        return None
    rows = _rows(sb.table("generations").select("id,kind,params").eq("id", quiz_gen_id).limit(1).execute())
    if not rows:
        raise TranslateRefused(f"the source kit's quiz generation {quiz_gen_id} is missing")
    params = rows[0].get("params") if isinstance(rows[0].get("params"), dict) else {}
    set_id = _s(params.get("question_set_id"))
    if not set_id:
        raise TranslateRefused(f"the source kit's quiz {quiz_gen_id} names no question set")
    sets = _rows(sb.table("question_sets").select("*").eq("id", set_id).limit(1).execute())
    if not sets:
        raise TranslateRefused(f"the source kit's question set {set_id} is missing")
    qset = sets[0]
    src_ids = [str(i) for i in (qset.get("question_ids") or []) if i]
    missing = [i for i in src_ids if i not in mapping]
    if missing:
        raise TranslateRefused(f"{len(missing)} quiz question(s) have no staged translation: "
                               + "; ".join(f"{q[:8]} ({(dropped or {}).get(q) or 'not translated'})" for q in missing[:4]))
    new_set = {
        "blueprint_id": qset.get("blueprint_id"), "topic_ids": qset.get("topic_ids") or [kit["topic_id"]],
        "language": language, "question_ids": [mapping[i] for i in src_ids],
        "seed": qset.get("seed"), "requested_by": None,
    }
    ins = _rows(sb.table("question_sets").insert(new_set).execute())
    new_id = str((ins or [{}])[0].get("id") or "")
    if not new_id:
        raise RuntimeError("question_sets insert returned no id")
    return {"question_set_id": new_id, "source_set_id": set_id, "blueprint_id": qset.get("blueprint_id"),
            "seed": qset.get("seed"), "questions": len(src_ids),
            "source_params": {k: params.get(k) for k in ("curriculum_header",) if k in params}}


# ── pre-check B: meaning ──────────────────────────────────────────────────

FIDELITY_SEGMENTS = 6
FIDELITY_ITEMS = 3


def fidelity_stage(client, scripts: list[dict], source_bodies: list[dict], items_src: list[dict],
                   items_dst: dict[str, dict], language: str) -> dict:
    """Back-translate a sample of the translated narration and stems into
    English and score how much of the source each keeps. Flags only."""
    samples: list[tuple[str, str, str]] = []   # (where, source, translated)
    for body, dst in zip(source_bodies, scripts):
        src_segs = {str(s.get("segment_id")): s for s in ((body.get("script") or {}).get("segments") or []) if isinstance(s, dict)}
        dst_segs = [s for s in (dst.get("segments") or []) if isinstance(s, dict) and _s(s.get("text"))]
        pick = dst_segs[:FIDELITY_SEGMENTS // 2] + dst_segs[-(FIDELITY_SEGMENTS - FIDELITY_SEGMENTS // 2):]
        for seg in pick:
            src = src_segs.get(str(seg.get("segment_id")))
            if src and _s(src.get("text")):
                samples.append((f"part {body.get('part')} {seg.get('segment_id')}", str(src["text"]), str(seg["text"])))
    for it in items_src[:FIDELITY_ITEMS]:
        dst = items_dst.get(str(it.get("id")))
        if dst and _s(dst.get("stem")):
            samples.append((f"item {str(it.get('id'))[:8]}", str(it.get("stem") or ""), str(dst.get("stem"))))
    if not samples:
        return check.fidelity_report([])
    back = translate_strings(client, [t for _, _, t in samples], SOURCE_LANGUAGE,
                             context=f"These are {_lang_name(language)} sentences from a school lesson. Translate each "
                                     "back into plain English as literally as you can; do not improve or shorten.")
    return check.fidelity_report([(w, s, b) for (w, s, _), b in zip(samples, back)])


# ── generations ─────────────────────────────────────────────────────────

# ── the worksheet (T1.5): translated from its verified questions ─────────

WORKSHEET_QUESTIONS_FILE = "worksheet_questions.json"
QUESTION_RULE = ("Each string is a line of a school MATHS worksheet or its answer key: translate the words only; keep "
                 "every number, unit symbol, variable, equation, expression and the π symbol exactly as written, in "
                 "the same place; a line that is only working (an equation, a value) is copied unchanged; option "
                 "texts stay DISTINCT; a one-word answer stays one word.")


def question_text_items(q: dict) -> list[tuple[str, str]]:
    """The WORDS of a worksheet question: the prompt always (``Solve: 3x + 5
    = 20`` has a verb to translate), a text answer, the accepted spellings,
    the options, the pairs and the working lines when they are prose — a
    string that is notation (check.is_maths) is never sent."""
    out: list[tuple[str, str]] = [("prompt", str(q.get("prompt") or ""))]
    if isinstance(q.get("answer"), str) and not check.is_maths(q["answer"]):
        out.append(("answer", q["answer"]))
    for i, x in enumerate(q.get("accept") or [] if isinstance(q.get("accept"), list) else []):
        if isinstance(x, str) and not check.is_maths(x):
            out.append((f"accept[{i}]", x))
    for i, o in enumerate(q.get("options") or [] if isinstance(q.get("options"), list) else []):
        if isinstance(o, str) and not check.is_maths(o):
            out.append((f"options[{i}]", o))
    for i, pair in enumerate(q.get("pairs") or [] if isinstance(q.get("pairs"), list) else []):
        if isinstance(pair, dict):
            for side in ("left", "right"):
                if isinstance(pair.get(side), str) and not check.is_maths(pair[side]):
                    out.append((f"pairs[{i}].{side}", pair[side]))
    for i, w in enumerate(q.get("working") or [] if isinstance(q.get("working"), list) else []):
        if isinstance(w, str) and not check.is_maths(w):
            out.append((f"working[{i}]", w))
    return [(path, text) for path, text in out if _s(text)]


def _question_protected(q: dict) -> list[str]:
    spans: list[str] = []
    for _, text in question_text_items(q):
        for sp in check.maths_spans(text):
            if sp not in spans:
                spans.append(sp)
    return spans


def translate_questions(client, questions: list[dict], language: str, terms, *, context: str) -> tuple[list[dict], list[dict]]:
    """Every question's words in one call (a retry for the ones the check
    refused, told why); returns ``(kept, dropped)`` — the translated
    questions in the source order, and ``{id, findings}`` for each the
    checks refused twice."""
    def attempt(batch: list[dict], rule: str) -> list[tuple[dict, dict, dict]]:
        flat: list[tuple[int, str, str]] = []
        protect: list[str] = []
        for i, q in enumerate(batch):
            for path, text in question_text_items(q):
                flat.append((i, path, text))
            for sp in _question_protected(q):
                if sp not in protect:
                    protect.append(sp)
        got = translate_strings(client, [t for _, _, t in flat], language, terms=terms,
                                context=f"{context}\n{rule}", protect=protect) if flat else []
        by_q: dict[int, list[tuple[str, str]]] = {}
        for (i, path, _), t in zip(flat, got):
            by_q.setdefault(i, []).append((path, t))
        out = []
        for i, src in enumerate(batch):
            dst = apply_strings(src, by_q.get(i, []))
            out.append((src, dst, check.check_question(src, dst)))
        return out

    first = attempt(questions, QUESTION_RULE)
    refused = [(src, rep) for src, _, rep in first if not rep["ok"]]
    second: dict[str, tuple[dict, dict]] = {}
    if refused:
        why = "; ".join(sorted({str(f.get("what") or "") for _, rep in refused for f in rep["blocking"]}))
        again = attempt([src for src, _ in refused],
                        QUESTION_RULE + f"\nA previous translation of these strings was refused ({why}). Translate the "
                                        "WORDS only: add no number, symbol or unit the English does not have; every "
                                        "equation, expression and value stays exactly as written.")
        second = {str(src.get("id")): (dst, rep) for src, dst, rep in again}
    kept: list[dict] = []
    dropped: list[dict] = []
    for src, dst, report in first:
        qid = str(src.get("id"))
        if not report["ok"]:
            dst2, report2 = second.get(qid, (None, None))
            if dst2 is None or report2 is None or not report2["ok"]:
                findings = report["blocking"][:3] + [{**f, "where": f"{f.get('where')} (retry)"}
                                                     for f in (report2 or {}).get("blocking", [])[:3]]
                dropped.append({"id": qid, "findings": findings})
                continue
            dst = dst2
        kept.append(dst)
    return kept, dropped


def source_questions(sb, source: dict, tmp: Path) -> Optional[dict]:
    """The source kit's worksheet ``questions_json`` payload, or None when
    the kit has no worksheet or the worksheet has no such artifact (a
    science worksheet: rebuilt from the article instead)."""
    docs = source["kit"].get("doc_generation_ids") if isinstance(source["kit"].get("doc_generation_ids"), dict) else {}
    gen_id = _s(docs.get("worksheet"))
    if not gen_id:
        return None
    rows = _rows(sb.table("artifacts").select("storage_path").eq("generation_id", gen_id)
                 .eq("kind", "questions_json").limit(1).execute())
    path = _s((rows or [{}])[0].get("storage_path"))
    if not path:
        return None
    local = db.download_artifact(sb, path, Path(tmp) / "source_questions.json")
    payload = json.loads(Path(local).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("questions"), list):
        raise TranslateRefused(f"the source worksheet's questions_json ({path}) is not a questions payload")
    return payload


def staged_questions_path(owner_id: str, kit_id: str) -> str:
    return f"{owner_id}/{kit_id}/translated/{WORKSHEET_QUESTIONS_FILE}"


def worksheet_stage(sb, client, source: dict, kit: dict, language: str, terms, *, title: Optional[str],
                    context: str, tmp: Path) -> Optional[dict]:
    """Translate the source worksheet's verified questions and stage them
    for the render side. Returns None when the source has no maths
    worksheet (nothing to do: the document is rebuilt), else a summary
    ``{questions, kept, dropped, path}`` — ``path`` is None when no
    question survived (the generations stage then flags the document)."""
    payload = source_questions(sb, source, tmp)
    if payload is None:
        return None
    questions = [q for q in payload["questions"] if isinstance(q, dict)]
    kept, dropped = translate_questions(client, questions, language, terms, context=context) if questions else ([], [])
    summary: dict = {"questions": len(questions), "kept": len(kept), "dropped": dropped, "path": None}
    if not kept:
        return summary
    from docgen.strings import _t as _doc_t
    # the sheet's title: the document word in the language and the translated
    # topic title (the article stage's); without one, the English title translated
    if _s(title):
        heading = f"{_doc_t('doc_worksheet', language)} — {_s(title)}"
    else:
        heading = translate_strings(client, [_s(payload.get("title")) or "Worksheet"], language,
                                    terms=terms, context=context)[0]
    staged = {"title": heading, "instructions": _doc_t("ws_instructions", language), "language": language,
              "questions": kept,
              "source": {"kit_id": source["kit"]["id"], "language": SOURCE_LANGUAGE, "title": payload.get("title"),
                         "questions": len(questions), "dropped": [d["id"] for d in dropped]}}
    local = Path(tmp) / WORKSHEET_QUESTIONS_FILE
    local.write_text(json.dumps(staged, ensure_ascii=False, indent=2), encoding="utf-8")
    owner = _s(source["presentation"].get("owner_id"))
    dest = staged_questions_path(owner, str(kit["id"]))
    db.upload_artifact(sb, str(local), dest)
    summary["path"] = dest
    return summary


def worksheet_flags(summary: Optional[dict]) -> list[dict]:
    """The reviewer's flags for the worksheet stage: one per dropped
    question, and one for a sheet that could not be made at all."""
    if not summary:
        return []
    kept, total = int(summary.get("kept") or 0), int(summary.get("questions") or 0)
    flags = []
    for d in summary.get("dropped") or []:
        why = "; ".join(sorted({str(f.get("what") or "") for f in d.get("findings") or []})) or "translation refused"
        flags.append({"where": f"worksheet {d.get('id')}", "what": DOCUMENT_FLAG,
                      "detail": f"question {d.get('id')} dropped from the translated worksheet ({why}); "
                                f"{kept} of {total} question(s) kept"})
    if not summary.get("path"):
        flags.append({"where": "worksheet", "what": DOCUMENT_FLAG,
                      "detail": f"no question of the verified maths worksheet could be kept in the language "
                                f"({total} tried); the online quiz and the question bank carry the assessment"})
    return flags


def render_translated_worksheet(sb, gen: dict, generation_id: str, out_dir: Path, base: str, branding: dict,
                                language: str) -> str:
    """The render side (worker/process.py, the catalogue branch): a
    translated worksheet generation names its staged questions in
    ``params.translation.questions``; the sheet and its key are written
    from them (docgen.quiz_sheet) with no model call and no verifier, and
    uploaded exactly as the document block does — the ``docx`` row, the
    answer key under its own kind, the questions for the quiz player."""
    from docgen.quiz_sheet import write_sheet
    from docgen.docx_builder import header_lines_of

    params = gen.get("params") if isinstance(gen.get("params"), dict) else {}
    translation = params.get("translation") if isinstance(params.get("translation"), dict) else {}
    path = _s(translation.get("questions"))
    if not path:
        raise RuntimeError("translated worksheet generation has no params.translation.questions")
    local = db.download_artifact(sb, path, Path(out_dir) / "staged_questions.json")
    payload = json.loads(Path(local).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload.get("questions"):
        raise RuntimeError(f"staged questions {path} hold no question")
    language = (_s(language) or _s(payload.get("language")) or "en").lower()
    topic = _s(params.get("topic_id"))
    subtitle = ""
    if topic:
        rows = _rows(sb.table("topics").select("subject").eq("id", topic).limit(1).execute())
        subtitle = _s((rows or [{}])[0].get("subject"))
    paths = write_sheet(Path(out_dir), payload, language=language, template=(branding or {}).get("docx_template"),
                        header_lines=header_lines_of(params.get("curriculum_header")), subtitle=subtitle)
    dest = f"{base}/worksheet.docx"
    db.upload_artifact(sb, str(paths[0]), dest)
    db.add_artifact_row(sb, generation_id, "docx", dest)
    key_dest = f"{base}/answer_key.docx"
    db.upload_artifact(sb, str(paths[1]), key_dest)
    try:
        db.add_artifact_row(sb, generation_id, "answer_key_docx", key_dest)
    except Exception as exc:  # noqa: BLE001 — a missing enum value must not fail the sheet
        log.warning("answer_key_docx row skipped for %s: %s", generation_id, exc)
    try:
        qlocal = Path(out_dir) / "questions.json"
        qlocal.write_text(json.dumps({"title": payload.get("title"), "instructions": payload.get("instructions"),
                                      "questions": payload["questions"]}, ensure_ascii=False, indent=2), encoding="utf-8")
        qdest = f"{base}/worksheet_questions.json"
        db.upload_artifact(sb, str(qlocal), qdest)
        db.add_artifact_row(sb, generation_id, "questions_json", qdest)
    except Exception as exc:  # noqa: BLE001
        log.warning("questions_json skipped for %s: %s", generation_id, exc)
    return _s(payload.get("title")) or "Worksheet"


# The authored document kinds a translated kit rebuilds from its translated
# article (the model, in the language — plan 5.2 step 12's fallback is the
# path: our .docx are many-run paragraphs, and a rebuild keeps styles, RTL
# and the maths ladder). The lesson plan is the lifecycle's; the quiz and
# the question bank are composed from the staged items.
DOC_KINDS_REBUILT = ("activity", "case_study", "worksheet")


def staged_script_path(owner_id: str, kit_id: str, part: int) -> str:
    return f"{owner_id}/{kit_id}/translated/script{'' if part == 1 else f'_part{part}'}.json"


def upload_staged_scripts(sb, owner_id: str, kit_id: str, scripts: list[tuple[int, int, dict]], language: str,
                          tmp: Path) -> list[str]:
    paths = []
    for part, of, script in scripts:
        body = {"part": part, "of": of, "language": language, "script": script}
        local = Path(tmp) / f"translated_{part}.json"
        local.write_text(json.dumps(body, ensure_ascii=False, default=str), encoding="utf-8")
        dest = staged_script_path(owner_id, kit_id, part)
        db.upload_artifact(sb, str(local), dest)
        paths.append(dest)
    return paths


def validate_translated_script(raw: dict):
    """The staged (translated) EpisodeScript through the generators' model,
    with the slide visuals the PIPELINE attaches after the model kept intact.
    ``slide_visual.kind = "figure"`` (a textbook figure, agent5_slides/
    figures.py, FEATURE_TEXTBOOK_FIGURES) is written onto the dumped English
    script after validation and is not in SlideVisual's Literal; validating
    the translation verbatim refused the Cells kit (2026-10-17: "Input should
    be 'flow', 'cycle', …, input_value='figure'"). Those visuals are lifted
    out, the rest is validated, and they are put back on the dump — the
    slide builders read the dict and know the kind. Returns ``(model,
    dump)``; ``dump`` is what the render uses."""
    import copy
    from typing import get_args

    from agent3_scripts.models import EpisodeScript, SlideVisual

    known = set(get_args(SlideVisual.model_fields["kind"].annotation))
    body = copy.deepcopy(raw)
    kept: dict[int, dict] = {}
    for i, seg in enumerate(body.get("segments") or []):
        sv = seg.get("slide_visual") if isinstance(seg, dict) else None
        if isinstance(sv, dict) and sv.get("kind") not in known:
            kept[i] = sv
            seg["slide_visual"] = None
    model = EpisodeScript.model_validate(body)
    dump = model.model_dump()
    for i, sv in kept.items():
        if i < len(dump.get("segments") or []):
            dump["segments"][i]["slide_visual"] = sv
    return model, dump


def load_staged_scripts(sb, translation: dict, tmp) -> list[dict]:
    """For the presentation build (worker/process.py): the translated
    EpisodeScript dicts in part order, from the paths the job staged."""
    out = []
    for p in list((translation or {}).get("scripts") or []):
        dest = db.download_artifact(sb, p, Path(tmp) / f"override_{Path(p).name}")
        body = json.loads(Path(dest).read_text(encoding="utf-8"))
        script = body.get("script") if isinstance(body, dict) and isinstance(body.get("script"), dict) else body
        out.append((int((body or {}).get("part") or _part_of_path(p)), script))
    out.sort(key=lambda t: t[0])
    return [s for _, s in out]


def generation_params(source: dict, kit: dict, language: str, article_id: str) -> dict:
    src_params = dict(source["presentation"].get("params") or {})
    pair = dict(kit.get("voice_pair") or {})
    return {
        "catalogue": True, "topic_id": kit["topic_id"], "kit_id": kit["id"], "article_id": article_id,
        "language": language, "narration_style": src_params.get("narration_style") or "dialogue",
        "teacher_avatar": kit.get("teacher_avatar") or src_params.get("teacher_avatar") or "female",
        "tts_voice": pair.get("teacher"), "student_voice": pair.get("student"),
        "curriculum_header": list(src_params.get("curriculum_header") or []),
        "translation": {"source_kit_id": source["kit"]["id"], "source_generation_id": source["presentation"]["id"],
                        "source_language": SOURCE_LANGUAGE, "kit_id": kit["id"]},
    }


def generations_stage(sb, source: dict, kit: dict, language: str, article_id: str, script_paths: list[str],
                      quiz: Optional[dict] = None, worksheet: Optional[dict] = None,
                      redraw: Optional[list[str]] = None) -> dict:
    """Every piece of the translated kit as a queued generation: the
    presentation (with the staged scripts), the deck, the authored
    documents the source kit has, the printable question bank, and the
    quiz — a composed worksheet with no kit_id (as the source's) whose
    ``params.translation.kit_id`` ties its completion to the kit. The kit
    row is pointed at all of them; the lifecycle adds the lesson plan.

    ``worksheet`` is the worksheet stage's summary (T1.5): a maths
    worksheet's verified questions, translated and staged under ``path``.
    Its generation carries that path in ``params.translation.questions``
    and renders without a model (render_translated_worksheet); a question
    the checks dropped is a flag, a sheet with none left is not made and
    the document is flagged as not translated."""
    owner = _s(source["presentation"].get("owner_id"))
    if not owner:
        raise RuntimeError("the source presentation has no owner")
    base = generation_params(source, kit, language, article_id)
    src_docs = source["kit"].get("doc_generation_ids") if isinstance(source["kit"].get("doc_generation_ids"), dict) else {}

    def row(kind: str, params: dict) -> dict:
        return {"owner_id": owner, "kind": kind, "status": "queued", "book_id": None, "chapter_ref": None,
                "school_id": None, "params": params}

    pres_translation = {**base["translation"], "scripts": list(script_paths)}
    if redraw:
        pres_translation["redraw"] = list(redraw)     # the render may draw THESE afresh (raster_assets)
    rows = [row("presentation", {**base, "translation": pres_translation}),
            row("deck", base)]
    skipped: list[dict] = []
    for kind in DOC_KINDS_REBUILT:
        if not src_docs.get(kind):
            continue
        if kind == "worksheet" and (worksheet is not None or _has_artifact(sb, _s(src_docs.get(kind)), "questions_json")):
            # A maths worksheet: its questions were VERIFIED in English (the
            # verifier reads English back), so a rebuild in the language
            # fails that gate (2026-10-16, the Circle kit). T1.5: the sheet
            # is translated from the artifact instead — the worksheet stage
            # staged the surviving questions; the render side draws the
            # documents from them. No survivor (or no stage run): flagged.
            skipped += worksheet_flags(worksheet or {"questions": 0, "kept": 0, "dropped": [], "path": None})
            if worksheet and worksheet.get("path"):
                rows.append(row(kind, {**base, "translation": {**base["translation"], "questions": worksheet["path"]}}))
            continue
        rows.append(row(kind, base))
    if src_docs.get("question_bank"):
        rows.append(row("question_bank", base))
    if quiz:
        qparams = {k: v for k, v in base.items() if k != "kit_id"}
        qparams.update({"question_set_id": quiz["question_set_id"], "blueprint_id": quiz.get("blueprint_id"),
                        "seed": quiz.get("seed")})
        rows.append(row("worksheet", qparams))        # the quiz: a composed set, no kit_id (as the source's)
    ins = sb.table("generations").insert(rows).execute()
    made = _rows(ins)
    ids: dict[str, str] = {}
    for r, spec in zip(made, rows):
        if not r.get("id"):
            continue
        label = "quiz" if (spec["kind"] == "worksheet" and spec["params"].get("question_set_id")) else spec["kind"]
        ids[label] = str(r["id"])
    if "presentation" not in ids or "deck" not in ids:
        raise RuntimeError(f"generations insert returned {sorted(ids)}; presentation and deck expected")
    docs = {k: v for k, v in ids.items() if k != "presentation"}
    sb.table("topic_kits").update({"presentation_generation_id": ids["presentation"],
                                   "doc_generation_ids": docs}).eq("id", kit["id"]).execute()
    if skipped:
        append_flags(sb, str(kit["id"]), skipped)
    return ids


DOCUMENT_FLAG = "document not translated"
REDRAW_FLAG = "picture redrawn"
REDRAW_ENV = "TRANSLATE_REDRAW_MISSING"      # "0" restores the refusal: a missing picture fails the job


def redraw_missing_enabled() -> bool:
    return str(os.getenv(REDRAW_ENV, "1") or "1").strip().lower() not in ("0", "false", "no", "off")


def append_flags(sb, kit_id: str, flags: list[dict]) -> None:
    """Add ``flags`` to the kit's CURRENT precheck (read back, never from a
    stale row in hand) — the bank's and the documents' flags are written at
    different stages and each must survive the other."""
    rows = _rows(sb.table("topic_kits").select("precheck").eq("id", kit_id).limit(1).execute())
    precheck = (rows or [{}])[0].get("precheck")
    precheck = dict(precheck) if isinstance(precheck, dict) else {}
    precheck["flags"] = list(precheck.get("flags") or []) + list(flags)
    sb.table("topic_kits").update({"precheck": precheck}).eq("id", kit_id).execute()


def document_flags(sb, kit_id: str) -> list[dict]:
    """The flags earlier stages left on the kit that the final write must
    carry: 'document not translated' and 'picture redrawn'."""
    rows = _rows(sb.table("topic_kits").select("precheck").eq("id", kit_id).limit(1).execute())
    precheck = (rows or [{}])[0].get("precheck")
    flags = precheck.get("flags") if isinstance(precheck, dict) else None
    return [f for f in (flags or []) if isinstance(f, dict) and f.get("what") in (DOCUMENT_FLAG, REDRAW_FLAG)]


def _has_artifact(sb, generation_id: str, kind: str) -> bool:
    if not generation_id:
        return False
    rows = _rows(sb.table("artifacts").select("id").eq("generation_id", generation_id).eq("kind", kind).limit(1).execute())
    return bool(rows)


# ── the job ─────────────────────────────────────────────────────────────


def translate_kit(sb, job_id: str, params: dict, client=None, tmp=None) -> dict:
    """The job proper. Returns the summary also written to ``jobs.stage``;
    raises on any failure (``run_translate_job`` records it)."""
    import tempfile

    source_kit_id = _s(params.get("source_kit_id"))
    language = _s(params.get("language")).lower()
    if not source_kit_id:
        raise TranslateRefused("kit_translate job without params.source_kit_id")
    if language not in LANGUAGES:
        raise TranslateRefused(f"language {language!r} is not a lesson language")
    if language == SOURCE_LANGUAGE:
        raise TranslateRefused("the source is already English")

    stage: dict = {"phase": "translate", "step": "load", "source_kit_id": source_kit_id, "language": language}
    db.set_stage(sb, job_id, dict(stage))
    db.set_progress(sb, job_id, 3)
    source = load_source(sb, source_kit_id)
    if client is None:
        from shared.llm import client_for
        client = client_for(language)

    kit = find_translation_kit(sb, source_kit_id, language) or create_translation_kit(sb, source, language, job_id)
    done = dict((kit.get("translation") or {}).get("stage") or {})
    stage["kit_id"] = kit["id"]
    topic = source["topic"]
    context = (f"The lesson is '{_s(source['article'].get('title'))}' ({_s(topic.get('subject'))}) for school "
               f"students; the teacher speaks to a class.")

    with tempfile.TemporaryDirectory() as _tmp:
        tmp = Path(tmp or _tmp)
        bodies = download_scripts(sb, source["script_paths"], tmp)

        # glossary
        stage["step"] = "glossary"
        db.set_stage(sb, job_id, dict(stage))
        db.set_progress(sb, job_id, 10)
        terms = [tuple(x) for x in (done.get("glossary") or {}).get("pairs") or []]
        if not terms:
            terms, g_summary = glossary_stage(sb, client, topic, source["article"], language)
            kit = _save_translation(sb, kit, {"stage": {"glossary": {**g_summary, "pairs": terms}}})
            done = dict(kit["translation"]["stage"])

        # article
        stage["step"] = "article"
        db.set_stage(sb, job_id, dict(stage))
        db.set_progress(sb, job_id, 25)
        article_id = _s((done.get("article") or {}).get("article_id"))
        if not article_id:
            article_id, a_summary = article_stage(sb, client, source, language, terms, kit)
            kit = _save_translation(sb, kit, {"stage": {"article": a_summary}})
            done = dict(kit["translation"]["stage"])
        title = _s((done.get("article") or {}).get("title")) or None

        # scripts + pre-check A
        stage["step"] = "script"
        db.set_stage(sb, job_id, dict(stage))
        db.set_progress(sb, job_id, 40)
        n = len(bodies)
        scripts: list[tuple[int, int, dict]] = []
        reports: list[dict] = []
        for i, body in enumerate(bodies):
            part = int(body.get("part") or i + 1)
            db.set_stage(sb, job_id, {**stage, "part": part, "total": n})
            dst, report = translate_episode(client, body, language, terms, title=title if n == 1 else None,
                                            context=context)
            scripts.append((part, n, dst))
            reports.append({"part": part, **report})
            db.set_progress(sb, job_id, 40 + round(35 * (i + 1) / n))

        # assets
        stage["step"] = "assets"
        db.set_stage(sb, job_id, dict(stage))
        keys = scene_asset_keys([s for _, _, s in scripts])
        found, missing = resolve_assets(sb, keys)
        redraw: list[str] = []
        if missing and redraw_missing_enabled():
            # Founder, 2026-10-17: a picture that exists nowhere but inside
            # the English video (generated at its render, never published,
            # the cache long gone) is drawn afresh by the translated render
            # from the English prompt — and every such picture is a flag the
            # reviewer sees, because the two videos then differ there.
            redraw = list(missing)
            append_flags(sb, str(kit["id"]), [{"where": k, "what": REDRAW_FLAG,
                                                "detail": "the English video's picture is in neither the library nor "
                                                          "the cache; the translation draws a new one from the same "
                                                          "prompt — compare the scene with the English video"}
                                               for k in missing])
            log.warning("translation kit %s: %d picture(s) will be redrawn: %s", kit["id"], len(missing), ", ".join(missing[:6]))
        elif missing:
            raise TranslateRefused(f"{len(missing)} scene asset(s) of the source video cannot be found and will not "
                                   f"be regenerated: {', '.join(missing[:6])}")

        # the bank (D4): staged for the kit, checked item by item
        stage["step"] = "bank"
        db.set_stage(sb, job_id, dict(stage))
        db.set_progress(sb, job_id, 78)
        mapping, bank_summary = bank_stage(sb, client, source, kit, language, terms, article_id)
        kit = _save_translation(sb, kit, {"stage": {"bank": {k: v for k, v in bank_summary.items() if k != "dropped"}}})
        if bank_summary.get("dropped"):
            # on the kit NOW, so a later stage's failure keeps the reasons —
            # APPENDED: the assets stage's 'picture redrawn' flags are already
            # there (the Cells kit lost its six to a replacing write here)
            append_flags(sb, str(kit["id"]), bank_flags(bank_summary))

        # the quiz: the same questions, by id
        stage["step"] = "quiz"
        db.set_stage(sb, job_id, dict(stage))
        quiz = quiz_stage(sb, source, kit, language, mapping,
                          dropped={d["source_item_id"]: "; ".join(str(f.get("what") or "") for f in d["findings"])
                                   for d in bank_summary.get("dropped") or []})

        # pre-check B: meaning, on a sample
        stage["step"] = "fidelity"
        db.set_stage(sb, job_id, dict(stage))
        db.set_progress(sb, job_id, 84)
        from catalogue.composer import load_bank as _load_bank
        src_items = _load_bank(sb, str(kit["topic_id"]), SOURCE_LANGUAGE)
        dst_rows = _rows(sb.table("topic_questions").select("id,source_item_id,stem").eq("translation_kit_id", kit["id"]).execute())
        items_dst = {str(r.get("source_item_id")): r for r in dst_rows}
        fidelity = fidelity_stage(client, [s for _, _, s in scripts], bodies, src_items, items_dst, language)

        # the worksheet (T1.5): a maths worksheet's verified questions, translated
        stage["step"] = "worksheet"
        db.set_stage(sb, job_id, dict(stage))
        db.set_progress(sb, job_id, 87)
        worksheet = worksheet_stage(sb, client, source, kit, language, terms, title=title, context=context, tmp=tmp)

        # generations
        stage["step"] = "generations"
        db.set_stage(sb, job_id, dict(stage))
        db.set_progress(sb, job_id, 90)
        owner = _s(source["presentation"].get("owner_id"))
        paths = upload_staged_scripts(sb, owner, kit["id"], scripts, language, tmp)
        ids = generations_stage(sb, source, kit, language, article_id, paths, quiz=quiz, worksheet=worksheet,
                                redraw=redraw)

    usage = getattr(client, "session_usage", None)
    precheck = check.merge_reports(*[{k: v for k, v in r.items() if k != "part"} for r in reports], fidelity)
    precheck["parts"] = [{"part": r["part"], "ok": r.get("ok"), "blocking": len(r.get("blocking") or []),
                          "flags": len(r.get("flags") or [])} for r in reports]
    if bank_summary.get("dropped"):
        precheck["flags"] = list(precheck.get("flags") or []) + bank_flags(bank_summary)
    # the documents stage's flags (a worksheet question dropped, a sheet not
    # made) are already on the kit; this final write replaces the whole
    # value, so carry them
    precheck["flags"] = list(precheck.get("flags") or []) + document_flags(sb, str(kit["id"]))
    precheck["bank"] = {k: v for k, v in bank_summary.items() if k != "dropped"}
    sb.table("topic_kits").update({"precheck": precheck}).eq("id", kit["id"]).execute()
    kit = _save_translation(sb, kit, {
        "stage": {"script": {"parts": n}, "assets": {"found": len(found), "keys": len(keys), "redraw": redraw,
                                                     # the board pictures the colour shelf answered for
                                                     "shelf": [f["key"] for f in found if f.get("colour")]},
                  "quiz": quiz, "fidelity": {"mean_score": fidelity.get("mean_score"), "samples": fidelity.get("samples")},
                  "worksheet": ({k: v for k, v in worksheet.items() if k != "dropped"} | {"dropped": [d["id"] for d in worksheet["dropped"]]})
                  if worksheet else None,
                  "generations": ids},
        "scripts": paths, "source_scripts": [{"path": b["_path"], "sha256": b["_sha256"]} for b in bodies],
        "assets": found, "model": str(getattr(client, "model", "") or ""),
        "cost": {"translation": {"usage": usage if isinstance(usage, dict) else None}, "conversion": {}},
        "glossary_pairs": len(terms),
    })
    if isinstance(usage, dict) and usage.get("calls"):
        db.set_job_usage(sb, job_id, usage)
    summary = {"phase": "translate", "step": "done", "kit_id": kit["id"], "language": language,
               "source_kit_id": source_kit_id, "article_id": article_id, "parts": n,
               "generations": ids, "assets": len(found), "glossary": len(terms),
               "bank": precheck["bank"], "quiz": bool(quiz), "fidelity": fidelity.get("mean_score"),
               "precheck_ok": precheck.get("ok"), "flags": len(precheck.get("flags") or [])}
    db.set_progress(sb, job_id, 100)
    return summary


# ── T2: one translation at a time (decision D7) ────────────────────────────
# The bulk button queues one kit_translate job per ready kit at once; the
# WORKER spaces them. A translation is "in flight" from the moment its job is
# claimed until every piece of the translated kit has rendered (the kit's
# status leaves `generating`). While one is in flight the observer lane does
# not claim the next kit_translate (worker/run.py), so a 48-kit sweep is 48
# kits in a row, each one's video and pictures done before the next one's
# model calls begin — never a burst on the shared Vertex pool, and the
# founder sees one kit land at a time in the review queue.


def translations_queued(sb) -> bool:
    """Whether any kit_translate job is waiting — the gate is only worth
    its two reads while a sweep is on."""
    try:
        rows = _rows(sb.table("jobs").select("id").eq("type", "kit_translate").eq("status", "queued")
                     .limit(1).execute())
    except Exception as exc:  # noqa: BLE001
        log.warning("translate lane: queue unreadable (%s)", exc)
        return False
    return bool(rows)


def translation_in_flight(sb) -> Optional[str]:
    """Why the next translation must wait, or None: a kit_translate job is
    processing (its text calls are running), or a translated kit is still
    `generating` (its video, deck, documents, bank or quiz are rendering).
    An unreadable database counts as in flight: no sweep step starts on a
    guess."""
    try:
        jobs = _rows(sb.table("jobs").select("id").eq("type", "kit_translate").eq("status", "processing")
                     .limit(1).execute())
        if jobs:
            return f"translation job {jobs[0].get('id')} is staging"
        kits = _rows(sb.table("topic_kits").select("id,language").eq("status", "generating").execute())
        for k in kits:
            if _s(k.get("language")).lower() not in ("", "en"):
                return f"translated kit {k.get('id')} is rendering"
    except Exception as exc:  # noqa: BLE001
        log.warning("translate lane: in-flight check unreadable (%s)", exc)
        return "the database did not answer"
    return None


def run_translate_job(sb, job: dict, client=None) -> Optional[dict]:
    """Entry point for run.py. Self-contained: finishes the job row itself
    (done with the summary in ``stage``; error with the message) and marks
    the translation kit failed on the way out."""
    job_id = job["id"]
    params = job.get("params") or {}
    kit = None
    try:
        summary = translate_kit(sb, job_id, params, client=client)
        db.set_stage(sb, job_id, summary)
        db.finish_job(sb, job_id)
        return summary
    except Exception as exc:  # noqa: BLE001
        log.exception("kit_translate %s failed", job_id)
        try:
            kit = find_translation_kit(sb, _s(params.get("source_kit_id")), _s(params.get("language")).lower())
        except Exception:  # noqa: BLE001
            kit = None
        _fail_translation(sb, kit, f"translation failed: {exc}", report=getattr(exc, "report", None))
        db.finish_job(sb, job_id, error=str(exc))
        return None


# ── after the render (pre-check C) ──────────────────────────────────────


def record_post_render(sb, kit_id: Optional[str], parts: list[dict]) -> Optional[dict]:
    """Merge the per-part post-render reports the presentation build
    computed into ``topic_kits.precheck.post_render``. Best-effort."""
    if not kit_id:
        return None
    reps = [p.get("post_render") for p in parts if isinstance(p, dict) and isinstance(p.get("post_render"), dict)]
    if not reps:
        return None
    try:
        kit = load_kit(sb, kit_id)
        pre = dict((kit or {}).get("precheck") or {})
        merged = check.merge_reports(*reps)
        pre["post_render"] = merged
        pre["ok"] = bool(pre.get("ok", True)) and bool(merged.get("ok"))
        sb.table("topic_kits").update({"precheck": pre}).eq("id", kit_id).execute()
        return merged
    except Exception:  # noqa: BLE001
        log.exception("post-render report not recorded for kit %s", kit_id)
        return None


__all__ = ["JOB_TYPE", "TranslateRefused", "apply_strings", "article_text_items", "bank_stage", "download_scripts",
           "fidelity_stage", "generation_params", "generations_stage", "glossary_stage", "item_text_items",
           "lesson_from_record", "lesson_text_items", "load_source", "load_staged_scripts", "quiz_stage",
           "recompile_maths", "record_post_render", "resolve_assets", "run_translate_job", "scene_asset_keys",
           "question_text_items", "render_translated_worksheet", "translate_questions", "worksheet_flags", "worksheet_stage",
           "segment_text_items", "staged_script_path", "translate_episode", "translate_kit", "translate_segments",
           "translate_strings", "translated_item", "validate_translated_script"]
