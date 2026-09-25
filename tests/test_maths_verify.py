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


def test_try_it_steps_are_verified_like_an_example():
    good = TryIt(problem="2x - 4 = 10", answer=["x = 7"],
                 steps=[Step(operation="add 4 to both sides", before=["2x - 4 = 10"], after=["2x = 14"], speech="add"),
                        Step(operation="divide by 2", before=["2x = 14"], after=["x = 7"], speech="divide")])
    assert verify_try_it(good).ok is True
    bad = TryIt(problem="2x - 4 = 10", answer=["x = 7"],
                steps=[Step(operation="add 4 to both sides", before=["2x - 4 = 10"], after=["2x = 6"], speech="add"),
                       Step(operation="divide by 2", before=["2x = 6"], after=["x = 7"], speech="divide")])
    c = verify_try_it(bad)
    assert c.ok is False and "step 1" in c.detail


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


def test_a_task_verb_in_front_of_the_problem_is_not_a_symbol():
    """Production, 2026-09-24: "Solve 4z = 16" parsed with Solve as a symbol
    and the try-it was dropped as wrong."""
    from maths.notation import parse_relation, strip_task_verb
    assert strip_task_verb("Solve 4z = 16") == "4z = 16"
    assert strip_task_verb("Solve for x: 2x + 1 = 7") == "2x + 1 = 7"
    assert strip_task_verb("Find x if 3x = 9") == "3x = 9"
    assert strip_task_verb("Simplify: 2x + 3x") == "2x + 3x"
    assert strip_task_verb("solve") == "solve"
    assert str(parse_relation("Solve 4z = 16").as_sympy()) == "Eq(4*z, 16)"
    assert verify_try_it(TryIt(problem="Solve 4z = 16", answer=["z = 4"])).ok is True


def test_text_fields_written_as_objects_or_lists_keep_their_words():
    """Production, 2026-09-24: student_question came back as
    {"who": "student", "line": "..."}."""
    from maths.schema import parse_example, Line
    ex = parse_example({"label": {"text": "Example 2"}, "difficulty": 2, "task": "solve", "problem": "x + 1 = 2",
                        "givens": ["x + 1 = 2"], "target": "x",
                        "student_question": {"who": "student", "line": "Do we subtract first, or add four first?"},
                        "intro_speech": ["Two lines", "joined"],
                        "steps": [{"kind": "transform", "operation": {"text": "subtract 1"}, "before": ["x + 1 = 2"],
                                   "after": [{"line": "x = 1"}], "speech": {"who": "teacher", "line": "Subtract one."}}],
                        "final_answer": {"line": "x = 1"}, "answer_speech": 7})
    assert ex.label == "Example 2" and ex.student_question.startswith("Do we subtract")
    assert ex.intro_speech == "Two lines joined" and ex.steps[0].operation == "subtract 1"
    assert ex.steps[0].after == ["x = 1"] and ex.final_answer == ["x = 1"] and ex.answer_speech == "7"
    assert Line.model_validate({"who": "narrator", "line": {"text": "hi"}}).who == "teacher"
    assert verify_example(ex).status == "verified"


def test_a_word_problem_try_it_is_verified_from_its_first_step():
    t = TryIt(problem="A number doubled is fourteen. Find it.", answer=["n = 7"],
              steps=[Step(operation="divide both sides by 2", before=["2n = 14"], after=["n = 7"], speech="halve")])
    assert verify_try_it(t).ok is True


def test_a_two_unknown_word_problem_under_task_solve_is_verified_as_a_system():
    """Hindi demo 2026-09-25: the model's correct solution of a two-unknown
    word problem (task "solve", target "x") was dropped because its two
    equations were read as alternatives."""
    steps = [Step(kind="setup", operation="write the equations", before=[], after=["x = 180 - y", "x = y - 40"],
                  speech="s"),
             Step(operation="substitute y in first equation", before=["x = 180 - y", "x = y - 40"],
                  after=["x = 70", "x = y - 40"], speech="a"),
             Step(operation="substitute x value into y", before=["x = 70", "x = y - 40"], after=["x = 70", "y = 110"],
                  speech="b")]
    ex = WorkedExample(label="Example 3", task="solve", problem="Two numbers add to 180 and differ by 40.",
                       givens=["x = 180 - y", "x = y - 40"], target="x", steps=steps,
                       final_answer=["x = 70", "y = 110"], answer_speech="z")
    rep = verify_example(ex)
    assert rep.status == "verified", [c.detail for c in rep.failures]
    # a quadratic's two cases are still alternatives, not a system
    quad = WorkedExample(label="Q", task="solve", problem="x^2 - 5x + 6 = 0", givens=["x^2 - 5x + 6 = 0"], target="x",
                         steps=[Step(operation="factorise", before=["x^2 - 5x + 6 = 0"], after=["(x - 2)(x - 3) = 0"], speech="f"),
                                Step(operation="each factor is zero", before=["(x - 2)(x - 3) = 0"], after=["x = 2", "x = 3"], speech="g")],
                         final_answer=["x = 2", "x = 3"], answer_speech="z")
    assert verify_example(quad).status == "verified"
    # and a wrong system step is still caught
    bad = ex.model_copy(deep=True)
    bad.steps[1].after = ["x = 60", "x = y - 40"]
    assert verify_example(bad).status == "failed"


def test_digit_grouped_numbers_are_integers_not_decimals():
    """Production, 2026-09-25 (Grade 6, large numbers): "58,672" was read as
    58.672 and "1,00,000" as 0 by the decimal-comma rule, so every place-value
    example verified as wrong; "17173, 8000" parsed as a Python tuple and the
    size guard's AttributeError took four document jobs down."""
    from maths.tokens import normalise
    assert normalise("58,672 + 57,875") == "58672 + 57875"
    assert normalise("1,00,000") == "100000"          # Indian grouping
    assert normalise("12,34,567") == "1234567"
    assert normalise("1,234.5") == "1234.5"
    assert normalise("3,14") == "3.14"                 # a decimal comma stays one
    assert normalise("0,5") == "0.5"

    ex = WorkedExample(label="e", task="evaluate", problem="58,672 + 57,875", givens=["58,672 + 57,875"],
                       target="expression",
                       steps=[Step(operation="add", before=["58,672 + 57,875"], after=["1,16,547"], speech="Add.")],
                       final_answer=["1,16,547"])
    rep = verify_example(ex)
    assert rep.status == "verified", [c.detail for c in rep.failures]


def test_a_list_of_numbers_is_reported_not_a_crash():
    import pytest
    from maths.notation import NotationError, parse_relation
    with pytest.raises(NotationError, match="one expression"):
        parse_relation("17173, 8000")
    ex = WorkedExample(label="r", task="evaluate", problem="17173, 8000", givens=["17173, 8000"], target="expression",
                       steps=[Step(operation="round each", before=["17173, 8000"], after=["17000, 8000"], speech="Round.")],
                       final_answer=["17000, 8000"])
    rep = verify_example(ex)               # dropped, never raised
    assert rep.status == "failed"
    assert any("could not be read" in c.detail for c in rep.checks)


# ── rounding and estimation ───────────────────────────────────────────────


def _round_ex(before, after, precision, answer, task="round", extra_steps=()):
    steps = [Step(kind="round", operation="round", precision=precision, before=[before], after=[after], speech="r")]
    steps += list(extra_steps)
    return WorkedExample(label="r", task=task, problem=before, givens=[before], target="expression",
                         steps=steps, final_answer=[answer])


def test_rounding_to_a_stated_unit_verifies_and_a_wrong_rounding_fails():
    assert verify_example(_round_ex("17173", "17000", "1000", "17000")).status == "verified"
    assert verify_example(_round_ex("17,173", "17,000", "nearest thousand", "17000")).status == "verified"
    assert verify_example(_round_ex("17500", "18000", "1000", "18000")).status == "verified"   # half up
    assert verify_example(_round_ex("3.14159", "3.14", "2 dp", "3.14")).status == "verified"
    assert verify_example(_round_ex("0.004567", "0.0046", "2 sf", "0.0046")).status == "verified"
    assert verify_example(_round_ex("-2.5", "-3", "1", "-3")).status == "verified"           # away from zero
    rep = verify_example(_round_ex("17173", "18000", "1000", "18000"))
    assert rep.status == "failed"
    assert any("rounded to 1000 is 17000, not 18000" in c.detail for c in rep.failures)
    rep = verify_example(_round_ex("7583", "7500", "100", "7500"))                           # truncation
    assert rep.status == "failed"


def test_a_rounding_step_may_not_change_the_shape_of_the_line():
    rep = verify_example(_round_ex("x = 17173", "17000", "1000", "17000"))
    assert rep.status == "failed" and any("numbers rounded" in c.detail for c in rep.failures)
    ex = WorkedExample(label="r", task="round", problem="x = 17173", givens=["x = 17173"], target="x",
                       steps=[Step(kind="round", precision="1000", before=["x = 17173"], after=["x = 17000"], speech="r")],
                       final_answer=["x = 17000"])
    assert verify_example(ex).status == "verified"


def test_without_a_stated_precision_any_power_of_ten_is_accepted_but_not_a_truncation():
    assert verify_example(_round_ex("7583", "8000", "", "8000")).status == "verified"
    assert verify_example(_round_ex("7583", "7580", "", "7580")).status == "verified"
    rep = verify_example(_round_ex("7583", "7500", "", "7500"))
    assert rep.status == "failed" and any("not a rounding" in c.detail for c in rep.failures)
    rep = verify_example(_round_ex("7583", "8000", "nearest banana", "8000"))
    assert rep.status == "failed"                                              # unreadable precision is unverified


def test_an_estimate_rounds_first_then_works_the_rounded_expression():
    """7583 + 3421 ≈ 8000 + 3000 = 11000: the old equivalence check called
    the rounding wrong and the answer wrong (it is not 11004)."""
    work = Step(operation="add", before=["8000 + 3000"], after=["11000"], speech="a")
    ex = _round_ex("7583 + 3421", "8000 + 3000", "1 sf", "11000", task="estimate", extra_steps=[work])
    rep = verify_example(ex)
    assert rep.status == "verified", [c.detail for c in rep.failures] + [c.detail for c in rep.unverified]
    # the exact value is NOT the estimate's answer
    ex.final_answer = ["11004"]
    rep = verify_example(ex)
    assert rep.status == "failed" and any("not the last line" in c.detail for c in rep.failures)
    # a slip in the arithmetic after the rounding is still caught
    bad = Step(operation="add", before=["8000 + 3000"], after=["12000"], speech="a")
    ex = _round_ex("7583 + 3421", "8000 + 3000", "1 sf", "12000", task="estimate", extra_steps=[bad])
    assert verify_example(ex).status == "failed"
    # rounding one term but not the other, to the stated precision
    ex = _round_ex("7500 + 83", "7500 + 100", "100", "7600", task="estimate",
                   extra_steps=[Step(operation="add", before=["7500 + 100"], after=["7600"], speech="a")])
    assert verify_example(ex).status == "verified"


def test_a_rounding_task_without_a_round_step_is_refused():
    ex = WorkedExample(label="r", task="round", problem="17173", givens=["17173"], target="expression",
                       steps=[Step(operation="round", before=["17173"], after=["17000"], speech="r")],
                       final_answer=["17000"])
    rep = verify_example(ex)
    assert rep.status == "failed"
    assert any("kind 'round'" in c.detail for c in rep.failures) or any(c.ok is False for c in rep.checks)


def test_a_try_it_with_a_rounding_step_is_verified_as_an_estimate():
    t = TryIt(problem="Estimate 4,912 + 2,087", answer=["7000"], speech="s", solution_speech="r",
              steps=[Step(kind="round", precision="1 sf", before=["4912 + 2087"], after=["5000 + 2000"], speech="a"),
                     Step(operation="add", before=["5000 + 2000"], after=["7000"], speech="b")],
              answer_speech="z")
    assert verify_try_it(t).ok is True
