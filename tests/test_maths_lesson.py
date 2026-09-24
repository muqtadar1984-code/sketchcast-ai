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
    "try_it": {"problem": "4x + 3 = 19", "answer": ["x = 4"], "speech": "Try this one: four x plus three equals nineteen. Pause and solve it."},
}


class FakeClient:
    """Answers the lesson prompt with LESSON and the regeneration prompt for
    Example 2 with FIXED_EX2 (the first regeneration) — recording each call."""

    model = "fake"

    def __init__(self, lesson=None, fixed=None, fail_regen=False):
        self.lesson = copy.deepcopy(lesson or LESSON)
        self.fixed = copy.deepcopy(fixed or FIXED_EX2)
        self.fail_regen = fail_regen
        self.calls: list[dict] = []

    def analyze(self, prompt, system="", max_tokens=0, retries=3, cache_prefix=None, response_schema=None, **kw):
        self.calls.append({"prompt": prompt, "schema": response_schema, "max_tokens": max_tokens})
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
    assert types == ["hook", "explore", "explore", "explore", "explore", "synthesis", "question_hook"]
    assert script.maths["verification"]["status"] == "verified"
    assert script.segments[-1].pause_for_question
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
