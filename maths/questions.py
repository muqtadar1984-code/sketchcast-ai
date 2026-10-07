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

from maths.facts import (FORMATS, RELATIONS, FactCheck, FactItem, is_english, parse_item, read_back_item,
                         verify_item)
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
        "Every question here has a NUMBER or an algebraic expression as its answer, reached by working written in "
        "notation (a perimeter, a missing angle, a time difference, a count). A question whose answer is a WORD "
        "— a shape's name, a type of angle, a direction, 'likely' — does not belong here: a separate section of "
        "the document asks those. If the topic has few computational questions, write fewer.",
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
    worded = 0
    for _round in range(rounds):
        need = {d: max(0, counts[d] - len(kept[d])) for d in counts}
        if sum(need.values()) == 0:
            break
        if _round and asked and worded == asked:
            # every question so far was a naming one ("triangle" -> "3"):
            # the chapter's answers are words, and another round would buy
            # the same refusals (2D shape and pattern, 2026-10-07). The
            # caller fills the set from maths.facts instead.
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
                if "not a quantity" in reason:
                    worded += 1
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
    return out, {"asked": asked, "verified": len(out), "rejected": rejected, "wanted": counts, "worded": worded}


# ── the categorical half: fact items, checked against maths.facts ────────────

FACT_SET_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {
        "format": {"type": "string", "enum": list(FORMATS)},
        "difficulty": {"type": "integer"},
        "q": {"type": "string"}, "answer": {"type": "string"},
        "relation": {"type": "string", "enum": list(RELATIONS)},
        "subject": {"type": "string"}, "value": {"type": "string"},
        "pairs": {"type": "array", "items": {"type": "object", "properties": {
            "left": {"type": "string"}, "right": {"type": "string"},
            "subject": {"type": "string"}, "value": {"type": "string"}},
            "required": ["left", "right", "subject", "value"]}}},
    "required": ["format", "difficulty", "q", "answer", "relation", "subject", "value", "pairs"]}}},
    "required": ["items"]}


READ_BACK_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {
        "q": {"type": "string"}, "answer": {"type": "string"},
        "pairs": {"type": "array", "items": {"type": "object", "properties": {
            "left": {"type": "string"}, "right": {"type": "string"}}, "required": ["left", "right"]}}},
    "required": ["q", "answer", "pairs"]}}},
    "required": ["items"]}

# The declaration is a machine-read key in every language; only the text a
# student reads is in the document's language. Without this the language
# directive ("write ALL output in Malay") reaches the declaration too.
_DECLARATION_RULE = (
    "LANGUAGE OF THE FIELDS: 'q', 'answer' and each pair's 'left' and 'right' are printed for the student — write "
    "them in the document's language. 'relation', 'subject' and 'value' (also inside 'pairs') are keys a checker "
    "reads: ALWAYS in English with numerals as digits, exactly as the list above names them, whatever the "
    "document's language. A true/false 'answer' is the English word true or false.")


def _read_back_prompt(items: list[FactItem]) -> str:
    rows = [{"q": it.q, "answer": it.answer if it.format == "fill_blank" else "",
             "pairs": [{"left": p.get("left", ""), "right": p.get("right", "")} for p in it.pairs]}
            for it in items]
    return "\n\n".join([
        "Translate each of the following worksheet items into English, LITERALLY. Translate what each sentence "
        "SAYS, word for word in meaning — never correct it, never complete it, never improve it: a sentence that "
        "states something false must stay false in English. Keep every blank (____) where it is. Write every "
        "number as digits, keep every number the text prints exactly as printed, and keep clock times as written.",
        f"Return ONLY one minified JSON object {{\"items\": [...]}} with exactly {len(rows)} entries, in the same "
        "order, each with the same fields: q, answer, pairs (left, right).",
        "ITEMS:\n" + json.dumps(rows, ensure_ascii=False),
    ])


def _read_back(client, items: list[FactItem]) -> list[Optional[dict]]:
    """The English reading of each printed non-English item, from a call
    that never sees what the item declares — so a sentence that says the
    wrong thing reads as the wrong thing. None where the reply has no entry
    for the item (that item is then dropped, never printed unchecked)."""
    if not items:
        return []
    plain = client.undirected() if hasattr(client, "undirected") else client
    data = _analyze(plain, _read_back_prompt(items), READ_BACK_SCHEMA, 8000)
    rows = data.get("items") if isinstance(data, dict) else None
    rows = rows if isinstance(rows, list) else []
    if len(rows) != len(items):
        # a reply that lost or merged an entry cannot be aligned with the
        # items, and a misaligned reading would check one sentence against
        # another's fact: nothing from it is used
        logger.info("maths fact read-back: %d items, %d readings — none used", len(items), len(rows))
        return [None] * len(items)
    return [r if isinstance(r, dict) else None for r in rows]


def _fact_prompt(*, topic: str, level: str | None, n: int, chapter_context: str, kind: str,
                 language: str = "en") -> str:
    relations = "\n".join(f"  - {r.name}: {r.describe}" for r in RELATIONS.values())
    ctx = [f"TOPIC: {topic}", f"LEARNER LEVEL: {level or 'school'}", f"LANGUAGE: {language or 'en'}",
           f"DOCUMENT: {'a practice worksheet' if kind == 'worksheet' else 'a test paper'}"]
    if chapter_context:
        ctx.append(chapter_context[:6000])
    return "\n\n".join([
        "\n".join(ctx),
        f"Write {n} short questions on this topic whose answers are FACTS — a shape's name or number of sides, "
        "faces, edges or vertices, a type of angle or triangle, lines of symmetry, a probability word, a compass "
        "direction after a turn, a 24-hour time. Mix three formats: fill_blank (a sentence with one ____ blank; "
        "'answer' is the word or number that fills it), true_false ('q' is the statement; 'answer' is 'true' or "
        "'false' — make some false), and at most ONE match exercise (3 to 6 'pairs', left and right, every right "
        "different; 'q' and 'answer' empty).",
        "EVERY item DECLARES the fact it tests, from this closed list ('relation', 'subject', 'value' — for a "
        "match, each pair has its own subject and value). Only these relations exist; a question that fits none "
        "of them is not wanted:\n" + relations,
        "Rules the checker enforces (an item that breaks one is thrown away): the sentence names the declared "
        "subject and nothing else of its kind; a blank's answer is the subject or the value, and the sentence "
        "does not also print it; a true/false statement says exactly subject and value, true or not; no 'not', "
        "'never' or 'no' in any sentence; a blank whose answer could be two things (4 sides: square? kite?) is "
        "not allowed; a polygon's lines of symmetry are asked of 'regular pentagon', never of a bare 'pentagon'; "
        "curved solids (cylinder, cone, sphere) are not in the list.",
        "Difficulty 1 or 2 for recall, 3 for a classification from measurements (side lengths, angles, a turn).",
        _DECLARATION_RULE,
        "=== OUTPUT ===\nReturn ONLY one minified JSON object: {\"items\": [ ... ]}. Unused fields are empty "
        "strings or an empty 'pairs' list.",
    ])


def fact_items(client, *, topic: str, level: str | None, language: str, n: int, chapter_context: str = "",
               kind: str = "worksheet") -> tuple[list[FactItem], dict]:
    """Up to ``n`` objective items whose every answer the closed table in
    maths.facts proves. One model call in English; in any other language a
    second call reads the printed sentences back into English and the table
    checks that reading (maths.facts.read_back_item). A matching exercise
    counts as one item. Returned items carry the PRINTED text."""
    if n <= 0:
        return [], {"asked": 0, "verified": 0, "rejected": []}
    # one spare in three: a rejection should not leave the set short
    ask = n + max(2, n // 3)
    data = _analyze(client, _fact_prompt(topic=topic, level=level, n=ask, chapter_context=chapter_context,
                                         kind=kind, language=language), FACT_SET_SCHEMA, 8000)
    raw = data.get("items") if isinstance(data, dict) else None
    items = [parse_item(d) for d in raw or []]
    if is_english(language):
        checked = items
        why_not: list[str] = [""] * len(items)
    else:
        checked, why_not = [], []
        for item, reading in zip(items, _read_back(client, items)):
            english, why = read_back_item(item, reading)
            checked.append(english)
            why_not.append(why)
    kept: list[FactItem] = []
    rejected: list[str] = []
    seen: set = set()
    for item, english, why in zip(items, checked, why_not):
        check = verify_item(english) if english is not None else FactCheck(False, why)
        if english is not None and english is not item and check.ok is False:
            check = FactCheck(False, f"read back as {english.q or english.pairs!r}: {check.detail}")
        if not check.ok:
            logger.info("maths fact item rejected (%s, %s): %r — %s", item.format, item.relation,
                        (item.q or str(item.pairs))[:80], check.detail)
            rejected.append(f"{(item.q or item.format)[:60]}: {check.detail[:200]}")
            continue
        key = (item.relation, item.subject.lower(), item.format) if item.format != "match" else ("match",)
        if key in seen:
            rejected.append(f"{item.q[:60]}: repeats an item already kept")
            continue
        seen.add(key)
        if len(kept) < n:
            kept.append(item)
    logger.info("maths fact items for %r: %d returned, %d verified, %d rejected", topic, len(raw or []),
                len(kept), len(rejected))
    return kept, {"asked": len(raw or []), "verified": len(kept), "rejected": rejected}


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


__all__ = ["question_ladder", "worked_solution", "fact_items", "SET_SCHEMA", "FACT_SET_SCHEMA", "READ_BACK_SCHEMA"]
