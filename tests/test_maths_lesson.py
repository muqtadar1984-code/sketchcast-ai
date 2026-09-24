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

    def analyze(self, prompt, system="", max_tokens=0, retries=3, cache_prefix=None, response_schema=None):
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
    assert verbs.count("write") >= 4 and "fade" in verbs and "underline" in verbs and "highlight" in verbs
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
