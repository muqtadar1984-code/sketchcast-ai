"""Maths worksheet and test paper -> TWO editable .docx files, from VERIFIED
questions.

The science builders ask the model for fill-in-the-blank, true/false and
matching items and print whatever comes back. A maths document is a ladder
of problems (simplest -> medium -> difficult -> extremely difficult), and its
answer key prints the WORKING — each step's result with the operation that
produced it — from the same structured record the video is compiled from,
proved step by step by SymPy (maths.questions -> maths.verify). A question
that does not verify is never printed.

When the worksheet belongs beside a maths video (same owner, book, chapter
and part), worker/process.py hands that lesson over (``maths_lesson``): the
questions then follow the lesson's own method card and avoid repeating its
examples. Without one, the chapter alone sets the topic.

Same student/teacher split as every other document (2026-08-18): build()
returns [student_document, answer_key].
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from docgen import docx_builder as dx
from maths.pretty import pretty
from maths.tokens import TokenError, tokenize
from maths.questions import question_ladder, worked_solution
from maths.schema import DATA_TASKS, DIFFICULTY_NAMES, Lesson


def is_data_list(text: str) -> bool:
    from maths.notation import is_notation, parse_relation
    try:
        return is_notation(text) and parse_relation(text).is_data
    except Exception:  # noqa: BLE001
        return False

logger = logging.getLogger("worker")

_SECTION = {1: "ws_warm_up", 2: "ws_practice", 3: "ws_challenge", 4: "ws_stretch"}   # strings keys
_LINES = {1: 3, 2: 4, 3: 6, 4: 8}
_MARKS = {1: 2, 2: 3, 3: 4, 4: 6}
_DEFAULT_N = {"worksheet": 10, "exam_paper": 8}


def _n(params: dict, kind: str) -> int:
    if kind == "exam_paper":
        obj = (params or {}).get("objective") or {}
        try:
            total = int((params or {}).get("subjective") or 0) + sum(int(obj.get(k) or 0) for k in obj)
        except (TypeError, ValueError):
            total = 0
        return max(4, min(16, total)) if total else _DEFAULT_N[kind]
    try:
        return max(1, min(24, int((params or {}).get("num_questions") or _DEFAULT_N[kind])))
    except (TypeError, ValueError):
        return _DEFAULT_N[kind]


_VERB = {"simplify": "verb_simplify", "expand": "verb_expand", "factorise": "verb_factorise",
         "evaluate": "verb_evaluate", "solve": "verb_solve", "solve_system": "verb_solve",
         "solve_inequality": "verb_solve", "round": "verb_round", "estimate": "verb_estimate",
         "mean": "verb_mean", "median": "verb_median", "mode": "verb_mode", "range": "verb_range"}   # strings keys


def _problem_text(ex, language: str = "en") -> str:
    """The printed question: a word problem as written; bare notation with
    the task as its verb ("Solve: 3x + 5 = 20"), in the document's language."""
    p = pretty(ex.problem) if ex.problem else "; ".join(pretty(g) for g in ex.givens)
    try:
        tokenize(ex.problem or ex.givens[0] if ex.givens else ex.problem)
        is_notation = True
    except (TokenError, IndexError):
        is_notation = False
    if not is_notation and ex.task in DATA_TASKS and not ex.problem and ex.givens:
        # a bare data list is the question once its verb is in front:
        # "Find the mean of: 4, 8, 6, 10, 12"
        is_notation = is_data_list(ex.givens[0])
    verb = dx._t(_VERB[ex.task], language) if ex.task in _VERB else ""
    if is_notation and verb and not p.lower().startswith((verb.lower(), "find")):
        return f"{verb}: {p}"
    return p


def build(book: dict, chapter: dict, analysis: dict, client, params: dict, out_dir: Path,
          template: str | None = None, language: str = "en", *, kind: str = "worksheet",
          maths_lesson: Optional[Lesson] = None) -> list[Path]:
    p = dict(params or {})
    n = _n(p, kind)
    grade = book.get("grade") or "school"
    subject = book.get("subject") or "Mathematics"
    chapter_title = chapter.get("title") or dx._t("chapter", language)
    topic = maths_lesson.topic if maths_lesson and maths_lesson.topic else chapter_title
    grounding = dx.chapter_grounding(book, chapter, analysis)

    questions, report = question_ladder(client, topic=topic, level=grade, language=language, n=n,
                                        lesson=maths_lesson, chapter_context=grounding, kind=kind)
    if not questions:
        raise RuntimeError(f"no {kind} question could be verified for {topic!r}: "
                           + "; ".join(report.get("rejected") or [])[:600])

    doc_kind = "worksheet" if kind == "worksheet" else "exam_paper"
    title = f"{dx._t('doc_worksheet' if kind == 'worksheet' else 'doc_test_paper', language)} — {topic}"
    subtitle = f"{grade} · {subject}"
    header_lines = p.get("curriculum_header")
    doc = dx.new_doc(title, subtitle, template=template, kind=doc_kind, language=language, header_lines=header_lines)
    instructions = dx._t("ws_instructions" if kind == "worksheet" else "exam_instructions", language)
    dx.instructions(doc, instructions)

    key_doc = dx.new_doc(f"{title} — {dx._t('answer_key', language)}", subtitle, template=template,
                         kind=doc_kind, language=language, header_lines=header_lines)
    dx.para(key_doc, dx._t("teacher_only", language), italic=True)
    dx.para(key_doc, dx._t("cas_note", language), italic=True)

    number = 0
    total_marks = 0
    by_level: dict[int, list] = {}
    for ex in questions:
        by_level.setdefault(int(ex.difficulty), []).append(ex)
    for level in (1, 2, 3, 4):
        items = by_level.get(level) or []
        if not items:
            continue
        name = f"{dx._t(_SECTION[level], language)} — {dx._t(f'difficulty_{level}', language)}"
        dx.heading(doc, name, 1)
        key_items: list[str] = []
        for ex in items:
            number += 1
            text = _problem_text(ex, language)
            if kind == "exam_paper":
                marks = _MARKS[level]
                total_marks += marks
                dx.question(doc, f"{number}. {text}    [{marks} marks]", first=(number == 1))
            else:
                dx.question(doc, f"{number}. {text}", first=(number == 1))
            dx.writing_lines(doc, _LINES[level])
            sol = worked_solution(ex, pretty, check=dx._t("sol_check", language),
                                  answer=dx._t("sol_answer", language), or_word=dx._t("sol_or", language))
            if kind == "exam_paper":
                scheme = dx._t("marks_scheme", language).format(m=_MARKS[level], method=max(1, _MARKS[level] - 1))
                key_items.append("\n".join(sol) + scheme)
            else:
                key_items.append("\n".join(sol))
        dx.answer_section(key_doc, name, key_items)
    if kind == "exam_paper":
        dx.para(doc, dx._t("ws_total_marks", language).format(n=total_marks), bold=True)
        dx.end_of_paper(doc)

    # the quiz player's structured questions (best-effort, like the science worksheet)
    try:
        from docgen.questions import write_worksheet
        short = [{"q": _problem_text(ex), "answer": " or ".join(pretty(a) for a in ex.final_answer)}
                 for ex in questions]
        write_worksheet(out_dir, title, instructions, [], [], [], short, language=language)
    except Exception as exc:  # noqa: BLE001
        logger.warning("questions.json (maths %s) skipped: %s", kind, exc)

    stem = "worksheet" if kind == "worksheet" else "exam_paper"
    sheet_path = dx.save(doc, out_dir / f"{stem}.docx")
    key_path = dx.save(key_doc, out_dir / f"{stem}_answer_key.docx")
    return [sheet_path, key_path]
