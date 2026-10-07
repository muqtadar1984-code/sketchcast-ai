"""Figure examples in the maths video lesson: a verified geometry question
becomes a worked example whose diagram draws on the board, whose deduce
steps highlight what they cite, and whose answer is written onto the
figure from the engine's proved values."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from maths import board as B
from maths import lesson as L
from maths.geometry import verify_question
from maths.geometry.board_adapter import targets_for
from maths.geometry.items import GeometryItem, normalise_question
from maths.schema import MethodCard
from maths.verify import verify_example
from spike.scene_engine.director import parse_scene_response
from spike.scene_engine.render import SceneRenderer
from spike.scene_engine.schema import Scene
from tests.test_maths_lesson import FakeClient

CORPUS = json.loads((Path(__file__).parent / "fixtures" / "geometry_corpus_v1.json").read_text(encoding="utf-8"))
ITEMS = {item["question"]["id"]: item["question"] for item in CORPUS["items"]}

B1_SPEECH = {
    "intro_speech": "Here is a straight line through A, B and C, and a ray from B to D. The angle A B D is "
                    "seventy degrees. We want the angle D B C, marked x.",
    "steps": ["Look at the two angles at B. They sit on the straight line A B C, so together they make one "
              "hundred and eighty degrees: seventy plus x equals one hundred and eighty.",
              "Take seventy away from both sides. So x equals one hundred and ten."],
    "answer_speech": "So the angle D B C is one hundred and ten degrees.",
}


def _b1_reply() -> dict:
    """Corpus B1 in the shape the model's reply takes, with its speech."""
    q = copy.deepcopy(ITEMS["B1"])
    q["difficulty"] = 1
    q["intro_speech"] = B1_SPEECH["intro_speech"]
    q["answer_speech"] = B1_SPEECH["answer_speech"]
    for st, speech in zip(q["steps"], B1_SPEECH["steps"]):
        st["speech"] = speech
    return q


def _item(q: dict) -> GeometryItem:
    """A GeometryItem the way geometry_items builds one from a reply."""
    spec, difficulty = normalise_question(q)
    rep = verify_question(spec)
    assert rep.ok, rep.refusal
    speech = {"intro": q.get("intro_speech", ""), "answer": q.get("answer_speech", ""),
              "observations": {o["figure"]: o["speech"] for o in q.get("observations") or []}}
    return GeometryItem(spec["id"], spec["prompt"], spec["figure_role"], difficulty, spec, rep, speech=speech)


def _render(scene: dict, narration: str) -> SceneRenderer:
    sc = parse_scene_response(scene, narration)
    assert sc is not None
    r = SceneRenderer(sc)
    r.compile(30.0)
    warnings = r.audit()["warnings"]
    assert not any(w.startswith("CUE_UNRESOLVED") for w in warnings), warnings
    return r


def test_a_find_x_figure_example_draws_its_figure_and_works_beside_it():
    ex = L.figure_example(_item(_b1_reply()))
    assert ex.has_figure and ex.final_answer == ["x = 110"] and ex.target == "x"
    assert [s.kind for s in ex.steps] == ["deduce", "transform"]
    assert ex.steps[0].theorem == "angles_on_line" and ex.steps[0].speech.startswith("Look at")
    # proved by the geometry chain, not SymPy
    assert verify_example(ex).status == "verified"

    scene, lines = B.example_scene(ex, MethodCard(title="METHOD", steps=["Angles on a line", "Solve for x"]), "s003")
    Scene.model_validate(scene)
    els = {e["id"]: e for e in scene["elements"]}
    strokes = [e for e in els.values() if e["type"] == "shape" and e["id"].startswith("fig_")]
    assert strokes, "the figure's strokes are on the board"
    for e in strokes:
        for x, y in e.get("points") or [e["center"]]:
            assert B.Q_AT[0] - 2 <= x <= B.FIG_RIGHT + 2 and B.FIRST_ROW_Y - 2 <= y <= B.WORK_BOTTOM + 2, (e["id"], x, y)
    # the working column moved right of the figure
    work = [e for e in els.values() if e["id"].startswith("w") and e["id"][1:].isdigit()]
    assert work and all(e["at"][0] == B.FIG_LINE_X for e in work), [e["at"] for e in work]
    assert {e.get("expr") for e in work} == {"70 + x = 180", "x = 110"}
    # the deduce step highlights the angle it cites — UNCUED because it is the
    # first step: it follows the figure's last stroke instead of firing at the
    # phrase on a half-drawn figure
    hl = [a for a in scene["actions"] if a["verb"] == "highlight"]
    assert hl and "at" not in hl[0]
    draws = [i for i, a in enumerate(scene["actions"]) if a["verb"] == "draw" and a["target"].startswith("fig_")]
    assert scene["actions"].index(hl[0]) > max(draws), "the highlight comes after the figure's strokes"
    assert any(a["target"] in set(sum((targets_for(fb, m, "angle_abd") for fb, m in [_fb(ex)]), []))
               for a in hl)
    # the theorem's reason sits beside the line it gave
    notes = [e["text"] for e in els.values() if e["id"].startswith("n")]
    assert any("straight line" in n for n in notes), notes
    # the proved value is written onto the figure, replacing x, at the answer
    reveal = [e for e in els.values() if e["id"].startswith("fig_m")]
    assert reveal and reveal[0]["text"] == "110°" and reveal[0]["color"] == "accent"
    assert any(a["verb"] == "fade" and a["target"] == els[a["target"]]["id"] and els[a["target"]].get("text") == "x"
               for a in scene["actions"] if a["verb"] == "fade" and a["target"] in els), "x fades as 110° is written"
    assert [a["verb"] for a in scene["actions"]].count("underline") == 1
    _render(scene, scene["narration"])
    assert any(l.who == "teacher" and "one hundred and ten" in l.line for l in lines)


def _fb(ex):
    """The single figure's FigureBoard and model, the way the board builds them."""
    board = B._Board()
    B._problem_elements(ex, board, None)
    fig = B._figure_panel(ex, board)
    fid = next(iter(fig.boards))
    return fig.boards[fid], fig.rep.models[fid]


def test_an_evidence_example_draws_its_figures_in_a_row_at_one_scale():
    q = copy.deepcopy(ITEMS["P1"])
    q["difficulty"] = 1
    q["intro_speech"] = "Here are three triangles. Let us look at their sides."
    q["answer_speech"] = "So those are the isosceles triangles."
    q["observations"] = [{"figure": f["id"], "speech": f"Look at triangle {f.get('label') or f['id']}: compare its sides."}
                         for f in q["figures"]]
    ex = L.figure_example(_item(q))
    assert ex.has_figure and len(ex.steps) == len(q["figures"])
    assert all(s.speech.startswith("Look at triangle") for s in ex.steps), "the observations reached the steps"
    assert all(s.kind == "deduce" and s.figure_ops == [{"op": "highlight", "target": s.uses[0]}] for s in ex.steps)
    assert all(": " in s.after[0] for s in ex.steps)       # "A: scalene"
    assert ex.final_answer and ex.final_answer[0]            # the computed selection
    scene, _lines = B.example_scene(ex, MethodCard(), "s003", has_card=False)
    Scene.model_validate(scene)
    els = scene["elements"]
    labels = [e for e in els if e["id"].endswith("_lab")]
    assert [e["text"] for e in labels] == [f.get("label") or f["id"] for f in q["figures"]]
    # every figure inside the row, the observations to the right of it
    for e in els:
        if e["type"] == "shape" and e["id"][0] == "f" and "_s" in e["id"]:
            for x, y in e.get("points") or [e["center"]]:
                assert B.Q_AT[0] - 2 <= x <= B.EV_RIGHT + 2 and y <= B.WORK_BOTTOM + 2, (e["id"], x, y)
    work = [e for e in els if e["id"].startswith("w") and e["id"][1:].isdigit()]
    assert work and all(e["at"][0] == B.EV_LINE_X for e in work)
    assert [a["verb"] for a in scene["actions"]].count("highlight") >= 3
    assert not any(a["verb"] == "fade" and a.get("to") == B.DIM for a in scene["actions"]), "observations stay bright"
    _render(scene, scene["narration"])


def test_the_lesson_takes_figure_examples_first_and_renders_them(monkeypatch):
    monkeypatch.delenv("MATHS_FIGURES", raising=False)
    c = FakeClient(figures=[_b1_reply()])
    lesson, report = L.verified_lesson(c, topic="Angles on a straight line", subject="Mathematics", level="Class 7",
                                       curriculum="CBSE", language="en", episode_context="")
    assert report["status"] == "verified" and report["figures"]["verified"] == 1, report["figures"]
    assert [e.label for e in lesson.examples] == ["Example 1", "Example 2", "Example 3", "Example 4"]
    assert lesson.examples[0].has_figure and not any(e.has_figure for e in lesson.examples[1:])
    assert report["examples"][0]["status"] == "verified"
    geo = [k for k in c.calls if "geometry.figure.v1" in k["prompt"]]
    assert len(geo) == 1 and "VIDEO LESSON" in geo[0]["prompt"] and "intro_speech" in geo[0]["prompt"]
    assert geo[0]["schema"]["properties"]["questions"]["items"]["properties"].get("observations")

    script = L.generate_maths_script({"title": "Angles on a straight line", "episode_num": 1}, {"concepts": {"concepts": []}},
                                     3, FakeClient(figures=[_b1_reply()]), language="en",
                                     avatars={"teacher": "avatar_teacher", "student": "avatar_student"}, book_id="bk")
    ex1 = script.segments[2]
    assert ex1.scene["scene_type"] == "worked_example"
    assert any(e["id"].startswith("fig_") for e in ex1.scene["elements"])
    assert "seventy degrees" in ex1.text
    for s in script.segments:
        _render(s.scene, s.text)
    stored = script.maths["lesson"]["examples"][0]
    assert stored["figure"]["figures"] and stored["steps"][0]["kind"] == "deduce"


def test_the_switch_turns_the_figure_call_off(monkeypatch):
    monkeypatch.setenv("MATHS_FIGURES", "0")
    c = FakeClient(figures=[_b1_reply()])
    lesson, report = L.verified_lesson(c, topic="t", subject=None, level=None, curriculum=None, language="en",
                                       episode_context="")
    assert not any("geometry.figure.v1" in k["prompt"] for k in c.calls)
    assert len(lesson.examples) == 3 and report["figures"] == {}


def test_a_figure_example_without_speech_is_rejected_not_taught(monkeypatch):
    monkeypatch.delenv("MATHS_FIGURES", raising=False)
    silent = copy.deepcopy(ITEMS["B1"])
    silent["difficulty"] = 1
    for st in silent["steps"]:
        st.pop("speech", None)
    c = FakeClient(figures=[silent])
    lesson, report = L.verified_lesson(c, topic="t", subject=None, level=None, curriculum=None, language="en",
                                       episode_context="")
    assert report["figures"]["verified"] == 0 and "speaks its steps" in " ".join(report["figures"]["rejected"])
    assert not any(e.has_figure for e in lesson.examples)


HEXAGON = {
    "id": "h", "difficulty": 1, "figure_role": "evidence",
    "prompt": "How many lines of symmetry does a regular hexagon have?",
    "figures": [{"id": "fig_hex", "figure": {"objects": [{"id": "hex", "make": "regular_polygon", "n": 6, "side": "3"}]}}],
    "asks": {"property": "lines_of_symmetry", "over": ["fig_hex"]},
    "answer": {"kind": "number", "value": "6"},
    "intro_speech": "Here is a regular hexagon. How many mirror lines does it have?",
    "observations": [{"figure": "fig_hex", "speech": "Fold it corner to corner, then edge to edge: every fold is a mirror line."}],
    "answer_speech": "So a regular hexagon has six lines of symmetry.",
}


def test_a_single_figure_evidence_example_writes_the_value_and_draws_the_mirror_lines():
    ex = L.figure_example(_item(copy.deepcopy(HEXAGON)))
    assert len(ex.steps) == 1 and ex.steps[0].after == ["6"], "no label prefix for a single figure"
    assert ex.steps[0].figure_ops == [{"op": "highlight", "target": "fig_hex"}, {"op": "show_symmetry", "target": "fig_hex"}]
    scene, _lines = B.example_scene(ex, MethodCard(), "s003", has_card=False)
    Scene.model_validate(scene)
    axes = [e for e in scene["elements"] if e["id"].startswith("fig_sym")]
    assert len(axes) == 6 and all(e["exact"] and e["shape"] == "line" for e in axes)
    # every axis passes through the hexagon's centre
    cx = sum(p[0] for e in axes for p in e["points"]) / 12
    cy = sum(p[1] for e in axes for p in e["points"]) / 12
    for e in axes:
        (x0, y0), (x1, y1) = e["points"]
        d = abs((x1 - x0) * (cy - y0) - (y1 - y0) * (cx - x0)) / ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        assert d < 1.0, (e["id"], d)
    verbs = [a["verb"] for a in scene["actions"]]
    assert verbs.count("draw") >= 6 + 6 and "highlight" in verbs
    texts = [e.get("text") or e.get("expr") for e in scene["elements"] if e["type"] in ("text", "math")]
    assert "6" in texts and not any(t.startswith("A:") for t in texts)
    assert verbs.count("underline") == 1
    _render(scene, scene["narration"])


def test_symmetry_axes_agree_with_the_count_on_the_corpus():
    from maths.geometry.properties import lines_of_symmetry, symmetry_axes
    checked = 0
    for qid, q in ITEMS.items():
        asks = q.get("asks") or {}
        if asks.get("property") != "lines_of_symmetry":
            continue
        rep = verify_question(q)
        for fid, m in rep.models.items():
            if m.grids or m.circles:
                continue
            assert len(symmetry_axes(m)) == lines_of_symmetry(m), (qid, fid)
            checked += 1
    assert checked >= 2
