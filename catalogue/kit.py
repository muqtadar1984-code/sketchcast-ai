"""A catalogue KIT's generations: what the worker reads before it builds one,
and what it writes after (catalogue Phase 3, 2026-09-06).

A kit is a set of ordinary ``generations`` rows owned by the system account
(``catalogue@sketchcast.app``) with ``params.catalogue = true`` and NO book:
``book_id`` and ``chapter_ref`` are NULL, and the lesson's source is the
topic's APPROVED knowledge article. ``worker/process.py`` recognises the flag
and, instead of its book prelude (download → structure → OCR → verify →
measure), calls ``prepare()`` here for everything the shared build needs:

  * the topic, the article and the kit row — REFUSED (decision 13, defence in
    depth behind the console's own gate) when the article is not
    ``approved`` or the kit is missing or ``rejected``, before any model
    call; an article refusal also marks the kit ``failed`` so the portal's
    Retry can act on it;
  * the analysis level and the student's age band from the DEPTH node's
    grade (decision 14 — mapped onto the analyzer's own vocabulary:
    ``primary_school`` / ``middle_school`` / ``high_school``);
  * the curriculum header lines every document prints (decision 10);
  * a synthetic ``book`` dict (title, grade, subject, curriculum, language,
    author "SketchCast", ``id`` None — anything keyed by book id is bypassed
    on this path);
  * the chapter dict (``catalogue.loader.article_to_chapter``) and its PARTS,
    chunked by ``build_chapter_parts`` with the catalogue's longer words
    budget (``CATALOGUE_PART_TARGET_MIN`` × 130 wpm, 17 min by default,
    never above 20) — the accumulator closes only at article-section
    boundaries, so no section is split across two videos;
  * the two voices: the teacher's (``params.tts_voice``) and the student's
    (``params.student_voice``, else the OTHER gender's premium student voice
    for the language; a language without one falls back to today's Edge
    student and ``student_voice_fallback`` is recorded on the generation).

After the build, the kit lifecycle runs in TWO halves (decision 6). A
finished presentation first — ``record_presentation()``, BEFORE the worker
marks its own generation done — writes its measured chapter timestamps, clips
and part plan onto ``topic_kits`` and INSERTS the ``lesson_plan`` generation
(the plan cites the clips, so it cannot exist before the video does). Only
then does the worker finish the job, and ``after_generation()`` checks
completion: when every generation the kit references is ``done`` the kit —
and its topic — move to ``in_review`` (guarded: only from ``generating``).
The order is the whole point (review finding, 2026-09-06): with
``WORKER_CONCURRENCY > 1`` a sibling thread finishing the kit's last document
between the presentation's ``finish_job`` and its lesson_plan insert saw
"every referenced generation done" and put a kit in review whose lesson plan
did not yet exist. Inserting the plan while the presentation still reads
``processing`` closes that window, and ``sync_kit_completion`` refuses a kit
that has a presentation but no lesson_plan at all. A failure moves the kit to
``failed`` (same guard) and leaves the topic where it was. Nothing here ever
writes ``approved``: that is the reviewer's, through the RPCs.

While a kit renders it must also YIELD to real users (the standing never-
starve rule's DURING half): ``ContentionProbe`` answers "is a user builder
live?" with a short cache so eight render threads cost one query, and
``yield_to_users()`` polls it between a presentation's parts. The scene
engine consults the same probe before every image generation
(``spike.scene_engine.raster_assets.set_user_yield``, armed by the worker's
catalogue branch for the build and removed after it).

The narration style is ``conversational`` — the one style
``spike.scene_engine.whiteboard.two_voice_dialogue`` recognises as two-voice
— whatever the params call it (they say ``dialogue``). Parts after the first
open with a RECAP segment and parts before the last close with a
CONTINUATION OUTRO (decision 3): deterministic authored text, teacher-only,
in the lesson language, validated as ``agent3_scripts.models.ScriptSegment``.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Callable, Iterable, Optional

from agent2_analysis.analyzer import NARRATION_WPM, build_chapter_parts
from catalogue import timestamps as ts
from catalogue.article import Mapping, load_article, load_mappings, load_topic, pick_depth_node
from catalogue.harvest import clean_heading
from catalogue.loader import article_to_chapter, section_ids_by_heading
from catalogue.node_kind import node_kind
from worker import client as db

log = logging.getLogger("worker.kit")

# The narration style the params call "dialogue". Two-voice playback is
# decided by ONE predicate (whiteboard.two_voice_dialogue) and today it says
# yes to exactly this label — so this is the label the kit renders with.
NARRATION_STYLE = "conversational"
PART_TARGET_MIN_DEFAULT = 17
PART_CEILING_MIN = 20
WINDOW_ENV = "CATALOGUE_WINDOW_UTC"
WINDOW_DEFAULT = "20:00-05:00"
WINDOW_ALWAYS = "always"
# A presentation renders for 30-60+ minutes. The kit lane claims one only
# when the window stays open at least this long, so a render claimed at 04:50
# does not run into the hours teachers use (documents, a minute each, need no
# margin). ``always`` ignores it, as it ignores the window.
PRESENTATION_MARGIN_ENV = "CATALOGUE_PRESENTATION_MARGIN_MIN"
PRESENTATION_MARGIN_DEFAULT_MIN = 60
# The DURING half of the never-starve rule: how often a rendering kit re-asks
# whether a user builder went live, and how long it will wait for one to
# finish before it gives the contended call up (the image is skipped and the
# board degrades to the vector tier — never made over a teacher's render).
YIELD_POLL_ENV = "CATALOGUE_YIELD_POLL_SECONDS"
YIELD_POLL_DEFAULT_S = 20.0
YIELD_MAX_ENV = "CATALOGUE_YIELD_MAX_SECONDS"
YIELD_MAX_DEFAULT_S = 1800.0
PROBE_TTL_S = 10.0
PAUSED_FOR_USERS = "paused: builder jobs queued"
# The one kind the composer renders from a question set (decision 9).
COMPOSED_KIND = "worksheet"
# Kinds a kit's generations may carry (decision 1 + the worker-inserted plan).
KIT_KINDS = frozenset({"presentation", "activity", "case_study", "worksheet", "deck", "lesson_plan"})
SYNTHETIC_AUTHOR = "SketchCast"
ARTICLE_APPROVED = "approved"
KIT_GENERATING = "generating"
KIT_IN_REVIEW = "in_review"
KIT_FAILED = "failed"
KIT_REJECTED = "rejected"
TOPIC_GENERATING = "generating"
TOPIC_IN_REVIEW = "in_review"
LEVEL_PRIMARY = "primary_school"
LEVEL_MIDDLE = "middle_school"
LEVEL_HIGH = "high_school"
RECAP_ID = "recap"
OUTRO_ID = "outro"
# The params a presentation hands on to the lesson_plan it spawns. A
# whitelist, not a copy: telemetry the worker merged in (coverage, tts_*),
# and anything a later kind adds, must not travel.
_INHERITED_PARAMS = ("catalogue", "topic_id", "kit_id", "article_id", "language", "narration_style",
                     "teacher_avatar", "tts_voice", "student_voice", "curriculum_header")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


class CatalogueRefused(RuntimeError):
    """The generation must not be built; the message says why."""


# ── pure helpers ────────────────────────────────────────────────────────


def is_catalogue(params: Optional[dict]) -> bool:
    """``params.catalogue`` is JSON true (the app writes a boolean; through
    PostgREST text paths it reads as "true"). The reading itself lives in
    ``worker.client`` so the book path and the reaper can ask without
    importing this package."""
    return db.is_catalogue_params(params)


def part_target_minutes() -> int:
    """``CATALOGUE_PART_TARGET_MIN`` clamped to [1, 20]; 17 by default. The
    ceiling is the spec's: a YouTube part is never planned past 20 minutes."""
    raw = str(os.getenv("CATALOGUE_PART_TARGET_MIN", "") or "").strip()
    try:
        minutes = int(raw) if raw else PART_TARGET_MIN_DEFAULT
    except ValueError:
        minutes = PART_TARGET_MIN_DEFAULT
    return max(1, min(PART_CEILING_MIN, minutes))


def part_words_budget() -> int:
    """The words bound handed to build_chapter_parts: minutes × 130 wpm
    (2,210 at the default 17; 2,600 at the 20-minute ceiling)."""
    return part_target_minutes() * NARRATION_WPM


def _parse_hhmm(text: str) -> Optional[dtime]:
    m = _TIME_RE.match(text.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return dtime(h, mi)


def _window_bounds() -> tuple[dtime, dtime]:
    """The configured window, or the DEFAULT when the value cannot be read.
    ``start == end`` counts as unreadable too: "05:00-05:00" is a typo of the
    default, and reading it as "all day" would open the kit lane for every
    hour — the exact fail-open the docstring below promises never happens."""
    raw = str(os.getenv(WINDOW_ENV, "") or "").strip().lower() or WINDOW_DEFAULT
    parts = raw.split("-", 1)
    start = _parse_hhmm(parts[0]) if len(parts) == 2 else None
    end = _parse_hhmm(parts[1]) if len(parts) == 2 else None
    if start is None or end is None or start == end:
        log.warning("%s=%r is not a HH:MM-HH:MM window; using the default window %s", WINDOW_ENV, raw, WINDOW_DEFAULT)
        d0, d1 = WINDOW_DEFAULT.split("-")
        start, end = _parse_hhmm(d0), _parse_hhmm(d1)
    return start, end


def _in_window(moment: datetime, start: dtime, end: dtime) -> bool:
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    t = moment.time().replace(second=0, microsecond=0)
    if start < end:
        return start <= t < end
    return t >= start or t < end         # wraps midnight


def presentation_margin_minutes() -> int:
    """``CATALOGUE_PRESENTATION_MARGIN_MIN`` (default 60), never negative."""
    raw = str(os.getenv(PRESENTATION_MARGIN_ENV, "") or "").strip()
    try:
        minutes = int(raw) if raw else PRESENTATION_MARGIN_DEFAULT_MIN
    except ValueError:
        minutes = PRESENTATION_MARGIN_DEFAULT_MIN
    return max(0, minutes)


def catalogue_window_open(now: Optional[datetime] = None, margin_minutes: int = 0) -> bool:
    """Whether catalogue GENERATIONS may be claimed right now.

    ``CATALOGUE_WINDOW_UTC`` is ``HH:MM-HH:MM`` in UTC (default 20:00-05:00,
    the hours with zero real-user generations in 60 days); a window that
    crosses midnight wraps. ``always`` disables the gate (a SUPERVISED pilot
    under the founder's eye — nothing else, because with it a kit renders
    into users' hours). An unparseable value — a zero-width ``HH:MM-HH:MM``
    included — keeps the DEFAULT window rather than opening the floodgates:
    a typo must fail closed, because a kit batch during users' hours is a
    total image outage for them.

    ``margin_minutes`` asks that the window still be open that many minutes
    from now: the kit lane passes the presentation margin before claiming a
    render that runs for the better part of an hour."""
    raw = str(os.getenv(WINDOW_ENV, "") or "").strip().lower() or WINDOW_DEFAULT
    if raw == WINDOW_ALWAYS:
        return True
    start, end = _window_bounds()
    moment = now or datetime.now(timezone.utc)
    if not _in_window(moment, start, end):
        return False
    # The whole span must stay inside: a margin longer than the window itself
    # can never be honoured, and says so by returning False.
    return margin_minutes <= 0 or _window_minutes_left(moment, start, end) >= margin_minutes


def _window_minutes_left(moment: datetime, start: dtime, end: dtime) -> float:
    """Minutes from ``moment`` to the window's end (the window is open at ``moment``)."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    close = moment.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if close <= moment:
        close += timedelta(days=1)
    return (close - moment).total_seconds() / 60.0


def grade_number(grade: object) -> Optional[int]:
    """The first integer in a grade label ("9", "Class 9", "Stage 7"), or None."""
    m = re.search(r"\d+", clean_heading(grade))
    return int(m.group(0)) if m else None


def level_for_grade(grade: object) -> str:
    """Decision 14, on the analyzer's vocabulary (agent2_analysis.models
    .DifficultyLevel): grade ≤ 5 → primary_school (the spec's "elementary"),
    6-8 → middle_school, 9 and above → high_school. No grade → the worker's
    DEFAULT_LEVEL, middle_school."""
    n = grade_number(grade)
    if n is None:
        return LEVEL_MIDDLE
    if n <= 5:
        return LEVEL_PRIMARY
    if n <= 8:
        return LEVEL_MIDDLE
    return LEVEL_HIGH


def student_band_for(grade: object) -> Optional[str]:
    """The roster age band the scene engine casts the student from. Best-effort:
    the engine module is heavy and the band is informational here (the cast
    itself recomputes it from the book grade)."""
    try:
        from spike.scene_engine.whiteboard import student_band_for_grade
        return student_band_for_grade(grade)
    except Exception:  # noqa: BLE001
        return None


def header_lines(mappings: Iterable[Mapping]) -> list[str]:
    """Decision 10: one line per curriculum, in mapping order.

    Objective-coded curricula (Cambridge: ``7Bs.01``) list their codes —
    ``Cambridge Lower Secondary Science 0893 · 7Bs.01, 7Bs.02``; content-coded
    ones (CBSE units/chapters/topics) name the class and the node —
    ``CBSE Science (Class 6-10) · Class 9 · Cell - Basic Unit of life``. The
    app composes the same lines in TypeScript for composed worksheets
    (src/utils/catalogue/kit.ts curriculumHeaderLines); keep the two in step."""
    groups: dict[str, dict] = {}
    for m in mappings:
        cur = m.curriculum or {}
        cid = str(cur.get("id") or cur.get("code") or "?")
        g = groups.setdefault(cid, {"name": clean_heading(cur.get("name")) or clean_heading(cur.get("code")) or "?",
                                    "codes": [], "content": []})
        if node_kind(m.node) == "objective":
            if m.code and m.code not in g["codes"]:
                g["codes"].append(m.code)
        else:
            title = clean_heading(m.node.get("title")) or m.code
            grade = m.grade
            label = f"Class {grade} · {title}" if grade else title
            if label and label not in g["content"]:
                g["content"].append(label)
    lines: list[str] = []
    for g in groups.values():
        pieces = [g["name"]]
        if g["codes"]:
            pieces.append(", ".join(g["codes"]))
        pieces.extend(g["content"])
        lines.append(" · ".join(pieces))
    return lines


def synthetic_book(topic: dict, grade: Optional[str], curriculum_name: Optional[str], language: str) -> dict:
    """Decision 14's book dict. ``id`` is None ON PURPOSE: every reader keyed by
    book id (analysis cache, chapter grounding, heal, OCR, measured parts,
    tutor warm cache) is bypassed on the catalogue path, and a None here
    makes a missed bypass fail loudly instead of writing under "None"."""
    return {"id": None, "title": clean_heading(topic.get("title")) or "Topic",
            "grade": grade or None, "subject": clean_heading(topic.get("subject")) or None,
            "curriculum": curriculum_name or None, "language": language, "author": SYNTHETIC_AUTHOR}


def student_voice_for(teacher_voice: Optional[str], language: str,
                      requested: Optional[str] = None) -> tuple[Optional[str], bool]:
    """``(student voice id, fallback)``. The requested id wins when the registry
    knows it as a student voice; else the OTHER gender to the teacher's voice
    for the language; ``(None, True)`` when the language has no student
    entry — the composer then keeps today's Edge student."""
    from shared.tts.registry import STUDENT_ROLE, get_voice, student_voice_id_for
    req = get_voice(requested)
    if req is not None and req.role == STUDENT_ROLE:
        return req.voice_id, False
    teacher = get_voice(teacher_voice)
    other = "m" if (teacher is not None and teacher.gender == "f") else "f"
    vid = student_voice_id_for(language, other)
    return (vid, False) if vid else (None, True)


def default_teacher_voice(language: str, teacher_avatar: Optional[str]) -> str:
    """``g-{lang}-{f|m}`` from the avatar gender token; a language without a
    Google pair falls back to the registry's premium default for it."""
    from shared.tts.registry import default_premium_voice_id_for, default_voice_id_for, get_voice
    gender = "m" if str(teacher_avatar or "").strip().lower() == "male" else "f"
    cand = f"g-{language}-{gender}"
    if get_voice(cand) is not None:
        return cand
    return default_premium_voice_id_for(language, gender=gender, provider="google") or default_voice_id_for(language, gender)


# ── the recap / outro segments (decision 3) ────────────────────────────

_STRINGS = {
    "en": {
        "recap_heading": "Recap · Part {part} of {total}",
        "recap": "Welcome back to part {part} of {total}. Last time we covered {sections}. Let us build on that.",
        "recap_plain": "Welcome back to part {part} of {total}. Let us pick up where we left off.",
        "outro_heading": "Next · Part {next} of {total}",
        "outro": "That brings part {part} of {total} to a close. In part {next} we continue with {sections}. See you there.",
        "outro_plain": "That brings part {part} of {total} to a close. Part {next} continues from here. See you there.",
        "and": " and ",
    },
    "ar": {
        "recap_heading": "مراجعة · الجزء {part} من {total}",
        "recap": "أهلاً بكم مجدداً في الجزء {part} من {total}. في المرة السابقة تناولنا {sections}. لنبنِ على ذلك.",
        "recap_plain": "أهلاً بكم مجدداً في الجزء {part} من {total}. لنتابع من حيث توقفنا.",
        "outro_heading": "التالي · الجزء {next} من {total}",
        "outro": "بهذا ينتهي الجزء {part} من {total}. في الجزء {next} نتابع مع {sections}. إلى اللقاء هناك.",
        "outro_plain": "بهذا ينتهي الجزء {part} من {total}. الجزء {next} يتابع من هنا. إلى اللقاء هناك.",
        "and": " و",
    },
    "fr": {
        "recap_heading": "Rappel · Partie {part} sur {total}",
        "recap": "Bon retour dans la partie {part} sur {total}. La dernière fois, nous avons vu {sections}. Continuons sur cette base.",
        "recap_plain": "Bon retour dans la partie {part} sur {total}. Reprenons là où nous nous étions arrêtés.",
        "outro_heading": "À suivre · Partie {next} sur {total}",
        "outro": "Voilà qui clôt la partie {part} sur {total}. Dans la partie {next}, nous poursuivrons avec {sections}. À très bientôt.",
        "outro_plain": "Voilà qui clôt la partie {part} sur {total}. La partie {next} reprend ici. À très bientôt.",
        "and": " et ",
    },
    "es": {
        "recap_heading": "Repaso · Parte {part} de {total}",
        "recap": "Bienvenidos de nuevo a la parte {part} de {total}. La última vez vimos {sections}. Sigamos a partir de ahí.",
        "recap_plain": "Bienvenidos de nuevo a la parte {part} de {total}. Retomemos donde lo dejamos.",
        "outro_heading": "Siguiente · Parte {next} de {total}",
        "outro": "Así termina la parte {part} de {total}. En la parte {next} continuaremos con {sections}. Nos vemos allí.",
        "outro_plain": "Así termina la parte {part} de {total}. La parte {next} continúa desde aquí. Nos vemos allí.",
        "and": " y ",
    },
}


def _strings(language: str) -> dict:
    return _STRINGS.get(str(language or "en").split("-")[0].lower(), _STRINGS["en"])


def _join_sections(sections: Iterable[str], language: str, cap: int = 3) -> str:
    names = [t for t in (clean_heading(s) for s in sections) if t][:cap]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + _strings(language)["and"] + names[-1]


def _spoken_seconds(text: str) -> int:
    return max(4, round(len(text.split()) / NARRATION_WPM * 60))


def _segment(seg_id: str, seg_type: str, heading: str, text: str, points: list[str]) -> dict:
    """A ScriptSegment-shaped dict with NO scene: the composer's whiteboard
    fallback draws the heading and points and the teacher speaks the text.
    ``dialogue`` is None so the single-voice path (the teacher's voice) reads
    it — these lines are the teacher's alone."""
    return {
        "segment_id": seg_id, "type": seg_type, "text": text, "elevenlabs_text": text,
        "slide_heading": heading, "slide_points": points, "slide_visual": None,
        "visual_request": None, "visual_action": None, "scene": None, "scene_assets": None,
        "dialogue": None, "pause_for_question": False,
        "estimated_duration_seconds": _spoken_seconds(text),
    }


def recap_segment(part: int, total: int, prev_sections: Iterable[str], language: str = "en") -> dict:
    """The opening of every part but the first: what the earlier parts taught."""
    s = _strings(language)
    prev = list(prev_sections or [])
    joined = _join_sections(prev, language)
    text = (s["recap"] if joined else s["recap_plain"]).format(part=part, total=total, sections=joined)
    return _segment(RECAP_ID, "activate", s["recap_heading"].format(part=part, total=total), text,
                    [clean_heading(t) for t in prev if clean_heading(t)][:3])


def outro_segment(part: int, total: int, next_sections: Iterable[str], language: str = "en") -> dict:
    """The close of every part but the last: what the next part will teach."""
    s = _strings(language)
    nxt = list(next_sections or [])
    joined = _join_sections(nxt, language)
    text = (s["outro"] if joined else s["outro_plain"]).format(part=part, total=total, next=part + 1, sections=joined)
    return _segment(OUTRO_ID, "preview", s["outro_heading"].format(next=part + 1, total=total), text,
                    [clean_heading(t) for t in nxt if clean_heading(t)][:3])


# ── database edges ─────────────────────────────────────────────────────


def _rows(res) -> list[dict]:
    return list(getattr(res, "data", None) or [])


def load_kit(sb, kit_id: str) -> Optional[dict]:
    rows = _rows(sb.table("topic_kits").select("*").eq("id", kit_id).limit(1).execute())
    return rows[0] if rows else None


def load_rendered_figures(sb, article_id: str) -> list[dict]:
    return _rows(sb.table("article_figures").select("id,figure_key,caption,status,labels,visual_asset_id")
                 .eq("article_id", article_id).execute())


def curriculum_header_lines(sb, topic_id: str) -> list[str]:
    """Decision 10, from the live mappings (topic_curriculum_map → nodes → curricula)."""
    return header_lines(load_mappings(sb, topic_id))


def _guarded_status(sb, table: str, row_id: str, from_status: str, to_status: str,
                    extra: Optional[dict] = None) -> bool:
    """``UPDATE … SET status = to WHERE id = row AND status = from``; True when a
    row moved. The guard is the whole point: a kit the reviewer already
    rejected, or a topic already in review, must never be relabelled by a
    worker finishing late."""
    payload = {"status": to_status, **(extra or {})}
    res = sb.table(table).update(payload).eq("id", row_id).eq("status", from_status).execute()
    return bool(_rows(res))


def mark_kit_failed(sb, kit_id: Optional[str], note: str) -> bool:
    """``topic_kits`` generating → failed, guarded and best-effort. The one
    implementation lives in ``worker.client`` so the crash-reaper — which
    must stay importable without this package — writes the same row the
    same way when it auto-fails a kit's poison-pill job."""
    return db.mark_kit_failed(sb, kit_id, note)


# ── yielding to real users while a kit renders (never-starve, DURING) ────


def _env_float(name: str, default: float) -> float:
    try:
        v = float(str(os.getenv(name, "") or "").strip() or default)
        return v if v >= 0 else default
    except (TypeError, ValueError):
        return default


def yield_poll_seconds() -> float:
    return _env_float(YIELD_POLL_ENV, YIELD_POLL_DEFAULT_S)


def yield_max_seconds() -> float:
    return _env_float(YIELD_MAX_ENV, YIELD_MAX_DEFAULT_S)


class ContentionProbe:
    """"Is a job a real user is waiting on live?" — ``catalogue.figures.
    builder_queued`` behind a short cache and a lock.

    The scene engine asks this from EVERY render thread before every image
    generation (eight at once, per part); the job thread's Supabase client is
    not built for eight concurrent readers, and eight identical queries a
    second would be their own load. So one reader at a time, and an answer is
    reused for ``ttl`` seconds — ten seconds of staleness against a render
    that pauses for a teacher's whole job is nothing. A probe that cannot
    read the queue says "contended" (the safe answer: a kit waits, a teacher
    never does)."""

    def __init__(self, sb, ttl: Optional[float] = None, reader: Optional[Callable[[object], bool]] = None):
        self._sb = sb
        self._ttl = max(0.0, float(PROBE_TTL_S if ttl is None else ttl))
        self._reader = reader
        self._lock = threading.Lock()
        self._at: Optional[float] = None
        self._value = False
        self.reads = 0

    def _read(self) -> bool:
        if self._reader is None:
            from catalogue.figures import builder_queued
            self._reader = builder_queued
        return bool(self._reader(self._sb))

    def __call__(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if self._at is not None and now - self._at < self._ttl:
                return self._value
            try:
                self._value = self._read()
            except Exception as exc:  # noqa: BLE001 — unreadable queue = contended, never silent
                log.warning("contention probe could not read the queue (%s); treating it as contended", exc)
                self._value = True
            self._at = now
            self.reads += 1
            return self._value

    def invalidate(self) -> None:
        with self._lock:
            self._at = None


def yield_to_users(probe: Callable[[], bool], *, poll_seconds: Optional[float] = None,
                   max_seconds: Optional[float] = None, on_wait: Optional[Callable[[float], None]] = None,
                   sleep: Optional[Callable[[float], None]] = None,
                   clock: Optional[Callable[[], float]] = None) -> bool:
    """Block while ``probe()`` says a user builder is live. True when the
    queue cleared (or was never contended); False when ``max_seconds`` passed
    with a user still waiting — the caller then SKIPS the contended work
    rather than doing it over a teacher's render. ``on_wait(elapsed)`` is
    called once per poll so the job's stage can say why it is paused.
    ``sleep`` / ``clock`` default to the real ones (resolved here, not at
    definition, so a test that patches ``time`` is honoured)."""
    sleep = time.sleep if sleep is None else sleep
    clock = time.monotonic if clock is None else clock
    poll = yield_poll_seconds() if poll_seconds is None else max(0.0, float(poll_seconds))
    limit = yield_max_seconds() if max_seconds is None else max(0.0, float(max_seconds))
    if not probe():
        return True
    started = clock()
    while True:
        elapsed = clock() - started
        if on_wait is not None:
            on_wait(elapsed)
        if elapsed >= limit:
            log.error("a user builder has been live for %.0fs; the kit gives this contended call up", elapsed)
            return False
        sleep(poll)
        if not probe():
            return True


# ── prepare ─────────────────────────────────────────────────────────────


@dataclass
class Prepared:
    """Everything the shared build needs in place of the book prelude."""

    topic_id: str
    topic: dict
    language: str
    level: str
    grade: Optional[str]
    band: Optional[str]
    book: dict
    curriculum_header: list[str]
    kit_id: Optional[str] = None
    kit: Optional[dict] = None
    article_id: Optional[str] = None
    article: Optional[dict] = None
    chapter: Optional[dict] = None
    chunks: list[dict] = field(default_factory=list)
    section_ids: dict = field(default_factory=dict)
    question_set_id: Optional[str] = None
    narration_style: str = NARRATION_STYLE
    teacher_avatar: str = "female"
    tts_voice: Optional[str] = None
    student_voice: Optional[str] = None
    student_voice_fallback: bool = False
    # "Is a user builder live?" — set by the worker's catalogue branch; the
    # presentation loop polls it between parts (yield_to_users).
    contention_probe: Optional[Callable[[], bool]] = None

    @property
    def chapter_title(self) -> str:
        return (self.chapter or {}).get("title") or self.book.get("title") or "Topic"

    @property
    def synthetic_book_id(self) -> str:
        """A stable, filesystem-safe id for the per-lesson working dirs and
        the scene-engine cast seed (``storage/analysis/<id>``, the video and
        slide dirs). Not a books row — nothing persists under it."""
        return f"catalogue-{self.topic_id}"


def prepare(sb, gen: dict) -> Prepared:
    """See the module doc. Raises CatalogueRefused before any model call."""
    params = dict(gen.get("params") or {})
    if not is_catalogue(params):
        raise CatalogueRefused("not a catalogue generation")
    topic_id = str(params.get("topic_id") or "").strip()
    if not topic_id:
        raise CatalogueRefused("catalogue generation has no topic_id")
    topic = load_topic(sb, topic_id)
    if topic is None:
        raise CatalogueRefused(f"topic {topic_id} not found")
    language = str(params.get("language") or topic.get("language") or "en").strip().lower() or "en"
    question_set_id = str(params.get("question_set_id") or "").strip() or None
    kind = str(gen.get("kind") or "").strip()
    if kind and kind not in KIT_KINDS:
        raise CatalogueRefused(f"kind {kind!r} is not a catalogue kind")

    kit_id = str(params.get("kit_id") or "").strip() or None
    kit = article = None
    article_id = str(params.get("article_id") or "").strip() or None
    if question_set_id is not None:
        # A question_set_id switches the kit/article gates OFF (its items were
        # approved one by one), so it may only ever arrive on the one kind the
        # composer renders, and never together with a kit — otherwise a row
        # with a set id and kind=presentation would reach the model on an
        # empty chapter with every gate of decision 13 skipped.
        if kind and kind != COMPOSED_KIND:
            raise CatalogueRefused(f"question_set_id is only rendered as a {COMPOSED_KIND}, not a {kind}")
        if kit_id:
            raise CatalogueRefused("a composed worksheet belongs to no kit: question_set_id and kit_id together")
    else:
        # A KIT generation: the kit row must exist and be alive, and the
        # article it cites must be approved. A composed worksheet
        # (question_set_id) has neither — its items were approved one by one.
        if not kit_id:
            raise CatalogueRefused("catalogue generation has no kit_id")
        kit = load_kit(sb, kit_id)
        if kit is None:
            raise CatalogueRefused(f"kit {kit_id} not found")
        if str(kit.get("status") or "") == KIT_REJECTED:
            raise CatalogueRefused(f"kit {kit_id} was rejected; nothing is built for it")
        article_id = article_id or (str(kit.get("article_id") or "").strip() or None)
        if not article_id:
            mark_kit_failed(sb, kit_id, "kit names no article")
            raise CatalogueRefused(f"kit {kit_id} names no article")
        article = load_article(sb, article_id)
        if article is None:
            mark_kit_failed(sb, kit_id, f"article {article_id} not found")
            raise CatalogueRefused(f"article {article_id} not found")
        status = str(article.get("status") or "")
        if status != ARTICLE_APPROVED:
            mark_kit_failed(sb, kit_id, f"article {article_id} is {status or 'unreviewed'}, not approved")
            raise CatalogueRefused(f"article {article_id} is {status or 'unreviewed'}, not approved — "
                                   "a kit is built only from an approved article")

    mappings = load_mappings(sb, topic_id)
    # The article's depth node wins when it names one (it was approved with
    # that depth); else the topic's; else the deepest mapped grade.
    depth_topic = dict(topic)
    if article and article.get("depth_node_id"):
        depth_topic["depth_node_id"] = article["depth_node_id"]
    depth = pick_depth_node(depth_topic, mappings)
    grade = clean_heading((depth or {}).get("grade")) or None
    level = level_for_grade(grade)
    curriculum_name = None
    for m in mappings:
        if depth is not None and m.node.get("id") == depth.get("id"):
            curriculum_name = clean_heading((m.curriculum or {}).get("name")) or None
            break
    if curriculum_name is None and mappings:
        curriculum_name = clean_heading((mappings[0].curriculum or {}).get("name")) or None
    header = list(params.get("curriculum_header") or []) or header_lines(mappings)

    teacher_avatar = str(params.get("teacher_avatar") or "female").strip().lower() or "female"
    tts_voice = str(params.get("tts_voice") or "").strip() or default_teacher_voice(language, teacher_avatar)
    student_voice, fallback = student_voice_for(tts_voice, language, params.get("student_voice"))

    prepared = Prepared(
        topic_id=topic_id, topic=topic, language=language, level=level, grade=grade,
        band=student_band_for(grade), book=synthetic_book(topic, grade, curriculum_name, language),
        curriculum_header=header, kit_id=kit_id, kit=kit, article_id=article_id, article=article,
        question_set_id=question_set_id, teacher_avatar=teacher_avatar, tts_voice=tts_voice,
        student_voice=student_voice, student_voice_fallback=fallback,
    )
    if article is not None:
        figures = load_rendered_figures(sb, article_id) if article_id else []
        prepared.chapter = article_to_chapter(article, figures)
        prepared.chunks = build_chapter_parts(prepared.chapter, max_words=part_words_budget())
        prepared.section_ids = section_ids_by_heading(article)
    return prepared


# ── after_generation (decision 6) ──────────────────────────────────────


def _kit_generation_ids(kit: dict) -> list[str]:
    ids = [kit.get("presentation_generation_id")]
    docs = kit.get("doc_generation_ids") if isinstance(kit.get("doc_generation_ids"), dict) else {}
    ids.extend(docs.values())
    return [str(i) for i in ids if i]


def _statuses(sb, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = _rows(sb.table("generations").select("id,status").in_("id", ids).execute())
    return {str(r.get("id")): str(r.get("status") or "") for r in rows}


def _inherited_params(params: dict) -> dict:
    return {k: params[k] for k in _INHERITED_PARAMS if k in params}


def write_timestamps(sb, kit: dict, parts: list[dict]) -> dict:
    """Merge the rendered parts' chapters, clips and plan into the kit row.
    ``parts`` = ``[{part, chapters, clips, plan}]`` from the presentation loop."""
    chapters = ts.merge_by_part(kit.get("chapters") or [],
                                [{"part": p["part"], "chapters": p.get("chapters") or []} for p in parts])
    clips = ts.merge_by_part(kit.get("clips") or [], [c for p in parts for c in (p.get("clips") or [])])
    plan = ts.merge_by_part(kit.get("part_plan") or [], [p["plan"] for p in parts if p.get("plan")])
    patch = {"chapters": chapters, "clips": clips, "part_plan": plan}
    sb.table("topic_kits").update(patch).eq("id", kit["id"]).execute()
    kit.update(patch)
    return patch


def _generation(sb, gen_id: str) -> Optional[dict]:
    rows = _rows(sb.table("generations").select("id,status,params").eq("id", gen_id).limit(1).execute())
    return rows[0] if rows else None


def _same_clips(a: object, b: object) -> bool:
    """Two clip lists cite the same video when every (part, start, end) agrees
    — labels and purposes are prose the plan does not depend on."""
    def key(clips):
        return sorted((int(c.get("part") or 0), round(float(c.get("start") or 0), 1), round(float(c.get("end") or 0), 1))
                      for c in (clips if isinstance(clips, list) else []) if isinstance(c, dict))
    try:
        return key(a) == key(b)
    except (TypeError, ValueError):
        return False


def insert_lesson_plan(sb, gen: dict, kit: dict) -> Optional[str]:
    """The kit's ``lesson_plan`` generation, inserted ONCE the video exists so
    its params can carry the clips (decision 1). Idempotent on a retry that
    reproduces the same video: a lesson_plan the kit already references is
    KEPT when it is live or done AND its ``params.clips`` cite the clips the
    kit now carries. A re-rendered presentation measures new clip
    boundaries (TTS durations are not deterministic), and a plan whose Mode B
    cites ``[mm:ss–mm:ss]`` from the OLD ones would point into a video that
    no longer has them (review finding): a plan still ``queued`` has its
    clips patched in place (nothing has read them yet); one already being or
    been built is replaced by a new row, and the kit points at the new one."""
    docs = dict(kit.get("doc_generation_ids") or {}) if isinstance(kit.get("doc_generation_ids"), dict) else {}
    clips = list(kit.get("clips") or [])
    existing = docs.get("lesson_plan")
    if existing:
        row = _generation(sb, str(existing))
        status = str((row or {}).get("status") or "")
        old_params = dict((row or {}).get("params") or {}) if isinstance((row or {}).get("params"), dict) else {}
        if status == "queued":
            if not _same_clips(old_params.get("clips"), clips):
                log.info("kit %s: lesson_plan %s is still queued; its clips are refreshed", kit["id"], existing)
                db.merge_generation_params(sb, str(existing), {"clips": clips})
            return str(existing)
        if status in ("processing", "done") and _same_clips(old_params.get("clips"), clips):
            return str(existing)
        if status in ("processing", "done"):
            log.warning("kit %s: lesson_plan %s cites clips the re-rendered video no longer has; a new plan is inserted",
                        kit["id"], existing)
    params = _inherited_params(dict(gen.get("params") or {}))
    params.update({"catalogue": True, "kit_id": kit["id"], "lesson_modes": True, "clips": clips})
    params.setdefault("curriculum_header", [])
    row = {"owner_id": gen.get("owner_id"), "kind": "lesson_plan", "status": "queued",
           "book_id": None, "chapter_ref": None, "params": params}
    ins = sb.table("generations").insert(row).execute()
    new_id = str((_rows(ins) or [{}])[0].get("id") or "")
    if not new_id:
        raise RuntimeError("lesson_plan insert returned no id")
    repoint_kit(sb, kit, "lesson_plan", new_id, replaces=str(existing) if existing else None)
    return new_id


def _rpc_rows(res) -> list[dict]:
    """A function returning a row type comes back from PostgREST as ONE
    object, not a list; a set-returning one as a list. Either way, rows."""
    data = getattr(res, "data", None)
    if isinstance(data, dict):
        return [data]
    return [r for r in (data or []) if isinstance(r, dict)]


def _rpc_missing(exc: BaseException) -> bool:
    """Is this failure "the database has no such function" — a pre-0115
    database (PGRST202 from the schema cache, 42883 from Postgres) or a test
    double without ``rpc`` — as opposed to the function REFUSING?"""
    if isinstance(exc, AttributeError):
        return True
    code = str(getattr(exc, "code", "") or "")
    text = f"{getattr(exc, 'message', '')} {exc}"
    return code in ("PGRST202", "42883") or "Could not find the function" in text


def repoint_kit(sb, kit: dict, kind: str, new_id: str, *, replaces: Optional[str]) -> None:
    """Point the kit at ``new_id`` for ``kind`` through ``repoint_kit_generation``
    (app migration 0115): ONE merged statement (``doc_generation_ids ||
    {kind: id}``) with a compare-and-swap on the id being replaced. The
    portal's Retry writes the same pointers the same way, so neither side can
    lose a key the other merged in between — the read-modify-write this
    replaced could drop a Retry's worksheet pointer while it merged the
    lesson_plan's (review of PR #40). A refused swap (23514: the pointer moved,
    or the kit left ``generating``) is a loud failure, never an overwrite; the
    presentation lifecycle records it on the kit for the portal's Retry. Only
    a database WITHOUT the function falls back to the in-Python merge, with a
    warning — the deploy order is 0115 first (README), so that path exists for
    the transition, not for production."""
    args = {"p_kit": kit["id"], "p_kind": kind, "p_generation": new_id, "p_replaces": replaces}
    try:
        res = sb.rpc("repoint_kit_generation", args).execute()
    except Exception as exc:  # noqa: BLE001 — classified below
        if not _rpc_missing(exc):
            raise RuntimeError(
                f"kit {kit['id']}: could not point {kind} at {new_id} (replacing {replaces or 'nothing'}): {exc}"
            ) from exc
        log.warning("repoint_kit_generation is not in this database (apply app migration 0115); "
                    "merging the %s pointer of kit %s in Python", kind, kit["id"])
        if kind == "presentation":
            patch: dict = {"presentation_generation_id": new_id}
        else:
            docs = dict(kit.get("doc_generation_ids") or {}) if isinstance(kit.get("doc_generation_ids"), dict) else {}
            docs[kind] = new_id
            patch = {"doc_generation_ids": docs}
        sb.table("topic_kits").update(patch).eq("id", kit["id"]).execute()
        kit.update(patch)
        return
    rows = _rpc_rows(res)
    if rows:
        kit.update({k: rows[0][k] for k in ("presentation_generation_id", "doc_generation_ids") if k in rows[0]})
    elif kind == "presentation":
        kit["presentation_generation_id"] = new_id
    else:
        docs = dict(kit.get("doc_generation_ids") or {}) if isinstance(kit.get("doc_generation_ids"), dict) else {}
        docs[kind] = new_id
        kit["doc_generation_ids"] = docs


def sync_kit_completion(sb, kit: dict) -> bool:
    """Kit → in_review (and topic → in_review) when EVERY referenced generation
    is done. Both guarded from ``generating``; returns whether the kit moved.

    A kit with a presentation but no ``lesson_plan`` reference is never
    complete: the plan is inserted by the presentation's own lifecycle, so
    its absence means that lifecycle has not run yet — whatever the other
    rows say (the concurrency window the module doc describes)."""
    ids = _kit_generation_ids(kit)
    if not ids:
        return False
    docs = kit.get("doc_generation_ids") if isinstance(kit.get("doc_generation_ids"), dict) else {}
    if kit.get("presentation_generation_id") and not docs.get("lesson_plan"):
        return False
    statuses = _statuses(sb, ids)
    if any(statuses.get(i) != "done" for i in ids):
        return False
    moved = _guarded_status(sb, "topic_kits", kit["id"], KIT_GENERATING, KIT_IN_REVIEW)
    if moved and kit.get("topic_id"):
        _guarded_status(sb, "topics", str(kit["topic_id"]), TOPIC_GENERATING, TOPIC_IN_REVIEW)
    return moved


def _result(kit_id: Optional[str]) -> dict:
    return {"kit": kit_id, "timestamps": False, "lesson_plan": None, "in_review": False, "failed": False}


def record_presentation(sb, gen: dict, kit_id: Optional[str], outcome: dict) -> dict:
    """Decision 6, FIRST half — run by the worker BEFORE it marks the
    presentation's own generation done: the measured timestamps onto the kit
    and the ``lesson_plan`` generation into the kit's references, so that
    no sibling thread can ever see "every referenced generation done" while
    the plan is missing. ``outcome`` = ``{"kind": "presentation", "parts":
    [{part, chapters, clips, plan}]}``. Never raises: the video exists and
    was uploaded, so a fault here is recorded on the kit (``failed`` +
    notes) for the portal's Retry, and the generation still finishes."""
    result = _result(kit_id)
    if not kit_id or str(outcome.get("kind") or gen.get("kind") or "") != "presentation":
        return result
    try:
        kit = load_kit(sb, kit_id)
        if kit is None:
            log.error("kit %s vanished before its lifecycle ran", kit_id)
            return result
        parts = [p for p in (outcome.get("parts") or []) if isinstance(p, dict) and p.get("part") is not None]
        if parts:
            write_timestamps(sb, kit, parts)
            result["timestamps"] = True
        result["lesson_plan"] = insert_lesson_plan(sb, gen, kit)
    except Exception as exc:  # noqa: BLE001
        log.exception("kit %s lifecycle failed after presentation", kit_id)
        result["failed"] = mark_kit_failed(sb, kit_id, f"kit lifecycle failed after presentation: {exc}")
        result["error"] = str(exc)
    return result


def after_generation(sb, gen: dict, kit_id: Optional[str], outcome: dict,
                     recorded: Optional[dict] = None) -> dict:
    """Decision 6, SECOND half — after the generation is finished. ``outcome``
    = ``{"status": "done"|"failed", "kind", "error"?, "parts"?}``. A failure
    marks the kit failed; a success checks completion. ``recorded`` is the
    ``record_presentation`` result the worker obtained before finishing the
    job; when it is None (a caller that did not split the halves) the
    presentation's recording runs here, as it did before the split. Never
    raises on the success path."""
    result = _result(kit_id)
    if not kit_id:
        return result                    # a composed worksheet: no kit lifecycle
    kind = str(outcome.get("kind") or gen.get("kind") or "")
    if str(outcome.get("status") or "") != "done":
        note = f"{kind or 'generation'} failed: {str(outcome.get('error') or 'unknown error')}"
        result["failed"] = mark_kit_failed(sb, kit_id, note)
        return result
    if kind == "presentation":
        if recorded is None:
            recorded = record_presentation(sb, gen, kit_id, {"kind": kind, "parts": outcome.get("parts")})
        result.update({k: recorded[k] for k in ("timestamps", "lesson_plan", "failed") if k in recorded})
        if recorded.get("error"):
            result["error"] = recorded["error"]
        if recorded.get("failed") or recorded.get("error"):
            return result                # the kit reads failed; nothing to complete
    try:
        kit = load_kit(sb, kit_id)
        if kit is None:
            log.error("kit %s vanished before its lifecycle ran", kit_id)
            return result
        result["in_review"] = sync_kit_completion(sb, kit)
    except Exception as exc:  # noqa: BLE001
        log.exception("kit %s lifecycle failed after %s", kit_id, kind)
        result["failed"] = mark_kit_failed(sb, kit_id, f"kit lifecycle failed after {kind}: {exc}")
        result["error"] = str(exc)
    return result


__all__ = ["CatalogueRefused", "ContentionProbe", "NARRATION_STYLE", "PAUSED_FOR_USERS", "Prepared",
           "after_generation", "catalogue_window_open", "curriculum_header_lines", "header_lines", "is_catalogue",
           "level_for_grade", "mark_kit_failed", "outro_segment", "part_words_budget", "prepare",
           "presentation_margin_minutes", "recap_segment", "record_presentation", "student_voice_for",
           "sync_kit_completion", "synthetic_book", "yield_to_users"]
