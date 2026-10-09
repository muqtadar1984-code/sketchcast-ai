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
                                Step(kind="transform", before=["3x = 6", "x - y = 1"], after=["x = 2", "x - y = 1"],
                                     operation="divide by 3", speech="Divide by three."),
                                Step(kind="transform", before=["x = 2", "x - y = 1"], after=["x = 2", "y = 1"],
                                     operation="substitute x = 2", speech="Put x back in.")],
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
    assert chart_for(quad) is None                        # one root named of two: no chart
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
    for key in ("lines_cross", "lines_cross_speech", "line_graph", "line_graph_speech",
                "roots_cross", "roots_cross_speech"):
        assert set(BOARD[key]) >= set(LANGS), key


def test_a_chart_renders_to_a_picture_for_paper_and_slides():
    from maths.charts import chart_image
    ch = chart_for(_system())
    img = chart_image(ch)
    assert img is not None
    png, width_mm = img
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 2000 and 30.0 < width_mm < 200.0
    assert chart_image(None) is None and chart_image({"closing": "nonsense"}) is None


def test_the_deck_slide_carries_the_chart(tmp_path):
    from agent5_slides.deck_generator import apply_maths_lesson
    from shared.lesson_model import LessonModel
    ex = _system()
    ex.chart = chart_for(ex)
    model = LessonModel(title="Simultaneous equations")
    n = apply_maths_lesson(model, {"examples": [ex.model_dump()]}, tmp_path, "en")
    assert n == 1 and model.worked_figures == {0: "maths_chart_1"}
    fig = model.figures["maths_chart_1"]
    assert fig.png.exists() and fig.w > 0 and fig.h > 0


def test_the_answer_key_prints_the_chart_under_a_system_question(tmp_path):
    from docgen import generate_document
    from tests.test_maths_geometry_worksheet import BOOK, CHAPTER, Client, _pictures
    from tests.test_maths_lesson import GOOD_EX1
    system = {
        "label": "Q", "difficulty": 2, "task": "solve_system", "problem": "Solve 2x + y = 5 and x - y = 1.",
        "givens": ["2x + y = 5", "x - y = 1"], "target": "x, y",
        "steps": [{"kind": "transform", "operation": "add the equations", "before": ["2x + y = 5", "x - y = 1"],
                   "after": ["3x = 6", "x - y = 1"], "explanation": "add the equations"},
                  {"kind": "transform", "operation": "divide by 3", "before": ["3x = 6", "x - y = 1"],
                   "after": ["x = 2", "x - y = 1"], "explanation": "divide by 3"},
                  {"kind": "transform", "operation": "substitute x = 2", "before": ["x = 2", "x - y = 1"],
                   "after": ["x = 2", "y = 1"], "explanation": "substitute"}],
        "final_answer": ["x = 2", "y = 1"],
    }
    client = Client([dict(GOOD_EX1), system], [], [])
    paths = generate_document("worksheet", BOOK, CHAPTER, {}, client, {"num_questions": 2}, tmp_path,
                              language="en", maths=True)
    # the sheet shows no chart (it would give the answer away); the key shows ONE, under the system
    assert _pictures(paths[0]) == 0
    assert _pictures(paths[1]) == 1


def _quadratic() -> WorkedExample:
    return WorkedExample(label="Example 2", task="solve", problem="x^2 - x - 6 = 0", givens=["x^2 - x - 6 = 0"], target="x",
                         intro_speech="A quadratic.",
                         steps=[Step(kind="transform", before=["x^2 - x - 6 = 0"], after=["(x - 3)(x + 2) = 0"],
                                     operation="factorise", speech="Factorise."),
                                Step(kind="transform", before=["(x - 3)(x + 2) = 0"], after=["x - 3 = 0", "x + 2 = 0"],
                                     operation="a product is zero when a factor is zero", speech="Either factor can be zero."),
                                Step(kind="transform", before=["x - 3 = 0", "x + 2 = 0"], after=["x = 3", "x = -2"],
                                     operation="solve each", speech="Solve each.")],
                         final_answer=["x = 3 or x = -2"], answer_speech="So x is three or minus two.")


def test_a_solved_quadratic_gets_its_parabola_roots_and_vertex():
    from maths.verify import verify_example
    ex = _quadratic()
    assert verify_example(ex).status == "verified"
    ch = chart_for(ex)
    assert ch and ch["kind"] == "parabola" and ch["roots"] == ["-2", "3"] and ch["vertex"] == "(1/2, -25/4)"
    objs = ch["closing"]["figures"][0]["figure"]["objects"]
    assert [o["make"] for o in objs] == ["axes", "curve_eq", "point_at", "point_at", "point_at"]
    beside = ch["beside"]["figures"][0]["figure"]["objects"]
    assert [o["make"] for o in beside] == ["axes", "curve_eq"]           # no roots given away
    ax = objs[0]
    assert ax["x"][0] <= -2 and ax["x"][1] >= 3 and ax["y"][0] <= -7 and ax["y"][1] >= 1
    assert ax["x"][0] % ax["step"] == 0 and ax["y"][1] % ax["step"] == 0
    rep = verify_question(ch["closing"])
    assert rep.ok, rep.refusal
    runs, label = rep.models["g"].curves["c1"]
    assert label == "x² - x - 6 = 0" and runs and all(len(r) >= 2 for r in runs)
    # a factorised trinomial is plotted the same way; a cubic is not; a
    # quadratic with no real root is not (yet)
    fac = WorkedExample(task="factorise", problem="x^2 + 5x + 6", givens=["x^2 + 5x + 6"], target="expression",
                        steps=[Step(kind="transform", before=["x^2 + 5x + 6"], after=["(x + 2)(x + 3)"], speech="s")],
                        final_answer=["(x + 2)(x + 3)"])
    assert chart_for(fac)["roots"] == ["-3", "-2"]
    cubic = WorkedExample(task="solve", problem="x^3 = 8", givens=["x^3 = 8"], target="x",
                          steps=[Step(kind="transform", before=["x^3 = 8"], after=["x = 2"], speech="s")], final_answer=["x = 2"])
    assert chart_for(cubic) is None
    none = WorkedExample(task="factorise", problem="x^2 + 1", givens=["x^2 + 1"], target="expression",
                         steps=[Step(kind="transform", before=["x^2 + 1"], after=["x^2 + 1"], speech="s")], final_answer=["x^2 + 1"])
    assert chart_for(none) is None


def test_the_parabola_draws_beside_the_working_and_closes_on_its_roots():
    ex = _quadratic()
    ex.chart = chart_for(ex)
    scene, _lines = B.example_scene(ex, MethodCard(title="METHOD", steps=["Factorise"]), "s003", has_card=True)
    assert not _audit(scene)
    ids = [e["id"] for e in scene["elements"]]
    assert any(i.startswith("fig_s") for i in ids), "the curve is drawn"
    out = B.chart_scene(ex, "s004", "en")
    assert out is not None
    close, lines = out
    texts = {e["text"] for e in close["elements"] if e["type"] == "text"}
    assert "(3, 0)" in texts and "(-2, 0)" in texts and "The curve crosses the x-axis at x = -2, x = 3" in texts
    assert "parabola" in lines[0].line and "(1/2, -25/4)" in lines[0].line
    assert not _audit(close)
    seg = B.chart_segment(ex, "s004", "en")
    assert seg["slide_heading"].startswith("The curve crosses")
