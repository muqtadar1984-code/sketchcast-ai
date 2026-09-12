"""Naming the same part twice, and the slides that depend on getting it right.

Two silent failures are pinned here. Neither raised, neither failed a count,
and both were found only by rendering a deck and looking at it:

  * `spec.parts` is written as identifiers (`axis_tilt`) and `vision.regions`
    names what the model SAW (`axis tilt`). A case-fold match found none of
    `climate_impact`'s five parts, so that figure produced no slide at all
    while three sibling figures rendered one or two labels instead of five.
    The SQL said `declared=5, stored=5` throughout, because the counts were
    right and the join was never tested.

  * the rule then existed in TWO places — `Figure.located` and the renderer's
    `_boxes`. Fixing the renderer's copy left the model's copy still answering
    "no parts", which is what actually withheld the slide.
"""

from __future__ import annotations

from shared.lesson_model import (Figure, LessonModel, Section, norm_part,
                                 part_boxes)

# Verbatim from the live Weather rows.
STORED = {
    "axis tilt": [[10.0, 10.0, 60.0, 60.0]],
    "ice sheet": [[70.0, 10.0, 120.0, 60.0]],
    "orbit": [[0.0, 0.0, 400.0, 300.0]],
    "precession wobble": [[130.0, 10.0, 180.0, 60.0]],
    "sun": [[200.0, 100.0, 240.0, 140.0]],
}
DECLARED = ["axis_tilt", "ice_sheet", "orbit", "precession_wobble", "sun"]


class TestTheJoinThatWasNeverTested:
    def test_snake_case_declared_meets_spaced_stored(self):
        for part in DECLARED:
            assert part_boxes(STORED, part), part

    def test_a_plain_case_fold_really_would_have_found_nothing(self):
        """The bug, reproduced: this is what the code did before."""
        have = {k.strip().lower() for k in STORED}
        assert [p for p in DECLARED if p.strip().lower() in have] == ["orbit", "sun"]

    def test_hyphens_level_too(self):
        assert part_boxes({"cell membrane": [[0, 0, 1, 1]]}, "cell-membrane")

    def test_the_exact_name_still_wins_first(self):
        regions = {"sun": [[0, 0, 1, 1]], "sun rays": [[5, 5, 6, 6]]}
        assert part_boxes(regions, "sun") == [(0.0, 0.0, 1.0, 1.0)]

    def test_a_name_that_matches_nothing_returns_nothing(self):
        assert part_boxes(STORED, "greenhouse gas") == []

    def test_it_survives_junk(self):
        assert part_boxes(None, "sun") == []
        assert part_boxes({}, "") == []
        assert part_boxes({"sun": "not a list"}, "sun") == []
        assert part_boxes({"sun": [[1, 2]]}, "sun") == []      # too few numbers

    def test_boxes_come_back_normalised(self):
        """A box stored with its corners the wrong way round must not produce
        a negative-area region that then reads as a speck."""
        assert part_boxes({"x": [[9.0, 9.0, 1.0, 1.0]]}, "x") == [(1.0, 1.0, 9.0, 9.0)]


class TestOneRuleNotTwo:
    def test_the_model_and_the_renderer_share_the_matcher(self):
        """`annotated_figure._boxes` IS `lesson_model.part_boxes`. If someone
        re-implements either, `climate_impact` loses its slide again."""
        from agent5_slides import annotated_figure as af
        assert af._boxes is part_boxes

    def test_located_uses_it_so_the_storyboard_agrees_with_the_renderer(self):
        fig = Figure(key="milankovitch_cycle", parts=DECLARED, regions=STORED,
                     w=400, h=300)
        assert fig.located() == DECLARED


class TestWhatMayBeZoomedInTo:
    def _fig(self) -> Figure:
        return Figure(key="f", parts=DECLARED, regions=STORED, w=400, h=300)

    def test_a_part_covering_the_frame_is_not_a_zoom_target(self):
        """`orbit` spans the whole 400x300 frame, so "cropping" to it returns
        the whole picture — a slide headed `orbit` showing the entire diagram,
        which reads as the crop having silently failed."""
        fig = self._fig()
        assert fig.encloses("orbit")
        assert "orbit" not in fig.zoomable()

    def test_the_ordinary_parts_are_still_offered(self):
        assert set(self._fig().zoomable()) == {"axis_tilt", "ice_sheet",
                                               "precession_wobble", "sun"}

    def test_a_figure_with_no_measured_frame_encloses_nothing(self):
        fig = Figure(key="f", parts=DECLARED, regions=STORED, w=0, h=0)
        assert not fig.encloses("orbit")


class TestTheWordsBesideTheZoom:
    def _model(self) -> LessonModel:
        m = LessonModel(title="Cells")
        m.glossary = [("nucleus", "A large organelle holding the genetic material.")]
        m.claims = [
            ("s1", "The nucleus contains the cell's genetic material."),
            ("s2", "Eukaryotic cells possess a true nucleus."),
            ("s3", "Sunlight drives photosynthesis in the chloroplast."),
            ("s4", "The sun is the source of that light."),
        ]
        return m

    def test_the_glossary_answers_for_a_part(self):
        assert self._model().definition_of("nucleus").startswith("A large organelle")

    def test_the_lookup_levels_separators_here_too(self):
        m = self._model()
        m.glossary = [("cell membrane", "the boundary")]
        assert m.definition_of("cell_membrane") == "the boundary"

    def test_claims_are_matched_on_WHOLE_WORDS(self):
        """The regex that does this was flattened to a literal backspace by a
        shell heredoc and matched nothing at all, silently — every focus slide
        lost the claims meant to sit under its definition. A substring match
        is the other failure: "sun" would drag in every "sunlight"."""
        m = self._model()
        assert len(m.claims_about("nucleus")) == 2
        assert m.claims_about("sun") == ["The sun is the source of that light."]

    def test_it_is_capped(self):
        m = self._model()
        m.claims = [("s", f"The nucleus fact {i}.") for i in range(9)]
        assert len(m.claims_about("nucleus", limit=3)) == 3

    def test_an_unmentioned_part_yields_nothing_rather_than_something_vague(self):
        assert self._model().claims_about("golgi apparatus") == []


class TestWhenTwoFiguresAreWorthComparing:
    def _pair(self, shared: int) -> LessonModel:
        m = LessonModel(title="t")
        common = {f"p{i}": [[i, i, i + 5, i + 5]] for i in range(shared)}
        a = Figure(key="a", parts=list(common), regions=dict(common), w=100, h=100)
        b = Figure(key="b", parts=list(common) + ["extra"],
                   regions={**common, "extra": [[50, 50, 55, 55]]}, w=100, h=100)
        for f in (a, b):
            f.png = __import__("pathlib").Path(__file__)
        m.figures = {"a": a, "b": b}
        return m

    def test_three_shared_parts_earns_a_comparison(self):
        a, b, common = self._pair(3).shared_parts()
        assert a is not None and b is not None and len(common) == 3

    def test_two_does_not(self):
        """Two diagrams that both happen to contain a sun are not a contrast."""
        assert self._pair(2).shared_parts() == (None, None, [])

    def test_a_lesson_with_one_figure_has_nothing_to_compare(self):
        m = self._pair(4)
        m.figures.pop("b")
        assert m.shared_parts() == (None, None, [])
