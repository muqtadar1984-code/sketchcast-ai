"""Circles in the geometry chain (2026-10-15): the "Circumference and Area
of a Circle" kit refused every figure — the theorems wrote r_<circle> and
nothing bound it; a deduce step could not take π as 3.14; a circle's area
could not be given; a diameter had no construction key."""
from __future__ import annotations

import copy

import pytest

from maths.geometry import verify_question
from maths.geometry.items import normalise_question


def _circle_q(radius="4", measures=None, steps=None, answer="25.12", bind=None, prompt="Find the circumference."):
    fig = {"units": "cm", "points": [{"id": "p_o", "label": "O"}],
           "objects": [{"id": "c1", "make": "circle", "centre": "p_o", "radius": radius}],
           "measures": measures or []}
    if bind:
        fig["bind"] = bind
    return {"id": "q", "figure_role": "reasoning", "prompt": prompt, "figures": [{"id": "f", "figure": fig}],
            "steps": steps, "answer": {"kind": "number", "value": answer, "unit": "cm"}}


def _ok(raw):
    spec, _d = normalise_question(copy.deepcopy(raw))
    rep = verify_question(spec)
    assert rep.ok, rep.refusal
    return rep


def test_the_circumference_theorem_reads_the_constructions_radius():
    rep = _ok(_circle_q(steps=[{"kind": "deduce", "theorem": "circumference", "uses": ["c1"], "after": ["C = 2 * pi * 4"]},
                              {"kind": "transform", "after": ["C = 8 * pi"]}], answer="25.12"))
    assert str(rep.proved["C"]) == "8*pi"
    assert any("π taken as 3.14" in c.detail for c in rep.checks if c.name.startswith("answer"))


def test_a_deduce_step_may_take_pi_as_a_school_value():
    # "C = 2 * 3.14 * 4" was step_not_equivalent on every roll of the kit
    rep = _ok(_circle_q(steps=[{"kind": "deduce", "theorem": "circumference", "uses": ["c1"], "after": ["C = 2 * 3.14 * 4"]},
                              {"kind": "transform", "after": ["C = 25.12"]}], answer="25.12"))
    assert str(rep.proved["C"]) == "628/25"
    assert any("π taken as 3.14" in c.detail for c in rep.checks if c.name.startswith("step 1"))
    rep = _ok(_circle_q(radius="7", steps=[{"kind": "deduce", "theorem": "area_circle", "uses": ["c1"], "after": ["A = 22/7 * 7^2"]},
                                           {"kind": "transform", "after": ["A = 154"]}], answer="154", prompt="Find the area."))
    assert str(rep.proved["A"]) == "154"
    # a wrong value of π is still wrong
    spec, _d = normalise_question(_circle_q(steps=[{"kind": "deduce", "theorem": "circumference", "uses": ["c1"], "after": ["C = 2 * 3.1 * 4"]}],
                                            answer="24.8"))
    rep = verify_question(spec)
    assert not rep.ok and rep.refusal["code"] == "step_not_equivalent"


def test_a_given_area_or_circumference_finds_the_radius():
    q = _circle_q(radius="r", bind={"r": 5}, prompt="The area of the circle is 78.5 cm². Find r.",
                  measures=[{"target": "c1", "value": "78.5", "unit": "cm", "kind": "area", "role": "given"}],
                  steps=[{"kind": "deduce", "theorem": "area_circle", "uses": ["c1"], "after": ["78.5 = 3.14 * r^2"]},
                         {"kind": "transform", "after": ["r^2 = 25"]},
                         {"kind": "transform", "after": ["r = 5"]}], answer="5")
    rep = _ok(q)
    assert str(rep.proved["r"]) == "5"
    assert any("negative root is not a length" in c.detail for c in rep.checks)
    q = _circle_q(radius="r", bind={"r": 5}, prompt="The circumference of the circle is 31.4 cm. Find r.",
                  measures=[{"target": "c1", "value": "31.4", "unit": "cm", "kind": "circumference", "role": "given"}],
                  steps=[{"kind": "deduce", "theorem": "circumference", "uses": ["c1"], "after": ["31.4 = 2 * 3.14 * r"]},
                         {"kind": "transform", "after": ["r = 5"]}], answer="5")
    assert str(_ok(q).proved["r"]) == "5"
    # the area written as a kind-less measure of the circle is its area (as for a polygon)
    q = _circle_q(radius="r", bind={"r": 5}, prompt="The area of the circle is 78.5 cm². Find r.",
                  measures=[{"target": "c1", "value": "78.5", "unit": "cm^2"}],
                  steps=[{"kind": "deduce", "theorem": "area_circle", "uses": ["c1"], "after": ["78.5 = 3.14 * r^2"]},
                         {"kind": "transform", "after": ["r^2 = 25"]}, {"kind": "transform", "after": ["r = 5"]}], answer="5")
    assert str(_ok(q).proved["r"]) == "5"


def test_a_diameter_is_read_as_a_radius():
    raw = _circle_q(steps=[{"kind": "deduce", "theorem": "area_circle", "uses": ["c1"], "after": ["A = pi * 5^2"]}],
                    answer="78.5", prompt="A circle has a diameter of 10 cm. Find its area.")
    obj = raw["figures"][0]["figure"]["objects"][0]
    obj.pop("radius")
    obj["diameter"] = "10"
    spec, _d = normalise_question(copy.deepcopy(raw))
    assert spec["figures"][0]["figure"]["objects"][0]["radius"] == "5"
    assert "diameter" not in spec["figures"][0]["figure"]["objects"][0]
    assert str(_ok(raw).proved["A"]) == "25*pi"
    obj["diameter"] = "d"
    assert normalise_question(copy.deepcopy(raw))[0]["figures"][0]["figure"]["objects"][0]["radius"] == "(d)/2"


def test_a_lost_root_that_is_not_a_length_is_still_refused():
    # x is nothing's length: x² = 25 to x = 5 drops a root the figure does not exclude
    raw = {"id": "q", "figure_role": "reasoning", "prompt": "Find x.",
           "figures": [{"id": "f", "figure": {"objects": [{"id": "c1", "make": "circle", "centre": "p_o", "radius": "4"}],
                                               "points": [{"id": "p_o", "label": "O"}]}}],
           "steps": [{"kind": "setup", "after": ["x^2 = 25"]}, {"kind": "transform", "after": ["x = 5"]}],
           "answer": {"kind": "number", "value": "5"}}
    spec, _d = normalise_question(raw)
    rep = verify_question(spec)
    assert not rep.ok and rep.refusal["code"] in ("step_not_equivalent", "bad_schema")


def _model_circle(prompt, radius, line, measures, steps, answer, bind=None):
    """The shape the live model writes (2026-10-15): the radius or diameter
    as a line_through the centre, measured under the line's id."""
    fig = {"units": "cm", "points": [{"id": p, "label": p[-1].upper()} for p in ["p_o"] + [q for q in line["points"] if q != "p_o"]],
           "objects": [{"id": "c1", "make": "circle", "centre": "p_o", "radius": radius}, line],
           "measures": measures}
    if bind:
        fig["bind"] = bind
    return {"id": "q", "figure_role": "reasoning", "prompt": prompt, "figures": [{"id": "f", "figure": fig}],
            "steps": steps, "answer": {"kind": "number", "value": answer, "unit": "cm"}}


def test_a_radius_or_diameter_drawn_as_a_line_through_the_centre_is_read():
    # the radius as a line_through, its measure under the line's id; the construction's radius followed the number
    raw = _model_circle("A circle has a radius of 4 cm. Find its circumference.", 3,
                        {"id": "s_oa", "make": "line_through", "points": ["p_o", "p_a"]},
                        [{"target": "s_oa", "value": "4", "unit": "cm", "role": "given"}, {"target": "c1", "value": "C", "role": "unknown"}],
                        [{"kind": "deduce", "theorem": "circumference", "uses": ["c1"], "after": ["C = 2 * 3.14 * 4"]},
                         {"kind": "transform", "after": ["C = 25.12"]}], "25.12")
    spec, _d = normalise_question(copy.deepcopy(raw))
    fig = spec["figures"][0]["figure"]
    assert fig["objects"][0]["radius"] == "4"
    assert [o["make"] for o in fig["objects"]] == ["circle", "point_on_circle"]
    assert {"id": "s_oa", "points": ["p_o", "p_a"]} in fig["segments"]
    assert str(_ok(raw).proved["C"]) == "628/25"
    # a diameter as a line through three points
    raw = _model_circle("A circle has a diameter of 14 cm. Find its area using pi = 22/7.", 7,
                        {"id": "l_ab", "make": "line_through", "points": ["p_a", "p_o", "p_b"]},
                        [{"target": "l_ab", "value": "14", "unit": "cm", "role": "given"}],
                        [{"kind": "deduce", "theorem": "area_circle", "uses": ["c1"], "after": ["A = 22/7 * 7^2"]},
                         {"kind": "transform", "after": ["A = 154"]}], "154")
    spec, _d = normalise_question(copy.deepcopy(raw))
    fig = spec["figures"][0]["figure"]
    assert [o["make"] for o in fig["objects"]] == ["circle", "point_on_circle", "point_on_circle"]
    assert {"id": "l_ab", "points": ["p_a", "p_b"]} in fig["segments"]
    assert str(_ok(raw).proved["A"]) == "154"
    # the kind written in the role's slot, the radius unknown and measured on the line
    raw = _model_circle("The circumference of a circle is 31.4 cm. Find its radius.", "r",
                        {"id": "s_oa", "make": "line_through", "points": ["p_o", "p_a"]},
                        [{"target": "c1", "value": "31.4", "role": "circumference"}, {"target": "s_oa", "value": "r", "role": "unknown"}],
                        [{"kind": "deduce", "theorem": "circumference", "uses": ["c1"], "after": ["31.4 = 2 * 3.14 * r"]},
                         {"kind": "transform", "after": ["31.4 = 6.28 * r", "r = 5"]}], "5", bind={"r": 5})
    spec, _d = normalise_question(copy.deepcopy(raw))
    ms = [m for m in spec["figures"][0]["figure"]["measures"] if m["target"] == "c1"][0]
    assert ms["kind"] == "circumference" and ms["role"] == "given"
    assert str(_ok(raw).proved["r"]) == "5"


def test_a_deduce_step_that_states_the_theorems_quantity_establishes_it():
    # no measure gives the circumference; the step says "31.4 = 2 * 3.14 * r"
    q = _circle_q(radius="r", bind={"r": 5}, prompt="The circumference of a circle is 31.4 cm. Find its radius.",
                  steps=[{"kind": "deduce", "theorem": "circumference", "uses": ["c1"], "after": ["31.4 = 2 * 3.14 * r"]},
                         {"kind": "transform", "after": ["r = 5"]}], answer="5")
    rep = _ok(q)
    assert str(rep.proved["r"]) == "5"
    # a wrong number for it is still wrong
    q["steps"][0]["after"] = ["30 = 2 * 3.14 * r"]
    q["steps"][1]["after"] = ["r = 4.78"]
    spec, _d = normalise_question(copy.deepcopy(q))
    rep = verify_question(spec)
    assert not rep.ok


def test_a_chord_through_the_centre_is_a_radius_and_a_chord_as_long_as_the_diameter_is_one():
    # live trial 2026-10-15: "point 'p_o' is placed twice"; "s_ab is given as 14 but the figure has 12.12"
    raw = _model_circle("Find the circumference of a circle of radius 4 cm.", "4",
                        {"id": "s_or", "make": "chord", "points": ["p_o", "p_r"], "circle": "c1"},
                        [{"target": "s_or", "value": "4", "unit": "cm", "role": "given"}, {"target": "c1", "value": "C", "role": "unknown"}],
                        [{"kind": "deduce", "theorem": "circumference", "uses": ["c1"], "after": ["C = 2 * 3.14 * 4"]},
                         {"kind": "transform", "after": ["C = 25.12"]}], "25.12")
    spec, _d = normalise_question(copy.deepcopy(raw))
    assert [o["make"] for o in spec["figures"][0]["figure"]["objects"]] == ["circle", "point_on_circle"]
    assert str(_ok(raw).proved["C"]) == "628/25"
    raw = _model_circle("Find the area of a circle of diameter 14 cm.", "7",
                        {"id": "s_ab", "make": "chord", "points": ["p_a", "p_b"], "circle": "c1"},
                        [{"target": "s_ab", "value": "14", "unit": "cm", "role": "given"}],
                        [{"kind": "deduce", "theorem": "area_circle", "uses": ["c1"], "after": ["A = 22/7 * 7^2"]},
                         {"kind": "transform", "after": ["A = 154"]}], "154")
    spec, _d = normalise_question(copy.deepcopy(raw))
    chord = [o for o in spec["figures"][0]["figure"]["objects"] if o["make"] == "chord"][0]
    assert chord["angles"] == ["0", "180"]
    assert str(_ok(raw).proved["A"]) == "154"


def test_a_measure_of_the_centre_is_the_radius_symbol_and_a_deduce_step_may_solve_on():
    # live trial 2026-10-15: {target: p_o, value: r} ("a coordinate pair is written (x, y)");
    # the deduce step "154 = 22/7 * r^2", "r^2 = 49", "r = 7" in one step
    q = _circle_q(radius="r", bind={"r": 7}, prompt="Find the circumference of a circle whose area is 154 cm².",
                  measures=[{"target": "c1", "value": "154", "role": "area"}, {"target": "p_o", "value": "r", "role": "unknown"},
                            {"target": "c1", "value": "C", "role": "unknown"}],
                  steps=[{"kind": "deduce", "theorem": "area_circle", "uses": ["c1"], "after": ["154 = 22/7 * r^2", "r^2 = 49", "r = 7"]},
                         {"kind": "deduce", "theorem": "circumference", "uses": ["c1"], "after": ["C = 2 * 22/7 * 7"]},
                         {"kind": "transform", "after": ["C = 44"]}], answer="44")
    spec, _d = normalise_question(copy.deepcopy(q))
    assert all(m["target"] != "p_o" for m in spec["figures"][0]["figure"]["measures"])
    rep = _ok(q)
    assert str(rep.proved["C"]) == "44"
    assert any("2 line(s) of working" in c.detail for c in rep.checks if c.name.startswith("step 1"))
    # a wrong line of working inside the step is still wrong
    q["steps"][0]["after"] = ["154 = 22/7 * r^2", "r^2 = 48", "r = 7"]
    spec, _d = normalise_question(copy.deepcopy(q))
    rep = verify_question(spec)
    assert not rep.ok and rep.refusal["code"] == "step_not_equivalent" and "line 2" in rep.refusal["message"]


def test_a_radius_or_diameter_as_a_bare_segment_is_read_and_a_given_number_takes_its_kind_from_the_step():
    # live trial 2026-10-15: segments [p_o, p_r] / [p_a, p_b] with no object placing the points; the
    # circumference given as a kind-less number of the circle
    def q(prompt, radius, segment, measures, steps, answer, bind=None):
        fig = {"units": "cm", "points": [{"id": p, "label": p[-1].upper()} for p in ["p_o"] + [x for x in segment["points"] if x != "p_o"]],
               "objects": [{"id": "c1", "make": "circle", "centre": "p_o", "radius": radius}], "segments": [segment], "measures": measures}
        if bind:
            fig["bind"] = bind
        return {"id": "q", "figure_role": "reasoning", "prompt": prompt, "figures": [{"id": "f", "figure": fig}],
                "steps": steps, "answer": {"kind": "number", "value": answer, "unit": "cm"}}
    raw = q("Find the circumference of a circle of radius 4 cm.", 4, {"id": "s_or", "points": ["p_o", "p_r"]},
            [{"target": "s_or", "value": "4", "unit": "cm", "role": "given"}, {"target": "c1", "value": "C", "role": "unknown"}],
            [{"kind": "deduce", "theorem": "circumference", "uses": ["c1", "s_or"], "after": ["C = 2 * 3.14 * 4"]},
             {"kind": "transform", "after": ["C = 25.12"]}], "25.12")
    spec, _d = normalise_question(copy.deepcopy(raw))
    assert [o["make"] for o in spec["figures"][0]["figure"]["objects"]] == ["circle", "point_on_circle"]
    assert str(_ok(raw).proved["C"]) == "628/25"
    raw = q("Find the area of a circle of diameter 14 cm.", 7, {"id": "s_ab", "points": ["p_a", "p_b"]},
            [{"target": "s_ab", "value": "14", "unit": "cm", "role": "given"}],
            [{"kind": "deduce", "theorem": "area_circle", "uses": ["c1", "s_ab"], "after": ["A = 22/7 * 7^2"]},
             {"kind": "transform", "after": ["A = 154"]}], "154")
    spec, _d = normalise_question(copy.deepcopy(raw))
    assert [o["make"] for o in spec["figures"][0]["figure"]["objects"]] == ["circle", "point_on_circle", "point_on_circle"]
    assert str(_ok(raw).proved["A"]) == "154"
    raw = q("The circumference of a circle is 31.4 cm. Find its radius.", "r", {"id": "s_or", "points": ["p_o", "p_r"]},
            [{"target": "c1", "value": "31.4", "role": "given"}, {"target": "s_or", "value": "r", "role": "unknown"}],
            [{"kind": "deduce", "theorem": "circumference", "uses": ["c1", "s_or"], "after": ["31.4 = 2 * 3.14 * r"]},
             {"kind": "transform", "after": ["r = 5"]}], "5", bind={"r": "5"})
    spec, _d = normalise_question(copy.deepcopy(raw))
    ms = [m for m in spec["figures"][0]["figure"]["measures"] if m["target"] == "c1"][0]
    assert ms["kind"] == "circumference"
    assert str(_ok(raw).proved["r"]) == "5"
    raw = q("Find the circumference of a circle whose area is 154 cm².", "r", {"id": "s_or", "points": ["p_o", "p_r"]},
            [{"target": "c1", "value": "154", "role": "given"}, {"target": "s_or", "value": "r", "role": "unknown"}],
            [{"kind": "deduce", "theorem": "area_circle", "uses": ["c1", "s_or"], "after": ["154 = 22/7 * r^2"]},
             {"kind": "transform", "after": ["r = 7"]},
             {"kind": "deduce", "theorem": "circumference", "uses": ["c1", "s_or"], "after": ["C = 2 * 22/7 * 7"]},
             {"kind": "transform", "after": ["C = 44"]}], "44", bind={"r": "7"})
    spec, _d = normalise_question(copy.deepcopy(raw))
    assert [m for m in spec["figures"][0]["figure"]["measures"] if m["target"] == "c1"][0]["kind"] == "area"
    assert str(_ok(raw).proved["C"]) == "44"


def test_a_radius_or_diameter_written_as_a_measure_of_the_circle_sets_its_radius():
    # live trial on the article's context (2026-10-15): {target: c_1, value: 14, kind: diameter}
    raw = _circle_q(radius="7", prompt="Find the circumference of a circle with a diameter of 14 cm.",
                    measures=[{"target": "c1", "value": "14", "unit": "cm", "role": "given", "kind": "diameter"},
                              {"target": "c1", "value": "C", "role": "unknown"}],
                    steps=[{"kind": "deduce", "theorem": "circumference", "uses": ["c1"], "after": ["C = pi * 14"]},
                           {"kind": "transform", "after": ["C = 14 * pi"]}], answer="44")
    spec, _d = normalise_question(copy.deepcopy(raw))
    fig = spec["figures"][0]["figure"]
    assert fig["objects"][0]["radius"] == "7" and fig.get("measures", []) == []
    assert str(_ok(raw).proved["C"]) == "14*pi"
    # the radius as such a measure, the construction's radius following it
    raw = _circle_q(radius="3", prompt="Find the area of a circle with a radius of 6 cm.",
                    measures=[{"target": "c1", "value": "6", "unit": "cm", "role": "given", "kind": "radius"}],
                    steps=[{"kind": "deduce", "theorem": "area_circle", "uses": ["c1"], "after": ["A = 3.14 * 6^2"]},
                           {"kind": "transform", "after": ["A = 113.04"]}], answer="113.04")
    spec, _d = normalise_question(copy.deepcopy(raw))
    assert spec["figures"][0]["figure"]["objects"][0]["radius"] == "6"
    assert str(_ok(raw).proved["A"]) == "2826/25"


def test_an_unknown_written_with_its_kind_is_the_theorems_to_supply():
    # the circle kit's try-it round (2026-10-15): {target: c1, value: C, kind: circumference, role: unknown}
    raw = _circle_q(radius="3", prompt="A circular coin has a radius of 3 cm. Find its circumference.",
                    measures=[{"target": "c1", "value": "C", "kind": "circumference", "role": "unknown"}],
                    steps=[{"kind": "deduce", "theorem": "circumference", "uses": ["c1"], "after": ["C = 2 * 3.14 * 3"]},
                           {"kind": "transform", "after": ["C = 18.84"]}], answer="18.84")
    spec, _d = normalise_question(copy.deepcopy(raw))
    assert spec["figures"][0]["figure"].get("measures", []) == []
    assert str(_ok(raw).proved["C"]) == "471/25"
    raw = _circle_q(radius="7", prompt="A circular plate has a radius of 7 cm. Find its area.",
                    measures=[{"target": "c1", "value": "A", "kind": "area", "role": "unknown"}],
                    steps=[{"kind": "deduce", "theorem": "area_circle", "uses": ["c1"], "after": ["A = 22/7 * 7^2"]},
                           {"kind": "transform", "after": ["A = 154"]}], answer="154")
    assert str(_ok(raw).proved["A"]) == "154"


def test_a_large_circle_alone_fits_the_board_at_half_the_scale_floor():
    # the circle kit's try-it round (2026-10-15): "the figure (20.0×20.0 units) does not fit the board panel"
    from maths import board as B
    from maths import lesson as L
    from maths.geometry.items import GeometryItem
    raw = _circle_q(radius="10", prompt="Find the area of a circle with a radius of 10 cm. Take pi as 3.14.",
                    measures=[{"target": "s_or", "value": "10", "unit": "cm", "role": "given"}],
                    steps=[{"kind": "deduce", "theorem": "area_circle", "uses": ["c1"], "after": ["A = 3.14 * 10^2"],
                            "speech": "The area is pi times the radius squared: three point one four times ten squared."},
                           {"kind": "transform", "after": ["A = 314"], "speech": "That is three hundred and fourteen."}], answer="314")
    raw["figures"][0]["figure"]["points"].append({"id": "p_r", "label": "R"})
    raw["figures"][0]["figure"]["segments"] = [{"id": "s_or", "points": ["p_o", "p_r"]}]
    raw["intro_speech"] = "Here is a circle of radius ten centimetres."
    raw["answer_speech"] = "So the area is three hundred and fourteen square centimetres."
    spec, difficulty = normalise_question(copy.deepcopy(raw))
    rep = verify_question(spec)
    assert rep.ok, rep.refusal
    item = GeometryItem(spec["id"], spec["prompt"], spec["figure_role"], difficulty, spec, rep,
                        speech={"intro": raw["intro_speech"], "answer": raw["answer_speech"], "observations": {}})
    ex = L.figure_example(item, "en")
    B.check_figure_example(ex, "en")                       # drawn, not refused
    B.check_figure_try_it(L.figure_try_it(ex, "en"), "en")
