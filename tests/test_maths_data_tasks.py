"""The data tasks — mean, median, mode, range — verified against the data.

"Mean 8 Class 7.1", 2026-09-26: a Grade 7 statistics chapter's worksheet and
exam paper both failed with "no question could be verified" while its video
passed. Every question the model wrote stated its data as a comma list —
"Find the arithmetic mean of the numbers 4, 8, 6, 10, and 12" — and the
parser's tuple guard (added the day before, after a list crashed a worksheet
job) refused every one: "Please write one expression — I found a list
separated by commas." Nothing was fabricated; the verifier had no notion of
a data set, and its task list stopped at solve, simplify, evaluate, round.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from maths.notation import NotationError, parse_relation, strip_task_verb
from maths.schema import DATA_TASKS, TASKS, Step, TryIt, WorkedExample
from maths.verify import verify_example, verify_try_it


def _ex(task, givens, steps, answer, problem="", **kw):
    return WorkedExample(label=task, task=task, problem=problem or givens, givens=[givens], target=task,
                         steps=[Step(operation=op, before=[b], after=[a], speech="s") for op, b, a in steps],
                         final_answer=[answer] if isinstance(answer, str) else list(answer), **kw)


# ── the notation: a data list ─────────────────────────────────────────────


class TestADataListIsNotation:
    def test_the_incident_lines_parse_as_data(self):
        assert parse_relation("4, 8, 6, 10, 12").data == (4, 8, 6, 10, 12)
        assert parse_relation("Find the arithmetic mean of the numbers 4, 8, 6, 10, and 12.").data == (4, 8, 6, 10, 12)
        assert parse_relation("Find the mode of the data: 3, 7, 3, 5, 2, 3, 9.").data == (3, 7, 3, 5, 2, 3, 9)
        assert parse_relation("5; 10; 15; 20").data == (5, 10, 15, 20)

    def test_a_data_relation_has_no_unknowns_and_is_neither_equation_nor_expression(self):
        r = parse_relation("2.5, 3, 1/2")
        assert r.is_data and not r.is_expression and not r.is_equation and r.free_symbols == set()
        assert len(r.data) == 3 and r.text == "2.5, 3, 1/2"

    def test_a_comma_without_a_space_is_still_a_decimal_or_a_digit_group(self):
        assert parse_relation("1,234").lhs == 1234 and not parse_relation("1,234").is_data
        assert parse_relation("1, 234").data == (1, 234)
        assert float(parse_relation("3,14").lhs) == pytest.approx(3.14)

    def test_a_list_with_an_unknown_or_a_relation_is_not_data(self):
        with pytest.raises(NotationError):
            parse_relation("x, 8, 6")
        with pytest.raises(NotationError):
            parse_relation("x = 4, 8")

    def test_the_task_verb_stripper_keeps_whole_words(self):
        # "Find the mean" used to lose the t of "the" and hand over "he mean of"
        assert strip_task_verb("Find the mean of 4, 8, 6") == "4, 8, 6"
        assert strip_task_verb("Find the median of the data: 5, 10") == "5, 10"
        assert strip_task_verb("Solve for x: 3x = 6") == "3x = 6"
        assert strip_task_verb("Find x if 2x = 8") == "2x = 8"
        assert parse_relation("Solve 4z = 16").text == "4z = 16"


# ── the four tasks ───────────────────────────────────────────────────────


class TestTheTasksVerify:
    def test_the_tasks_exist(self):
        assert DATA_TASKS == ("mean", "median", "mode", "range") and set(DATA_TASKS) <= set(TASKS)

    def test_the_incident_mean_question_verifies(self):
        ex = _ex("mean", "4, 8, 6, 10, 12",
                 [("add the values and divide by how many", "4, 8, 6, 10, 12", "(4 + 8 + 6 + 10 + 12)/5"),
                  ("add", "(4 + 8 + 6 + 10 + 12)/5", "40/5"), ("divide", "40/5", "8")], "8",
                 problem="Find the arithmetic mean of the numbers 4, 8, 6, 10, and 12.")
        rep = verify_example(ex)
        assert rep.status == "verified", rep.reasons
        assert [c.ok for c in rep.checks] == [True] * len(rep.checks)

    def test_the_answer_may_name_the_statistic(self):
        ex = _ex("mean", "5, 10, 15, 20", [("mean", "5, 10, 15, 20", "(5 + 10 + 15 + 20)/4"), ("work out", "(5 + 10 + 15 + 20)/4", "12.5")],
                 "mean = 12.5")
        assert verify_example(ex).status == "verified"

    def test_a_wrong_mean_fails_on_the_answer_and_the_wrong_first_step_fails_on_the_step(self):
        ex = _ex("mean", "5, 10, 15, 20", [("mean", "5, 10, 15, 20", "(5 + 10 + 15 + 20)/4"), ("work out", "(5 + 10 + 15 + 20)/4", "50/4")],
                 "mean = 12")
        rep = verify_example(ex)
        assert rep.status == "failed"
        assert any(c.name == "answer" and c.ok is False and "is 25/2, not" in c.detail for c in rep.checks)
        ex = _ex("mean", "5, 10, 15, 20", [("mean", "5, 10, 15, 20", "(5 + 10 + 15 + 20)/5")], "10")
        rep = verify_example(ex)
        assert any(c.name == "step 1" and c.ok is False and "the mean of '5, 10, 15, 20' is 25/2" in c.detail
                   for c in rep.checks)

    def test_the_median_sorts_then_takes_the_middle(self):
        ex = _ex("median", "3, 7, 3, 5, 2, 3, 9",
                 [("sort", "3, 7, 3, 5, 2, 3, 9", "2, 3, 3, 3, 5, 7, 9"), ("middle value", "2, 3, 3, 3, 5, 7, 9", "3")],
                 "median = 3")
        assert verify_example(ex).status == "verified"

    def test_an_even_count_takes_the_mean_of_the_middle_two(self):
        ex = _ex("median", "20, 5, 15, 10", [("sort", "20, 5, 15, 10", "5, 10, 15, 20"),
                                             ("middle two", "5, 10, 15, 20", "(10 + 15)/2"), ("divide", "(10 + 15)/2", "12.5")], "12.5")
        assert verify_example(ex).status == "verified"

    def test_a_sort_that_drops_or_changes_a_value_fails(self):
        ex = _ex("median", "3, 1, 2", [("sort", "3, 1, 2", "1, 2"), ("middle", "1, 2", "2")], "2")
        rep = verify_example(ex)
        assert rep.status == "failed"
        assert any(c.name == "step 1" and c.ok is False and "is not the same data as" in c.detail for c in rep.checks)

    def test_the_mode_is_the_commonest_value_and_may_be_two(self):
        assert verify_example(_ex("mode", "3, 7, 3, 5, 2, 3, 9", [("count", "3, 7, 3, 5, 2, 3, 9", "3")], "3")).status == "verified"
        assert verify_example(_ex("mode", "1, 2, 2, 3, 3", [("count", "1, 2, 2, 3, 3", "2, 3")], "2, 3")).status == "verified"
        rep = verify_example(_ex("mode", "1, 2, 2, 3, 3", [("count", "1, 2, 2, 3, 3", "2")], "2"))
        assert rep.status == "failed" and any("is 2, 3, not" in c.detail for c in rep.failures)

    def test_data_with_no_mode_cannot_have_one(self):
        rep = verify_example(_ex("mode", "1, 2, 3", [("count", "1, 2, 3", "1")], "1"))
        assert rep.status == "failed" and any("has no mode" in c.detail for c in rep.failures)

    def test_the_range_is_largest_minus_smallest(self):
        ex = _ex("range", "12, 15, 18, 9, 11", [("largest minus smallest", "12, 15, 18, 9, 11", "18 - 9"), ("subtract", "18 - 9", "9")],
                 "range = 9")
        assert verify_example(ex).status == "verified"
        rep = verify_example(_ex("range", "12, 15, 18, 9, 11", [("subtract", "12, 15, 18, 9, 11", "18 - 11")], "7"))
        assert rep.status == "failed" and any("the range of '12, 15, 18, 9, 11' is 9" in c.detail for c in rep.failures)

    def test_decimals_and_fractions_are_exact(self):
        ex = _ex("mean", "2.5, 3.5, 4", [("mean", "2.5, 3.5, 4", "(2.5 + 3.5 + 4)/3"), ("work out", "(2.5 + 3.5 + 4)/3", "10/3")], "10/3")
        assert verify_example(ex).status == "verified"
        ex = _ex("mean", "2.5, 3.5, 4", [("mean", "2.5, 3.5, 4", "(2.5 + 3.5 + 4)/3"), ("work out", "(2.5 + 3.5 + 4)/3", "10/3")], "3.33")
        assert verify_example(ex).status == "failed", "a rounded answer is not the exact statistic"

    def test_a_data_list_under_a_non_data_task_is_refused_not_guessed(self):
        rep = verify_example(_ex("evaluate", "4, 8, 6", [("mean", "4, 8, 6", "(4 + 8 + 6)/3")], "6"))
        assert rep.status == "failed"
        assert any(c.ok is None and "only worked in a mean/median/mode/range task" in c.detail for c in rep.checks)

    def test_a_data_task_whose_givens_are_not_a_list_is_unverifiable(self):
        rep = verify_example(_ex("mean", "4 + 8 + 6", [("divide", "4 + 8 + 6", "18/3")], "6"))
        assert rep.status == "failed"
        assert any(c.name == "answer" and c.ok is None and "needs a data list" in c.detail for c in rep.checks)


class TestTheTryIt:
    def test_a_try_it_over_data_is_verified_as_the_statistic_its_words_ask_for(self):
        t = TryIt(problem="Find the median of 7, 2, 9, 4, 4", answer=["4"],
                  steps=[Step(operation="sort", before=["7, 2, 9, 4, 4"], after=["2, 4, 4, 7, 9"], speech="s"),
                         Step(operation="middle", before=["2, 4, 4, 7, 9"], after=["4"], speech="s")])
        assert verify_try_it(t).ok is True
        t = TryIt(problem="Find the range of 7, 2, 9, 4, 4", answer=["7"])
        assert verify_try_it(t).ok is True
        t = TryIt(problem="Find the mean of 7, 2, 9, 4, 4", answer=["5"])
        assert verify_try_it(t).ok is False, "the mean is 26/5"


# ── the prompt and the documents ─────────────────────────────────────────


def test_the_prompt_teaches_the_data_tasks():
    from maths.lesson import _NOTATION_RULES, _STEP_RULES, build_prompt
    assert '"4, 8, 6, 10, 12"' in _NOTATION_RULES
    assert '"(4 + 8 + 6 + 10 + 12)/5"' in _STEP_RULES and "mode" in _STEP_RULES
    prompt = build_prompt(topic="Mean", subject="Mathematics", level="Grade 7", curriculum=None, language="en",
                          episode_context="")
    assert "| mean | median | mode | range" in prompt


def test_a_bare_data_list_prints_with_its_verb():
    from docgen.maths_worksheet import _problem_text
    assert _problem_text(WorkedExample(task="mean", givens=["4, 8, 6, 10, 12"])) == "Find the mean of: 4, 8, 6, 10, 12"
    assert _problem_text(WorkedExample(task="range", givens=["4, 8"]), "ms") == "Cari julat bagi: 4, 8"
    assert _problem_text(WorkedExample(task="mean", problem="Find the arithmetic mean of the numbers 4, 8, 6, 10, and 12.",
                                       givens=["4, 8, 6, 10, 12"])) == "Find the arithmetic mean of the numbers 4, 8, 6, 10, and 12."


def test_the_incident_worksheet_now_prints_its_statistics_questions(tmp_path):
    """The exact shape the model returned on 2026-09-26, through the ladder
    and into the two documents."""
    from docgen import generate_document
    from shared.coverage import docx_text

    def q(label, d, task, problem, givens, steps, answer):
        return {"label": label, "difficulty": d, "task": task, "problem": problem, "givens": [givens], "target": task,
                "steps": [{"kind": "transform", "operation": op, "before": [b], "after": [a], "speech": "s"} for op, b, a in steps],
                "final_answer": [answer], "intro_speech": "", "answer_speech": ""}

    qs = [
        q("Q", 1, "mean", "Find the arithmetic mean of the numbers 4, 8, 6, 10, and 12.", "4, 8, 6, 10, 12",
          [("add and divide by 5", "4, 8, 6, 10, 12", "(4 + 8 + 6 + 10 + 12)/5"), ("add", "(4 + 8 + 6 + 10 + 12)/5", "40/5"),
           ("divide", "40/5", "8")], "8"),
        q("Q", 1, "mean", "Find the mean of the data: 5, 10, 15, 20.", "5, 10, 15, 20",
          [("add and divide by 4", "5, 10, 15, 20", "(5 + 10 + 15 + 20)/4"), ("work out", "(5 + 10 + 15 + 20)/4", "12.5")], "12.5"),
        q("Q", 2, "mode", "Find the mode of the data: 3, 7, 3, 5, 2, 3, 9.", "3, 7, 3, 5, 2, 3, 9",
          [("the value occurring most", "3, 7, 3, 5, 2, 3, 9", "3")], "3"),
        q("Q", 2, "median", "Find the median of 3, 7, 3, 5, 2, 3, 9.", "3, 7, 3, 5, 2, 3, 9",
          [("sort", "3, 7, 3, 5, 2, 3, 9", "2, 3, 3, 3, 5, 7, 9"), ("middle value", "2, 3, 3, 3, 5, 7, 9", "3")], "3"),
        q("Q", 3, "range", "Find the range of 12, 15, 18, 9, 11.", "12, 15, 18, 9, 11",
          [("largest minus smallest", "12, 15, 18, 9, 11", "18 - 9"), ("subtract", "18 - 9", "9")], "9"),
        q("Q", 3, "mean", "WRONG: the mean of 1, 2, 3", "1, 2, 3", [("mean", "1, 2, 3", "(1 + 2 + 3)/3")], "3"),
    ]

    class Client:
        model = "fake"

        def analyze(self, prompt, system="", max_tokens=0, retries=3, cache_prefix=None, response_schema=None, **kw):
            return {"data": {"questions": copy.deepcopy(qs)}, "usage": {}, "truncated": False}

    book = {"title": "Mean 8 Class 7.1", "grade": "Grade 7", "subject": "Mathematics"}
    chapter = {"title": "Mean 8 Class 7.1", "sections": []}
    paths = generate_document("worksheet", book, chapter, {}, Client(), {"num_questions": 5}, tmp_path,
                              language="en", maths=True)
    assert len(paths) == 2 and all(Path(p).exists() for p in paths)
    sheet, key = docx_text(paths[0]), docx_text(paths[1])
    assert "Find the arithmetic mean of the numbers 4, 8, 6, 10, and 12." in sheet
    assert "Find the mode of the data: 3, 7, 3, 5, 2, 3, 9." in sheet
    assert "WRONG" not in sheet, "an unverified question is never printed"
    assert "(4 + 8 + 6 + 10 + 12)/5" in key and "2, 3, 3, 3, 5, 7, 9" in key and "Answer: 12.5" in key
