"""A maths lesson: generated as structure, verified, then compiled to a script.

The entry point ``generate_maths_script`` is the maths profile's stand-in for
agent3's ``generate_episode_script`` at the one seam in worker/process.py, and
returns the same EpisodeScript, so slides, TTS, the composer, the final
render, the acceptance check and the upload are untouched.

    prompt (blueprint + ladder + notation rules)
      -> client.analyze(..., response_schema=LESSON_SCHEMA)
      -> maths.schema.parse_lesson
      -> maths.verify.verify_lesson
      -> regenerate ONLY the examples that failed, with the verifier's
         reasons, up to REGEN_ATTEMPTS times; an example still failing is
         dropped when at least MIN_EXAMPLES verified ones remain, else the
         generation fails loudly (never a wrong lesson shipped quietly)
      -> maths.board.compile_lesson -> segments -> EpisodeScript

The lesson record and its verification report travel on
``EpisodeScript.maths`` into the script_json artifact and the generation's
params, so the console can show which steps were proved and the documents
can derive from the same verified working.
"""

from __future__ import annotations

import json
import logging
import math
import os
import uuid
from datetime import datetime

from agent3_scripts.models import EpisodeScript, ScriptSegment, SegmentType
from agent3_scripts.script_generator import _build_episode_context
from maths import board
from maths.schema import (EXAMPLE_SCHEMA, LESSON_SCHEMA, DIFFICULTY_NAMES, Lesson, Line, TryIt,
                          WorkedExample, parse_example, parse_lesson)
from maths.geometry.errors import GeometryRefusal
from maths.geometry.items import GeometryItem, figure_client, geometry_items, key_lines
from maths.schema import Step, WorkedExample
from maths.verify import verify_example, verify_lesson, verify_try_it
from shared import coverage as _coverage
from shared import lesson_length

logger = logging.getLogger("worker")

REGEN_ATTEMPTS = 2
MIN_EXAMPLES = 2
FIGURE_EXAMPLES = 2      # figure examples asked of the geometry engine's call, per lesson
MAX_TOKENS = 20000
JOB_KIND = "maths_lesson"

# The length floor (shared/lesson_length.py) on a maths lesson. The lesson
# is built as STRUCTURE — a hook, a concept, a ladder of verified worked
# examples, a recap, a try-it — so its length is the size of that ladder,
# not the size of the article it came from: Quadratic Expressions and
# Factorising Trinomials (2026-09-28) shipped at 4:10 against a 5-minute
# floor from a 1,488-word article, because nothing measured the maths
# script and the prompt said only "8-12 minutes". A lesson under the floor
# is EXTENDED, never rewritten: further worked examples on the same topic,
# each verified step by step like the ladder's, appended after it (plus
# any further concept lines), up to MAX_LENGTH_ROUNDS times; the worker
# refuses what is still short.
MAX_LENGTH_ROUNDS = lesson_length.MAX_LENGTH_RETRIES
MAX_EXAMPLES_PER_ROUND = 3


class MathsVerificationError(RuntimeError):
    """The lesson could not be verified after regeneration. The message is
    written for the console: which example, which step, why."""


# ── the prompt ───────────────────────────────────────────────────────────

_SYSTEM = ("You are an experienced mathematics teacher writing a worked-example video lesson for "
           "SketchCast AI. You return ONLY the JSON object requested — no prose, no markdown.")

_NOTATION_RULES = """=== NOTATION (for every 'problem', 'givens', 'before', 'after', 'final_answer', 'from_state', 'wrong_state') ===
Write mathematics in plain LINEAR notation that a computer algebra system can read:
  - variables are single letters (x, y, a, b); powers with ^ (x^2, (x+1)^2); roots as sqrt(...); fractions with / and brackets where needed ((x + 1)/2, 3/4)
  - multiplication by juxtaposition (3x, 2(x + 1)) or * between numbers (2 * 3); never × or ÷ or · ; never LaTeX, never \\frac, never words inside notation
  - one relation per string: "3x + 5 = 20", "x < -2"; NEVER "x = 2 or 3" — write ["x = 2", "x = 3"]
  - a state is the FULL working at that moment: for a system, one string per equation; for a quadratic that splits, one string per case
  - a DATA LIST (tasks mean, median, mode, range) is ONE string of numbers separated by comma and space: "4, 8, 6, 10, 12" — never a sentence, never a table
  - every 'after' must be exactly what the student would write on the next line of working"""

_STEP_RULES = """=== STEPS ===
Each step is ONE operation, machine-checkable:
  - kind "transform": 'before' is the current state, 'after' is the state after ONE valid operation (subtract 5 from both sides; divide by 3; expand the bracket; collect like terms; factorise; add the equations). The two states MUST be mathematically equivalent (same solutions). 'operation' names the operation in at most six words. 'explanation' is a short board note (at most six words), e.g. "subtract 5 from both sides".
  - kind "setup": introducing a variable, translating words into an equation ("let x be the number of tickets"), or bringing in a relation the problem's words supply — a theorem or a formula (Pythagoras: "c^2 = a^2 + b^2"; an area formula); 'after' is the equation(s) written down, and any given value still needed ("a = 3", "b = 4") stays in 'after' until a step uses it. Use for word problems and for formula problems.
  - a step that keeps only SOME solutions (a negative root for a length) must say why in 'operation': "reject the negative root: c is a length".
  - kind "check": substituting the answer back: 'after' is a TRUE numeric statement like "3*5 + 5 = 20" with no variables left.
  - task evaluate (substitution): 'givens' is the expression and then each given value as its own line ("3x + 7", "x = 4"); the first transform substitutes the values ("3(4) + 7"), later steps work it out ("12 + 7" -> "19"); 'final_answer' is the value alone ("19"). Never give the expression a name ("E = 3x + 7"); the 'check' step does not apply.
  - kind "round" (tasks round and estimate ONLY): 'after' is 'before' with its numbers replaced by their roundings and NOTHING else changed ("7583 + 3421" -> "8000 + 3000"; "x = 17173" -> "x = 17000"); 'precision' says what they were rounded to, machine-readable: a unit ("1000", "100", "0.01", "nearest thousand"), "2 dp" or "1 sf". Round half up (17500 -> 18000). Never round inside a "transform" step, and never write ≈.
  - DATA tasks (mean, median, mode, range): 'givens' is the data list. The first transform step turns the list into the statistic as an expression of the data — mean: "4, 8, 6, 10, 12" -> "(4 + 8 + 6 + 10 + 12)/5"; range: -> "12 - 4"; median: first a step whose 'after' is the SAME numbers sorted ("2, 3, 3, 3, 5, 7, 9"), then the middle value ("3") or the mean of the middle two ("(10 + 15)/2"); mode: -> the value(s) occurring most often ("3", or "2, 3" for two modes). Later steps work the expression out as usual ("40/5" -> "8"). 'final_answer' is the exact value ("8", or "mean = 8"); never a rounded one.
  - 'before' of each step equals the 'after' of the previous step. The first step's 'before' is the problem's givens.
  - 'speech': what the teacher SAYS for this step, in words a voice can read — never symbols: say "x squared", "three x plus five equals twenty", "x over two". Two or three sentences: what we do, why, and what we get.
  - 'student' (optional): a short line from the student — a genuine question, a "so x is 5?", a likely misconception — used sparingly, never on every step."""

_LADDER = """=== THE LESSON (fixed blueprint — do not reorder) ===
1. hook: 2-4 short spoken lines (teacher, optionally one student line) — a real situation where this topic matters.
2. concept: 3-6 spoken lines that state the idea and the general method, plus 'concept_points' (2-3 board points, at most eight words each) and the 'method' card: a title and 3-5 numbered method steps of at most five words each, written so that each worked example can visibly follow them in order.
3. examples: a ladder of {n_examples} worked examples on THIS topic, difficulty 1 to {n_examples}:
   - 1 "simplest": direct application, one or two steps, no twist.
   - 2 "medium": three or four steps, one twist (a negative, a fraction, a bracket, terms on both sides).
   - 3 "difficult": multi-step; combines this topic with an earlier one, or a word problem that must be translated first (use a "setup" step).
   - 4 "extremely difficult": exam-style at this level, needing an insight. Include 'common_mistake' here: a tempting WRONG route from a real state of THIS example ('from_state' -> 'wrong_state' that is NOT equivalent), 'why_wrong' in at most ten words, and 'speech' explaining it in words.
   {difficulty_note}
   Every example: 'label' ("Example 1"), 'task' (solve | solve_system | solve_inequality | simplify | expand | factorise | evaluate | round | estimate | mean | median | mode | range — round: round a number, the working is one 'round' step; estimate: round the numbers first, then work the rounded expression out in 'transform' steps; the final_answer of both is the rounded value; mean/median/mode/range: the givens are a data list, see STEPS), 'problem' (as the student reads it: notation, or the word problem in words), 'givens' (the equations or expression the working starts from, in notation), 'target' ("x", "x, y" or "expression"), 'intro_speech' (the teacher introducing the example, in words), optional 'student_question', the 'steps', 'final_answer' (one relation or expression per string), 'answer_speech'.
   Examples 1-3 stay clean and progressive: no wrong routes there.
4. recap: 2-4 spoken lines restating the method, and 'misconceptions': 1-3 board points naming the mistakes students make most (at most ten words each).
5. try_it: one problem for the student to pause on (difficulty like example 2): 'problem' (the notation itself and nothing before it — no "the expression", no "factorise": those words go in 'speech'), 'answer' (notation), 'speech' (the teacher setting it and telling the learner to pause the video and try it, in words), then — because the video resumes by solving it on the board — 'solution_speech' (one sentence resuming after the pause, e.g. inviting the learner to compare their working), 'steps' (2-4 steps in exactly the format of an example's steps) and 'answer_speech'.
6. closing: one or two spoken sentences ending the lesson: the teacher hopes the learner now understands {topic} better and encourages a little practice.
Keep every spoken line natural, in {language}, for a learner of {level}. {length_rule}"""

_OUTPUT = """=== OUTPUT ===
Return one JSON object with exactly these keys: topic, level, hook, concept, concept_points, method, examples, recap, misconceptions, try_it, closing.
hook/concept/recap are arrays of {{"who": "teacher"|"student", "line": "..."}}. method is {{"title": "...", "steps": [...]}}. examples is the array described above. try_it is {{"problem": "...", "answer": ["..."], "speech": "...", "solution_speech": "...", "steps": [...], "answer_speech": "..."}}. closing is a string.
Minified JSON. No trailing commas. No comments."""


_DEFAULT_LENGTH_RULE = "The whole lesson should run 8-12 minutes when spoken."


def _length_rule(min_minutes: float | None) -> str:
    """The floor, stated as the science prompt states it — minutes, words
    AND characters of spoken lines — or the old guidance without one."""
    if not min_minutes or min_minutes <= 0:
        return _DEFAULT_LENGTH_RULE
    return (f"MINIMUM LENGTH (hard requirement): the lesson must run at least {min_minutes:g} minutes "
            f"when spoken — at least {lesson_length.min_words(min_minutes):,} words "
            f"({lesson_length.min_chars(min_minutes):,} characters) across every spoken field: the "
            f"hook and concept lines, each example's intro_speech, every step's speech, the "
            f"answer_speech, the recap, the try-it and the closing. A shorter lesson is refused. Reach "
            f"it by teaching, never by padding: every step's speech says what we do, why, and what we "
            f"get; the concept lines explain the idea with a small illustration; the answer_speech says "
            f"what the answer means.")


def _difficulty_note(level: str) -> str:
    return ("Difficulty is judged for THIS level: what is 'extremely difficult' for a Class 8 learner is "
            "not an A-level problem. Never leave the syllabus of the level.")


def build_prompt(*, topic: str, subject: str | None, level: str | None, curriculum: str | None,
                 language: str, episode_context: str, n_examples: int = 4,
                 min_minutes: float | None = None) -> str:
    lang = language or "en"
    lvl = level or "school"
    head = (f"SUBJECT: {subject or 'Mathematics'}\nTOPIC: {topic}\nLEARNER LEVEL: {lvl}\n"
            f"CURRICULUM: {curriculum or 'not specified'}\nLANGUAGE OF NARRATION: {lang}\n\n"
            f"{episode_context}\n")
    body = _LADDER.format(n_examples=n_examples, difficulty_note=_difficulty_note(lvl),
                          language=lang, level=lvl, topic=topic, length_rule=_length_rule(min_minutes))
    return "\n\n".join([head, _NOTATION_RULES, _STEP_RULES, body, _OUTPUT])


# ── the extension (the length floor) ─────────────────────────────────────

EXTENSION_SCHEMA = {"type": "object", "properties": {
    "examples": {"type": "array", "items": EXAMPLE_SCHEMA},
    "concept": {"type": "array", "items": {"type": "object", "properties": {
        "who": {"type": "string"}, "line": {"type": "string"}}, "required": ["who", "line"]}}},
    "required": ["examples", "concept"]}


def build_extend_prompt(*, lesson: Lesson, language: str, n_examples: int, measured: dict) -> str:
    """The re-ask for a lesson under the floor: FURTHER worked examples on
    the same topic (new problems, the ladder's shape, verified like the
    ladder's) and, optionally, further concept lines — never a rewrite of
    what is already verified."""
    lang = language or "en"
    lvl = lesson.level or "school"
    existing = [f"  - {ex.label or 'example'} ({DIFFICULTY_NAMES.get(ex.difficulty, ex.difficulty)}): {ex.problem}"
                for ex in lesson.examples]
    first_label = len(lesson.examples) + 1
    return "\n\n".join([
        f"TOPIC: {lesson.topic}\nLEARNER LEVEL: {lvl}\nLANGUAGE OF NARRATION: {lang}",
        f"A worked-example video lesson on this topic has been written and verified, but it runs about "
        f"{measured.get('est_minutes', 0):g} minutes when spoken ({measured.get('chars', 0):,} characters "
        f"of spoken lines) against a floor of {measured.get('min_minutes', 0):g} minutes "
        f"({measured.get('min_chars', 0):,} characters). It needs at least "
        f"{lesson_length.chars_short(measured):,} more characters of TEACHING.",
        "THE LESSON'S EXISTING WORKED EXAMPLES (keep away from these problems):\n" + "\n".join(existing),
        "METHOD CARD (the steps every example visibly follows): " + "; ".join(lesson.method.steps),
        f"Write {n_examples} FURTHER worked example(s) on THIS topic, labelled "
        f"\"Example {first_label}\"{' onwards' if n_examples > 1 else ''}, difficulty 2 or 3 (medium to "
        f"difficult: a twist, a bracket, terms on both sides, or a short word problem with a 'setup' step), "
        f"each with a NEW problem, in exactly the shape of the ladder's examples: 'label', 'difficulty', 'task', "
        f"'problem', 'givens', 'target', 'intro_speech', 'steps', 'final_answer', 'answer_speech'. Every "
        f"step's speech says what we do, why, and what we get, in words a voice can read. You may also add "
        f"up to three further 'concept' lines ({{\"who\": \"teacher\"|\"student\", \"line\": ...}}) that "
        f"deepen the idea — an illustration, a why, a misconception — or return an empty list.",
        _NOTATION_RULES, _STEP_RULES,
        "=== OUTPUT ===\nReturn ONLY one minified JSON object: {\"examples\": [...], \"concept\": [...]}.",
    ])


def measure_lesson(lesson: Lesson, minutes: float, avatars: dict | None, language: str) -> dict:
    """The lesson's spoken length against the floor, measured on the very
    segments the board would compile — the same measure the worker takes
    on a science script (shared/lesson_length.measure)."""
    segs = board.compile_lesson(lesson, avatars, language=language)
    return lesson_length.measure({"segments": segs}, minutes)


def _examples_to_add(lesson: Lesson, measured: dict, avatars: dict | None, language: str) -> int:
    """How many further examples close the shortfall: the shortfall over
    what one of this lesson's examples speaks, at least one, at most
    MAX_EXAMPLES_PER_ROUND."""
    short = lesson_length.chars_short(measured)
    per = [len(lesson_length.spoken_text({"segments": [board.example_segment(ex, lesson, "s000", language)]}))
           for ex in lesson.examples]
    typical = max(1, sum(per) // max(1, len(per))) if per else lesson_length.SEGMENT_CHARS
    return max(1, min(MAX_EXAMPLES_PER_ROUND, math.ceil(short / typical)))


def build_regen_prompt(*, topic: str, level: str | None, language: str, example: WorkedExample,
                       reasons: list[str], method_steps: list[str]) -> str:
    return "\n\n".join([
        f"TOPIC: {topic}\nLEARNER LEVEL: {level or 'school'}\nLANGUAGE OF NARRATION: {language or 'en'}",
        "The following worked example was checked by a computer algebra system and REJECTED. "
        "Rewrite it as ONE JSON object of the same shape (same label, same difficulty, same task, on the "
        "same topic; you may change the problem). Every transform step must be exactly one valid operation "
        "whose 'before' and 'after' are equivalent, and the final answer must be right.",
        "WHAT WAS WRONG:\n" + "\n".join(f"  - {r}" for r in reasons),
        "THE REJECTED EXAMPLE:\n" + json.dumps(example.model_dump(), ensure_ascii=False),
        "METHOD CARD (the steps the example should visibly follow): " + "; ".join(method_steps),
        _NOTATION_RULES, _STEP_RULES,
        "=== OUTPUT ===\nReturn ONLY the corrected example as one minified JSON object.",
    ])


# ── generation ───────────────────────────────────────────────────────────


def _analyze(client, prompt: str, schema: dict, max_tokens: int) -> dict:
    # constrained decoding for THIS call (the payload is closed): the two
    # first production runs bent the shape without it
    result = client.analyze(prompt=prompt, system=_SYSTEM, max_tokens=max_tokens, response_schema=schema,
                            strict_schema=True)
    if result.get("truncated"):
        raise RuntimeError("the maths lesson reply was cut off at the output cap; nothing parsed from it is complete")
    data = result.get("data", result)
    return data if isinstance(data, dict) else {}


def generate_lesson(client, prompt: str) -> Lesson:
    return parse_lesson(_analyze(client, prompt, LESSON_SCHEMA, MAX_TOKENS))


def regenerate_example(client, prompt: str) -> WorkedExample:
    return parse_example(_analyze(client, prompt, EXAMPLE_SCHEMA, 8000))


# ── figure examples (geometry.figure.v1) ─────────────────────────────────


def figures_enabled() -> bool:
    """MATHS_FIGURES=0 turns the figure examples off without a deploy."""
    return os.environ.get("MATHS_FIGURES", "1").strip().lower() not in ("0", "false", "off", "no")


def _num(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(round(f))) if abs(f - round(f)) < 1e-9 else f"{f:.2f}".rstrip("0").rstrip(".")


def _plain_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, set, tuple)):
        return ", ".join(str(x) for x in sorted(v, key=str))
    if isinstance(v, dict):
        return "; ".join(f"{k}: {_plain_value(x)}" for k, x in v.items())
    return _num(v) if isinstance(v, (int, float)) else str(v)


def figure_example(item: GeometryItem) -> WorkedExample:
    """A verified figure question as a worked example. A reasoning question
    keeps its deduce / transform steps (with their speech) and answers with
    what the chain PROVED; an evidence question becomes one observation step
    per figure saying what the ENGINE computed for it, in the order the
    question asks them, closing on the computed selection."""
    spec, rep, speech = item.spec, item.report, item.speech
    labels = {f["id"]: f.get("label") or f["id"] for f in spec.get("figures") or []}
    steps: list[Step] = []
    if item.role == "reasoning":
        for st in spec.get("steps") or []:
            kind = st.get("kind") or "transform"
            steps.append(Step(kind=kind if kind in ("deduce", "transform", "setup", "check") else "transform",
                              theorem=st.get("theorem") or "", uses=list(st.get("uses") or []),
                              before=list(st.get("before") or []), after=list(st.get("after") or []),
                              speech=st.get("speech") or "", figure_ops=list(st.get("figure_ops") or [])))
        final = [f"{k} = {_num(v)}" for k, v in rep.proved.items()]
        target = ", ".join(rep.proved) or "x"
    else:
        asks = spec.get("asks") or ((spec.get("parts") or [{}])[0].get("asks")) or {}
        prop = str(asks.get("property") or "")
        values = rep.computed.get(prop) or {}
        obs = speech.get("observations") or {}
        over = list(asks.get("over") or list(labels))
        for fid in over:
            lab = labels.get(fid, fid)
            value = values.get(lab, values.get(fid))
            # one figure: the value alone ("6"), not "A: 6" — the label names
            # nothing the student can confuse it with (live demo a13f7761)
            line = _plain_value(value) if len(over) == 1 else f"{lab}: {_plain_value(value)}"
            ops = [{"op": "highlight", "target": fid}]
            if prop == "lines_of_symmetry":
                ops.append({"op": "show_symmetry", "target": fid})   # the mirror lines, drawn
            steps.append(Step(kind="deduce", uses=[fid], after=[line],
                              speech=obs.get(fid) or obs.get(lab) or "", figure_ops=ops))
        final = [ln.split(": ", 1)[-1] for ln in key_lines(item, reasons=False)][:1]
        target = prop
    if not steps:
        raise GeometryRefusal("bad_schema", "a lesson example has steps to teach")
    if not any(st.speech for st in steps):
        raise GeometryRefusal("bad_schema", "a lesson example speaks its steps")
    return WorkedExample(label="", difficulty=item.difficulty, task="solve", problem=item.prompt, givens=[],
                         target=target, intro_speech=speech.get("intro") or "", steps=steps, final_answer=final,
                         answer_speech=speech.get("answer") or "", figure=spec)


def missed_concepts(lesson: Lesson, examples: list[WorkedExample], analysis: dict | None, episode: dict | None,
                    language: str) -> list[str]:
    """The chapter's concepts the lesson, with THESE examples, does not yet
    address — measured the way the coverage gate measures a script, on the
    words the board would speak and show. Nothing to measure against (no
    analysis) is an empty list, never a guess."""
    if not analysis:
        return []
    try:
        tmp = lesson.model_copy()
        tmp.examples = list(examples)
        text = _coverage.script_text({"segments": board.compile_lesson(tmp, None, language=language)})
        rep = _coverage.measure(analysis, episode, text)
    except Exception as exc:  # noqa: BLE001 — a focus list is a hint, never a reason to fail the lesson
        logger.warning("maths lesson %r: could not measure the missed concepts: %s", lesson.topic, exc)
        return []
    return [str(x) for x in (rep.get("missed") or []) if str(x).strip()] if rep.get("checked") else []


def figure_examples(client, *, topic: str, level: str | None, language: str, context: str, n: int,
                    report: dict, focus: list[str] | None = None) -> list[WorkedExample]:
    """Up to ``n`` figure examples from the geometry engine's own call —
    unconstrained JSON on the script model, the shape the worksheet already
    yields from (the lesson's constrained call strips construction
    parameters) — each compiled onto the board once to prove it fits. The
    call's report (asked / verified / rejected) lands in ``report``."""
    items, frep = geometry_items(figure_client(client, language), topic=topic, level=level, language=language,
                                 n=n, chapter_context=context, kind="lesson", render=False, focus=focus)
    frep["focus"] = list(focus or [])
    out: list[WorkedExample] = []
    for it in items:
        try:
            ex = figure_example(it)
            board.check_figure_example(ex, language)
        except (GeometryRefusal, ValueError) as exc:
            code = getattr(exc, "code", "board")
            msg = getattr(exc, "message", str(exc))
            logger.info("figure example rejected at the board (%s): %r — %s", code, it.prompt[:60], msg[:200])
            frep["rejected"] = list(frep.get("rejected") or []) + [f"{it.prompt[:60]}: {code}: {msg[:200]}"]
            continue
        out.append(ex)
    frep["verified"] = len(out)
    report.update(frep)
    return out


def _verify_examples(client, examples: list[WorkedExample], lesson: Lesson, language: str, attempts: int,
                     history: list[dict], dropped: list[str]) -> list[WorkedExample]:
    """Verify each example, regenerate ONLY the ones that fail with the
    verifier's reasons (up to ``attempts`` times each), and return the
    verified ones in order; the rest are named in ``dropped``."""
    kept: list[WorkedExample] = []
    for ex in examples:
        rep = verify_example(ex)
        history.append({"label": ex.label, "attempt": 0, **rep.to_dict()})
        tries = 0
        while rep.status != "verified" and tries < attempts:
            tries += 1
            logger.warning("maths %s failed verification (attempt %d): %s", ex.label, tries,
                           "; ".join(rep.reasons)[:400])
            rp = build_regen_prompt(topic=lesson.topic, level=lesson.level, language=language, example=ex,
                                    reasons=rep.reasons, method_steps=lesson.method.steps)
            new = regenerate_example(client, rp)
            if new.steps:
                new.label = new.label or ex.label
                new.difficulty = ex.difficulty
                ex = new
            rep = verify_example(ex)
            history.append({"label": ex.label, "attempt": tries, **rep.to_dict()})
        if rep.status == "verified":
            kept.append(ex)
        else:
            dropped.append(f"{ex.label or 'example'}: {'; '.join(rep.reasons)[:300]}")
    return kept


def extend_to_floor(client, lesson: Lesson, report: dict, *, minutes: float, avatars: dict | None,
                    language: str, attempts: int = REGEN_ATTEMPTS,
                    rounds: int = MAX_LENGTH_ROUNDS) -> tuple[Lesson, dict]:
    """Measure the verified lesson against the floor and, while it is
    under, ask for further worked examples (and concept lines), verify
    them like the ladder's and append what survives — at most ``rounds``
    asks. The report records every round (``length_rounds``, ``extended``)
    and the final measure (``length``); a lesson still under is returned
    as it is, for the caller to refuse."""
    if not minutes or minutes <= 0:
        return lesson, report
    measured = measure_lesson(lesson, minutes, avatars, language)
    report["length_rounds"] = 0
    report.setdefault("extended", [])
    while measured.get("under") and report["length_rounds"] < rounds:
        report["length_rounds"] += 1
        n_add = _examples_to_add(lesson, measured, avatars, language)
        logger.warning("maths lesson %r under the length floor (round %d/%d): %s — asking for %d further example(s)",
                       lesson.topic, report["length_rounds"], rounds, lesson_length.summary(measured), n_add)
        data = _analyze(client, build_extend_prompt(lesson=lesson, language=language, n_examples=n_add,
                                                    measured=measured), EXTENSION_SCHEMA, MAX_TOKENS)
        new = [parse_example(e) for e in (data.get("examples") or []) if isinstance(e, dict)]
        new = [e for e in new if e.steps]
        for i, ex in enumerate(new, len(lesson.examples) + 1):
            ex.label = ex.label or f"Example {i}"
            ex.difficulty = min(3, max(2, int(ex.difficulty or 2)))
        history = report.setdefault("history", [])
        dropped = report.setdefault("dropped", [])
        kept = _verify_examples(client, new, lesson, language, attempts, history, dropped)
        for i, ex in enumerate(kept, len(lesson.examples) + 1):
            ex.label = f"Example {i}"
        lesson.examples = list(lesson.examples) + kept
        report["extended"].extend(ex.label for ex in kept)
        lines = [Line.model_validate(x) for x in (data.get("concept") or []) if isinstance(x, dict)][:3]
        lesson.concept = list(lesson.concept) + [ln for ln in lines if ln.line]
        if not kept and not lines:
            logger.warning("maths lesson %r: the extension round returned nothing verifiable", lesson.topic)
        measured = measure_lesson(lesson, minutes, avatars, language)
    report["length"] = measured
    report["examples"] = [verify_example(ex).to_dict() for ex in lesson.examples]
    return lesson, report


def verified_lesson(client, *, topic: str, subject: str | None, level: str | None, curriculum: str | None,
                    language: str, episode_context: str, attempts: int = REGEN_ATTEMPTS,
                    min_minutes: float | None = None, analysis: dict | None = None,
                    episode: dict | None = None) -> tuple[Lesson, dict]:
    """Generate, verify, regenerate what failed, and return the lesson with
    its report. Raises MathsVerificationError when too little survives."""
    prompt = build_prompt(topic=topic, subject=subject, level=level, curriculum=curriculum,
                          language=language, episode_context=episode_context, min_minutes=min_minutes)
    lesson = generate_lesson(client, prompt)
    if not lesson.examples:
        raise MathsVerificationError("the model returned a lesson with no worked examples")
    lesson.topic = lesson.topic or topic
    lesson.level = lesson.level or (level or "")
    history: list[dict] = []
    dropped: list[str] = []
    kept = _verify_examples(client, lesson.examples, lesson, language, attempts, history, dropped)
    # the diagram examples: a chapter that has none answers with an empty
    # list (the engine's prompt says so); a shapes chapter opens on a shape
    figures_report: dict = {}
    # the figure call is told which of the chapter's concepts the verified
    # ladder leaves untaught (a13f7761: three triangle questions, no polygon)
    focus = missed_concepts(lesson, kept, analysis, episode, language) if figures_enabled() else []
    figures = figure_examples(client, topic=topic, level=level, language=language, context=episode_context,
                              n=FIGURE_EXAMPLES, report=figures_report, focus=focus) if figures_enabled() else []
    if len(kept) + len(figures) < MIN_EXAMPLES:
        raise MathsVerificationError(
            f"only {len(kept)} of {len(lesson.examples)} worked examples could be verified after "
            f"{attempts} regeneration(s) each"
            + (f" and {len(figures)} figure example(s) survived" if figures_enabled() else "")
            + f" — {' | '.join(dropped)[:1500]}")
    if figures:
        # one ladder, by difficulty, figures first among equals; relabelled
        # in the order they are taught
        merged = sorted(figures + kept, key=lambda e: e.difficulty)[:4]
        for i, ex in enumerate(merged, 1):
            ex.label = f"Example {i}"
        lesson.examples = merged
    else:
        for i, ex in enumerate(kept, 1):
            ex.label = ex.label or f"Example {i}"
        lesson.examples = kept
    t = verify_try_it(lesson.try_it)
    # the try-it is TAUGHT on the board after the pause, so "could not be
    # verified" is as fatal as "wrong" — a try-it the verifier cannot read
    # once reached the board unchecked (recorded as try_it null)
    if lesson.try_it.problem and t.ok is not True:
        logger.warning("maths try-it dropped: %s", t.detail)
        lesson.try_it = TryIt()
        dropped.append(f"try-it: {t.detail[:300]}")
    report = verify_lesson(lesson)
    report["dropped"] = dropped
    report["history"] = history
    report["figures"] = figures_report
    return lesson, report


# ── the script ───────────────────────────────────────────────────────────


def to_episode_script(lesson: Lesson, report: dict, *, book_id: str, chapter_num: int, episode_num: int,
                      title: str, avatars: dict | None, language: str) -> EpisodeScript:
    segs = board.compile_lesson(lesson, avatars, language=language)
    segments: list[ScriptSegment] = []
    for s in segs:
        segments.append(ScriptSegment(
            segment_id=s["segment_id"], type=SegmentType(s["type"]), text=s["text"],
            elevenlabs_text=s["elevenlabs_text"], dialogue=s.get("dialogue"),
            slide_heading=s.get("slide_heading", ""), slide_points=s.get("slide_points") or [],
            pause_for_question=bool(s.get("pause_for_question")), scene=s.get("scene"),
            hold_secs=float(s.get("hold_secs") or 0.0),
            estimated_duration_seconds=int(s.get("estimated_duration_seconds") or 8),
        ))
    total = sum(s.estimated_duration_seconds for s in segments)
    return EpisodeScript(
        script_id=str(uuid.uuid4()), book_id=book_id, chapter_num=chapter_num, episode_num=episode_num,
        episode_title=title, generated_at=datetime.now().isoformat(), narrator_persona="Maths teacher",
        segments=segments, visual_plan=None, avatars=avatars,
        maths={"lesson": lesson.model_dump(), "verification": report, "language": language},
        total_estimated_duration_seconds=total,
        question_hook_count=sum(1 for s in segments if s.pause_for_question),
    )


def generate_maths_script(episode: dict, analysis: dict, chapter_num: int, client, *, language: str = "en",
                          avatars: dict | None = None, subject: str | None = None, curriculum: str | None = None,
                          learner_age: str | None = None, part_info: dict | None = None,
                          book_id: str = "", min_minutes: float | None = None) -> EpisodeScript:
    """The maths profile's script for one part (= one topic of the chapter).
    ``min_minutes`` is the spoken-length floor: stated in the prompt, then
    measured on the verified lesson, which is extended with further verified
    examples while it is under (extend_to_floor). The worker refuses a
    script still under it."""
    topic = str(episode.get("title") or "").strip() or "Mathematics"
    sections = [str(s) for s in (episode.get("sections_covered") or []) if str(s).strip()]
    if sections and sections[0].lower() not in topic.lower() and len(sections) == 1:
        topic = f"{topic}: {sections[0]}"
    ctx = _build_episode_context(episode, analysis)
    if part_info and int(part_info.get("total", 1) or 1) > 1:
        ctx += (f"\n\nThis is part {part_info.get('part')} of {part_info.get('total')} of the chapter: "
                f"teach ONLY this part's topic as a complete lesson of its own.")
    lesson, report = verified_lesson(
        client, topic=topic, subject=subject, level=learner_age, curriculum=curriculum,
        language=language, episode_context=ctx, min_minutes=min_minutes,
        analysis=analysis if isinstance(analysis, dict) else None, episode=episode)
    lesson, report = extend_to_floor(client, lesson, report, minutes=min_minutes or 0.0, avatars=avatars,
                                     language=language)
    n_ok = sum(1 for e in report.get("examples", []) if e.get("status") == "verified")
    logger.info("maths lesson %r: %d verified example(s), %d dropped, status %s", lesson.topic, n_ok,
                len(report.get("dropped") or []), report.get("status"))
    return to_episode_script(lesson, report, book_id=book_id, chapter_num=chapter_num,
                             episode_num=int(episode.get("episode_num", 1) or 1),
                             title=lesson.topic or topic, avatars=avatars, language=language)


__all__ = ["MathsVerificationError", "build_prompt", "build_regen_prompt", "build_extend_prompt",
           "figure_example", "figure_examples", "figures_enabled", "missed_concepts", "FIGURE_EXAMPLES",
           "generate_lesson", "regenerate_example", "verified_lesson", "extend_to_floor", "measure_lesson",
           "to_episode_script", "generate_maths_script", "REGEN_ATTEMPTS", "MIN_EXAMPLES",
           "MAX_LENGTH_ROUNDS", "MAX_EXAMPLES_PER_ROUND", "EXTENSION_SCHEMA"]
