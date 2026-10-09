"""The v3 corpus — 3D solids and nets (geometry-3d-v3-proposal.md §6,
founder decisions 2026-10-09: all eight solids, isometric, the eleven cube
nets with folding as the check). Golden tests: each item verifies, proves
what the proposal says, draws, and refuses what it must."""

from __future__ import annotations

import copy

import pytest

from maths.geometry import parse_question, verify_question
from maths.geometry.nets import LAYOUTS, fold, folds_to_cube
from maths.geometry.realise import realise
from maths.geometry.render_static import render_figure

V3 = "geometry.figure.v3"


def _pts(*labels):
    return [{"id": f"p_{lab.lower()}", "label": lab} for lab in labels]


def _seg(a, b):
    return {"id": f"s_{a.lower()}{b.lower()}", "points": [f"p_{a.lower()}", f"p_{b.lower()}"]}


def _given(a, b, value, unit="cm"):
    return {"target": f"s_{a.lower()}{b.lower()}", "value": value, "unit": unit, "role": "given"}


def _reasoning(qid, prompt, objects, segments, measures, steps, answer, points, bind=None):
    fig = {"units": "cm", "points": points, "objects": objects, "segments": segments, "measures": measures}
    if bind:
        fig["bind"] = bind
    return {"schema_version": V3, "id": qid, "figure_role": "reasoning", "prompt": prompt,
            "figures": [{"id": "f", "figure": fig}], "steps": steps, "answer": answer}


CUBOID = [{"make": "cuboid", "id": "s", "length": "6", "width": "4", "height": "3"}]
BOX_PTS = _pts("A", "B", "C", "D", "E", "F", "G", "H")

D1 = {"schema_version": V3, "id": "D1", "figure_role": "evidence", "prompt": "How many faces, edges and vertices does the cuboid have?",
      "figures": [{"id": "f", "figure": {"points": BOX_PTS, "objects": CUBOID}}],
      "parts": [{"asks": {"property": "faces", "over": ["f"]}, "answer": {"kind": "number", "value": "6"}},
                {"asks": {"property": "edges", "over": ["f"]}, "answer": {"kind": "number", "value": "12"}},
                {"asks": {"property": "vertices", "over": ["f"]}, "answer": {"kind": "number", "value": "8"}}]}

D2 = {"schema_version": V3, "id": "D2", "figure_role": "evidence", "prompt": "Which of these nets fold into a cube?",
      "figures": [{"id": "fig_a", "label": "A", "figure": {"objects": [{"make": "net", "id": "n", "solid": "cube", "layout": "cross"}]}},
                  {"id": "fig_b", "label": "B", "figure": {"objects": [{"make": "net", "id": "n", "solid": "cube",
                                                                         "cells": [[0, 0], [0, 1], [0, 2], [1, 0], [1, 1], [1, 2]]}]}},
                  {"id": "fig_c", "label": "C", "figure": {"objects": [{"make": "net", "id": "n", "solid": "cube",
                                                                         "cells": [[0, 1], [1, 1], [2, 1], [2, 0], [3, 0], [3, 2]]}]}},
                  {"id": "fig_d", "label": "D", "figure": {"objects": [{"make": "net", "id": "n", "solid": "cube",
                                                                         "cells": [[0, 0], [1, 0], [1, 1], [2, 1], [2, 2], [3, 2]]}]}}],
      "asks": {"property": "folds_to_cube", "over": ["fig_a", "fig_b", "fig_c", "fig_d"], "select": True},
      "answer": {"kind": "label_set", "value": ["A", "D"]}}

D3 = {"schema_version": V3, "id": "D3", "figure_role": "evidence", "prompt": "Name the solid.",
      "figures": [{"id": "f", "figure": {"points": _pts("A", "B", "C", "D", "E", "F"),
                                         "objects": [{"make": "prism", "id": "s", "base": "4", "base_height": "3", "length": "7"}]}}],
      "asks": {"property": "solid_name", "over": ["f"]},
      "answer": {"kind": "label_set", "value": ["triangular prism"]}}

D4 = {"schema_version": V3, "id": "D4", "figure_role": "evidence", "prompt": "The net folds into a cube. Which face is opposite the shaded face?",
      "figures": [{"id": "f", "figure": {"objects": [{"make": "net", "id": "n", "solid": "cube", "layout": "cross", "shaded": "1"}]}}],
      "asks": {"property": "opposite_face", "over": ["f"]},
      "answer": {"kind": "label_set", "value": ["6"]}}

D5 = _reasoning("D5", "Find the volume of the cuboid.", CUBOID, [_seg("A", "B"), _seg("B", "C"), _seg("A", "E")],
                [_given("A", "B", "6"), _given("B", "C", "4"), _given("A", "E", "3")],
                [{"kind": "deduce", "theorem": "volume_cuboid", "uses": ["s"], "after": ["V = 6 * 4 * 3"]},
                 {"kind": "transform", "after": ["V = 72"]}],
                {"kind": "number", "value": "72", "unit": "cm"}, BOX_PTS)

D6 = _reasoning("D6", "Find the surface area of the cuboid.", CUBOID, [_seg("A", "B"), _seg("B", "C"), _seg("A", "E")],
                [_given("A", "B", "6"), _given("B", "C", "4"), _given("A", "E", "3")],
                [{"kind": "deduce", "theorem": "surface_area_cuboid", "uses": ["s"], "after": ["S = 2 * (6 * 4 + 4 * 3 + 6 * 3)"]},
                 {"kind": "transform", "after": ["S = 108"]}],
                {"kind": "number", "value": "108", "unit": "cm"}, BOX_PTS)

D7 = _reasoning("D7", "Find the volume of the cylinder. Leave your answer in terms of pi.",
                [{"make": "cylinder", "id": "s", "radius": "3", "height": "8"}], [_seg("P", "R"), _seg("S", "U")],
                [_given("P", "R", "3"), _given("S", "U", "8")],
                [{"kind": "deduce", "theorem": "volume_cylinder", "uses": ["s"], "after": ["V = pi * 3^2 * 8"]},
                 {"kind": "transform", "after": ["V = 72 * pi"]}],
                {"kind": "number", "value": "72*pi", "unit": "cm"}, _pts("O", "P"))

D8 = _reasoning("D8", "The volume of the cuboid is 72 cm³. Find its height h.",
                [{"make": "cuboid", "id": "s", "length": "6", "width": "4", "height": "h"}],
                [_seg("A", "B"), _seg("B", "C"), _seg("A", "E")],
                [_given("A", "B", "6"), _given("B", "C", "4"), {"target": "s_ae", "value": "h", "unit": "cm", "role": "unknown"},
                 {"target": "s", "value": "72", "unit": "cm", "kind": "volume"}],
                [{"kind": "deduce", "theorem": "volume_cuboid", "uses": ["s"], "after": ["72 = 6 * 4 * h"]},
                 {"kind": "transform", "after": ["h = 3"]}],
                {"kind": "number", "value": "3", "unit": "cm"}, BOX_PTS, bind={"h": 3})

D9 = _reasoning("D9", "Find the volume of the triangular prism.",
                [{"make": "prism", "id": "s", "base": "4", "base_height": "3", "length": "7"}],
                [_seg("A", "B"), _seg("A", "C"), _seg("A", "D")],
                [_given("A", "B", "4"), _given("A", "C", "3"), _given("A", "D", "7")],
                [{"kind": "deduce", "theorem": "volume_prism", "uses": ["s"], "after": ["V = (4 * 3 / 2) * 7"]},
                 {"kind": "transform", "after": ["V = 42"]}],
                {"kind": "number", "value": "42", "unit": "cm"}, _pts("A", "B", "C", "D", "E", "F"))

D10 = _reasoning("D10", "A cuboid has 6 faces and 8 vertices. Use Euler's formula to find how many edges it has.",
                 CUBOID, [], [],
                 [{"kind": "deduce", "theorem": "euler_solids", "uses": ["s"], "after": ["F = 6", "N = 8", "F + N - E = 2"]},
                  {"kind": "transform", "after": ["6 + 8 - E = 2"]},
                  {"kind": "transform", "after": ["E = 12"]}],
                 {"kind": "number", "value": "12"}, BOX_PTS)

D11 = _reasoning("D11", "Find the volume of the square-based pyramid.",
                 [{"make": "pyramid", "id": "s", "base_side": "6", "height": "4"}], [_seg("A", "B"), _seg("O", "T")],
                 [_given("A", "B", "6"), _given("O", "T", "4")],
                 [{"kind": "deduce", "theorem": "volume_pyramid", "uses": ["s"], "after": ["V = 6^2 * 4 / 3"]},
                  {"kind": "transform", "after": ["V = 48"]}],
                 {"kind": "number", "value": "48", "unit": "cm"}, _pts("A", "B", "C", "D", "T", "O"))

D12 = _reasoning("D12", "Find the volume of the cone, in terms of pi.",
                 [{"make": "cone", "id": "s", "radius": "3", "height": "4"}], [_seg("O", "R"), _seg("O", "T")],
                 [_given("O", "R", "3"), _given("O", "T", "4")],
                 [{"kind": "deduce", "theorem": "volume_cone", "uses": ["s"], "after": ["V = pi * 3^2 * 4 / 3"]},
                  {"kind": "transform", "after": ["V = 12 * pi"]}],
                 {"kind": "number", "value": "12*pi", "unit": "cm"}, _pts("O", "T"))

D13 = _reasoning("D13", "Find the volume of the sphere, in terms of pi.",
                 [{"make": "sphere", "id": "s", "radius": "3"}], [_seg("O", "R")], [_given("O", "R", "3")],
                 [{"kind": "deduce", "theorem": "volume_sphere", "uses": ["s"], "after": ["V = 4 * pi * 3^3 / 3"]},
                  {"kind": "transform", "after": ["V = 36 * pi"]}],
                 {"kind": "number", "value": "36*pi", "unit": "cm"}, _pts("O"))

CORPUS = {q["id"]: q for q in (D1, D2, D3, D4, D5, D6, D7, D8, D9, D10, D11, D12, D13)}
EXPECT = {"D5": {"V": "72"}, "D6": {"S": "108"}, "D7": {"V": "72*pi"}, "D8": {"h": "3"}, "D9": {"V": "42"},
          "D10": {"E": "12"}, "D11": {"V": "48"}, "D12": {"V": "12*pi"}, "D13": {"V": "36*pi"}}


@pytest.mark.parametrize("qid", sorted(CORPUS))
def test_the_v3_corpus_verifies(qid):
    rep = verify_question(copy.deepcopy(CORPUS[qid]))
    assert rep.ok, (qid, rep.refusal)
    if qid in EXPECT:
        assert {k: str(v) for k, v in rep.proved.items()} == EXPECT[qid], qid


@pytest.mark.parametrize("qid", sorted(CORPUS))
def test_every_v3_figure_draws_as_its_projection_under_every_policy(qid):
    q = parse_question(copy.deepcopy(CORPUS[qid]))
    rep = verify_question(copy.deepcopy(CORPUS[qid]))
    for ref in q.figures:
        metric = rep.models[ref.id]
        assert realise(ref.figure, "assessment_schematic", metric=metric) is metric
        r = render_figure(metric, ref.figure, role=q.figure_role, policy="assessment_schematic")
        assert len(r.png) > 2000 and r.svg.startswith("<svg")
        if metric.solids:
            assert "Not drawn to scale" in r.svg or "scale" in r.svg.lower()


def test_the_cuboids_hidden_edges_are_the_three_at_the_far_corner():
    rep = verify_question(copy.deepcopy(D5))
    sd = rep.models["f"].solids["s"]
    assert {(a, b) for a, b, h in sd.edges if h} == {("p_b", "p_c"), ("p_c", "p_d"), ("p_c", "p_g")}
    assert len(sd.edges) == 12 and sd.counts == (6, 12, 8) and sd.name == "cuboid"
    # every axis-aligned edge keeps its true length on the page
    m = rep.models["f"]
    assert abs(m.length_float(frozenset(("p_a", "p_b"))) - 6) < 1e-9
    assert abs(m.length_float(frozenset(("p_a", "p_e"))) - 3) < 1e-9


def test_exactly_the_eleven_layouts_fold_and_the_block_does_not():
    assert len(LAYOUTS) == 11 and all(folds_to_cube(c) for c in LAYOUTS.values())
    assert not folds_to_cube([(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)])          # a 2 x 3 block
    assert not folds_to_cube([(0, 0), (0, 1), (0, 2), (0, 3), (0, 4), (0, 5)])          # a row of six
    assert fold([(0, 0), (0, 1), (0, 2), (1, 5), (1, 6), (1, 7)]) is None               # not connected
    # an improvised layout the list does not name is checked by folding, never refused for its name
    rep = verify_question(copy.deepcopy(D2))
    assert rep.ok and rep.models["fig_d"].nets["n"].folds and not rep.models["fig_b"].nets["n"].folds
    assert rep.models["fig_c"].nets["n"].layout == "custom"


def test_the_cross_nets_opposite_faces_and_a_shaded_cell():
    rep = verify_question(copy.deepcopy(D4))
    assert rep.ok, rep.refusal
    nt = rep.models["f"].nets["n"]
    assert nt.opposite == {"1": "6", "6": "1", "2": "4", "4": "2", "3": "5", "5": "3"}
    q = copy.deepcopy(D4)
    q["answer"]["value"] = ["3"]
    assert verify_question(q).refusal["code"] == "answer_mismatch"
    q = copy.deepcopy(D4)
    del q["figures"][0]["figure"]["objects"][0]["shaded"]
    assert verify_question(q).refusal["code"] == "bad_schema"


def test_refusals_a_solid_is_counted_not_measured_and_never_rotated():
    q = copy.deepcopy(D1)
    q["parts"] = [{"asks": {"property": "polygon_name", "over": ["f"]}, "answer": {"kind": "label_set", "value": ["cuboid"]}}]
    assert verify_question(q).refusal["code"] == "not_discernible"
    q = copy.deepcopy(D5)
    q["figures"][0]["figure"]["orientation"] = 30
    assert verify_question(q).refusal["code"] == "bad_schema"
    q = copy.deepcopy(D5)
    q["figures"][0]["figure"]["measures"][0]["value"] = "5"            # AB given 5 but built 6
    assert verify_question(q).refusal["code"] == "given_not_realised"
    q = copy.deepcopy(D5)
    q["steps"][0]["theorem"] = "volume_cylinder"
    assert verify_question(q).refusal["code"] == "theorem_premise"
    q = copy.deepcopy(D7)
    q["steps"][0]["theorem"] = "euler_solids"
    q["steps"][0]["after"] = ["F = 3"]
    assert verify_question(q).refusal["code"] == "theorem_premise"
    q = copy.deepcopy(D5)
    q["steps"][1]["after"] = ["V = 70"]
    assert verify_question(q).refusal["code"] in ("step_not_equivalent", "answer_mismatch")
    q = copy.deepcopy(D3)
    q["figures"][0]["figure"]["objects"][0]["shape"] = "pentagon"
    assert verify_question(q).refusal["code"] == "unsupported_feature"


def test_a_v2_record_is_still_a_valid_record():
    from tests.test_geometry_corpus_v2 import C3
    q = copy.deepcopy(C3)
    assert verify_question(q).ok


def test_the_reasons_of_the_solid_theorems_exist_in_every_language():
    from maths.geometry.theorems import REASONS, THEOREMS
    from maths.i18n import LANGS, REASONS as TABLE
    for t in ("volume_cuboid", "volume_prism", "volume_cylinder", "volume_pyramid", "volume_cone", "volume_sphere",
              "surface_area_cuboid", "surface_area_cylinder", "euler_solids"):
        assert t in THEOREMS and t in REASONS and set(TABLE[t]) == set(LANGS), t


def test_the_prompt_card_teaches_solids_and_the_key_prints_cubic_units():
    from maths.geometry.items import GeometryItem, geometry_prompt, key_lines
    text = geometry_prompt(topic="Volume of cuboids", level="Grade 7", language="en", n=2, chapter_context="", kind="worksheet")
    assert "cuboid: id, length" in text and "3D SOLIDS AND NETS" in text and "net: id, solid" in text
    rep = verify_question(copy.deepcopy(D5))
    item = GeometryItem("D5", D5["prompt"], "reasoning", 2, D5, rep)
    assert key_lines(item)[-1] == "Answer: V = 72 cm³"


def test_a_solid_reaches_the_board_without_text_overlaps():
    from maths import board as B
    from maths import lesson as L
    from maths.geometry.items import GeometryItem
    from maths.schema import MethodCard
    from spike.scene_engine.director import parse_scene_response
    from spike.scene_engine.render import SceneRenderer
    from spike.scene_engine.schema import Scene

    for src in (D5, D7, D11):
        q = copy.deepcopy(src)
        for st in q["steps"]:
            st["speech"] = "Multiply the three lengths together."
        rep = verify_question(copy.deepcopy(q))
        item = GeometryItem(q["id"], q["prompt"], "reasoning", 2, q, rep,
                            speech={"intro": "Here is the solid, with its lengths marked.", "answer": "That is the volume.",
                                    "observations": {}})
        ex = L.figure_example(item)
        scene, _ = B.example_scene(ex, MethodCard(), "s003", has_card=False)
        Scene.model_validate(scene)
        dashed = [e for e in scene["elements"] if e["type"] == "shape" and e.get("color") == "muted"]
        assert dashed, f"{q['id']}: the hidden edges are on the board, muted"
        sc = parse_scene_response(scene, scene["narration"])
        r = SceneRenderer(sc)
        r.compile(20.0)
        warnings = [w for w in r.audit()["warnings"] if w.startswith("TEXT_OVERLAP")]
        assert not warnings, (q["id"], warnings)
