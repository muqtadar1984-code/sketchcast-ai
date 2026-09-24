"""The subject profile: one object, resolved once, three switches."""

from __future__ import annotations

import pytest

from shared import subject_profile as sp


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv(sp.FEATURE_FLAG, raising=False)


@pytest.mark.parametrize("subject", ["Mathematics", "maths", "Math", "Algebra I",
                                     "Geometry", "الرياضيات", "رياضيات", "Calculus AB"])
def test_maths_subjects_are_recognised(subject):
    assert sp.is_maths_subject(subject)


@pytest.mark.parametrize("subject", ["Science", "Physics", "Aftermath studies", "", None, "History"])
def test_other_subjects_are_not(subject):
    assert not sp.is_maths_subject(subject)


def test_default_is_science_even_for_a_maths_book_while_the_flag_is_off():
    p = sp.resolve("Mathematics")
    assert p is sp.SCIENCE_PROFILE and not p.maths and p.sketch_lexicon and not p.math_speech


def test_the_flag_turns_maths_on_for_maths_subjects_only(monkeypatch):
    monkeypatch.setenv(sp.FEATURE_FLAG, "1")
    assert sp.resolve("Mathematics").worked_examples
    assert sp.resolve("Science") is sp.SCIENCE_PROFILE


def test_params_pin_one_generation_either_way(monkeypatch):
    assert sp.resolve("Science", params={"subject_profile": "maths"}).maths, "the demo lever"
    monkeypatch.setenv(sp.FEATURE_FLAG, "1")
    assert not sp.resolve("Mathematics", params={"subject_profile": "science"}).maths, "the rollback lever"
    assert sp.resolve("Mathematics", params={"subject_profile": "nonsense"}).maths, "an unknown pin is ignored"


def test_the_maths_profile_switches_exactly_the_three_things():
    p = sp.MATHS_PROFILE
    assert p.lesson_mode == sp.MODE_WORKED_EXAMPLE
    assert p.sketch_lexicon is False and p.math_speech is True
    assert p.as_dict() == {"kind": "maths", "lesson_mode": "worked_example",
                           "sketch_lexicon": False, "math_speech": True}
