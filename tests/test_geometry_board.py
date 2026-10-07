"""A verified figure becomes scene elements the video board can draw, and
a step's figure_ops become actions aimed at the right strokes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from maths.geometry import parse_question, verify_question
from maths.geometry.board_adapter import LABEL_PX, figure_board, op_actions, targets_for
from spike.scene_engine.schema import Scene

CORPUS = json.loads((Path(__file__).parent / "fixtures" / "geometry_corpus_v1.json").read_text(encoding="utf-8"))
ITEMS = {item["question"]["id"]: item for item in CORPUS["items"]}
PANEL = (60.0, 118.0, 470.0, 452.0)


def _figure(qid: str):
    rep = verify_question(ITEMS[qid]["question"])
    assert rep.ok, rep.refusal
    q = parse_question(ITEMS[qid]["question"])
    ref = q.figures[0]
    return rep.models[ref.id], ref.figure, q


def _scene(fb, extra_elements=(), extra_actions=()):
    return Scene.model_validate({"id": "t", "scene_type": "worked_example", "narration": "n",
                                 "elements": fb.elements + list(extra_elements),
                                 "actions": fb.actions + list(extra_actions)})


def test_a_straight_line_figure_draws_inside_the_panel():
    m, fig, _q = _figure("B1")
    fb = figure_board(m, fig, panel=PANEL, cue={"phrase": "straight line"})
    scene = _scene(fb)
    kinds = [e.type for e in scene.elements]
    assert kinds.count("shape") >= 2 and kinds.count("text") >= 6   # A B C D, 70°, x
    # every stroke and label lies inside the panel
    for e in fb.elements:
        pts = e.get("points") or ([e["at"]] if "at" in e else [e["center"]])
        for x, y in pts:
            assert PANEL[0] - 2 <= x <= PANEL[2] + 2 and PANEL[1] - 2 <= y <= PANEL[3] + 2, (e["id"], x, y)
    # every stroke is drawn EXACTLY — no hand wobble on a geometric figure
    assert all(e.get("exact") is True for e in fb.elements if e["type"] == "shape")
    # the first stroke is cued to the narration; labels are pinned text
    assert fb.actions[0]["verb"] == "draw" and fb.actions[0]["at"] == {"phrase": "straight line"}
    assert all(e.get("fixed") for e in fb.elements if e["type"] == "text")
    # the label text keeps its board size
    sizes = {e["size"] for e in fb.elements if e["type"] == "text"}
    assert all(abs(s - LABEL_PX) < 2.0 for s in sizes), sizes


def test_figure_ops_aim_at_the_strokes_they_cite():
    m, fig, _q = _figure("B1")
    fb = figure_board(m, fig, panel=PANEL)
    arc_ids = targets_for(fb, m, "angle_abd")
    assert arc_ids and all(any(e["id"] == i for e in fb.elements) for i in arc_ids)
    els, acts = op_actions(fb, m, {"op": "highlight", "target": "angle_abd"}, cue={"phrase": "seventy"})
    assert not els and acts[0] == {"verb": "highlight", "target": arc_ids[0], "at": {"phrase": "seventy"}}
    # the line ABC is a highlightable target too
    assert targets_for(fb, m, "l_abc")
    # revealing the found measure writes it where the unknown's label was, fading "x"
    els, acts = op_actions(fb, m, {"op": "reveal_measure", "target": "angle_dbc", "value": "110"})
    assert els and els[0]["text"] == "110°" and els[0]["color"] == "accent"
    assert [a["verb"] for a in acts] == ["fade", "write"]
    _scene(fb, els, acts)   # still a valid scene


def test_a_hidden_construction_line_waits_for_its_step():
    m, fig, _q = _figure("B8")
    fb = figure_board(m, fig, panel=PANEL)
    assert "aux" in fb.hidden and fb.hidden["aux"]
    drawn_first = {a["target"] for a in fb.actions if a["verb"] == "draw"}
    assert not (set(fb.hidden["aux"]) & drawn_first), "the auxiliary line is not drawn with the figure"
    els, acts = op_actions(fb, m, {"op": "reveal_object", "target": "aux"})
    assert [a["target"] for a in acts] == fb.hidden["aux"]
    _scene(fb, els, acts)


def test_an_evidence_triangle_set_draws_each_figure():
    rep = verify_question(ITEMS["P1"]["question"])
    q = parse_question(ITEMS["P1"]["question"])
    for i, ref in enumerate(q.figures):
        fb = figure_board(rep.models[ref.id], ref.figure, panel=PANEL, prefix=f"f{i}")
        assert fb.elements and fb.actions
        _scene(fb)


def test_a_grid_pattern_is_refused_on_the_board():
    from maths.geometry.errors import GeometryRefusal
    rep = verify_question(ITEMS["P8"]["question"])
    q = parse_question(ITEMS["P8"]["question"])
    with pytest.raises(GeometryRefusal) as err:
        figure_board(rep.models[q.figures[0].id], q.figures[0].figure, panel=PANEL)
    assert err.value.code == "unsupported_feature"


def test_an_exact_stroke_binds_on_the_authors_line_while_a_plain_one_is_roughened():
    """The renderer's hand wobble is skipped for an `exact` shape: every
    bound point lies on the line the adapter gave; the same points without
    the flag come back wobbled (the board's own hand style)."""
    import math

    from spike.scene_engine.director import parse_scene_response
    from spike.scene_engine.render import SceneRenderer

    m, fig, _q = _figure("B1")
    fb = figure_board(m, fig, panel=PANEL)
    line = next(e for e in fb.elements if e["type"] == "shape" and len(e["points"]) == 2)
    plain = {**line, "id": "plain", "exact": False}
    scene = {"id": "t", "scene_type": "worked_example", "narration": "n",
             "elements": fb.elements + [plain], "actions": fb.actions + [{"verb": "draw", "target": "plain"}]}
    r = SceneRenderer(parse_scene_response(scene, "n"))
    r.compile(10.0)
    (x0, y0), (x1, y1) = line["points"]
    length = math.hypot(x1 - x0, y1 - y0)

    def off_line(eid: str) -> float:
        pts = r.bound[eid].layers[0].strokes[0].pts
        return max(abs((x1 - x0) * (y - y0) - (y1 - y0) * (x - x0)) / length for x, y in pts)

    assert off_line(line["id"]) < 1e-6
    assert off_line("plain") > 0.5
