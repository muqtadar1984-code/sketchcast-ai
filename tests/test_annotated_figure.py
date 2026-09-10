"""The geometry that turns a region box into a label a teacher can trust.

Every number below was measured off the live `animal_cell` row (704x613, nine
declared parts, nine stored regions) — the first figure the pipeline ever
rendered with an asset attached. Three faults were found by building the slide
and looking at it, and each one pointed the reader at the wrong structure while
every count and every ratio said the slide was fine.
"""

from __future__ import annotations

from agent5_slides.annotated_figure import (_anchor_ladder, _boxes, _ellipse_x,
                                            figure_geometry, unresolved_parts,
                                            validate)

W, H = 704.0, 613.0

# Trimmed from the live vision payload — the parts each fault involved.
LIVE = {
    "cell membrane": [[12.7, 14.7, 690.6, 598.9]],
    "cytoplasm": [[39.4, 44.7, 670.9, 583.0]],
    "nucleus": [[213.3, 195.5, 425.2, 393.5]],
    "nucleolus": [[293.6, 277.7, 346.4, 330.4]],
    "mitochondrion": [[64.8, 107.9, 126.0, 195.5], [494.9, 57.0, 576.6, 136.7],
                      [492.1, 345.7, 561.1, 441.4], [374.5, 494.1, 490.0, 544.3],
                      [124.6, 476.3, 205.6, 535.8]],
    "golgi apparatus": [[444.2, 174.7, 594.2, 319.4]],
}
PARTS = ["cell membrane", "cytoplasm", "nucleus", "nucleolus",
         "mitochondrion", "Golgi apparatus"]


class TestResolvingPartsToBoxes:
    def test_the_case_fold_the_pipeline_already_lives_with(self):
        """`Golgi apparatus` is DECLARED capitalised and STORED lowercase, on
        both live Cells figures. A case-sensitive lookup drops it silently and
        the slide ships one organelle short."""
        assert _boxes(LIVE, "Golgi apparatus")
        assert _boxes(LIVE, "  golgi APPARATUS ")

    def test_a_part_the_artwork_cannot_locate_is_reported_not_guessed(self):
        assert unresolved_parts(PARTS + ["lysosome"], LIVE) == ["lysosome"]
        assert unresolved_parts(PARTS, LIVE) == []


class TestEnclosureIsAPropertyOfOneInstance:
    """The live bug. Five mitochondria scattered from (64,107) to (576,544)
    have a UNION covering 61% of the frame, so a union-based test called
    `mitochondrion` an encloser — and an encloser's leader is aimed at the
    outline of the whole picture. The label would have pointed at the cell
    membrane while reading "mitochondrion"."""

    def _by_part(self):
        return {g["part"]: g for g in figure_geometry(PARTS, LIVE, W, H)}

    def test_a_scattered_part_is_not_an_encloser(self):
        assert not self._by_part()["mitochondrion"]["encloser"]

    def test_its_union_really_would_have_said_otherwise(self):
        boxes = LIVE["mitochondrion"]
        ux0 = min(b[0] for b in boxes); uy0 = min(b[1] for b in boxes)
        ux1 = max(b[2] for b in boxes); uy1 = max(b[3] for b in boxes)
        assert (ux1 - ux0) * (uy1 - uy0) / (W * H) > 0.55      # the trap
        biggest = max((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)
        assert biggest / (W * H) < 0.55                        # the truth

    def test_the_real_enclosers_are_still_recognised(self):
        by = self._by_part()
        assert by["cell membrane"]["encloser"] and by["cytoplasm"]["encloser"]
        assert not by["nucleus"]["encloser"]


class TestLeadersCannotCross:
    """Non-crossing is a property of the solver, not a hope the validator
    checks afterwards. `wanted` arrives non-decreasing (labels are stacked down
    the gutter); the bounds are non-decreasing by construction; min and max
    preserve that. So these cases assert a guarantee, not a sample."""

    def _monotone(self, spans, wanted):
        got = _anchor_ladder(spans, wanted)
        assert all(got[i] <= got[i + 1] + 1e-9 for i in range(len(got) - 1)), got
        return got

    def test_containment_the_case_that_broke_the_greedy_version(self):
        """The nucleolus sits INSIDE the nucleus, so their spans overlap while
        font pitch forces their labels apart. Greedy pushed the nucleus's
        anchor below the nucleolus's box entirely — a 0.0007in crossing that no
        one would see and that still lied about which structure was which."""
        got = self._monotone([(195.5, 393.5), (277.7, 330.4)], [384.0, 414.0])
        assert 195.5 <= got[0] <= 393.5
        assert 277.7 <= got[1] <= 330.4

    def test_a_sprawling_part_beside_a_compact_one(self):
        """The ER spans 88..493 of a 613px frame and can reach back up past
        anything; the ladder is what stops it."""
        self._monotone([(97.5, 103.6), (237.8, 254.4), (88.9, 492.9),
                        (195.5, 393.5), (277.7, 330.4)],
                       [100.0, 250.0, 300.0, 340.0, 380.0])

    def test_labels_wanting_the_same_height(self):
        self._monotone([(10.0, 100.0), (20.0, 110.0), (30.0, 120.0)],
                       [60.0, 60.0, 60.0])

    def test_it_stays_inside_each_span_when_that_is_possible(self):
        got = self._monotone([(0.0, 50.0), (60.0, 100.0)], [500.0, 500.0])
        assert got[0] <= 50.0 and 60.0 <= got[1] <= 100.0

    def test_empty_is_empty(self):
        assert _anchor_ladder([], []) == []


class TestTheAnchorLandsOnTheShape:
    def test_a_circle_is_not_its_bounding_box(self):
        """At three-quarter height the nucleus's outline has moved ~100px in
        from the box edge. Anchoring on the box put the dot in blank cytoplasm
        between the ER and the nuclear envelope — pointing at nothing."""
        box = (213.3, 195.5, 425.2, 393.5)
        mid = _ellipse_x(box, (box[1] + box[3]) / 2, "left")
        low = _ellipse_x(box, 331.0, "left")
        assert abs(mid - box[0]) < 1.0          # widest row IS the box edge
        assert low - box[0] > 5.0               # ...and no other row is
        assert box[0] <= low <= box[2]

    def test_a_degenerate_box_does_not_divide_by_zero(self):
        assert _ellipse_x((10.0, 20.0, 30.0, 20.0), 20.0, "right") == 30.0


class TestSubjectBlindness:
    def test_renaming_every_domain_noun_changes_nothing(self):
        """The strongest guard available: a bijective rename of every part name
        must produce a structurally identical answer. This catches indirect
        hardcoding — a lookup keyed by subject, an ordering by string length —
        that reading the source would not."""
        fake_names = {p: f"q{i}z" for i, p in enumerate(LIVE)}
        fake = {fake_names[k]: v for k, v in LIVE.items()}
        fake_parts = [fake_names.get(p.lower(), fake_names.get(p, p))
                      for p in (p.lower() for p in PARTS)]

        real = figure_geometry(PARTS, LIVE, W, H)
        blind = figure_geometry(fake_parts, fake, W, H)

        assert len(real) == len(blind) == len(PARTS)
        assert [g["encloser"] for g in real] == [g["encloser"] for g in blind]
        assert [g["side"] for g in real] == [g["side"] for g in blind]
        assert [g["target"] for g in real] == [g["target"] for g in blind]


class TestSidesAreBalanced:
    def test_nine_parts_do_not_all_pile_into_one_gutter(self):
        """Not merely ugly: nine labels in one column forces a font small
        enough to be unreadable projected, with half the slide left empty."""
        geo = figure_geometry(PARTS, LIVE, W, H)
        left = sum(1 for g in geo if g["side"] == "left")
        assert abs(left - (len(geo) - left)) <= 1


class TestTheValidatorRefusesABadSlide:
    def _rep(self, labels):
        return {"picture": (100.0, 100.0, 500.0, 400.0), "labels": labels}

    def _lab(self, part, side, order, box, ay):
        return {"part": part, "side": side, "order": order,
                "label": box, "anchor": (0.0, ay)}

    def test_a_clean_slide_reports_nothing(self):
        assert validate(self._rep([
            self._lab("a", "left", 0, (0.0, 100.0, 90.0, 30.0), 120.0),
            self._lab("b", "left", 1, (0.0, 200.0, 90.0, 30.0), 220.0)])) == []

    def test_stacked_labels_are_caught(self):
        f = validate(self._rep([
            self._lab("a", "left", 0, (0.0, 100.0, 90.0, 30.0), 120.0),
            self._lab("b", "left", 1, (0.0, 110.0, 90.0, 30.0), 220.0)]))
        assert any("overlap" in x for x in f)

    def test_a_label_lying_on_the_artwork_is_caught(self):
        f = validate(self._rep([
            self._lab("a", "left", 0, (200.0, 200.0, 90.0, 30.0), 210.0)]))
        assert any("on the artwork" in x for x in f)

    def test_a_crossing_is_caught(self):
        f = validate(self._rep([
            self._lab("a", "left", 0, (0.0, 100.0, 90.0, 30.0), 300.0),
            self._lab("b", "left", 1, (0.0, 200.0, 90.0, 30.0), 120.0)]))
        assert any("cross" in x for x in f)

    def test_a_font_below_the_floor_is_caught(self):
        assert any("below" in x for x in validate(self._rep([]), label_pt=8))
