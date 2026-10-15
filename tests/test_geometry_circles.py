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
