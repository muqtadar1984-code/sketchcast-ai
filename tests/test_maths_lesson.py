"""Generation -> verification -> regeneration of what failed -> a script the
existing pipeline can render."""

from __future__ import annotations

import copy
import json

import pytest

from maths import lesson as L
from maths.speech import has_notation
from spike.scene_engine.director import parse_scene_response
from spike.scene_engine.render import SceneRenderer

GOOD_EX1 = {
    "label": "Example 1", "difficulty": 1, "task": "solve", "problem": "3x + 5 = 20", "givens": ["3x + 5 = 20"],
    "target": "x", "intro_speech": "Let's start simple: three x plus five equals twenty.",
    "student_question": "Do we deal with the five or the three first?",
    "steps": [
        {"kind": "transform", "operation": "subtract 5 from both sides", "before": ["3x + 5 = 20"], "after": ["3x = 15"],
         "explanation": "subtract 5 from both sides", "speech": "The five was added last, so we undo it first. Subtract five from both sides: three x equals fifteen."},
        {"kind": "transform", "operation": "divide both sides by 3", "before": ["3x = 15"], "after": ["x = 5"],
         "explanation": "divide both sides by 3", "speech": "Now divide both sides by three. So x equals five.", "student": "So x is five?"},
        {"kind": "check", "operation": "check", "before": ["x = 5"], "after": ["3*5 + 5 = 20"], "speech": "Check it: three fives are fifteen, plus five is twenty."},
    ],
    "final_answer": ["x = 5"], "answer_speech": "So the solution is x equals five.",
}
BAD_EX2 = {
    "label": "Example 2", "difficulty": 2, "task": "solve", "problem": "2(x - 3) = 8", "givens": ["2(x - 3) = 8"],
    "target": "x", "intro_speech": "Now one with a bracket.",
    "steps": [
        {"kind": "transform", "operation": "expand the bracket", "before": ["2(x - 3) = 8"], "after": ["2x - 3 = 8"],
         "speech": "Expand the bracket."},          # WRONG: 2x - 6
        {"kind": "transform", "operation": "add 3 to both sides", "before": ["2x - 3 = 8"], "after": ["2x = 11"], "speech": "Add three."},
        {"kind": "transform", "operation": "divide by 2", "before": ["2x = 11"], "after": ["x = 11/2"], "speech": "Divide by two."},
    ],
    "final_answer": ["x = 11/2"], "answer_speech": "x is eleven over two.",
}
FIXED_EX2 = {
    **BAD_EX2,
    "steps": [
        {"kind": "transform", "operation": "expand the bracket", "before": ["2(x - 3) = 8"], "after": ["2x - 6 = 8"],
         "explanation": "expand the bracket", "speech": "Expand the bracket: two x minus six equals eight."},
        {"kind": "transform", "operation": "add 6 to both sides", "before": ["2x - 6 = 8"], "after": ["2x = 14"],
         "explanation": "add 6 to both sides", "speech": "Add six to both sides: two x equals fourteen."},
        {"kind": "transform", "operation": "divide both sides by 2", "before": ["2x = 14"], "after": ["x = 7"],
         "explanation": "divide by 2", "speech": "Divide both sides by two: x equals seven."},
    ],
    "final_answer": ["x = 7"], "answer_speech": "So x equals seven.",
}
EX3 = {
    "label": "Example 3", "difficulty": 3, "task": "solve", "problem": "5x - 2 = 3x + 8", "givens": ["5x - 2 = 3x + 8"],
    "target": "x", "intro_speech": "Terms on both sides now.",
    "steps": [
        {"kind": "transform", "operation": "subtract 3x from both sides", "before": ["5x - 2 = 3x + 8"], "after": ["2x - 2 = 8"],
         "explanation": "subtract 3x from both sides", "speech": "Bring the x terms together: subtract three x from both sides."},
        {"kind": "transform", "operation": "add 2 to both sides", "before": ["2x - 2 = 8"], "after": ["2x = 10"],
         "explanation": "add 2 to both sides", "speech": "Add two to both sides: two x equals ten."},
        {"kind": "transform", "operation": "divide both sides by 2", "before": ["2x = 10"], "after": ["x = 5"],
         "explanation": "divide by 2", "speech": "Divide by two: x equals five."},
    ],
    "final_answer": ["x = 5"], "answer_speech": "So x equals five.",
    "common_mistake": {"from_state": ["5x - 2 = 3x + 8"], "wrong_state": ["8x - 2 = 8"], "operation": "add 3x instead",
                       "why_wrong": "moving 3x across changes its sign", "speech": "A common slip is to add the three x instead of subtracting it."},
}
LESSON = {
    "topic": "Linear Equations in One Variable", "level": "Class 8",
    "hook": [{"who": "teacher", "line": "Have you ever worked out a number from a clue like double it and add five gives twenty-one?"}],
    "concept": [{"who": "teacher", "line": "An equation is a balance. Whatever we do to one side we do to the other."},
                {"who": "student", "line": "Why both sides?"},
                {"who": "teacher", "line": "Because the two sides are equal, and they must stay equal."}],
    "concept_points": ["An equation is a balance", "Undo operations in reverse order", "Check by substituting back"],
    "method": {"title": "METHOD", "steps": ["Simplify both sides", "Move variable terms", "Move constants", "Divide by the coefficient", "Check"]},
    "examples": [GOOD_EX1, BAD_EX2, EX3],
    "recap": [{"who": "teacher", "line": "Undo in reverse, keep the balance, and always check your answer."}],
    "misconceptions": ["Moving a term across changes its sign", "Divide every term, not just one"],
    "try_it": {"problem": "4x + 3 = 19", "answer": ["x = 4"], "speech": "Try this one: four x plus three equals nineteen. Pause and solve it.",
               "solution_speech": "Let us compare our working.",
               "steps": [
                   {"kind": "transform", "operation": "subtract 3 from both sides", "before": ["4x + 3 = 19"], "after": ["4x = 16"],
                    "explanation": "subtract 3 from both sides", "speech": "Subtract three from both sides: four x equals sixteen."},
                   {"kind": "transform", "operation": "divide both sides by 4", "before": ["4x = 16"], "after": ["x = 4"],
                    "explanation": "divide by 4", "speech": "Divide both sides by four: x equals four."}],
               "answer_speech": "So x equals four. Did you get the same?"},
    "closing": "I hope you now have a better understanding of linear equations. Practise a few more and see you next time.",
}


class FakeClient:
    """Answers the lesson prompt with LESSON and the regeneration prompt for
    Example 2 with FIXED_EX2 (the first regeneration) — recording each call."""

    model = "fake"

    def __init__(self, lesson=None, fixed=None, fail_regen=False, figures=None):
        self.lesson = copy.deepcopy(lesson or LESSON)
        self.fixed = copy.deepcopy(fixed or FIXED_EX2)
        self.fail_regen = fail_regen
        self.figures = copy.deepcopy(figures or [])   # the geometry engine's call: no diagrams unless given
        self.calls: list[dict] = []

    def analyze(self, prompt, system="", max_tokens=0, retries=3, cache_prefix=None, response_schema=None, **kw):
        self.calls.append({"prompt": prompt, "schema": response_schema, "max_tokens": max_tokens})
        if "geometry.figure.v1" in prompt:
            return {"data": {"questions": self.figures}, "usage": {}, "truncated": False}
        if "REJECTED" in prompt:
            data = copy.deepcopy(BAD_EX2) if self.fail_regen else self.fixed
            return {"data": data, "usage": {}, "truncated": False}
        return {"data": self.lesson, "usage": {}, "truncated": False}


def test_a_failed_example_is_regenerated_with_the_verifiers_reasons_and_only_that_one():
    c = FakeClient()
    lesson, report = L.verified_lesson(c, topic="Linear equations", subject="Mathematics", level="Class 8",
                                       curriculum="CBSE", language="en", episode_context="")
    assert report["status"] == "verified"
    assert [e.label for e in lesson.examples] == ["Example 1", "Example 2", "Example 3"]
    assert lesson.examples[1].final_answer == ["x = 7"]
    regen = [k for k in c.calls if "REJECTED" in k["prompt"]]
    assert len(regen) == 1, "only the failing example goes back"
    assert "step 1" in regen[0]["prompt"] and "not equivalent" in regen[0]["prompt"].lower() or "WRONG" in regen[0]["prompt"]
    assert regen[0]["schema"] is not None and regen[0]["schema"]["properties"].get("steps")
    assert c.calls[0]["schema"]["required"] and "examples" in c.calls[0]["schema"]["properties"]
    assert len(report["history"]) == 4 and report["dropped"] == []


def test_an_example_that_never_verifies_is_dropped_when_enough_remain():
    c = FakeClient(fail_regen=True)
    lesson, report = L.verified_lesson(c, topic="t", subject=None, level=None, curriculum=None, language="en",
                                       episode_context="", attempts=2)
    assert [e.label for e in lesson.examples] == ["Example 1", "Example 3"]
    assert len(report["dropped"]) == 1 and "Example 2" in report["dropped"][0]
    assert sum(1 for k in c.calls if "REJECTED" in k["prompt"]) == 2


def test_too_few_verified_examples_fails_loudly():
    bad = copy.deepcopy(LESSON)
    bad["examples"] = [BAD_EX2, {**BAD_EX2, "label": "Example 3"}]
    c = FakeClient(lesson=bad, fail_regen=True)
    with pytest.raises(L.MathsVerificationError) as exc:
        L.verified_lesson(c, topic="t", subject=None, level=None, curriculum=None, language="en", episode_context="")
    assert "0 of 2" in str(exc.value) and "Example 2" in str(exc.value)


def test_wrong_try_it_steps_are_dropped_with_the_question_and_the_closing_stays():
    """The solution is taught on the board, so its steps are verified like an
    example's; a try-it that fails is left out with its solution, and the
    lesson still ends with the sign-off."""
    bad = copy.deepcopy(LESSON)
    bad["try_it"]["steps"][0]["after"] = ["4x = 22"]
    c = FakeClient(lesson=bad)
    lesson, report = L.verified_lesson(c, topic="Linear equations", subject="Mathematics", level="Class 8",
                                       curriculum=None, language="en", episode_context="")
    assert lesson.try_it.problem == "" and report["status"] == "verified"
    from maths.board import compile_lesson
    types = [s["type"] for s in compile_lesson(lesson)]
    assert types[-2:] == ["synthesis", "preview"] and "question_hook" not in types


def test_the_closing_falls_back_to_the_topic_when_the_model_gave_none():
    from maths.board import closing_segment
    from maths.schema import Lesson, MethodCard
    seg = closing_segment(Lesson(topic="Linear equations", method=MethodCard(steps=["Move constants"])), "s9")
    assert seg["type"] == "preview" and "better understanding of Linear equations" in seg["text"]
    assert any(e["id"] == "card_box" for e in seg["scene"]["elements"])


def test_a_wrong_try_it_is_dropped_not_taught():
    bad = copy.deepcopy(LESSON)
    bad["try_it"]["answer"] = ["x = 5"]
    lesson, report = L.verified_lesson(FakeClient(lesson=bad), topic="t", subject=None, level=None, curriculum=None,
                                       language="en", episode_context="")
    assert lesson.try_it.problem == "" and report["status"] == "verified"


def test_the_script_has_the_blueprint_shape_and_renders():
    c = FakeClient()
    episode = {"title": "Linear Equations in One Variable", "episode_num": 1, "sections_covered": ["2.1 Linear Equations"],
               "key_concepts_introduced": []}
    script = L.generate_maths_script(episode, {"concepts": {"concepts": []}}, 2, c, language="en",
                                     avatars={"teacher": "avatar_teacher", "student": "avatar_student"},
                                     subject="Mathematics", learner_age="Class 8", book_id="bk")
    types = [s.type.value for s in script.segments]
    assert types == ["hook", "explore", "explore", "explore", "explore", "synthesis", "question_hook",
                     "explore", "preview"]
    assert script.maths["verification"]["status"] == "verified"
    try_it, solution, closing = script.segments[-3:]
    assert try_it.pause_for_question and try_it.hold_secs == 3.0
    assert solution.hold_secs == 0.0 and closing.hold_secs == 0.0
    # the solution is the try-it worked like an example: its states written, the answer underlined
    exprs = [e.get("expr") for e in solution.scene["elements"] if e["type"] == "math"]
    assert "4x = 16" in exprs and "x = 4" in exprs
    assert any(a["verb"] == "underline" for a in solution.scene["actions"])
    assert "compare our working" in solution.text and "x equals four" in solution.text
    assert closing.text.startswith("I hope you now have a better understanding")
    ex1 = script.segments[2]
    assert ex1.dialogue and any(d["who"] == "student" for d in ex1.dialogue), "two voices in a worked example"
    for s in script.segments:
        assert s.scene and s.text
        assert not has_notation(s.text), s.text
        assert s.text == " ".join(d["line"] for d in s.dialogue) if s.dialogue else True
        sc = parse_scene_response(s.scene, s.text)
        assert sc is not None, s.segment_id
        r = SceneRenderer(sc)
        r.compile(30.0)
        assert not any(w.startswith("CUE_UNRESOLVED") for w in r.audit()["warnings"]), (s.segment_id, r.audit()["warnings"])
    kinds = {e["type"] for e in ex1.scene["elements"]}
    assert "math" in kinds and "arrow" in kinds and "text" in kinds
    verbs = [a["verb"] for a in ex1.scene["actions"]]
    assert verbs.count("write") >= 4 and "fade" in verbs and "underline" in verbs
    marker = [e for e in ex1.scene["elements"] if e.get("color") == "marker"]
    assert marker and any(a["verb"] == "draw" and a["target"] == marker[0]["id"] for a in ex1.scene["actions"])
    assert all(s.dialogue for s in script.segments), "every maths segment is per-line dialogue"
    mistake_seg = script.segments[4]
    assert any(a["verb"] == "draw" and a["target"].startswith("strike") for a in mistake_seg.scene["actions"])
    dump = json.loads(script.model_dump_json())
    assert dump["maths"]["lesson"]["examples"][1]["final_answer"] == ["x = 7"]


def test_the_prompt_carries_the_ladder_the_notation_rules_and_the_level():
    p = L.build_prompt(topic="Factorising quadratics", subject="Mathematics", level="Year 10", curriculum="GCSE",
                       language="en", episode_context="KEY CONCEPTS TO TEACH: factorising")
    for needle in ("simplest", "medium", "difficult", "extremely difficult", "common_mistake", "LINEAR notation",
                   "sqrt(", "Year 10", "GCSE", "KEY CONCEPTS TO TEACH", "try_it", "method"):
        assert needle in p, needle


def test_a_wipe_takes_the_notes_and_leaders_with_the_lines():
    """Production 2026-09-24: after the column filled and was wiped, the next
    notes were written over the old ones (TEXT_OVERLAP n2+n15)."""
    from maths.board import example_scene
    from maths.schema import MethodCard, Step, WorkedExample
    steps = []
    state = ["x + 100 = 200"]
    for k in range(9):
        nxt = [f"x + {100 - (k + 1) * 10} = {200 - (k + 1) * 10}"]
        steps.append(Step(operation=f"subtract 10 from both sides", before=state, after=nxt,
                          speech=f"Step number {k + 1}: subtract ten from both sides of the equation."))
        state = nxt
    ex = WorkedExample(label="Long", task="solve", problem="x + 100 = 200", givens=["x + 100 = 200"], target="x",
                       steps=steps, final_answer=["x = 100"], answer_speech="So x is one hundred.")
    scene, _lines = example_scene(ex, MethodCard(steps=["Move constants"]), "s9")
    wipes = [a for a in scene["actions"] if a["verb"] == "erase"]
    assert wipes, "nine lines do not fit the column"
    grp = next(e for e in scene["elements"] if e["id"] == wipes[0]["target"])
    assert any(c.startswith("n") for c in grp["children"]) and any(c.startswith("w") for c in grp["children"])
    from spike.scene_engine.director import parse_scene_response
    from spike.scene_engine.render import SceneRenderer
    r = SceneRenderer(parse_scene_response(scene, scene["narration"]))
    r.compile(60.0)
    # the audit measures boxes without regard to time, so an erased note under
    # a later one still counts; what must not happen is two LIVE notes overlapping
    erased = set(grp["children"])
    live_overlaps = [w for w in r.audit()["warnings"] if w.startswith("TEXT_OVERLAP")
                     and not (set(w.split()[1].split("+")) & erased)]
    assert not live_overlaps, live_overlaps


def _long_example(problem="x + 100 = 200", n=9, check=True, mistake=False):
    from maths.schema import Mistake, Step, WorkedExample
    steps = []
    state = [problem] if "=" in problem else ["x + 100 = 200"]
    for k in range(n):
        nxt = [f"x + {100 - (k + 1) * 10} = {200 - (k + 1) * 10}"] if k < n - 1 else ["x = 100"]
        steps.append(Step(operation="subtract 10 from both sides", before=state, after=nxt,
                          speech=f"Step number {k + 1}: subtract ten from both sides of the equation."))
        state = nxt
    if check:
        steps.append(Step(kind="check", operation="substitute", before=state, after=["100 + 100 = 200"],
                          speech="Now we check by substituting one hundred back in."))
    m = Mistake(from_state=[problem], wrong_state=["x = 300"], operation="add 100", why_wrong="wrong direction",
                speech="A common mistake is to add one hundred instead of subtracting.") if mistake else None
    return WorkedExample(label="Long", task="solve", problem=problem, givens=["x + 100 = 200"], target="x",
                         steps=steps, final_answer=["x = 100"], answer_speech="So the answer is x equals one hundred.",
                         common_mistake=m)


def _compiled(scene):
    from spike.scene_engine.director import parse_scene_response
    from spike.scene_engine.render import SceneRenderer
    r = SceneRenderer(parse_scene_response(scene, scene["narration"]))
    r.compile(80.0)
    return r


def test_the_answer_is_written_again_when_the_check_wipes_the_column():
    """Maths demo 2026-09-24: the check line wiped the column and the answer
    underline was drawn where the wiped row had been — a squiggle under
    nothing while the teacher said the answer."""
    from maths.board import example_scene
    from maths.schema import MethodCard
    scene, _ = example_scene(_long_example(), MethodCard(steps=["Move constants", "Check"]), "s9")
    wipes = [a for a in scene["actions"] if a["verb"] == "erase"]
    assert wipes
    erased = {c for a in wipes for c in next(e for e in scene["elements"] if e["id"] == a["target"])["children"]}
    underlines = [a for a in scene["actions"] if a["verb"] == "underline"]
    assert underlines and not any(a["target"] in erased for a in underlines)
    by_id = {e["id"]: e for e in scene["elements"]}
    assert by_id[underlines[-1]["target"]]["expr"] == "x = 100"
    # the answer sits above the check line, both live after the wipe
    check = next(e for e in scene["elements"] if e.get("expr") == "100 + 100 = 200")
    assert by_id[underlines[-1]["target"]]["at"][1] < check["at"][1]
    assert not any(w.startswith("CUE_UNRESOLVED") for w in _compiled(scene).audit()["warnings"])


def test_a_wipe_restarts_below_a_word_problem():
    """The first line after a wipe sat on the word problem's second line."""
    from maths.board import example_scene
    from maths.schema import MethodCard
    ex = _long_example(problem="The sum of two consecutive numbers is 201 and we need the smaller number of the two.")
    scene, _ = example_scene(ex, MethodCard(steps=["Move constants"]), "s9")
    q1 = next(e for e in scene["elements"] if e["id"] == "q1")
    rows = [e for e in scene["elements"] if e["type"] == "math" and e["id"].startswith("w")]
    assert min(e["at"][1] for e in rows) >= q1["at"][1] + 46
    warns = _compiled(scene).audit()["warnings"]
    assert not any(w.startswith("TEXT_OVERLAP") and "q1" in w for w in warns), warns


def test_the_card_highlight_moves_instead_of_piling_up():
    from maths.board import example_scene
    from maths.schema import MethodCard, Step, WorkedExample
    steps = [Step(operation="subtract 5 from both sides", before=["3x + 5 = 20"], after=["3x = 15"],
                  speech="First subtract five from both sides."),
             Step(operation="divide both sides by 3", before=["3x = 15"], after=["x = 5"],
                  speech="Then divide both sides by three.")]
    ex = WorkedExample(label="E", task="solve", problem="3x + 5 = 20", givens=["3x + 5 = 20"], target="x",
                       steps=steps, final_answer=["x = 5"], answer_speech="So x is five.")
    scene, _ = example_scene(ex, MethodCard(steps=["Move the constants", "Divide by the coefficient"]), "s3")
    hl = [e for e in scene["elements"] if e.get("color") == "marker"]
    assert len(hl) == 2
    acts = scene["actions"]
    i_first = next(i for i, a in enumerate(acts) if a["verb"] == "draw" and a["target"] == hl[0]["id"])
    i_fade = next(i for i, a in enumerate(acts) if a["verb"] == "fade" and a["target"] == hl[0]["id"])
    i_second = next(i for i, a in enumerate(acts) if a["verb"] == "draw" and a["target"] == hl[1]["id"])
    assert i_first < i_fade < i_second
    assert not any(a["verb"] == "highlight" for a in acts)
    # the marker covers the whole card line, with room to spare
    from maths.board import CARD_X, _M, CARD_LINE_SIZE
    line = next(e for e in scene["elements"] if e["id"] == "card_1")
    assert hl[1]["points"][1][0] >= CARD_X + _M.text_width(line["text"], CARD_LINE_SIZE)


def test_abbreviated_sides_are_spoken_as_words():
    from maths.board import say
    assert say("Substitute back to verify that L.H.S. = R.H.S.") == \
        "Substitute back to verify that the left-hand side equals the right-hand side."
    assert say("Check the LHS equals the RHS. Then stop.") == "Check the left-hand side equals the right-hand side. Then stop."
    assert say("The L.H.S. is 4.") == "The left-hand side is 4."


def test_a_teacher_only_segment_is_still_dialogue():
    from maths.board import _segment
    from maths.schema import Line
    seg = _segment("s3", "explore", [Line(line="One."), Line(line="Two.")], heading="h", points=[])
    assert seg["dialogue"] == [{"who": "teacher", "line": "One."}, {"who": "teacher", "line": "Two."}]


def _five_step_scene():
    from maths.board import example_scene
    from maths.schema import MethodCard, Step, WorkedExample
    from spike.scene_engine.whiteboard import narration_stream, student_element, teacher_element
    steps = [Step(operation="subtract n from both sides", before=["5n - 17 = n + 40"], after=["4n - 17 = 40"],
                  speech="We transpose the positive n from the right side to the left side, where it becomes subtraction."),
             Step(operation="add 17 to both sides", before=["4n - 17 = 40"], after=["4n = 57"],
                  speech="Now add seventeen to both sides of the equation to move the constant across.")]
    ex = WorkedExample(label="Example 3", task="solve", problem="5n - 17 = n + 40", givens=["5n - 17 = n + 40"],
                       target="n", steps=steps, final_answer=["n = 57/4"], answer_speech="So n is fifty-seven over four.",
                       intro_speech="Here is a number puzzle that we turn into an equation before solving it.")
    method = MethodCard(title="Solving Linear Equations", steps=["Write down the equation", "Isolate the variable term",
                                                                  "Perform inverse operations", "Find the variable value",
                                                                  "Check your answer"])
    scene, lines = example_scene(ex, method, "s005")
    d = [{"who": l.who, "line": l.line} for l in lines]
    scene["elements"] += [teacher_element(), student_element()]
    els, acts = narration_stream(scene["narration"], uid="s005", dialogue=d,
                                 line_starts=[5.0 * i for i in range(len(d))], total_secs=5.0 * len(d))
    scene["elements"] += els
    scene["actions"] += acts
    return scene


def test_a_five_line_card_keeps_its_last_line_under_a_tall_caption():
    """Maths demo 2026-09-24: '5. Check your answer' was lettered above the
    card's title — the caption keep-out, taken from a bubble's first text
    line, landed on the card's last line and the collision pass stacked it
    to the top row."""
    scene = _five_step_scene()
    box = next(e for e in scene["elements"] if e["id"] == "card_box")
    assert max(p[1] for p in box["points"]) <= 270, "the frame clears the tallest bubble"
    r = _compiled(scene)
    authored = {e["id"]: e["at"][1] for e in scene["elements"] if e["id"].startswith("card_") and "at" in e}
    for k in range(5):
        assert abs(r.bound[f"card_{k}"].box[1] - authored[f"card_{k}"]) < 1.0, (k, r.audit()["warnings"])
    assert not any(w.startswith("LABEL_MOVED") for w in r.audit()["warnings"]), r.audit()["warnings"]


def test_the_recap_heading_matches_its_points():
    from maths.board import recap_segment
    from maths.schema import Lesson, MethodCard
    lesson = Lesson(topic="Linear equations", method=MethodCard(steps=["Move constants"]),
                    misconceptions=["Forgetting to change the sign of a term during transposition."])
    seg = recap_segment(lesson, "s006")
    assert seg["slide_heading"] == "Common mistakes"
    assert next(e for e in seg["scene"]["elements"] if e["id"] == "wb_h")["text"] == "Common mistakes"


def test_board_text_is_never_relocated_by_the_caption_keep_out():
    """Production 2026-09-24 (overlapping_text=9): notes under the caption
    band were moved off it and stacked up over the word problem."""
    from maths.board import example_scene
    from maths.schema import MethodCard, Step, WorkedExample
    from spike.scene_engine.whiteboard import narration_stream, student_element, teacher_element
    long_line = "(2x - 3)/3 - (x - 5)/2 = 4"
    steps = [Step(operation="multiply both sides by 6", before=[long_line], after=["2(2x - 3) - 3(x - 5) = 24"],
                  speech="Multiply every term by six, the lowest common multiple of the denominators."),
             Step(operation="expand the brackets", before=["2(2x - 3) - 3(x - 5) = 24"], after=["4x - 6 - 3x + 15 = 24"],
                  speech="Expand both brackets carefully, watching the signs."),
             Step(operation="collect like terms", before=["4x - 6 - 3x + 15 = 24"], after=["x + 9 = 24"],
                  speech="Collect the x terms and the numbers."),
             Step(operation="subtract 9 from both sides", before=["x + 9 = 24"], after=["x = 15"],
                  speech="Subtract nine from both sides to finish.")]
    ex = WorkedExample(label="Example 4", task="solve", problem=long_line, givens=[long_line], target="x",
                       steps=steps, final_answer=["x = 15"], answer_speech="So x equals fifteen.",
                       intro_speech="A harder one with fractions on both sides now.")
    scene, lines = example_scene(ex, MethodCard(steps=["Clear fractions", "Expand", "Collect", "Solve"]), "s006")
    assert all(e.get("fixed") for e in scene["elements"] if e["type"] == "text")
    d = [{"who": l.who, "line": l.line} for l in lines]
    scene["elements"] += [teacher_element(), student_element()]
    els, acts = narration_stream(scene["narration"], uid="s006", dialogue=d,
                                 line_starts=[6.0 * i for i in range(len(d))], total_secs=6.0 * len(d))
    scene["elements"] += els
    scene["actions"] += acts
    r = _compiled(scene)
    warns = r.audit()["warnings"]
    assert not any(w.startswith("LABEL_MOVED") for w in warns), warns
    for e in scene["elements"]:
        if e["type"] == "text" and e["id"].startswith("n"):
            assert abs(r.bound[e["id"]].box[1] + (r.bound[e["id"]].box[3] - r.bound[e["id"]].box[1]) / 2 - e["at"][1]) < 2.0


def test_an_unverifiable_try_it_is_dropped_too():
    """Production 2026-09-24: a word-problem try-it whose text is not
    notation reached the board with try_it recorded as null."""
    bad = copy.deepcopy(LESSON)
    bad["try_it"] = {"problem": "A number doubled is fourteen. Find it.", "answer": ["n = 7"],
                     "speech": "Try this one.", "steps": [], "answer_speech": "n is seven."}
    c = FakeClient(lesson=bad)
    lesson, report = L.verified_lesson(c, topic="Linear equations", subject="Mathematics", level="Class 8",
                                       curriculum=None, language="en", episode_context="")
    assert lesson.try_it.problem == "" and any(x.startswith("try-it") for x in report["dropped"])


def test_the_board_speaks_and_writes_in_the_lesson_language():
    """Founder ask 2026-09-24: the maths profile for every lesson language.
    The board's fixed strings and fallbacks follow the language; the
    scenes still compile with Arabic and Hindi text on them."""
    from maths.board import compile_lesson, say
    from maths.schema import Lesson, MethodCard, TryIt, Step
    from spike.scene_engine.director import parse_scene_response
    from spike.scene_engine.render import SceneRenderer
    steps = [Step(operation="subtract 3 from both sides", before=["4x + 3 = 19"], after=["4x = 16"], speech="اطرح ثلاثة."),
             Step(operation="divide both sides by 4", before=["4x = 16"], after=["x = 4"], speech="اقسم على أربعة.")]
    lesson = Lesson(topic="المعادلات الخطية", method=MethodCard(steps=["انقل الثوابت", "اقسم على المعامل"]),
                    try_it=TryIt(problem="4x + 3 = 19", answer=["x = 4"], speech="جرّب هذه.", steps=steps,
                                 answer_speech="إذن x يساوي أربعة."))
    segs = compile_lesson(lesson, language="ar")
    by_type = {s["type"]: s for s in segs}
    assert by_type["question_hook"]["slide_heading"] == "جرّب بنفسك"
    pause = next(e for e in by_type["question_hook"]["scene"]["elements"] if e["id"] == "pause")
    assert pause["text"] == "أوقف الفيديو وجرّب"
    assert by_type["synthesis"]["slide_heading"] == "أخطاء شائعة"
    assert by_type["preview"]["text"].startswith("أرجو أن تكون قد فهمت المعادلات الخطية")
    card = next(e for e in by_type["preview"]["scene"]["elements"] if e["id"] == "card_title")
    assert card["text"] == "الطريقة"
    sol = [s for s in segs if s["type"] == "explore"][-1]
    assert sol["text"].startswith("هيا نحلّها معًا.")
    for s in segs[1:]:
        sc = parse_scene_response(s["scene"], s["text"])
        assert sc is not None, s["segment_id"]
        SceneRenderer(sc).compile(20.0)
    # notation that slips into a spoken line is said in the lesson's words
    assert say("تحقق أن L.H.S. = R.H.S.", "ar") == "تحقق أن الطرف الأيسر يساوي الطرف الأيمن."
    hi = compile_lesson(lesson, language="hi")
    assert {s["type"]: s for s in hi}["question_hook"]["slide_heading"] == "अब आप कीजिए"
    assert compile_lesson(lesson, language="xx")[-1]["text"].startswith("I hope you now have")


_SCRIPTS = {
    "ar": dict(topic="المعادلات الخطية في متغير واحد", title="طريقة الحل",
               steps=["اكتب المعادلة", "اعزل الحد المتغير", "نفّذ العمليات العكسية", "أوجد قيمة المتغير", "تحقق من إجابتك"],
               points=["المعادلة ميزان: ما نفعله بطرف نفعله بالطرف الآخر", "نتراجع عن العمليات بالترتيب العكسي", "نتحقق بالتعويض"],
               problem="أوجد عددًا إذا ضُرب في 5 وطُرح 17 من الناتج كانت النتيجة 40 أكثر من العدد نفسه."),
    "hi": dict(topic="एक चर वाले रैखिक समीकरण", title="हल की विधि",
               steps=["समीकरण लिखिए", "चर पद को अलग कीजिए", "विपरीत संक्रियाएँ कीजिए", "चर का मान ज्ञात कीजिए", "उत्तर की जाँच कीजिए"],
               points=["समीकरण एक तुला है: जो एक पक्ष में करें वही दूसरे में करें", "संक्रियाओं को उल्टे क्रम में हटाइए", "उत्तर को वापस रखकर जाँचिए"],
               problem="एक संख्या ज्ञात कीजिए जिसे 5 से गुणा करके गुणनफल में से 17 घटाने पर परिणाम उस संख्या से 40 अधिक हो।"),
    "te": dict(topic="ఒక చరరాశిలో రేఖీయ సమీకరణాలు", title="పద్ధతి",
               steps=["సమీకరణం రాయండి", "చరరాశి పదాన్ని వేరు చేయండి", "విలోమ ప్రక్రియలు చేయండి", "చరరాశి విలువ కనుగొనండి", "జవాబు సరిచూడండి"],
               points=["సమీకరణం ఒక త్రాసు: ఒక వైపు చేసినది మరో వైపు చేయాలి", "ప్రక్రియలను వ్యతిరేక క్రమంలో తొలగించండి", "జవాబును తిరిగి ప్రతిక్షేపించి సరిచూడండి"],
               problem="ఒక సంఖ్యను 5తో గుణించి లబ్ధం నుండి 17 తీసివేస్తే ఫలితం ఆ సంఖ్య కంటే 40 ఎక్కువ. ఆ సంఖ్యను కనుగొనండి."),
}


def test_tall_and_wide_scripts_keep_the_board_free_of_overlaps():
    """Production ar/hi demos 2026-09-25 (52 and 12 overlaps): Arabic runs
    43 px tall at the card's size against a 33 px pitch, Devanagari a third
    wider than the handwriting face. Geometry is measured with the
    renderer's own faces now, shaped Arabic included."""
    from maths.board import CARD_X, compile_lesson
    from maths.schema import Lesson, MethodCard, Step, WorkedExample
    from spike.scene_engine.director import parse_scene_response
    from spike.scene_engine.render import SceneRenderer
    for lang, c in _SCRIPTS.items():
        steps = [Step(kind="setup", operation="write the equation", before=[], after=["5n - 17 = n + 40"], speech="s"),
                 Step(operation="subtract n from both sides", before=["5n - 17 = n + 40"], after=["4n - 17 = 40"], speech="x"),
                 Step(operation="add 17 to both sides", before=["4n - 17 = 40"], after=["4n = 57"], speech="y")]
        ex = WorkedExample(label="Example 3", task="solve", problem=c["problem"], givens=["5n - 17 = n + 40"], target="n",
                           steps=steps, final_answer=["n = 57/4"], answer_speech="z", intro_speech="intro")
        lesson = Lesson(topic=c["topic"], method=MethodCard(title=c["title"], steps=c["steps"]),
                        concept_points=c["points"], misconceptions=c["points"][:2], examples=[ex])
        for s in compile_lesson(lesson, language=lang):
            if s["segment_id"] == "s001":
                continue
            r = SceneRenderer(parse_scene_response(s["scene"], s["text"]))
            r.compile(20.0)
            live = [w for w in r.audit()["warnings"] if w.startswith("TEXT_OVERLAP")]
            assert not live, (lang, s["segment_id"], live)
            box = next((e for e in s["scene"]["elements"] if e["id"] == "card_box"), None)
            if box:
                assert max(p[1] for p in box["points"]) <= 276, (lang, s["segment_id"])
            for eid in ("wb_p0", "wb_p1", "wb_p2", "q0", "q1", "wb_h"):
                b = r.bound.get(eid)
                if b is not None and b.text is not None:
                    assert b.box[2] <= CARD_X - 16, (lang, s["segment_id"], eid, b.box)


# ── the length floor: stated, measured, extended — never rewritten ──────────

EX4 = {
    "label": "Example 4", "difficulty": 2, "task": "solve", "problem": "3(x + 2) = 21", "givens": ["3(x + 2) = 21"],
    "target": "x", "intro_speech": "One more with a bracket, so we can practise expanding it before we undo anything.",
    "steps": [
        {"kind": "transform", "operation": "expand the bracket", "before": ["3(x + 2) = 21"], "after": ["3x + 6 = 21"],
         "explanation": "expand the bracket", "speech": "Three times x is three x, and three times two is six, so we have three x plus six equals twenty-one."},
        {"kind": "transform", "operation": "subtract 6 from both sides", "before": ["3x + 6 = 21"], "after": ["3x = 15"],
         "explanation": "subtract 6 from both sides", "speech": "The six was added last, so we take it away from both sides first: three x equals fifteen."},
        {"kind": "transform", "operation": "divide both sides by 3", "before": ["3x = 15"], "after": ["x = 5"],
         "explanation": "divide by 3", "speech": "Finally divide both sides by three, and x equals five."},
    ],
    "final_answer": ["x = 5"], "answer_speech": "So x equals five, and you can check it: three times seven is twenty-one.",
}
BAD_EX5 = {**EX4, "label": "Example 5", "problem": "4(x - 1) = 12", "givens": ["4(x - 1) = 12"],
           "steps": [{"kind": "transform", "operation": "expand", "before": ["4(x - 1) = 12"], "after": ["4x - 1 = 12"],
                      "speech": "Expand."}], "final_answer": ["x = 13/4"], "answer_speech": "Thirteen over four."}


class ExtendingClient(FakeClient):
    """Answers the extension prompt with `extension` (examples + concept
    lines), and the regeneration prompt with BAD_EX5 again (never fixed)."""

    def __init__(self, extension, **kw):
        super().__init__(**kw)
        self.extension = copy.deepcopy(extension)

    def analyze(self, prompt, system="", max_tokens=0, retries=3, cache_prefix=None, response_schema=None, **kw):
        if "FURTHER worked example" in prompt:
            self.calls.append({"prompt": prompt, "schema": response_schema, "max_tokens": max_tokens})
            return {"data": copy.deepcopy(self.extension), "usage": {}, "truncated": False}
        if "REJECTED" in prompt and "Example 2" not in prompt:
            self.calls.append({"prompt": prompt, "schema": response_schema, "max_tokens": max_tokens})
            return {"data": copy.deepcopy(BAD_EX5), "usage": {}, "truncated": False}
        return super().analyze(prompt, system, max_tokens, retries, cache_prefix, response_schema, **kw)


def _extension_calls(c):
    return [k for k in c.calls if "FURTHER worked example" in k["prompt"]]


def test_the_prompt_states_the_floor_in_words_and_characters_when_one_applies():
    from shared import lesson_length
    with_floor = L.build_prompt(topic="t", subject=None, level="Class 8", curriculum=None, language="en",
                                episode_context="", min_minutes=5.0)
    assert "MINIMUM LENGTH (hard requirement)" in with_floor
    assert f"{lesson_length.min_chars(5.0):,} characters" in with_floor and f"{lesson_length.min_words(5.0):,} words" in with_floor
    assert "8-12 minutes" not in with_floor
    assert "nothing before it" in with_floor, "the try-it problem is notation alone"
    without = L.build_prompt(topic="t", subject=None, level=None, curriculum=None, language="en", episode_context="")
    assert "8-12 minutes" in without and "MINIMUM LENGTH" not in without


def test_a_lesson_over_the_floor_is_not_extended():
    c = ExtendingClient({"examples": [EX4], "concept": []})
    lesson, report = L.verified_lesson(c, topic="t", subject=None, level=None, curriculum=None, language="en",
                                       episode_context="")
    lesson, report = L.extend_to_floor(c, lesson, report, minutes=0.05, avatars=None, language="en")
    assert _extension_calls(c) == [] and report["length_rounds"] == 0 and report["extended"] == []
    assert report["length"]["under"] is False and [e.label for e in lesson.examples] == ["Example 1", "Example 2", "Example 3"]


def test_a_short_lesson_is_extended_with_verified_examples_appended_after_the_ladder():
    """The re-ask adds to the verified lesson; it never rewrites it. The new
    example is verified like the ladder's, labelled in sequence, and the
    concept lines are appended."""
    c = ExtendingClient({"examples": [EX4], "concept": [{"who": "teacher", "line": "Think of the bracket as a parcel: open it before you sort what is inside."}]})
    lesson, report = L.verified_lesson(c, topic="t", subject=None, level=None, curriculum=None, language="en",
                                       episode_context="")
    before = L.measure_lesson(lesson, 2.0, None, "en")
    assert before["under"], "the fixture lesson is under a two-minute floor"
    n_concept = len(lesson.concept)
    lesson, report = L.extend_to_floor(c, lesson, report, minutes=2.0, avatars=None, language="en", rounds=1)
    calls = _extension_calls(c)
    assert len(calls) == 1
    p = calls[0]["prompt"]
    assert "3x + 5 = 20" in p and "2(x - 3) = 8" in p, "the existing problems are named so the model keeps away from them"
    assert f"{before['chars']:,} characters" in p and "Example 4" in p
    assert calls[0]["schema"] is L.EXTENSION_SCHEMA
    assert [e.label for e in lesson.examples] == ["Example 1", "Example 2", "Example 3", "Example 4"]
    assert lesson.examples[3].final_answer == ["x = 5"] and lesson.examples[0].final_answer == ["x = 5"]
    assert len(lesson.concept) == n_concept + 1
    assert report["length_rounds"] == 1 and report["extended"] == ["Example 4"]
    assert report["length"]["chars"] > before["chars"]
    assert len(report["examples"]) == 4 and all(e["status"] == "verified" for e in report["examples"])
    from maths.board import compile_lesson
    types = [s["type"] for s in compile_lesson(lesson)]
    assert types.count("worked_example") == 4 if "worked_example" in types else len(types) >= 8


def test_an_extension_example_that_fails_is_dropped_and_the_floor_is_reported_still_under():
    c = ExtendingClient({"examples": [BAD_EX5], "concept": []})
    lesson, report = L.verified_lesson(c, topic="t", subject=None, level=None, curriculum=None, language="en",
                                       episode_context="")
    lesson, report = L.extend_to_floor(c, lesson, report, minutes=5.0, avatars=None, language="en", attempts=1)
    assert [e.label for e in lesson.examples] == ["Example 1", "Example 2", "Example 3"]
    assert report["length_rounds"] == L.MAX_LENGTH_ROUNDS and report["extended"] == []
    assert any(x.startswith("Example 5") or x.startswith("Example 4") for x in report["dropped"])
    assert report["length"]["under"] is True, "still short: the worker refuses it, the module does not hide it"


def test_the_rounds_and_examples_per_round_are_bounded():
    c = ExtendingClient({"examples": [EX4], "concept": []})
    lesson, report = L.verified_lesson(c, topic="t", subject=None, level=None, curriculum=None, language="en",
                                       episode_context="")
    lesson, report = L.extend_to_floor(c, lesson, report, minutes=30.0, avatars=None, language="en")
    assert len(_extension_calls(c)) == L.MAX_LENGTH_ROUNDS
    assert all(f"Write {L.MAX_EXAMPLES_PER_ROUND} FURTHER" in k["prompt"] for k in _extension_calls(c))


def test_generate_maths_script_extends_and_carries_the_rounds_in_the_report():
    c = ExtendingClient({"examples": [EX4], "concept": []})
    script = L.generate_maths_script({"title": "Linear equations", "episode_num": 1}, {}, 1, c, language="en",
                                     min_minutes=2.0)
    ver = script.maths["verification"]
    assert ver["length_rounds"] >= 1 and "Example 4" in ver["extended"]
    assert ver["length"]["min_minutes"] == 2.0
    assert "MINIMUM LENGTH" in c.calls[0]["prompt"]


def test_the_worker_measures_the_maths_script_against_the_floor_and_refuses_a_short_one():
    import worker.process as wp
    src = open(wp.__file__).read()
    i = src.index("generate_maths_script(")
    branch = src[i:i + 6000]
    assert "min_minutes=_min_minutes or None" in branch
    assert "_with_length(report, script_dict, _min_minutes)" in branch
    assert "coverage.under_length(report)" in branch and "lesson script is too short" in branch
    assert branch.index("_with_length(") < branch.index("save_script(script)")


def test_a_wiped_column_leaves_no_overlap_in_the_report():
    """Production a0fcb332 (Pythagoras, 2026-10-02): a setup that carries the
    givens is three lines, so every example filled the column and wiped it;
    the notes written after the wipe sat where the wiped notes had been and
    the bind-time audit paired them — 8 of 9 scenes "overlapping", frames
    correct, lesson refused. The report must judge the pairs on the timeline."""
    from maths.board import compile_lesson
    from maths.schema import Lesson, MethodCard, Mistake, Step, WorkedExample

    def ex(label, a, b, c, problem):
        sq = f"c^2 = {a}^2 + {b}^2"
        steps = [Step(kind="setup", operation="Write Pythagoras' theorem with the given sides", before=[],
                      after=["c^2 = a^2 + b^2", f"a = {a}", f"b = {b}"], speech="Write the theorem and the two legs."),
                 Step(operation="Substitute the given sides", before=["c^2 = a^2 + b^2", f"a = {a}", f"b = {b}"],
                      after=[sq], speech="Substitute the given sides into the theorem."),
                 Step(operation="Evaluate the squares", before=[sq], after=[f"c^2 = {a * a} + {b * b}"], speech="Square each side."),
                 Step(operation="Add", before=[f"c^2 = {a * a} + {b * b}"], after=[f"c^2 = {c * c}"], speech="Add them."),
                 Step(operation="Take the positive square root; reject the negative root: c is a length",
                      before=[f"c^2 = {c * c}"], after=[f"c = {c}"], speech="Take the positive square root."),
                 Step(kind="check", operation="check", before=[f"c = {c}"], after=[f"{c}^2 = {a}^2 + {b}^2"], speech="Check it.")]
        return WorkedExample(label=label, task="solve", problem=problem, givens=[f"a = {a}", f"b = {b}"], target="c",
                             steps=steps, final_answer=[f"c = {c}"], answer_speech=f"So the hypotenuse is {c}.",
                             intro_speech="Here is the next example.", student_question="How do we start?",
                             common_mistake=Mistake(from_state=[f"c^2 = {a * a} + {b * b}"], wrong_state=[f"c = {a} + {b}"],
                                                    operation="add the sides", why_wrong="you cannot add before squaring",
                                                    speech="A common mistake is to add the legs directly."))

    lesson = Lesson(topic="Pythagoras' theorem", level="Class 8",
                    method=MethodCard(title="Method", steps=["Write the theorem", "Substitute the sides", "Square and add", "Square root"]),
                    concept_points=["c² = a² + b²", "c is the hypotenuse", "Square, add, root"],
                    misconceptions=["adding sides before squaring", "forgetting the root"],
                    examples=[ex("Example 1", 3, 4, 5, "A right-angled triangle has legs a = 3 cm and b = 4 cm. Find the hypotenuse c."),
                              ex("Example 2", 5, 12, 13, "A ladder's foot is 5 m from a wall and reaches 12 m up it. How long is the ladder c?")])
    wiped = 0
    for s in compile_lesson(lesson):
        sc = s["scene"]
        if sc.get("scene_type") != "worked_example":
            continue
        wiped += sum(1 for a in sc["actions"] if a["verb"] == "erase")
        r = SceneRenderer(parse_scene_response(sc, s["text"]))
        r.compile(30.0)
        overlaps = [w for w in r.audit()["warnings"] if w.startswith("TEXT_OVERLAP")]
        assert not overlaps, (s["segment_id"], overlaps)
    assert wiped, "the scenario is a column that fills and is wiped"
