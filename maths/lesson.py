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
import uuid
from datetime import datetime

from agent3_scripts.models import EpisodeScript, ScriptSegment, SegmentType
from agent3_scripts.script_generator import _build_episode_context
from maths import board
from maths.schema import (EXAMPLE_SCHEMA, LESSON_SCHEMA, DIFFICULTY_NAMES, Lesson, TryIt,
                          WorkedExample, parse_example, parse_lesson)
from maths.verify import verify_example, verify_lesson, verify_try_it

logger = logging.getLogger("worker")

REGEN_ATTEMPTS = 2
MIN_EXAMPLES = 2
MAX_TOKENS = 20000
JOB_KIND = "maths_lesson"


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
  - every 'after' must be exactly what the student would write on the next line of working"""

_STEP_RULES = """=== STEPS ===
Each step is ONE operation, machine-checkable:
  - kind "transform": 'before' is the current state, 'after' is the state after ONE valid operation (subtract 5 from both sides; divide by 3; expand the bracket; collect like terms; factorise; add the equations). The two states MUST be mathematically equivalent (same solutions). 'operation' names the operation in at most six words. 'explanation' is a short board note (at most six words), e.g. "subtract 5 from both sides".
  - kind "setup": introducing a variable or translating words into an equation ("let x be the number of tickets"); 'after' is the equation(s) written down. Use only for word problems.
  - kind "check": substituting the answer back: 'after' is a TRUE numeric statement like "3*5 + 5 = 20" with no variables left.
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
   Every example: 'label' ("Example 1"), 'task' (solve | solve_system | solve_inequality | simplify | expand | factorise | evaluate), 'problem' (as the student reads it: notation, or the word problem in words), 'givens' (the equations or expression the working starts from, in notation), 'target' ("x", "x, y" or "expression"), 'intro_speech' (the teacher introducing the example, in words), optional 'student_question', the 'steps', 'final_answer' (one relation or expression per string), 'answer_speech'.
   Examples 1-3 stay clean and progressive: no wrong routes there.
4. recap: 2-4 spoken lines restating the method, and 'misconceptions': 1-3 board points naming the mistakes students make most (at most ten words each).
5. try_it: one problem for the student to pause on (difficulty like example 2): 'problem', 'answer' (notation), 'speech' (the teacher setting it and telling the learner to pause the video and try it, in words), then — because the video resumes by solving it on the board — 'solution_speech' (one sentence resuming after the pause, e.g. inviting the learner to compare their working), 'steps' (2-4 steps in exactly the format of an example's steps) and 'answer_speech'.
6. closing: one or two spoken sentences ending the lesson: the teacher hopes the learner now understands {topic} better and encourages a little practice.
Keep every spoken line natural, in {language}, for a learner of {level}. The whole lesson should run 8-12 minutes when spoken."""

_OUTPUT = """=== OUTPUT ===
Return one JSON object with exactly these keys: topic, level, hook, concept, concept_points, method, examples, recap, misconceptions, try_it, closing.
hook/concept/recap are arrays of {{"who": "teacher"|"student", "line": "..."}}. method is {{"title": "...", "steps": [...]}}. examples is the array described above. try_it is {{"problem": "...", "answer": ["..."], "speech": "...", "solution_speech": "...", "steps": [...], "answer_speech": "..."}}. closing is a string.
Minified JSON. No trailing commas. No comments."""


def _difficulty_note(level: str) -> str:
    return ("Difficulty is judged for THIS level: what is 'extremely difficult' for a Class 8 learner is "
            "not an A-level problem. Never leave the syllabus of the level.")


def build_prompt(*, topic: str, subject: str | None, level: str | None, curriculum: str | None,
                 language: str, episode_context: str, n_examples: int = 4) -> str:
    lang = language or "en"
    lvl = level or "school"
    head = (f"SUBJECT: {subject or 'Mathematics'}\nTOPIC: {topic}\nLEARNER LEVEL: {lvl}\n"
            f"CURRICULUM: {curriculum or 'not specified'}\nLANGUAGE OF NARRATION: {lang}\n\n"
            f"{episode_context}\n")
    body = _LADDER.format(n_examples=n_examples, difficulty_note=_difficulty_note(lvl),
                          language=lang, level=lvl, topic=topic)
    return "\n\n".join([head, _NOTATION_RULES, _STEP_RULES, body, _OUTPUT])


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


def verified_lesson(client, *, topic: str, subject: str | None, level: str | None, curriculum: str | None,
                    language: str, episode_context: str, attempts: int = REGEN_ATTEMPTS) -> tuple[Lesson, dict]:
    """Generate, verify, regenerate what failed, and return the lesson with
    its report. Raises MathsVerificationError when too little survives."""
    prompt = build_prompt(topic=topic, subject=subject, level=level, curriculum=curriculum,
                          language=language, episode_context=episode_context)
    lesson = generate_lesson(client, prompt)
    if not lesson.examples:
        raise MathsVerificationError("the model returned a lesson with no worked examples")
    lesson.topic = lesson.topic or topic
    lesson.level = lesson.level or (level or "")
    history: list[dict] = []
    kept: list[WorkedExample] = []
    dropped: list[str] = []
    for ex in lesson.examples:
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
    if len(kept) < MIN_EXAMPLES:
        raise MathsVerificationError(
            f"only {len(kept)} of {len(lesson.examples)} worked examples could be verified after "
            f"{attempts} regeneration(s) each — {' | '.join(dropped)[:1500]}")
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
                          book_id: str = "") -> EpisodeScript:
    """The maths profile's script for one part (= one topic of the chapter)."""
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
        language=language, episode_context=ctx)
    n_ok = sum(1 for e in report.get("examples", []) if e.get("status") == "verified")
    logger.info("maths lesson %r: %d verified example(s), %d dropped, status %s", lesson.topic, n_ok,
                len(report.get("dropped") or []), report.get("status"))
    return to_episode_script(lesson, report, book_id=book_id, chapter_num=chapter_num,
                             episode_num=int(episode.get("episode_num", 1) or 1),
                             title=lesson.topic or topic, avatars=avatars, language=language)


__all__ = ["MathsVerificationError", "build_prompt", "build_regen_prompt", "generate_lesson",
           "regenerate_example", "verified_lesson", "to_episode_script", "generate_maths_script",
           "REGEN_ATTEMPTS", "MIN_EXAMPLES"]
