"""Lesson plan generator → editable .docx.

Teaching modes (catalogue kits, Phase 3 decision 11, 2026-09-06): when
``params.lesson_modes`` is truthy the plan is written for a lesson that HAS a
video, and the prompt asks for three extra sections rendered after the lesson
flow as "Mode A — Full lesson" (watch the whole video → activity → worksheet),
"Mode B — In-class micro-clips" (2–4 of the supplied clips, each with a task,
cited ``[mm:ss–mm:ss]``) and "Mode C — Flipped / pre-watch" (watch at home;
class = discussion prompts + activity). The clips come from
``params.clips [{part, start, end, label, purpose}]`` — the worker's
timestamp pass writes them, so the citations are REAL positions in the
rendered video, computed here from start/end and never typed by the model
(a model-typed timestamp is a guess). Without the flag the prompt and the
document are byte-for-byte what they were: textbook lesson plans have no
video to cite.

``params.curriculum_header`` (decision 10) renders as the curriculum block
under the subtitle, like every catalogue document.
"""

from __future__ import annotations

import logging
from pathlib import Path

from docgen import docx_builder as dx

logger = logging.getLogger("worker")

PROMPT = """You are an expert teacher writing a classroom-ready LESSON PLAN based on the chapter provided above.

Total lesson duration: {duration} minutes.
Return ONLY valid JSON in exactly this shape:
{{
  "title": "Lesson plan title",
  "duration_minutes": {duration},
  "learning_objectives": ["..."],
  "materials": ["..."],
  "key_vocabulary": [{{"term": "...", "definition": "..."}}],
  "lesson_flow": [
    {{"phase": "Introduction / Hook", "minutes": 5, "teacher_does": "...", "students_do": "..."}}
  ],
  "assessment": ["..."]{homework_field}{diff_field}{modes_field}
}}
Make it specific to the chapter content. 4-6 lesson_flow phases that sum to roughly {duration} minutes.{modes_block}"""

# The three extra fields, spliced into the JSON shape only when modes are on.
MODES_FIELD = """,
  "full_lesson": {"steps": ["Watch the whole video ...", "Then the activity ...", "Then the worksheet ..."]},
  "micro_clip": [{"clip": 1, "task": "What the class does with this clip"}],
  "flipped": {"pre_watch": "What students watch and note at home", "discussion_prompts": ["..."], "activity": "The in-class activity"}"""

MODES_BLOCK = """

TEACHING MODES: this lesson has a VIDEO, cut into the clips below (cite a clip by its number only; the
timestamps are printed for you). Write three ways to teach with it:
- "full_lesson": 3-5 steps — the class watches the whole video, then does the activity, then the worksheet.
- "micro_clip": choose {min_clips}-{max_clips} of the clips, each with one concrete task the class does right after
  watching it (a question to answer, a prediction to check, a diagram to complete).
- "flipped": students watch the video at home ("pre_watch": what to watch and note), and class time is
  2-4 "discussion_prompts" plus one "activity".
CLIPS:
{clips}"""

MIN_CLIPS, MAX_CLIPS = 2, 4


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def mmss(seconds) -> str:
    """``mm:ss`` for a clip boundary; hours roll into the minutes (a 20-minute
    part never needs an hour field, and one format keeps the cite greppable)."""
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        total = 0
    return f"{total // 60:02d}:{total % 60:02d}"


def clip_cite(clip: dict) -> str:
    """``[mm:ss–mm:ss]`` from the clip's start/end — the one citation format
    the plan uses, so a teacher can scrub to it and a test can grep for it."""
    return f"[{mmss(clip.get('start'))}–{mmss(clip.get('end'))}]"


def usable_clips(params: dict) -> list[dict]:
    """``params.clips`` entries that carry a numeric start < end; anything
    else cannot be cited and is dropped."""
    out: list[dict] = []
    for c in (params or {}).get("clips") or []:
        if not isinstance(c, dict):
            continue
        try:
            start, end = float(c.get("start")), float(c.get("end"))
        except (TypeError, ValueError):
            continue
        if end > start >= 0:
            out.append({**c, "start": start, "end": end})
    return out


def _clip_label(clip: dict, many_parts: bool) -> str:
    label = str(clip.get("label") or "").strip()
    part = f"Part {clip.get('part')} · " if many_parts and clip.get("part") is not None else ""
    return f"{part}{clip_cite(clip)} {label}".strip()


def clips_block(clips: list[dict]) -> str:
    many = len({c.get("part") for c in clips}) > 1
    lines = []
    for i, c in enumerate(clips, 1):
        purpose = str(c.get("purpose") or "").strip()
        lines.append(f"{i}. {_clip_label(c, many)}" + (f" — {purpose}" if purpose else ""))
    return "\n".join(lines) or "(none)"


def pick_micro_clips(data: dict, clips: list[dict]) -> list[tuple[dict, str]]:
    """The (clip, task) pairs Mode B renders: the model's choices by clip
    number, unknown numbers and repeats dropped, capped at MAX_CLIPS. Fewer
    than MIN_CLIPS valid choices are topped up from the unused clips in order,
    each carrying its own authored ``purpose`` as the task — a mode with one
    clip is not a mode, and the purpose is teacher-written text, not a guess."""
    picked: list[tuple[dict, str]] = []
    used: set[int] = set()
    for item in (data.get("micro_clip") or []):
        if not isinstance(item, dict):
            continue
        n = _int(item.get("clip"), 0)
        if not (1 <= n <= len(clips)) or n in used:
            continue
        used.add(n)
        picked.append((clips[n - 1], str(item.get("task") or "").strip()))
        if len(picked) == MAX_CLIPS:
            break
    for n, clip in enumerate(clips, 1):
        if len(picked) >= MIN_CLIPS:
            break
        if n not in used:
            used.add(n)
            picked.append((clip, str(clip.get("purpose") or "").strip()))
    return picked


def _render_modes(doc, data: dict, clips: list[dict], language: str) -> None:
    letters = dx.letters(language)
    mode = dx._t("mode", language)
    many = len({c.get("part") for c in clips}) > 1

    dx.heading(doc, f"{mode} {letters[0]} — {dx._t('mode_full_lesson', language)}", 1)
    full = data.get("full_lesson") if isinstance(data.get("full_lesson"), dict) else {}
    steps = [s for s in (full.get("steps") or []) if str(s).strip()]
    if steps:
        dx.numbered(doc, steps)

    dx.heading(doc, f"{mode} {letters[1]} — {dx._t('mode_micro_clips', language)}", 1)
    for i, (clip, task) in enumerate(pick_micro_clips(data, clips), 1):
        dx.question(doc, f"{i}. {_clip_label(clip, many)}", first=(i == 1))
        if task:
            dx.para(doc, task)

    dx.heading(doc, f"{mode} {letters[2]} — {dx._t('mode_flipped', language)}", 1)
    flipped = data.get("flipped") if isinstance(data.get("flipped"), dict) else {}
    if flipped.get("pre_watch"):
        dx.labelled(doc, dx._t("pre_watch", language), str(flipped["pre_watch"]))
    prompts = [p for p in (flipped.get("discussion_prompts") or []) if str(p).strip()]
    if prompts:
        dx.para(doc, f"{dx._t('discussion_questions', language)}:", bold=True)
        dx.bullets(doc, prompts)
    if flipped.get("activity"):
        dx.labelled(doc, dx._t("activity", language), str(flipped["activity"]))


def build(book: dict, chapter: dict, analysis: dict, client, params: dict, out_dir: Path,
          template: str | None = None, language: str = "en") -> Path:
    p = params or {}
    try:
        duration = max(10, min(180, int(p.get("duration_minutes", 45))))
    except (TypeError, ValueError):
        duration = 45
    want_homework = p.get("include_homework", True)
    want_diff = p.get("include_differentiation", True)
    # Modes need clips to cite: the flag alone, with no usable clip, renders
    # the plain plan rather than three empty headings.
    clips = usable_clips(p) if p.get("lesson_modes") else []
    modes = bool(clips)

    grade = book.get("grade") or "school"
    subject = book.get("subject") or "general"
    chapter_title = chapter.get("title") or dx._t("chapter", language)
    prompt = PROMPT.format(
        duration=duration,
        homework_field=',\n  "homework": ["..."]' if want_homework else "",
        diff_field=',\n  "differentiation": {"support": "...", "challenge": "..."}' if want_diff else "",
        modes_field=MODES_FIELD if modes else "",
        modes_block=MODES_BLOCK.format(min_clips=min(MIN_CLIPS, len(clips)), max_clips=min(MAX_CLIPS, len(clips)),
                                       clips=clips_block(clips)) if modes else "",
    )
    grounding = dx.chapter_grounding(book, chapter, analysis)
    data = client.analyze(prompt, max_tokens=4500 if modes else 3000, cache_prefix=grounding).get("data", {}) or {}

    minutes_text = dx._t("n_minutes", language).format(n=data.get("duration_minutes", duration))
    doc = dx.new_doc(
        data.get("title") or f"{dx._t('doc_lesson_plan', language)} — {chapter_title}",
        f"{grade} · {subject}    |    {dx._t('duration', language)}: {minutes_text}",
        template=template, kind="lesson_plan", language=language,
        header_lines=p.get("curriculum_header"),
    )

    dx.heading(doc, dx._t("learning_objectives", language), 1)
    dx.bullets(doc, data.get("learning_objectives", []))

    if data.get("materials"):
        dx.heading(doc, dx._t("materials", language), 1)
        dx.bullets(doc, data["materials"])

    vocab = data.get("key_vocabulary", [])
    if vocab:
        dx.heading(doc, dx._t("key_vocabulary", language), 1)
        dx.table(
            doc, [dx._t("term", language), dx._t("definition", language)],
            [[v.get("term", ""), v.get("definition", "")] for v in vocab if isinstance(v, dict)],
        )

    flow = data.get("lesson_flow", [])
    if flow:
        dx.heading(doc, dx._t("lesson_flow", language), 1)
        dx.table(
            doc, [dx._t("phase", language), dx._t("min_col", language),
                  dx._t("teacher_does", language), dx._t("students_do", language)],
            [[f.get("phase", ""), f.get("minutes", ""), f.get("teacher_does", ""), f.get("students_do", "")]
             for f in flow if isinstance(f, dict)],
        )

    if modes:
        _render_modes(doc, data, clips, language)

    if data.get("assessment"):
        dx.heading(doc, dx._t("assessment", language), 1)
        dx.bullets(doc, data["assessment"])

    if data.get("homework"):
        dx.heading(doc, dx._t("homework", language), 1)
        dx.bullets(doc, data["homework"])

    diff = data.get("differentiation") or {}
    if diff:
        dx.heading(doc, dx._t("differentiation", language), 1)
        if diff.get("support"):
            dx.labelled(doc, dx._t("support", language), diff["support"])
        if diff.get("challenge"):
            dx.labelled(doc, dx._t("challenge", language), diff["challenge"])

    return dx.save(doc, out_dir / "lesson_plan.docx")
