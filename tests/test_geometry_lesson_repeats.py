"""A lesson never teaches the same question twice (the circle video
b7caa85b, 2026-10-15: "radius 5 cm, find the circumference" as Examples 1,
2 and 5 and the try-it, reworded each time)."""
from __future__ import annotations

from maths import lesson as L
from maths.schema import TryIt, WorkedExample


def _circle_ex(problem, radius, theorem, answer, *, given_measures=None, bind=None):
    fig = {"units": "cm", "points": [{"id": "p_o", "label": "O"}],
           "objects": [{"id": "c1", "make": "circle", "centre": "p_o", "radius": radius}],
           "measures": given_measures or []}
    if bind:
        fig["bind"] = bind
    spec = {"id": "g", "figure_role": "reasoning", "prompt": problem, "figures": [{"id": "f", "figure": fig}],
            "steps": [{"kind": "deduce", "theorem": theorem, "uses": ["c1"], "after": ["x"]}]}
    return WorkedExample(problem=problem, final_answer=[answer], figure=spec)


def test_the_same_question_in_other_words_is_one_question():
    a = _circle_ex("A circle has a radius of 5 cm. Find its circumference, taking pi as 3.14.", "5", "circumference", "C = 31.4")
    b = _circle_ex("Find the circumference of a circle with a radius of 5 cm. (Take π = 3.14)", "5", "circumference", "C = 31.4")
    c = _circle_ex("Find the circumference of the circle shown. Use pi = 3.14.", "5", "circumference", "C = 31.4")
    assert L.example_signature(a) == L.example_signature(b) == L.example_signature(c)
    kept, repeats = L.distinct_examples([a, b, c])
    assert kept == [a] and repeats == [b, c]


def test_other_numbers_another_quantity_or_the_inverse_question_are_distinct():
    a = _circle_ex("A circle has a radius of 5 cm. Find its circumference, taking pi as 3.14.", "5", "circumference", "C = 31.4")
    other_radius = _circle_ex("A circle has a radius of 7 cm. Find its circumference, taking pi as 22/7.", "7", "circumference", "C = 44")
    area = _circle_ex("A circle has a radius of 5 cm. Find its area, taking pi as 3.14.", "5", "area_circle", "A = 78.5")
    inverse = _circle_ex("The circumference of a circle is 31.4 cm. Find its radius, taking pi as 3.14.", "r", "circumference",
                         "r = 5", given_measures=[{"target": "c1", "value": "31.4", "kind": "circumference", "role": "given"}],
                         bind={"r": 5})
    sigs = {L.example_signature(x) for x in (a, other_radius, area, inverse)}
    assert len(sigs) == 4


def test_questions_with_no_numbers_are_told_apart_by_their_sentence():
    a = WorkedExample(problem="Which of these shapes are trapeziums?", figure={"asks": {"property": "polygon_name"}, "figures": []})
    b = WorkedExample(problem="Which of these triangles are isosceles?", figure={"asks": {"property": "triangle_type"}, "figures": []})
    assert L.example_signature(a) != L.example_signature(b)


def test_the_try_it_is_never_one_of_the_examples():
    taught = [_circle_ex("A circle has a radius of 5 cm. Find its circumference.", "5", "circumference", "C = 31.4")]
    spare = [_circle_ex("Find the circumference of a circle of radius 5 cm.", "5", "circumference", "C = 31.4"),
             _circle_ex("A circle has a radius of 8 cm. Find its area.", "8", "area_circle", "A = 200.96")]
    kept, repeats = L.distinct_examples(spare, taught)
    assert [ex.problem for ex in kept] == ["A circle has a radius of 8 cm. Find its area."] and len(repeats) == 1
    # a try-it reads like an example: its own answer list
    t = TryIt(problem="Find the circumference of a circle with radius 5 cm.", answer=["C = 31.4"], figure=spare[0].figure)
    assert L.example_signature(t) == L.example_signature(taught[0])


def test_digits_inside_ids_are_not_numbers():
    assert L._numbers({"id": "p_1", "radius": "5", "points": ["p_a2", "p_b"]}) == {"5"}
    assert L._numbers("pi = 22/7 and 3.14") == {"22/7", "157/50"}


def test_the_engine_is_told_what_the_lesson_already_teaches():
    from maths.geometry.items import geometry_prompt
    p = geometry_prompt(topic="Circles", level="Class 9", language="en", n=2, chapter_context="", kind="lesson",
                        avoid=["A circle has a radius of 5 cm. Find its circumference."])
    assert "ALREADY IN THIS LESSON" in p and "radius of 5 cm" in p
    assert "ALREADY IN THIS LESSON" not in geometry_prompt(topic="Circles", level="Class 9", language="en", n=2,
                                                          chapter_context="", kind="lesson")
