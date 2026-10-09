"""Step-level verification: a right answer through a wrong step fails."""

from __future__ import annotations

import sympy as sp

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


def test_a_list_of_numbers_is_data_and_never_a_crash():
    """Once a refusal ("Please write one expression"); since 2026-09-26 a
    comma list is a DATA LIST (tests/test_maths_data_tasks.py). Under a
    task that is not a data task it still fails cleanly — the step changes
    the data, the answer is not an expression — and never raises."""
    import pytest
    from maths.notation import NotationError, parse_relation
    assert parse_relation("17173, 8000").data == (17173, 8000)
    with pytest.raises(NotationError, match="one expression"):
        parse_relation("(17173, 8000)")     # a bracketed tuple is still not an expression
    ex = WorkedExample(label="r", task="evaluate", problem="17173, 8000", givens=["17173, 8000"], target="expression",
                       steps=[Step(operation="round each", before=["17173, 8000"], after=["17000, 8000"], speech="Round.")],
                       final_answer=["17000, 8000"])
    rep = verify_example(ex)               # dropped, never raised
    assert rep.status == "failed"
    assert any("is not the same data as" in c.detail for c in rep.failures)


# ── rounding and estimation ───────────────────────────────────────────────


# ── π as a school approximation, and exact decimals (2026-10-09) ──────────


def _by(rep):
    return {c.name: c for c in rep.checks}


def test_decimal_arithmetic_is_exact_not_floating_point():
    # the first geometry kit: '188.4 + 56.52' was "not equivalent to" '244.92' by 2.8e-14
    ex = WorkedExample(label="d", task="evaluate", problem="188.4 + 56.52", givens=["188.4 + 56.52"], target="expression",
                       steps=[Step(operation="add", before=["188.4 + 56.52"], after=["244.92"], speech="Add.")],
                       final_answer=["244.92"])
    rep = verify_example(ex)
    assert rep.status == "verified", rep.reasons


def _cylinder(approx="3.14", first="2 * 3.14 * 3 * 10 + 2 * 3.14 * 3^2", answer="244.92"):
    return WorkedExample(
        label="c", task="evaluate", problem=f"Find the total surface area of a cylinder with r = 3 cm and h = 10 cm, taking pi = {approx}.",
        givens=["2 * pi * r * h + 2 * pi * r^2", "r = 3", "h = 10"], target="expression",
        steps=[Step(operation="substitute the values", before=["2 * pi * r * h + 2 * pi * r^2", "r = 3", "h = 10"],
                    after=[first], speech="Substitute."),
               Step(operation="multiply", before=[first], after=["188.4 + 56.52"], speech="Multiply."),
               Step(operation="add", before=["188.4 + 56.52"], after=[answer], speech="Add.")],
        final_answer=[answer])


def test_pi_taken_as_a_school_approximation_verifies_and_the_report_names_it():
    rep = verify_example(_cylinder())
    assert rep.status == "verified", rep.reasons
    by = _by(rep)
    assert "π taken as 3.14" in by["step 1"].detail and by["step 2"].ok and by["step 3"].ok
    assert by["answer"].ok and "π taken as 3.14" in by["answer"].detail
    # 22/7 is a school value too; 3.1 is not an approximation anyone teaches
    seven = _cylinder("22/7", "2 * 22/7 * 3 * 10 + 2 * 22/7 * 3^2", "1716/7")
    seven.steps = seven.steps[:1]
    rep = verify_example(seven)
    assert _by(rep)["step 1"].ok and "22/7" in _by(rep)["step 1"].detail, rep.reasons
    crude = _cylinder("3.1", "2 * 3.1 * 3 * 10 + 2 * 3.1 * 3^2", "241.8")
    crude.problem = "Find the total surface area of a cylinder with r = 3 cm and h = 10 cm."  # 3.1 not declared
    crude.steps = crude.steps[:1]
    assert _by(verify_example(crude))["step 1"].ok is False
    # the way back is not a step: once π is 3.14 it does not become π again
    back = WorkedExample(label="b", task="simplify", problem="2 * 3.14 * 5", givens=["2 * 3.14 * 5"], target="expression",
                         steps=[Step(operation="write pi", before=["2 * 3.14 * 5"], after=["10pi"], speech="Pi.")],
                         final_answer=["10pi"])
    assert verify_example(back).status == "failed"


def test_an_answer_in_terms_of_pi_still_verifies_exactly_and_a_wrong_one_fails():
    ex = WorkedExample(label="v", task="evaluate", problem="V = pi r^2 h with r = 3 and h = 10",
                       givens=["pi * r^2 * h", "r = 3", "h = 10"], target="expression",
                       steps=[Step(operation="substitute", before=["pi * r^2 * h", "r = 3", "h = 10"], after=["pi * 3^2 * 10"], speech="Sub."),
                              Step(operation="multiply", before=["pi * 3^2 * 10"], after=["90pi"], speech="Ninety pi.")],
                       final_answer=["90pi"])
    assert verify_example(ex).status == "verified", verify_example(ex).reasons
    ex.final_answer = ["90"]
    assert _by(verify_example(ex))["answer"].ok is False


def test_a_decimal_answer_may_be_the_pi_value_rounded_to_two_places():
    ex = WorkedExample(label="r", task="evaluate", problem="pi * 3^2 * 10 to 2 decimal places", givens=["pi * 3^2 * 10"],
                       target="expression",
                       steps=[Step(operation="multiply", before=["pi * 3^2 * 10"], after=["90pi"], speech="Ninety pi."),
                              Step(operation="use the calculator's pi and round", before=["90pi"], after=["282.74"], speech="Round.")],
                       final_answer=["282.74"])
    rep = verify_example(ex)
    assert rep.status == "verified", rep.reasons
    assert "then rounded" in _by(rep)["step 2"].detail
    ex.steps[1].after = ["283.74"]
    ex.final_answer = ["283.74"]
    assert verify_example(ex).status == "failed"


def test_a_declared_pi_line_is_an_approximation_not_a_false_equation():
    # the kit's volume example: "pi = 3.14" among the givens made the state's solution set EmptySet
    givens = ["V = pi * r^2 * h", "r = 5", "h = 8", "pi = 3.14"]
    ex = WorkedExample(label="V", task="solve", problem="A cylinder has r = 5 cm and h = 8 cm. Take pi = 3.14. Find its volume V.",
                       givens=givens, target="V",
                       steps=[Step(operation="substitute the given values", before=givens, after=["V = 3.14 * 5^2 * 8"], speech="Sub."),
                              Step(operation="evaluate the square", before=["V = 3.14 * 5^2 * 8"], after=["V = 3.14 * 25 * 8"], speech="Square."),
                              Step(operation="multiply", before=["V = 3.14 * 25 * 8"], after=["V = 628"], speech="Multiply.")],
                       final_answer=["V = 628"])
    rep = verify_example(ex)
    assert rep.status == "verified", rep.reasons
    by = _by(rep)
    assert by["step 1"].ok and "3.14" in by["step 1"].detail and by["answer"].ok and by["chain end"].ok
    # a declared value off the standard list is honoured because it was declared
    three = WorkedExample(label="3", task="evaluate", problem="Take pi = 3. Find 2 * pi * 7.", givens=["2 * pi * 7", "pi = 3"],
                          target="expression",
                          steps=[Step(operation="substitute", before=["2 * pi * 7", "pi = 3"], after=["2 * 3 * 7"], speech="Sub.")],
                          final_answer=["42"])
    assert verify_example(three).status == "verified", verify_example(three).reasons


def test_a_check_step_may_substitute_pi_as_an_approximation():
    ex = _linear()
    ex.givens = ["2 * pi * r = 18.84"]
    ex.problem = "2 * pi * r = 18.84, pi = 3.14"
    ex.target = "r"
    ex.steps = [Step(operation="divide both sides by 2pi, pi = 3.14", before=["2 * pi * r = 18.84"], after=["r = 18.84 / 6.28"], speech="Divide."),
                Step(operation="divide", before=["r = 18.84 / 6.28"], after=["r = 3"], speech="Three."),
                Step(kind="check", operation="check", before=["r = 3"], after=["2 * 3.14 * 3 = 18.84"], speech="Check.")]
    ex.final_answer = ["r = 3"]
    ex.common_mistake = None
    rep = verify_example(ex)
    assert rep.status == "verified", rep.reasons
    assert _by(rep)["step 3"].ok and _by(rep)["answer"].ok


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


def test_a_try_it_problem_with_a_lead_in_phrase_starts_the_working_at_its_notation():
    """Production 2026-09-28 (Quadratic Expressions and Factorising
    Trinomials): the model wrote the try-it problem as "the quadratic
    expression x^2 - x - 12"; the parser read the words as symbols and the
    chain check found the working did not start from the problem."""
    t = TryIt(problem="the quadratic expression x^2 - x - 12", answer=["(x - 4)(x + 3)"],
              speech="Pause and factorise this one.", solution_speech="Let us compare.",
              steps=[Step(kind="transform", operation="find two numbers", before=["x^2 - x - 12"],
                          after=["(x - 4)(x + 3)"], speech="Minus four and plus three multiply to minus twelve and add to minus one.")],
              answer_speech="So it factorises as x minus four times x plus three.")
    c = verify_try_it(t)
    assert c.ok is True, c.detail
    from maths.verify import try_it_example
    ex = try_it_example(t)
    assert ex.givens == ["x^2 - x - 12"] and ex.task == "simplify"


def test_a_word_problem_try_it_still_starts_from_its_first_step():
    t = TryIt(problem="A number doubled plus five gives twenty-one. Find it.", answer=["x = 8"],
              speech="Pause here.", solution_speech="Compare.",
              steps=[Step(kind="setup", operation="write the equation", before=[], after=["2x + 5 = 21"], speech="Let x be the number."),
                     Step(kind="transform", operation="subtract 5", before=["2x + 5 = 21"], after=["2x = 16"], speech="Subtract five."),
                     Step(kind="transform", operation="divide by 2", before=["2x = 16"], after=["x = 8"], speech="Divide by two.")],
              answer_speech="x is eight.")
    from maths.verify import try_it_example
    assert try_it_example(t).givens == ["2x + 5 = 21"]
    assert verify_try_it(t).ok is True


# ── a relation the problem's words supply: Pythagoras (2026-10-02) ───────────
# A teacher's "pythagoras theorem" book failed every question and every
# worked example with "solutions before: a = 3, b = 4; after: no solution":
# the sides were solved for the hypotenuse, a substitution dropped the known
# sides, and the positive root was "half the solutions".

def _hypotenuse(root_step="take the square root", problem="A right-angled triangle has legs a = 3 cm and b = 4 cm. "
                                                            "Find the hypotenuse c."):
    return WorkedExample(label="Ex", task="solve", problem=problem, givens=["a = 3", "b = 4"], target="c",
                         steps=[Step(kind="transform", operation="apply Pythagoras' theorem", before=["a = 3", "b = 4"],
                                     after=["c^2 = 3^2 + 4^2"]),
                                Step(kind="transform", operation="evaluate", before=["c^2 = 3^2 + 4^2"], after=["c^2 = 25"]),
                                Step(kind="transform", operation=root_step, before=["c^2 = 25"], after=["c = 5"])],
                         final_answer=["c = 5"])


def test_a_relation_brought_in_from_known_values_is_a_setup_whatever_it_was_called():
    rep = verify_example(_hypotenuse())
    assert rep.status == "verified", rep.reasons
    step1 = next(c for c in rep.checks if c.name == "step 1")
    assert step1.ok is None and step1.detail.startswith("setup:")
    assert next(c for c in rep.checks if c.name == "answer").ok is True


def test_the_positive_root_of_a_magnitude_is_accepted_but_a_plain_equation_keeps_both_roots():
    assert verify_example(_hypotenuse()).status == "verified"
    plain = WorkedExample(label="Ex", task="solve", problem="Solve x^2 = 25", givens=["x^2 = 25"], target="x",
                          steps=[Step(kind="transform", operation="take the square root", before=["x^2 = 25"], after=["x = 5"])],
                          final_answer=["x = 5"])
    rep = verify_example(plain)
    assert rep.status == "failed"
    assert "solutions before: {-5, 5}; after: {5}" in next(c for c in rep.checks if c.name == "step 1").detail


def test_a_stated_rejection_of_a_root_is_accepted_for_any_problem():
    plain = WorkedExample(label="Ex", task="solve", problem="Solve x^2 = 25 for the positive x", givens=["x^2 = 25"], target="x",
                          steps=[Step(kind="transform", operation="reject the negative root", before=["x^2 = 25"], after=["x = 5"])],
                          final_answer=["x = 5"])
    rep = verify_example(plain)
    assert rep.status == "verified", rep.reasons
    assert "discarded for the stated reason" in next(c for c in rep.checks if c.name == "step 1").detail


def test_a_magnitude_problem_may_not_discard_a_positive_root_without_saying_why():
    ex = WorkedExample(label="Ex", task="solve", problem="The side s of a square satisfies s^2 - 5s + 6 = 0. Find s.",
                       givens=["s^2 - 5s + 6 = 0"], target="s",
                       steps=[Step(kind="transform", operation="factorise", before=["s^2 - 5s + 6 = 0"], after=["(s - 2)(s - 3) = 0"]),
                              Step(kind="transform", operation="take the first root", before=["(s - 2)(s - 3) = 0"], after=["s = 2"])],
                       final_answer=["s = 2"])
    assert verify_example(ex).status == "failed"


def test_a_declared_setup_carries_the_givens_and_the_substitution_is_a_projection():
    ex = WorkedExample(label="Ex", task="solve", problem="Legs 3 and 4; find the hypotenuse c.", givens=["a = 3", "b = 4"], target="c",
                       steps=[Step(kind="setup", operation="Pythagoras' theorem", before=[], after=["c^2 = a^2 + b^2", "a = 3", "b = 4"]),
                              Step(kind="transform", operation="substitute the given sides",
                                   before=["c^2 = a^2 + b^2", "a = 3", "b = 4"], after=["c^2 = 3^2 + 4^2"]),
                              Step(kind="transform", operation="evaluate", before=["c^2 = 3^2 + 4^2"], after=["c^2 = 25"]),
                              Step(kind="transform", operation="reject the negative root: c is a length", before=["c^2 = 25"],
                                   after=["c = 5"])],
                       final_answer=["c = 5"])
    rep = verify_example(ex)
    assert rep.status == "verified", rep.reasons
    assert "solutions unchanged for c" in next(c for c in rep.checks if c.name == "step 2").detail
    assert not any(c.name == "chain start" for c in rep.checks), "the setup wrote the start; the givens are its input"


def test_a_wrong_substitution_and_a_wrong_theorem_still_fail():
    wrong_sub = WorkedExample(label="Ex", task="solve", problem="find c", givens=["c^2 = a^2 + b^2", "a = 3", "b = 4"], target="c",
                              steps=[Step(kind="transform", operation="substitute",
                                          before=["c^2 = a^2 + b^2", "a = 3", "b = 4"], after=["c^2 = 7"])],
                              final_answer=["c = 5"])
    assert verify_example(wrong_sub).status == "failed"
    wrong_theorem = WorkedExample(label="Ex", task="solve", problem="Legs a = 3 and b = 4. Find the hypotenuse c.",
                                  givens=["a = 3", "b = 4"], target="c",
                                  steps=[Step(kind="transform", operation="apply Pythagoras' theorem", before=["a = 3", "b = 4"],
                                              after=["c = 3 + 4"]),
                                         Step(kind="transform", operation="add", before=["c = 3 + 4"], after=["c = 7"])],
                                  final_answer=["c = 5"])
    rep = verify_example(wrong_theorem)
    assert rep.status == "failed"
    assert next(c for c in rep.checks if c.name == "answer").ok is False, "the answer is judged from the setup's equation"


def test_a_leg_from_the_hypotenuse_verifies():
    ex = WorkedExample(label="Ex", task="solve",
                       problem="A ladder 13 m long reaches 12 m up a wall. How far is its foot from the wall? Call the distance b.",
                       givens=["c = 13", "a = 12"], target="b",
                       steps=[Step(kind="transform", operation="apply Pythagoras' theorem", before=["c = 13", "a = 12"],
                                   after=["13^2 = 12^2 + b^2"]),
                              Step(kind="transform", operation="evaluate the squares", before=["13^2 = 12^2 + b^2"], after=["169 = 144 + b^2"]),
                              Step(kind="transform", operation="subtract 144 from both sides", before=["169 = 144 + b^2"], after=["b^2 = 25"]),
                              Step(kind="transform", operation="take the square root", before=["b^2 = 25"], after=["b = 5"])],
                       final_answer=["b = 5"])
    assert verify_example(ex).status == "verified", verify_example(ex).reasons


def test_the_prompt_tells_the_model_about_formula_setups_and_stated_rejections():
    from maths.lesson import _STEP_RULES
    assert "Pythagoras" in _STEP_RULES and "stays in 'after' until a step uses it" in _STEP_RULES
    assert "reject the negative root" in _STEP_RULES


def test_a_try_it_solved_for_one_side_of_a_formula_is_judged_on_that_side():
    """Production a0fcb332 (2026-10-02): the try-it's setup wrote the theorem
    with both legs, so its first line named a, b and c and the answer "c = 10"
    was compared with solutions that carried a = 6 and b = 8 too — dropped
    for "the answer says c = 10". The unknown is what the answer names."""
    from maths.verify import try_it_example

    def try_it(answer, root="take the positive square root: c is a length"):
        return TryIt(problem="A right-angled triangle has legs 6 cm and 8 cm. Find the hypotenuse c.", answer=answer,
                     steps=[Step(kind="setup", operation="Pythagoras' theorem", before=[],
                                 after=["c^2 = a^2 + b^2", "a = 6", "b = 8"], speech="s"),
                            Step(operation="substitute the sides", before=["c^2 = a^2 + b^2", "a = 6", "b = 8"],
                                 after=["c^2 = 6^2 + 8^2"], speech="t"),
                            Step(operation="evaluate", before=["c^2 = 6^2 + 8^2"], after=["c^2 = 100"], speech="u"),
                            Step(operation=root, before=["c^2 = 100"], after=["c = 10"], speech="v")])
    ok = verify_try_it(try_it(["c = 10"]))
    assert ok.ok is True, ok.detail
    assert try_it_example(try_it(["c = 10"])).target == "c"
    wrong = verify_try_it(try_it(["c = 14"]))
    assert wrong.ok is False
    # an answer naming every unknown of the line is judged as before
    assert try_it_example(TryIt(problem="x + y = 10", answer=["x = 4", "y = 6"])).target == "x, y"


# ── an expression task carries values: substitution (2026-10-06) ──────────────
# Kit 604b3b79 "Substitution: Evaluating Algebraic Expressions": every step
# read as "'x + 5' is not equivalent to '3 + 5'", the answer as "'8' is not
# equivalent to 'x + 5'"; a lesson that wrote the values as lines or named the
# expression ("E = 3x + 7") was "mixed" and unreadable.

def _evaluate(problem, givens, steps, answer, target="expression"):
    return WorkedExample(label="Ex", task="evaluate", problem=problem, givens=givens, target=target,
                         steps=[Step(operation=op, before=b, after=a, speech="s") for op, b, a in steps],
                         final_answer=answer)


def test_a_worksheet_substitution_verifies_with_the_value_from_the_words():
    ex = _evaluate("Evaluate x + 5 when x = 3", ["x + 5"],
                   [("substitute x with 3", ["x + 5"], ["3 + 5"]), ("add", ["3 + 5"], ["8"])], ["8"])
    rep = verify_example(ex)
    assert rep.status == "verified", [c.detail for c in rep.failures]
    wrong_sub = _evaluate("Evaluate x + 5 when x = 3", ["x + 5"],
                          [("substitute x with 3", ["x + 5"], ["4 + 5"]), ("add", ["4 + 5"], ["9"])], ["9"])
    rep = verify_example(wrong_sub)
    assert rep.status == "failed" and any("with x = 3" in c.detail for c in rep.failures)
    wrong_answer = _evaluate("Evaluate 4x when x = 5", ["4x"], [("substitute", ["4x"], ["4(5)"]), ("multiply", ["4(5)"], ["20"])], ["25"])
    assert verify_example(wrong_answer).status == "failed"


def test_the_values_may_be_lines_of_the_givens_and_the_expression_may_carry_a_name():
    as_lines = _evaluate("Find the value of 3x + 7 when x = 4.", ["3x + 7", "x = 4"],
                         [("Substitute 4 for x", ["3x + 7", "x = 4"], ["3(4) + 7"]),
                          ("Multiply", ["3(4) + 7"], ["12 + 7"]), ("Add", ["12 + 7"], ["19"])], ["19"])
    rep = verify_example(as_lines)
    assert rep.status == "verified", [c.detail for c in rep.failures]
    named = _evaluate("Find the value of E = 3x + 7 when x = 4.", ["E = 3x + 7", "x = 4"],
                      [("Substitute 4 for x", ["E = 3x + 7", "x = 4"], ["E = 3(4) + 7"]),
                       ("Multiply", ["E = 3(4) + 7"], ["E = 12 + 7"]), ("Add", ["E = 12 + 7"], ["E = 19"])], ["E = 19"])
    rep = verify_example(named)
    assert rep.status == "verified", [c.detail for c in rep.failures]
    # "E = 3x + 7" in the words gives E no value; only x has one
    from maths.verify import _values
    assert _values(named) == {sp.Symbol("x"): 4}


def test_two_letters_a_squared_negative_and_a_two_line_answer():
    two = _evaluate("Evaluate (4a + b)/(2a - b) when a = 2 and b = 1", ["(4a + b)/(2a - b)", "a = 2", "b = 1"],
                    [("Substitute values for a and b", ["(4a + b)/(2a - b)", "a = 2", "b = 1"], ["(4(2) + 1)/(2(2) - 1)"]),
                     ("Work out", ["(4(2) + 1)/(2(2) - 1)"], ["9/3"]), ("Divide", ["9/3"], ["3"])], ["(4(2) + 1)/(2(2) - 1)", "3"])
    rep = verify_example(two)
    assert rep.status == "verified", [c.detail for c in rep.failures]
    sq = _evaluate("Evaluate 2y^2 - y when y = -3", ["2y^2 - y", "y = -3"],
                   [("Substitute -3 for y", ["2y^2 - y", "y = -3"], ["2(-3)^2 - (-3)"]),
                    ("Evaluate exponent (-3)^2", ["2(-3)^2 - (-3)"], ["2(9) + 3"]),
                    ("Multiply 2 by 9", ["2(9) + 3"], ["18 + 3"]), ("Add", ["18 + 3"], ["21"])], ["21"])
    rep = verify_example(sq)
    assert rep.status == "verified", [c.detail for c in rep.failures]
    # the answer must be a value, and a value that is not the expression's fails
    sq.final_answer = ["2y^2 - y"]
    assert verify_example(sq).status == "failed"


def test_a_substitution_mistake_is_still_a_mistake_and_other_expression_tasks_are_untouched():
    ex = _evaluate("Evaluate 3x + 7 when x = 4", ["3x + 7"],
                   [("Substitute", ["3x + 7"], ["3(4) + 7"]), ("Work out", ["3(4) + 7"], ["19"])], ["19"])
    ex.common_mistake = Mistake(from_state=["3(4) + 7"], wrong_state=["3 + 4 + 7"], operation="add instead of multiply",
                                why_wrong="3x means 3 times x", speech="s")
    rep = verify_example(ex)
    assert rep.status == "verified", [c.detail for c in rep.failures]
    assert {c.name: c for c in rep.checks}["mistake"].ok is True
    # simplify without values: a step that changes the expression still fails
    bad = WorkedExample(label="s", task="simplify", problem="2x + 3x", givens=["2x + 3x"], target="expression",
                        steps=[Step(operation="collect", before=["2x + 3x"], after=["6x"], speech="s")], final_answer=["6x"])
    assert verify_example(bad).status == "failed"


def test_the_model_is_told_the_shape_of_an_evaluate_task():
    from maths.lesson import _STEP_RULES
    assert "task evaluate" in _STEP_RULES and '"3x + 7", "x = 4"' in _STEP_RULES and "Never give the expression a name" in _STEP_RULES


def test_adjacent_letters_are_a_product_and_a_value_may_be_followed_by_a_word():
    """Worksheet 604b3b79 after the first fix: 'Evaluate 2a^2 - 3ab + b when
    a = 3 and b = -2' still failed — "a = 3 and" lost its value to the word
    after it, and "3ab" parsed as three times one symbol named ab."""
    from maths.notation import parse_relation
    from maths.verify import _VALUE_RE
    a, b, m, v = sp.symbols("a b m v")
    assert parse_relation("2a^2 - 3ab + b").lhs == 2 * a**2 - 3 * a * b + b
    assert parse_relation("0.5mv^2").lhs == sp.Float(0.5) * m * v**2
    assert parse_relation("sin(x) + pi").lhs.free_symbols == {sp.Symbol("x")}
    assert _VALUE_RE.findall("Evaluate 2a^2 - 3ab + b when a = 3 and b = -2") == [("a", "3"), ("b", "-2")]
    two = _evaluate("Evaluate 2a^2 - 3ab + b when a = 3 and b = -2", ["2a^2 - 3ab + b"],
                    [("Substitute a = 3 and b = -2", ["2a^2 - 3ab + b"], ["2(3)^2 - 3(3)(-2) + (-2)"]),
                     ("Work out", ["2(3)^2 - 3(3)(-2) + (-2)"], ["18 + 18 - 2"]), ("Add", ["18 + 18 - 2"], ["34"])], ["34"])
    rep = verify_example(two)
    assert rep.status == "verified", [c.detail for c in rep.failures]
    formula = WorkedExample(label="Ex", task="evaluate", target="expression",
                            problem="The kinetic energy of an object is E = 0.5mv^2. Find E when m = 4 and v = -6.",
                            givens=["E = 0.5mv^2", "m = 4", "v = -6"],
                            steps=[Step(kind="setup", operation="write the formula", before=[], after=["E = 0.5mv^2", "m = 4", "v = -6"], speech="s"),
                                   Step(operation="Substitute m = 4 and v = -6", before=["E = 0.5mv^2", "m = 4", "v = -6"], after=["E = 0.5(4)(-6)^2"], speech="s"),
                                   Step(operation="Square", before=["E = 0.5(4)(-6)^2"], after=["E = 0.5(4)(36)"], speech="s"),
                                   Step(operation="Multiply", before=["E = 0.5(4)(36)"], after=["E = 72"], speech="s")],
                            final_answer=["E = 72"])
    rep = verify_example(formula)
    assert rep.status == "verified", [c.detail for c in rep.failures]
