"""A maths worksheet is a verified ladder; its answer key prints the working."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from docgen import generate_document
from docgen.maths_worksheet import _problem_text
from maths.schema import parse_lesson, WorkedExample
from shared.coverage import docx_text
from tests.test_maths_lesson import EX3, GOOD_EX1, FIXED_EX2, LESSON

Q_BAD = {"label": "Q", "difficulty": 2, "task": "solve", "problem": "4x - 1 = 11", "givens": ["4x - 1 = 11"],
         "target": "x", "steps": [{"kind": "transform", "operation": "add 1", "before": ["4x - 1 = 11"],
                                   "after": ["4x = 10"], "speech": "s"}],       # WRONG (12)
         "final_answer": ["x = 5/2"]}
Q_WORD = {"label": "Q", "difficulty": 3, "task": "solve",
          "problem": "A number doubled and then increased by 7 gives 19. Find the number.", "givens": ["2n + 7 = 19"],
          "target": "n", "steps": [{"kind": "setup", "operation": "let n be the number", "before": [], "after": ["2n + 7 = 19"], "speech": "s"},
                                   {"kind": "transform", "operation": "subtract 7 from both sides", "before": ["2n + 7 = 19"], "after": ["2n = 12"], "speech": "s"},
                                   {"kind": "transform", "operation": "divide both sides by 2", "before": ["2n = 12"], "after": ["n = 6"], "speech": "s"}],
          "final_answer": ["n = 6"]}


def _q(ex: dict, difficulty: int, problem: str | None = None) -> dict:
    q = copy.deepcopy(ex)
    q["difficulty"] = difficulty
    if problem:
        q["problem"] = problem
        q["givens"] = [problem]
        q["steps"][0]["before"] = [problem]
    return q


class FakeClient:
    model = "fake"

    def __init__(self):
        self.calls = []

    def analyze(self, prompt, system="", max_tokens=0, retries=3, cache_prefix=None, response_schema=None, **kw):
        self.calls.append(prompt)
        qs = [_q(GOOD_EX1, 1), _q(GOOD_EX1, 1, "2x + 3 = 11"), _q(FIXED_EX2, 2), Q_BAD, _q(EX3, 3), Q_WORD,
              _q(EX3, 4, "7x - 4 = 3x + 12")]
        return {"data": {"questions": qs}, "usage": {}, "truncated": False}


def test_a_maths_worksheet_prints_only_verified_questions_and_the_working(tmp_path):
    lesson = parse_lesson(copy.deepcopy(LESSON))
    book = {"title": "Mathematics", "grade": "Class 8", "subject": "Mathematics"}
    chapter = {"title": "Linear Equations in One Variable", "sections": []}
    c = FakeClient()
    paths = generate_document("worksheet", book, chapter, {}, c, {"num_questions": 6}, tmp_path,
                              language="en", maths=True, maths_lesson=lesson)
    assert len(paths) == 2 and all(Path(p).exists() for p in paths)
    sheet, key = docx_text(paths[0]), docx_text(paths[1])
    assert "Solve: 3x + 5 = 20" in sheet and "Warm-up" in sheet and "Stretch" in sheet
    assert "A number doubled" in sheet, "a word problem prints as written"
    assert "4x − 1 = 11" not in sheet, "the question whose working is wrong is not printed"
    assert "x = 5" in key and "subtract 5 from both sides" in key and "Answer:" in key
    assert "Check: 3 × 5 + 5 = 20" in key
    assert "computer algebra" in key
    assert "METHOD TAUGHT" in c.calls[0] and "3x + 5 = 20" in c.calls[0], "the sibling lesson steers the set"
    qjson = json.loads((tmp_path / "questions.json").read_text())
    assert qjson["questions"] and all(q["prompt"] and q["answer"] for q in qjson["questions"])


def test_a_maths_test_paper_carries_marks_and_a_mark_scheme(tmp_path):
    book = {"title": "Mathematics", "grade": "Class 8", "subject": "Mathematics"}
    chapter = {"title": "Linear Equations", "sections": []}
    paths = generate_document("exam_paper", book, chapter, {}, FakeClient(),
                              {"objective": {"fill_blank": 2, "true_false": 2}, "subjective": 2}, tmp_path,
                              language="en", maths=True)
    sheet, key = docx_text(paths[0]), docx_text(paths[1])
    assert "[2 marks]" in sheet and "Total:" in sheet
    assert "for the method" in key


def test_science_documents_are_untouched(tmp_path, monkeypatch):
    called = {}
    import docgen.worksheet as ws
    monkeypatch.setattr(ws, "build", lambda *a, **k: called.setdefault("ws", True) or [tmp_path / "a", tmp_path / "b"])
    generate_document("worksheet", {}, {}, {}, FakeClient(), {}, tmp_path, language="en", maths=False)
    assert called.get("ws")


def test_problem_text_gets_its_verb():
    assert _problem_text(WorkedExample(task="solve", problem="3x + 5 = 20")) == "Solve: 3x + 5 = 20"
    assert _problem_text(WorkedExample(task="expand", problem="(x + 2)(x + 3)")) == "Expand: (x + 2)(x + 3)"
    assert _problem_text(WorkedExample(task="solve", problem="Find x if 3x + 5 = 20")) == "Find x if 3x + 5 = 20"
    assert _problem_text(WorkedExample(task="solve", problem="A train travels 60 km in 40 minutes. Find its speed in km per hour.")).startswith("A train")


def test_the_worksheet_words_follow_the_document_language():
    from docgen import docx_builder as dx
    from docgen.maths_worksheet import _problem_text
    from maths.schema import WorkedExample
    ex = WorkedExample(task="solve", problem="3x + 5 = 20", givens=["3x + 5 = 20"], target="x", final_answer=["x = 5"])
    assert _problem_text(ex, "ar").startswith("حلّ: ")
    assert _problem_text(ex, "hi").startswith("हल कीजिए: ")
    assert _problem_text(ex, "en").startswith("Solve: ")
    assert dx._t("ws_warm_up", "ar") == "تمهيد" and dx._t("ws_total_marks", "hi").format(n=20) == "कुल: 20 अंक"
    for lang in ("en", "ms", "ms-arab", "ar", "fr", "es", "pt", "hi", "mr", "te"):
        for key in ("ws_warm_up", "ws_practice", "ws_challenge", "ws_stretch", "difficulty_1", "difficulty_4",
                    "verb_solve", "verb_factorise", "ws_instructions", "exam_instructions", "cas_note",
                    "ws_total_marks", "marks_scheme", "sol_check", "sol_answer", "sol_or"):
            assert dx._t(key, lang), (key, lang)
