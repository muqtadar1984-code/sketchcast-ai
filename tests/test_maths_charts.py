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
                "roots_cross", "roots_cross_speech", "ineq_caption", "ineq_right_open", "ineq_right_closed",
                "ineq_left_open", "ineq_left_closed", "ineq_between",
                "data_caption_mean", "data_caption_median", "data_caption_mode", "data_caption_range",
                "data_mean_speech", "data_median_speech", "data_median_even_speech", "data_mode_speech",
                "data_range_speech", "region_caption", "region_speech_closed", "region_speech_strict"):
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


def _inequality() -> WorkedExample:
    return WorkedExample(label="Example 3", task="solve_inequality", problem="3x - 1 >= 5", givens=["3x - 1 >= 5"], target="x",
                         intro_speech="An inequality.",
                         steps=[Step(kind="transform", before=["3x - 1 >= 5"], after=["3x >= 6"], operation="add 1", speech="Add one."),
                                Step(kind="transform", before=["3x >= 6"], after=["x >= 2"], operation="divide by 3", speech="Divide by three.")],
                         final_answer=["x >= 2"], answer_speech="So x is at least two.")


def test_a_solved_inequality_gets_its_number_line_with_the_solution_set():
    from maths.verify import verify_example
    ex = _inequality()
    assert verify_example(ex).status == "verified"
    ch = chart_for(ex)
    assert ch and ch["kind"] == "number_line" and ch["shape"] == "right" and ch["a"] == "2" and ch["closed"] == [True, False]
    assert ch["answer"] == "x ≥ 2"                   # as the working writes it
    beside = ch["beside"]["figures"][0]["figure"]["objects"]
    closing = ch["closing"]["figures"][0]["figure"]["objects"]
    assert [o["make"] for o in beside] == ["number_line"]                # bare, beside the working
    assert [o["make"] for o in closing] == ["number_line", "interval"]
    assert closing[1] == {"make": "interval", "id": "s", "from_closed": True, "to_closed": False, "from": "2"}
    assert closing[0]["range"][0] <= 0 and closing[0]["range"][1] >= 5     # room for the arrow to run
    rep = verify_question(ch["closing"])
    assert rep.ok, rep.refusal
    m = rep.models["g"]
    assert m.number_line is not None and m.intervals["s"].lo == 2.0 and m.intervals["s"].hi is None
    # a strict pair of bounds ("x > 1" and "x <= 4") is one interval, between
    pair = _inequality()
    pair.givens, pair.final_answer = ["x > 1", "x <= 4"], ["x > 1", "x <= 4"]
    pair.steps = [Step(kind="transform", before=["x > 1", "x <= 4"], after=["x > 1", "x <= 4"], speech="s")]
    ch2 = chart_for(pair)
    assert ch2 and ch2["shape"] == "between" and (ch2["a"], ch2["b"], ch2["closed"]) == ("1", "4", [False, True])
    # a flipped answer is not the givens' set: no chart, never a wrong one
    wrong = _inequality()
    wrong.final_answer = ["x <= 2"]
    assert chart_for(wrong) is None
    # two variables: not a number line — a half-plane (phase 7)
    two = WorkedExample(task="solve_inequality", problem="x + y <= 4", givens=["x + y <= 4"], target="y",
                        steps=[Step(kind="transform", before=["x + y <= 4"], after=["y <= 4 - x"], speech="s")],
                        final_answer=["y <= 4 - x"])
    assert chart_for(two)["kind"] == "half_plane"


def test_the_number_line_draws_beside_the_working_and_closes_on_the_solution_set():
    ex = _inequality()
    ex.chart = chart_for(ex)
    scene, _lines = B.example_scene(ex, MethodCard(title="METHOD", steps=["Collect", "Divide"]), "s003", has_card=True)
    assert not _audit(scene)
    ids = [e["id"] for e in scene["elements"]]
    assert any(i.startswith("fig_s") for i in ids), "the number line is drawn"
    out = B.chart_scene(ex, "s004", "en")
    assert out is not None
    close, lines = out
    texts = {e["text"] for e in close["elements"] if e["type"] == "text"}
    assert "x ≥ 2 on the number line" in texts
    assert "filled circle at 2" in lines[0].line and "greater than 2" in lines[0].line
    assert not _audit(close)
    # the closed bound is a solid disc: a stroke as wide as the dot (the
    # renderer fills an ellipse with mist, never ink)
    widths = [float(e.get("width", 0)) for e in close["elements"] if e["type"] == "shape"]
    assert max(widths) >= 12.0, widths
    from maths.charts import chart_image
    img = chart_image(ex.chart)
    assert img is not None and img[0][:4] == b"\x89PNG"


def _data(task, data, steps, answer) -> WorkedExample:
    return WorkedExample(label="Example 4", task=task, problem=f"Find the {task} of {data}.", givens=[data], target=task,
                         intro_speech="Some data.", answer_speech="There it is.",
                         steps=[Step(kind="transform", operation=op, before=[b], after=[a], speech="s") for op, b, a in steps],
                         final_answer=[answer])


def test_a_data_task_gets_its_bar_chart_with_the_statistic_marked():
    from maths.verify import verify_example
    mean = _data("mean", "4, 8, 6, 10, 12", [("mean", "4, 8, 6, 10, 12", "(4 + 8 + 6 + 10 + 12)/5"), ("work out", "(4 + 8 + 6 + 10 + 12)/5", "8")], "8")
    assert verify_example(mean).status == "verified"
    ch = chart_for(mean)
    assert ch and ch["kind"] == "data" and ch["stat"] == "mean" and ch["value"] == "8"
    beside = ch["beside"]["figures"][0]["figure"]["objects"][0]
    closing = ch["closing"]["figures"][0]["figure"]["objects"][0]
    assert beside == {"make": "bar_chart", "id": "bc", "values": ["4", "8", "6", "10", "12"]}   # as given, no answer
    assert closing["mean"] == "8" and "highlight" not in closing
    rep = verify_question(ch["closing"])
    assert rep.ok, rep.refusal
    bc = rep.models["g"].bar_chart
    assert bc.values == [4.0, 8.0, 6.0, 10.0, 12.0] and bc.step == 2.0 and bc.steps == 6 and bc.mean == 8.0
    # median: the closing bars are SORTED and the middle one is highlighted
    median = _data("median", "3, 7, 3, 5, 2, 3, 9", [("sort", "3, 7, 3, 5, 2, 3, 9", "2, 3, 3, 3, 5, 7, 9"), ("middle", "2, 3, 3, 3, 5, 7, 9", "3")], "3")
    ch = chart_for(median)
    assert ch["sorted"] and ch["closing"]["figures"][0]["figure"]["objects"][0] == {
        "make": "bar_chart", "id": "bc", "values": ["2", "3", "3", "3", "5", "7", "9"], "highlight": [3]}
    even = _data("median", "20, 5, 15, 10", [("sort", "20, 5, 15, 10", "5, 10, 15, 20"), ("middle two", "5, 10, 15, 20", "(10 + 15)/2"),
                                            ("work out", "(10 + 15)/2", "12.5")], "12.5")
    ch = chart_for(even)
    assert ch and ch["a"] == "10" and ch["b"] == "15" and ch["value"] == "25/2"
    # mode: every bar of the commonest value; range: the ends and a bracket
    mode = _data("mode", "3, 7, 3, 5, 2, 3, 9", [("count", "3, 7, 3, 5, 2, 3, 9", "3")], "3")
    assert chart_for(mode)["closing"]["figures"][0]["figure"]["objects"][0]["highlight"] == [0, 2, 5]
    rng = _data("range", "12, 15, 18, 9, 11", [("largest minus smallest", "12, 15, 18, 9, 11", "18 - 9"), ("subtract", "18 - 9", "9")], "9")
    ch = chart_for(rng)
    assert ch["closing"]["figures"][0]["figure"]["objects"][0] == {
        "make": "bar_chart", "id": "bc", "values": ["12", "15", "18", "9", "11"], "highlight": [2, 3], "range": True, "range_label": "9"}
    assert (ch["lo"], ch["hi"]) == ("9", "18")
    # a wrong statistic gets no chart; the answer may name the statistic
    wrong = _data("range", "12, 15, 18, 9, 11", [("subtract", "12, 15, 18, 9, 11", "18 - 11")], "7")
    assert chart_for(wrong) is None
    named = _data("mean", "5, 10, 15, 20", [("mean", "5, 10, 15, 20", "(5 + 10 + 15 + 20)/4"), ("work out", "(5 + 10 + 15 + 20)/4", "12.5")], "mean = 12.5")
    assert chart_for(named)["value"] == "25/2"


def test_the_bar_chart_draws_beside_the_working_and_closes_on_the_statistic():
    from maths.charts import chart_image
    for task, data, steps, answer, want_caption, want_words in (
        ("mean", "4, 8, 6, 10, 12", [("mean", "4, 8, 6, 10, 12", "(4 + 8 + 6 + 10 + 12)/5"), ("work out", "(4 + 8 + 6 + 10 + 12)/5", "8")], "8",
         "Mean = 8", "dashed line"),
        ("range", "12, 15, 18, 9, 11", [("largest minus smallest", "12, 15, 18, 9, 11", "18 - 9"), ("subtract", "18 - 9", "9")], "9",
         "Range = 9", "from the smallest bar, 9, to the largest, 18"),
    ):
        ex = _data(task, data, steps, answer)
        ex.chart = chart_for(ex)
        assert ex.chart is not None, task
        scene, _lines = B.example_scene(ex, MethodCard(title="METHOD", steps=["Add them up", "Divide by how many"]), "s003", has_card=True)
        assert not _audit(scene), task
        fills = [e for e in scene["elements"] if e["type"] == "shape" and e.get("fill") in ("blue", "yellow")]
        assert len(fills) == 5, task                                       # one bar per number
        out = B.chart_scene(ex, "s004", "en")
        assert out is not None, task
        close, lines = out
        texts = {e["text"] for e in close["elements"] if e["type"] == "text"}
        assert want_caption in texts and want_words in lines[0].line, task
        assert not _audit(close), task
        assert chart_image(ex.chart) is not None, task


def _half() -> WorkedExample:
    return WorkedExample(label="Example 5", task="solve_inequality", problem="Show the region x + y <= 4.", givens=["x + y <= 4"],
                         target="y", intro_speech="A region.", answer_speech="Shade below the line.",
                         steps=[Step(kind="transform", before=["x + y <= 4"], after=["y <= 4 - x"], operation="subtract x", speech="Take x across.")],
                         final_answer=["y <= 4 - x"])


def test_a_two_variable_inequality_verifies_as_a_half_plane_and_is_shaded():
    from maths.verify import half_plane, verify_example
    from maths.notation import parse_relation
    assert half_plane(parse_relation("x + y <= 4")) == half_plane(parse_relation("y <= 4 - x"))
    assert half_plane(parse_relation("2x + 2y < 8")) == half_plane(parse_relation("x + y < 4"))
    assert half_plane(parse_relation("x + y <= 4")) != half_plane(parse_relation("x + y < 4"))       # strictness matters
    assert half_plane(parse_relation("x + y <= 4")) != half_plane(parse_relation("x + y >= 4"))      # the side matters
    assert half_plane(parse_relation("x^2 + y <= 4")) is None and half_plane(parse_relation("x <= 4")) is None
    ex = _half()
    rep = verify_example(ex)
    assert rep.status == "verified", rep.reasons
    ch = chart_for(ex)
    assert ch and ch["kind"] == "half_plane" and ch["answer"] == "y ≤ 4 - x" and ch["strict"] is False
    objs = ch["closing"]["figures"][0]["figure"]["objects"]
    assert [o["make"] for o in objs] == ["axes", "line_eq", "half_plane"] and objs[1]["dashed"] is False
    assert [o["make"] for o in ch["beside"]["figures"][0]["figure"]["objects"]] == ["axes", "line_eq"]
    rep = verify_question(ch["closing"])
    assert rep.ok, rep.refusal
    m = rep.models["g"]
    assert len(m.regions["r1"]) >= 3 and all((x + y) <= 4 + 1e-6 for x, y in m.regions["r1"])
    # a strict inequality: the boundary is dashed; a wrong side gets no chart
    strict = _half()
    strict.givens, strict.final_answer = ["y > 2x - 1"], ["y > 2x - 1"]
    strict.steps = [Step(kind="transform", before=["y > 2x - 1"], after=["y > 2x - 1"], speech="s")]
    ch2 = chart_for(strict)
    assert ch2 and ch2["strict"] and ch2["closing"]["figures"][0]["figure"]["objects"][1]["dashed"] is True
    wrong = _half()
    wrong.final_answer = ["y >= 4 - x"]
    assert chart_for(wrong) is None
    assert verify_example(wrong).status != "verified"


def test_the_half_plane_draws_beside_the_working_and_closes_on_the_shaded_region():
    from maths.charts import chart_image
    ex = _half()
    ex.chart = chart_for(ex)
    scene, _lines = B.example_scene(ex, MethodCard(title="METHOD", steps=["Rearrange", "Shade"]), "s003", has_card=True)
    assert not _audit(scene)
    assert any(e["id"].startswith("fig_s") for e in scene["elements"]), "the boundary is drawn"
    out = B.chart_scene(ex, "s004", "en")
    assert out is not None
    close, lines = out
    texts = {e["text"] for e in close["elements"] if e["type"] == "text"}
    assert "The shaded region is y ≤ 4 - x" in texts and "drawn solid" in lines[0].line
    tints = [e for e in close["elements"] if e["type"] == "shape" and e.get("fill") is True]
    assert len(tints) == 1, "the region is one translucent wash"
    assert not _audit(close)
    assert chart_image(ex.chart) is not None


def test_a_straight_line_task_gets_the_line_it_was_verified_against_with_its_points():
    from maths.charts import chart_for
    from maths.schema import Step, WorkedExample
    ex = WorkedExample(label="L", task="line", givens=["(2, 5)", "m = 3"], target="equation",
                       steps=[Step(operation="substitute", before=["y = mx + c"], after=["5 = 3(2) + c"], speech="…"),
                              Step(operation="solve", before=["5 = 3(2) + c"], after=["c = -1"], speech="…")],
                       final_answer=["y = 3x - 1"])
    chart = chart_for(ex)
    assert chart and chart["kind"] == "lines" and chart["lines"] == ["y = 3x - 1"] and chart["point"] == "(2, 5)"
    objs = chart["closing"]["figures"][0]["figure"]["objects"]
    assert any(o["make"] == "point_at" and o["x"] == "2" and o["y"] == "5" for o in objs)
    assert not any(o["make"] == "point_at" for o in chart["beside"]["figures"][0]["figure"]["objects"])
    inter = WorkedExample(label="L", task="line", givens=["4x + 3y = 12"], target="x-intercept",
                          steps=[Step(operation="set y to zero", before=["4x + 3y = 12"], after=["x = 3"], speech="…")],
                          final_answer=["x = 3"])
    chart = chart_for(inter)
    assert chart and chart["point"] == "(3, 0)"
