"""Charts for algebra: derived from the givens and the proved answer, never
authored (founder decisions, 2026-10-09)."""

from __future__ import annotations

from maths import board as B
from maths.charts import chart_for
from maths.geometry import parse_question, verify_question
from maths.schema import Lesson, MethodCard, Step, WorkedExample
from spike.scene_engine.director import parse_scene_response
from spike.scene_engine.render import SceneRenderer


def _system() -> WorkedExample:
    return WorkedExample(label="Example 1", task="solve_system", problem="Solve 2x + y = 5 and x - y = 1.",
                         givens=["2x + y = 5", "x - y = 1"], target="x, y",
                         intro_speech="Two equations, two unknowns.",
                         steps=[Step(kind="transform", before=["2x + y = 5", "x - y = 1"], after=["3x = 6", "x - y = 1"],
                                     operation="add the equations", speech="Add them: the y terms cancel."),
                                Step(kind="transform", after=["x = 2", "x - y = 1"], operation="divide by 3", speech="Divide by three."),
                                Step(kind="transform", after=["x = 2", "y = 1"], operation="substitute x = 2", speech="Put x back in.")],
                         final_answer=["x = 2", "y = 1"], answer_speech="So x is two and y is one.")


def test_a_system_gets_its_two_lines_and_the_crossing_point():
    ch = chart_for(_system())
    assert ch and ch["kind"] == "lines" and ch["point"] == "(2, 1)" and ch["lines"] == ["2x + y = 5", "x - y = 1"]
    beside, closing = ch["beside"], ch["closing"]
    makes = [o["make"] for o in beside["figures"][0]["figure"]["objects"]]
    assert makes == ["axes", "line_eq", "line_eq"], makes                 # the lines only, beside the working
    makes = [o["make"] for o in closing["figures"][0]["figure"]["objects"]]
    assert makes == ["axes", "line_eq", "line_eq", "point_at"], makes     # the point joins at the close
    ax = beside["figures"][0]["figure"]["objects"][0]
    # the axes hold both intercepts (x: 5/2 and 1; y: 5 and -1) and the point
    assert ax["x"][0] <= 0 and ax["x"][1] >= 3 and ax["y"][0] <= -1 and ax["y"][1] >= 5
    rep = verify_question(closing)
    assert rep.ok, rep.refusal
    m = rep.models["g"]
    # the lines' ends are hidden helper points: no name, no dot, no evidence
    assert all(m.points[p].hidden for p in m.points if p != "p_s")
    assert m.line_labels == {"l1": "2x + y = 5", "l2": "x - y = 1"}


def test_a_single_line_in_x_and_y_is_graphed_and_a_quadratic_or_a_wrong_answer_is_not():
    one = WorkedExample(task="solve", problem="Draw y = 2x + 1.", givens=["y = 2x + 1"], target="y",
                        steps=[Step(kind="setup", after=["y = 2x + 1"], speech="s")], final_answer=["y = 2x + 1"])
    ch = chart_for(one)
    assert ch and ch["point"] is None and ch["lines"] == ["y = 2x + 1"]
    quad = WorkedExample(task="solve", problem="x^2 = 9", givens=["x^2 = 9"], target="x",
                         steps=[Step(kind="transform", after=["x = 3"], speech="s")], final_answer=["x = 3"])
    assert chart_for(quad) is None
    wrong = _system()
    wrong.final_answer = ["x = 3", "y = 1"]              # not on both lines: no chart, never a wrong one
    assert chart_for(wrong) is None
    wide = _system()
    wide.givens = ["x + y = 500", "x - y = 100"]
    wide.final_answer = ["x = 300", "y = 200"]
    assert chart_for(wide) is None                        # beyond the axes the board can number


def _audit(scene: dict) -> list[str]:
    sc = parse_scene_response(scene, scene["narration"])
    assert sc is not None
    r = SceneRenderer(sc)
    r.compile(30.0)
    return [w for w in r.audit()["warnings"] if w.startswith("TEXT_OVERLAP") or w.startswith("CUE_UNRESOLVED")]


def test_the_lines_draw_beside_the_working_and_the_point_in_a_closing_scene():
    ex = _system()
    ex.chart = chart_for(ex)
    scene, _lines = B.example_scene(ex, MethodCard(title="METHOD", steps=["Eliminate", "Substitute"]), "s003", has_card=True)
    texts = {e["text"] for e in scene["elements"] if e["type"] == "text"}
    assert "2x + y = 5" in texts and "x - y = 1" in texts          # the lines, labelled, beside the working
    assert "(2, 1)" not in texts                                   # the answer is not given away
    assert not _audit(scene)
    out = B.chart_scene(ex, "s004", "en")
    assert out is not None
    close, lines = out
    texts = {e["text"] for e in close["elements"] if e["type"] == "text"}
    assert "(2, 1)" in texts and "The lines cross at (2, 1)" in texts
    assert lines[0].line.startswith("On the graph, the two lines cross at the point (2, 1)")
    assert not _audit(close)


def test_the_chart_segment_follows_its_example_in_the_lesson():
    ex = _system()
    ex.chart = chart_for(ex)
    lesson = Lesson(topic="Simultaneous equations", level="Class 8", method=MethodCard(title="METHOD", steps=["Eliminate"]),
                    examples=[ex])
    segs = B.compile_lesson(lesson, None, "en")
    types = [(s["segment_id"], s["slide_heading"][:25]) for s in segs]
    i = next(k for k, s in enumerate(segs) if s["slide_heading"].startswith("Example 1"))
    assert segs[i + 1]["slide_heading"].startswith("The lines cross at (2, 1)"), types
    assert segs[i + 1]["segment_id"] == f"s{int(segs[i]['segment_id'][1:]) + 1:03d}"


def test_every_chart_string_exists_in_every_lesson_language():
    from maths.i18n import BOARD, LANGS
    for key in ("lines_cross", "lines_cross_speech", "line_graph", "line_graph_speech"):
        assert set(BOARD[key]) >= set(LANGS), key
