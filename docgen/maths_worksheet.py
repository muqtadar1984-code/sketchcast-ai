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

A chapter whose content is DIAGRAMS — classify these triangles, count the
right angles, find x on a straight line — is asked as figure questions
(maths.geometry.items): the model describes each diagram as a construction,
a deterministic engine builds, proves and draws it, and only a verified
figure is printed, at true size when the student is meant to measure it
and deliberately off-scale when the student is meant to reason.

A chapter whose answers are NAMES — polygons, solids, angle and triangle
types, symmetry, directions, probability words — cannot be proved by
algebra (2D shape and pattern, 2026-10-07: every question rejected, the
worksheet failed). What the ladder and the figures cannot fill is asked as
fill-in, true/false and matching items, each checked against the closed
table of facts in maths.facts; an item the table cannot prove is never
printed.

Same student/teacher split as every other document (2026-08-18): build()
returns [student_document, answer_key].
"""

from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Optional

from docgen import docx_builder as dx
from maths.facts import FactItem
from maths.geometry.items import GeometryItem, figure_client, geometry_items, key_lines, quiz_image_data_url
from maths.pretty import pretty
from maths.tokens import TokenError, tokenize
from maths.questions import fact_items, question_ladder, worked_solution
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
# the share of the count offered to figure questions first (see build());
# the ladder and the facts fill whatever the figures leave
FIGURE_SHARE = 0.5


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


def _fact_sections(doc, key_doc, facts: list[FactItem], language: str, *, exam: bool) -> int:
    """The verified fact items, grouped by format the way the science
    worksheet groups them (fill-in, true/false, one matching table), on the
    sheet and in the key. Returns the marks they carry (exam: one a blank or
    statement, one a pair)."""
    fill = [f for f in facts if f.format == "fill_blank"]
    tf = [f for f in facts if f.format == "true_false"]
    match = next((f for f in facts if f.format == "match"), None)
    marks = 0
    si = 0

    def tag(k: int) -> str:
        return f"    [{k} mark{'s' if k != 1 else ''}]" if exam else ""

    if fill:
        name = dx.section_heading(language, si, "sec_fill_blank")
        dx.heading(doc, name, 1)
        dx.numbered(doc, [dx.strip_leading_number(f.q) + tag(1) for f in fill])
        dx.answer_section(key_doc, name, [f.answer for f in fill])
        marks += len(fill)
        si += 1
    if tf:
        name = dx.section_heading(language, si, "sec_true_false")
        dx.heading(doc, name, 1)
        dx.numbered(doc, [dx.strip_leading_number(f.q) + tag(1) for f in tf])
        dx.answer_section(key_doc, name, [dx.tf_word(f.truth, language) for f in tf])
        marks += len(tf)
        si += 1
    if match is not None:
        name = dx.section_heading(language, si, "sec_match")
        dx.heading(doc, name, 1)
        dx.para(doc, dx.match_instruction(language) + tag(len(match.pairs)), italic=True)
        letters = dx.letters(language)
        order = list(range(len(match.pairs)))
        random.shuffle(order)   # Column B shuffled, so it is a real matching task
        rows = [[f"{i + 1}. {match.pairs[i]['left']}", f"{letters[i]}. {match.pairs[order[i]]['right']}"]
                for i in range(len(match.pairs))]
        dx.table(doc, [dx.column_label(language, 0), dx.column_label(language, 1)], rows)
        dx.answer_section(key_doc, name, [letters[order.index(i)] for i in range(len(match.pairs))])
        marks += len(match.pairs)
    return marks


_figure_client = figure_client   # the figure call's client; it lives in maths.geometry.items


def _plain(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    try:
        f = float(v)
        return str(int(round(f))) if abs(f - round(f)) < 1e-9 else f"{f:.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(v)


def _figure_quiz(item: GeometryItem, language: str) -> list[dict]:
    """A verified figure question in the quiz player's schema, with its
    picture. Answers come from what the ENGINE proved or computed."""
    spec, rep = item.spec, item.report
    image = quiz_image_data_url(item)
    if item.role == "reasoning":
        if len(rep.proved) != 1:
            return []
        value = _plain(next(iter(rep.proved.values())))
        return [{"type": "fill_blank", "prompt": f"{item.prompt} {dx._t('quiz_number_only', language)}",
                 "answer": value, "marks": item.marks, "image": image}]
    asks = spec.get("asks") or ((spec.get("parts") or [{}])[0].get("asks")) or {}
    prop = str(asks.get("property") or "")
    values = rep.computed.get(prop) or {}
    if not values:
        return []
    labels = {f["id"]: f.get("label") or f["id"] for f in spec.get("figures") or []}
    order = [labels.get(fid, fid) for fid in (asks.get("over") or list(labels))]
    order = [lab for lab in order if lab in values] or list(values)
    select = asks.get("select")
    if select is not None:
        want = str(select).lower()
        return [{"type": "true_false", "prompt": f"{item.prompt} — {lab}",
                 "answer": str(values[lab]).lower() == want, "marks": 1, "image": image} for lab in order]
    if all(isinstance(values[lab], bool) for lab in order):
        return [{"type": "true_false", "prompt": f"{item.prompt} — {lab}", "answer": bool(values[lab]),
                 "marks": 1, "image": image} for lab in order]
    if len(order) == 1:
        value = values[order[0]]
        hint = f" {dx._t('quiz_number_only', language)}" if isinstance(value, (int, float)) else ""
        return [{"type": "fill_blank", "prompt": f"{item.prompt}{hint}", "answer": _plain(value), "marks": 1,
                 "image": image}]
    pairs = [{"left": lab, "right": _plain(values[lab])} for lab in order]
    return [{"type": "match", "prompt": item.prompt, "pairs": pairs, "marks": len(pairs), "image": image}]


def _figure_section(doc, key_doc, items: list[GeometryItem], language: str, *, exam: bool,
                    number: int) -> tuple[int, int]:
    """The verified figure questions: each question, its drawing(s) at the
    width the renderer chose, writing lines; the key prints the computed
    answer or the proof with its reasons. Returns (next number, marks)."""
    if not items:
        return number, 0
    name = dx._t("sec_figures", language)
    dx.heading(doc, name, 1)
    marks = 0
    key_items: list[str] = []
    for item in items:
        number += 1
        text = dx.strip_leading_number(item.prompt) or dx._t("sec_figures", language)
        if exam:
            marks += item.marks
            dx.question(doc, f"{number}. {text}    [{item.marks} marks]", first=(number == 1))
        else:
            dx.question(doc, f"{number}. {text}", first=(number == 1))
        if len(item.images) == 1 and not item.images[0].label:
            dx.picture(doc, item.images[0].png, item.images[0].width_mm)
        else:
            dx.picture_row(doc, [(im.png, im.width_mm, im.label) for im in item.images])
        dx.writing_lines(doc, item.lines)
        lines = key_lines(item, answer_word=dx._t("sol_answer", language), language=language)
        if exam and item.role == "reasoning":
            scheme = dx._t("marks_scheme", language).format(m=item.marks, method=max(1, item.marks - 1))
            key_items.append("\n".join(lines) + scheme)
        else:
            key_items.append("\n".join(lines))
    dx.answer_section(key_doc, name, key_items)
    return number, marks


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

    # The diagram half comes FIRST and holds a share of the count: figure
    # questions — a construction the geometry engine builds, proves and
    # draws (maths.geometry); a question it refuses is never printed. They
    # were a fallback for a short ladder until 2026-10-07, when "2D shape
    # and pattern" filled all ten slots with perimeters and angle sums the
    # ladder could prove, left the figure call unmade, and scored 0.286 on
    # coverage with every shape concept missed. A chapter without diagram
    # questions answers with an empty list and the ladder fills everything.
    # Asked of the SCRIPT role's model, not the document kind's: measured on
    # the same prompt, gemini-3.5-flash-lite verified 2 of 7 figure
    # questions and gemini-3.5-flash 6 of 7 — a construction grammar is a
    # harder reply than a worksheet's prose.
    figure_share = n if n < 4 else max(2, round(n * FIGURE_SHARE))
    figures, figure_report = geometry_items(_figure_client(client, language), topic=topic, level=grade,
                                            language=language, n=figure_share, chapter_context=grounding,
                                            kind=kind, note=dx._t("not_to_scale", language))
    questions, report = question_ladder(client, topic=topic, level=grade, language=language,
                                        n=n - len(figures), lesson=maths_lesson, chapter_context=grounding,
                                        kind=kind)
    # The categorical half (2D shape and pattern, 2026-10-07): a chapter
    # whose answers are names — polygons, solids, angle types, directions —
    # verified nothing through SymPy and failed every time. What the ladder
    # and the figures could not fill is asked as fill-in / true-false /
    # match items, each declaring a fact the closed table in maths.facts
    # proves; an item it cannot prove is never printed. Computational
    # questions keep their place first in the count.
    facts, fact_report = fact_items(client, topic=topic, level=grade, language=language,
                                    n=n - len(questions) - len(figures), chapter_context=grounding, kind=kind)
    if not questions and not facts and not figures:
        raise RuntimeError(
            f"no {kind} question could be verified for {topic!r}: "
            + "; ".join(report.get("rejected") or [])[:600]
            + (" | figure questions: " + "; ".join(figure_report.get("rejected") or [])[:400]
               if figure_report.get("rejected") else "")
            + (" | fact items: " + "; ".join(fact_report.get("rejected") or [])[:400]
               if fact_report.get("rejected") else ""))
    logger.info("maths %s for %r: %d computational question(s) verified by algebra, %d figure question(s) "
                "verified by the geometry engine, %d fact item(s) verified by the table (of %d wanted)",
                kind, topic, len(questions), len(figures), len(facts), n)

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
    if questions:
        dx.para(key_doc, dx._t("cas_note", language), italic=True)
    if figures:
        dx.para(key_doc, dx._t("figures_note", language), italic=True)
    if facts:
        dx.para(key_doc, dx._t("facts_note", language), italic=True)

    total_marks = _fact_sections(doc, key_doc, facts, language, exam=(kind == "exam_paper"))
    number = 0
    number, figure_marks = _figure_section(doc, key_doc, figures, language, exam=(kind == "exam_paper"),
                                           number=number)
    total_marks += figure_marks
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
            first = number == 1 and not facts
            if kind == "exam_paper":
                marks = _MARKS[level]
                total_marks += marks
                dx.question(doc, f"{number}. {text}    [{marks} marks]", first=first)
            else:
                dx.question(doc, f"{number}. {text}", first=first)
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

    # the quiz player's structured questions (best-effort, like the science
    # worksheet). A figure question carries its picture as a data URL and
    # maps onto the player's own types, so it is auto-marked like the rest:
    # find x -> fill_blank on the number; "which of these are …" -> one
    # true/false per figure; "classify each" -> match; a count -> fill_blank.
    try:
        from docgen.questions import write_worksheet
        short = [{"q": _problem_text(ex), "answer": " or ".join(pretty(a) for a in ex.final_answer)}
                 for ex in questions]
        fill = [{"q": f.q, "answer": f.answer} for f in facts if f.format == "fill_blank"]
        tf = [{"statement": f.q, "answer": f.truth} for f in facts if f.format == "true_false"]
        match = next(([{"left": p["left"], "right": p["right"]} for p in f.pairs]
                      for f in facts if f.format == "match"), [])
        extra: list[dict] = []
        for item in figures:
            try:
                extra.extend(_figure_quiz(item, language))
            except Exception as exc:  # noqa: BLE001 — one picture must not sink the paper's quiz
                logger.warning("quiz question for figure %s skipped: %s", item.id, exc)
        write_worksheet(out_dir, title, instructions, fill, tf, match, short, language=language, extra=extra)
    except Exception as exc:  # noqa: BLE001
        logger.warning("questions.json (maths %s) skipped: %s", kind, exc)

    # What the diagrams say, in words, beside the document: the coverage gate
    # reads text, and a page of drawn triangles teaches scalene, isosceles
    # and equilateral without those words reaching the page (shared.coverage
    # .document_text picks the sidecar up). Written from the engine's own
    # facts, so it describes what was drawn, not what the model intended.
    if figures:
        from maths.geometry.items import describe
        from shared.coverage import FIGURE_TRANSCRIPT
        transcript = "\n\n".join(describe(item, answer_word=dx._t("sol_answer", language)) for item in figures)
        (out_dir / FIGURE_TRANSCRIPT).write_text(transcript, encoding="utf-8")

    stem = "worksheet" if kind == "worksheet" else "exam_paper"
    sheet_path = dx.save(doc, out_dir / f"{stem}.docx")
    key_path = dx.save(key_doc, out_dir / f"{stem}_answer_key.docx")
    return [sheet_path, key_path]
