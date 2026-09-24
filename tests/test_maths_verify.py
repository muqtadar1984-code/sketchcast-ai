"""Step-level verification: a right answer through a wrong step fails."""

from __future__ import annotations

from maths.schema import Mistake, Step, TryIt, WorkedExample, Lesson
from maths.verify import verify_example, verify_lesson, verify_try_it


def _linear():
    return WorkedExample(
        label="Example 1", difficulty=1, task="solve", problem="3x + 5 = 20", givens=["3x + 5 = 20"],
        target="x", intro_speech="Let us solve three x plus five equals twenty.",
        steps=[
            Step(kind="transform", operation="subtract 5 from both sides", before=["3x + 5 = 20"],
                 after=["3x = 15"], speech="Subtract five from both sides."),
            Step(kind="transform", operation="divide both sides by 3", before=["3x = 15"],
                 after=["x = 5"], speech="Divide both sides by three."),
            Step(kind="check", operation="check", before=["x = 5"], after=["3*5 + 5 = 20"],
                 speech="Check: three fives are fifteen, plus five is twenty."),
        ],
        final_answer=["x = 5"], answer_speech="So x equals five.",
        common_mistake=Mistake(from_state=["3x + 5 = 20"], wrong_state=["3x = 25"],
                               operation="add 5 instead of subtracting", why_wrong="moving 5 across changes its sign",
                               speech="A common slip is to add five."),
    )


def test_a_clean_linear_example_verifies_every_step():
    rep = verify_example(_linear())
    assert rep.status == "verified", rep.reasons
    by = {c.name: c for c in rep.checks}
    assert by["step 1"].ok and by["step 2"].ok and by["step 3"].ok
    assert by["answer"].ok and by["chain end"].ok and by["mistake"].ok
    assert "x = 5" in by["answer"].detail or "{5}" in by["answer"].detail


def test_a_correct_answer_through_a_wrong_step_fails():
    ex = _linear()
    ex.steps[0].after = ["3x = 25"]          # wrong
    ex.steps[1].before = ["3x = 25"]
    ex.steps[1].after = ["x = 5"]            # magically right again
    rep = verify_example(ex)
    assert rep.status == "failed"
    names = [c.name for c in rep.failures]
    assert "step 1" in names and "step 2" in names
    assert any("WRONG" in r for r in rep.reasons)


def test_a_wrong_final_answer_fails_even_when_the_steps_are_fine():
    ex = _linear()
    ex.final_answer = ["x = 4"]
    rep = verify_example(ex)
    assert rep.status == "failed"
    by = {c.name: c for c in rep.checks}
    assert by["answer"].ok is False and "x = 4" in by["answer"].detail.replace("{4}", "x = 4")


def test_a_mistake_that_is_actually_valid_is_refused():
    ex = _linear()
    ex.common_mistake = Mistake(from_state=["3x + 5 = 20"], wrong_state=["3x = 15"],
                                why_wrong="(it is not wrong)", speech="…")
    rep = verify_example(ex)
    by = {c.name: c for c in rep.checks}
    assert by["mistake"].ok is False and rep.status == "failed"


def test_an_unverifiable_transform_fails_but_a_setup_does_not():
    ex = _linear()
    ex.steps.insert(0, Step(kind="setup", operation="let x be the number", before=[], after=["3x + 5 = 20"],
                            speech="Let x be the number."))
    rep = verify_example(ex)
    assert rep.status == "verified", rep.reasons
    ex.steps[1].after = ["three x = 15"]     # prose, not notation
    ex.steps[2].before = ["three x = 15"]
    rep = verify_example(ex)
    assert rep.status == "failed"
    assert any("could not be verified" in r for r in rep.reasons)


def test_a_quadratic_split_into_two_cases_is_a_union():
    ex = WorkedExample(
        label="Example 3", difficulty=3, task="solve", problem="x^2 + 5x + 6 = 0",
        givens=["x^2 + 5x + 6 = 0"], target="x",
        steps=[
            Step(operation="factorise", before=["x^2 + 5x + 6 = 0"], after=["(x + 2)(x + 3) = 0"], speech="Factorise."),
            Step(operation="a product is zero when a factor is zero", before=["(x + 2)(x + 3) = 0"],
                 after=["x + 2 = 0", "x + 3 = 0"], speech="Either factor can be zero."),
            Step(operation="solve each", before=["x + 2 = 0", "x + 3 = 0"], after=["x = -2", "x = -3"], speech="Solve each."),
        ],
        final_answer=["x = -2 or x = -3"],
    )
    rep = verify_example(ex)
    assert rep.status == "verified", rep.reasons


def test_simultaneous_equations_hold_together():
    ex = WorkedExample(
        label="Example 2", difficulty=2, task="solve_system", problem="x + y = 5 and x - y = 1",
        givens=["x + y = 5", "x - y = 1"], target="x, y",
        steps=[
            Step(operation="add the equations", before=["x + y = 5", "x - y = 1"], after=["2x = 6", "x - y = 1"], speech="Add."),
            Step(operation="divide by 2", before=["2x = 6", "x - y = 1"], after=["x = 3", "x - y = 1"], speech="Divide."),
            Step(operation="substitute", before=["x = 3", "x - y = 1"], after=["x = 3", "3 - y = 1"], speech="Substitute."),
            Step(operation="solve for y", before=["x = 3", "3 - y = 1"], after=["x = 3", "y = 2"], speech="Solve."),
        ],
        final_answer=["x = 3, y = 2"],
    )
    rep = verify_example(ex)
    assert rep.status == "verified", rep.reasons
    ex.steps[0].after = ["2x = 4", "x - y = 1"]
    ex.steps[1].before = ["2x = 4", "x - y = 1"]
    assert verify_example(ex).status == "failed"


def test_an_inequality_that_forgets_to_flip_the_sign_fails():
    ex = WorkedExample(
        label="Example 4", difficulty=4, task="solve_inequality", problem="-2x + 3 > 7",
        givens=["-2x + 3 > 7"], target="x",
        steps=[
            Step(operation="subtract 3", before=["-2x + 3 > 7"], after=["-2x > 4"], speech="Subtract three."),
            Step(operation="divide by -2 and flip", before=["-2x > 4"], after=["x < -2"], speech="Divide by negative two and flip."),
        ],
        final_answer=["x < -2"],
        common_mistake=Mistake(from_state=["-2x > 4"], wrong_state=["x > -2"], why_wrong="dividing by a negative flips the sign",
                               speech="Forgetting to flip."),
    )
    rep = verify_example(ex)
    assert rep.status == "verified", rep.reasons
    ex.steps[1].after = ["x > -2"]
    ex.final_answer = ["x > -2"]
    ex.common_mistake = None
    rep = verify_example(ex)
    assert rep.status == "failed" and any(c.name == "step 2" for c in rep.failures)


def test_expression_tasks_check_equivalence_and_form():
    ex = WorkedExample(label="e", task="expand", problem="(x + 2)(x + 3)", givens=["(x + 2)(x + 3)"], target="expression",
                       steps=[Step(operation="multiply out", before=["(x + 2)(x + 3)"], after=["x^2 + 3x + 2x + 6"], speech="Multiply."),
                              Step(operation="collect like terms", before=["x^2 + 3x + 2x + 6"], after=["x^2 + 5x + 6"], speech="Collect.")],
                       final_answer=["x^2 + 5x + 6"])
    assert verify_example(ex).status == "verified"
    ex.final_answer = ["(x + 2)(x + 3)"]
    rep = verify_example(ex)
    assert rep.status == "failed" and "not fully expanded" in {c.name: c for c in rep.checks}["answer"].detail
    fac = WorkedExample(label="f", task="factorise", problem="x^2 + 5x + 6", givens=["x^2 + 5x + 6"], target="expression",
                        steps=[Step(operation="split", before=["x^2 + 5x + 6"], after=["(x + 2)(x + 3)"], speech="Factorise.")],
                        final_answer=["(x + 2)(x + 3)"])
    assert verify_example(fac).status == "verified"
    fac.final_answer = ["x^2 + 5x + 6"]
    assert verify_example(fac).status == "failed"


def test_the_working_must_start_from_the_problem():
    ex = _linear()
    ex.givens = ["3x + 5 = 21"]
    rep = verify_example(ex)
    assert rep.status == "failed"
    assert any(c.name == "chain start" and c.ok is False for c in rep.checks)


def test_try_it_and_lesson_roll_up():
    ok = verify_try_it(TryIt(problem="2x - 4 = 10", answer=["x = 7"]))
    assert ok.ok is True
    bad = verify_try_it(TryIt(problem="2x - 4 = 10", answer=["x = 6"]))
    assert bad.ok is False
    lesson = Lesson(topic="Linear equations", examples=[_linear()], try_it=TryIt(problem="2x - 4 = 10", answer=["x = 7"]))
    rep = verify_lesson(lesson)
    assert rep["status"] == "verified" and rep["examples"][0]["status"] == "verified"
    lesson.examples[0].final_answer = ["x = 1"]
    assert verify_lesson(lesson)["status"] == "failed"


def test_a_reply_with_bare_strings_where_lists_belong_still_parses():
    """Production, 2026-09-24: with the Vertex schema flag off the model wrote
    `"from_state": "(z - 3)/5 = (z - 5)/3"`. A one-line state is a one-line
    list; the lesson must parse, not fail the generation."""
    from maths.schema import parse_lesson
    lesson = parse_lesson({
        "topic": "t", "hook": "Just a string hook.", "concept": ["one", {"who": "student", "line": "why?"}],
        "concept_points": "single point", "method": {"title": "M", "steps": "only step"},
        "examples": [{"label": "Example 1", "difficulty": "2", "task": "solve", "problem": "3x + 5 = 20",
                      "givens": "3x + 5 = 20", "target": "x", "intro_speech": "hi",
                      "steps": [{"kind": "transform", "operation": "subtract 5", "before": "3x + 5 = 20",
                                 "after": "3x = 15", "speech": "s"}, "junk",
                                {"kind": "transform", "operation": "divide by 3", "before": "3x = 15",
                                 "after": "x = 5", "speech": "s"}],
                      "final_answer": "x = 5", "answer_speech": "a",
                      "common_mistake": {"from_state": "3x + 5 = 20", "wrong_state": "3x = 25",
                                         "why_wrong": "sign", "speech": "m"}},
                     "not an example",
                     {"label": "Example 2", "difficulty": 9, "task": "solve", "problem": "x = 1", "givens": ["x = 1"],
                      "target": "x", "steps": [], "final_answer": [], "common_mistake": {}}],
        "recap": "done", "try_it": {"problem": "2x = 8", "answer": "x = 4", "speech": "try"},
    })
    assert lesson.hook[0].who == "teacher" and lesson.concept[1].who == "student"
    assert lesson.concept_points == ["single point"] and lesson.method.steps == ["only step"]
    ex = lesson.examples[0]
    assert ex.difficulty == 2 and ex.givens == ["3x + 5 = 20"] and ex.final_answer == ["x = 5"]
    assert len(ex.steps) == 2 and ex.steps[0].before == ["3x + 5 = 20"]
    assert ex.common_mistake is not None and ex.common_mistake.wrong_state == ["3x = 25"]
    assert lesson.examples[1].difficulty == 4 and lesson.examples[1].common_mistake is None
    assert lesson.try_it.answer == ["x = 4"]
    assert verify_example(ex).status == "verified"
