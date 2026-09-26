"""A graded question set on a topic, verified, for worksheets and tests.

The same ladder as the lesson (simplest -> medium -> difficult -> extremely
difficult), the same structured example record, the same verifier — so an
answer key's worked solution is a proved chain of steps, never prose the
model wrote independently of the mathematics. Built from the lesson's own
method card and examples when a sibling video exists (worker/process.py
hands them over), else from the chapter alone.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from maths.lesson import _NOTATION_RULES, _STEP_RULES, _SYSTEM, _analyze
from maths.schema import EXAMPLE_SCHEMA, DIFFICULTY_NAMES, Lesson, WorkedExample, parse_example
from maths.verify import verify_example

logger = logging.getLogger("worker")

SET_SCHEMA = {"type": "object", "properties": {"questions": {"type": "array", "items": EXAMPLE_SCHEMA}},
              "required": ["questions"]}


def _ladder(n: int) -> dict[int, int]:
    """How many questions per difficulty for a set of n."""
    n = max(1, int(n))
    d1 = max(1, round(n * 0.3))
    d2 = max(1 if n >= 2 else 0, round(n * 0.3))
    d3 = max(1 if n >= 3 else 0, round(n * 0.2))
    d4 = max(0, n - d1 - d2 - d3)
    if n >= 4 and d4 == 0:
        d3, d4 = max(0, d3 - 1), 1
    return {1: d1, 2: d2, 3: d3, 4: d4}


def _prompt(*, topic: str, level: str | None, language: str, counts: dict[int, int],
            lesson: Optional[Lesson], chapter_context: str, kind: str) -> str:
    want = ", ".join(f"{k} of difficulty {d} ({DIFFICULTY_NAMES[d]})" for d, k in counts.items() if k)
    ctx = [f"TOPIC: {topic}", f"LEARNER LEVEL: {level or 'school'}", f"LANGUAGE: {language or 'en'}",
           f"DOCUMENT: {'a practice worksheet' if kind == 'worksheet' else 'a test paper'}"]
    if lesson is not None:
        ctx.append("METHOD TAUGHT IN THE LESSON: " + "; ".join(lesson.method.steps))
        ctx.append("THE LESSON'S OWN EXAMPLES (do NOT repeat these problems; write NEW ones of the same kinds):\n" +
                   "\n".join(f"  - {e.label} (difficulty {e.difficulty}): {e.problem}" for e in lesson.examples))
    if chapter_context:
        ctx.append(chapter_context[:6000])
    return "\n\n".join([
        "\n".join(ctx),
        f"Write {sum(counts.values())} NEW questions on this topic: {want}. Each question is one complete worked "
        "example in the JSON shape below — the problem, its givens, typed steps a student would write, the final "
        "answer — so that the answer key can print the working. Word problems are welcome at difficulty 3 and 4 "
        "(use a 'setup' step). Vary the numbers and forms; every answer must be exact.",
        "Difficulty means, for THIS level: 1 direct application (one or two steps); 2 one twist (a negative, a "
        "fraction, a bracket, terms on both sides); 3 multi-step or a word problem; 4 exam-style needing an insight. "
        "Never leave the level's syllabus.",
        _NOTATION_RULES, _STEP_RULES,
        "=== OUTPUT ===\nReturn ONLY one minified JSON object: {\"questions\": [ ...examples... ]}. Each example: "
        "label, difficulty, task, problem, givens, target, intro_speech (may be short), steps, final_answer, "
        "answer_speech (may be short). No common_mistake needed.",
    ])


def question_ladder(client, *, topic: str, level: str | None, language: str, n: int,
                    lesson: Optional[Lesson] = None, chapter_context: str = "",
                    kind: str = "worksheet", rounds: int = 2) -> tuple[list[WorkedExample], dict]:
    """``n`` verified questions across the ladder (as many as verify, in at
    most ``rounds`` model calls). Returns (questions sorted by difficulty,
    report {asked, verified, rejected: [...]})."""
    counts = _ladder(n)
    kept: dict[int, list[WorkedExample]] = {1: [], 2: [], 3: [], 4: []}
    rejected: list[str] = []
    asked = 0
    for _round in range(rounds):
        need = {d: max(0, counts[d] - len(kept[d])) for d in counts}
        if sum(need.values()) == 0:
            break
        # ask for one spare per level so a single rejection does not cost a round
        ask = {d: (k + 1 if k else 0) for d, k in need.items()}
        data = _analyze(client, _prompt(topic=topic, level=level, language=language, counts=ask, lesson=lesson,
                                        chapter_context=chapter_context, kind=kind), SET_SCHEMA, 16000)
        raw = data.get("questions") if isinstance(data, dict) else None
        for item in (raw or []):
            ex = parse_example(item)
            asked += 1
            rep = verify_example(ex)
            if rep.status != "verified":
                # Logged one by one, at the moment of rejection: the ladder
                # used to report counts alone, so a run that rejected 14 of
                # 20 (Mean 8 Class 7.1, 2026-09-26, after the data tasks
                # shipped) said nothing about WHY — a model slip and a gap in
                # the verifier look identical as a number.
                reason = "; ".join(rep.reasons)[:300]
                logger.info("maths question rejected (round %d, difficulty %s, task %s): %r — %s",
                            _round + 1, ex.difficulty, ex.task, ex.problem[:80], reason)
                rejected.append(f"{ex.problem[:60]}: {reason[:200]}")
                continue
            d = int(ex.difficulty)
            if len(kept[d]) < counts[d]:
                kept[d].append(ex)
            else:
                # a spare: keep it for the nearest short level
                for e in (d - 1, d + 1, d - 2, d + 2):
                    if e in kept and len(kept[e]) < counts[e]:
                        ex.difficulty = e
                        kept[e].append(ex)
                        break
    out: list[WorkedExample] = []
    for d in (1, 2, 3, 4):
        for i, ex in enumerate(kept[d], 1):
            ex.label = f"Q{len(out) + 1}"
            out.append(ex)
    logger.info("maths question ladder for %r: %d asked, %d verified, %d rejected (%s)", topic, asked, len(out),
                len(rejected), "reasons logged above" if rejected else "nothing rejected")
    return out, {"asked": asked, "verified": len(out), "rejected": rejected, "wanted": counts}


def worked_solution(ex: WorkedExample, pretty, *, check: str = "Check", answer: str = "Answer",
                    or_word: str = "or") -> list[str]:
    """The answer key's lines for one question: each step's result with
    its operation, then the answer. ``pretty`` prints notation; the three
    labels arrive in the document's language."""
    lines: list[str] = []
    for st in ex.steps:
        if not st.after:
            continue
        state = "; ".join(pretty(x) for x in st.after)
        note = st.note
        if st.kind == "check":
            lines.append(f"{check}: {state}")
        elif note:
            lines.append(f"{state}   ({note})")
        else:
            lines.append(state)
    final = f" {or_word} ".join(pretty(a) for a in ex.final_answer) if ex.task != "solve_system" \
        else ", ".join(pretty(a) for a in ex.final_answer)
    lines.append(f"{answer}: {final}")
    return lines


__all__ = ["question_ladder", "worked_solution", "SET_SCHEMA"]
