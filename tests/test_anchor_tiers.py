"""The anchor ladder, and the guarantee that it is not about biology.

Two live failures drive this file.

`mitochondria_region` (Cells kit, 2026-09-09). The annotator wrote
"mitochondrion", the plan asked for "mitochondria_region", and the anchor died
twice over: the vocabulary test used raw set membership, so the artwork was
judged not to know the word at all; and the final lookup was exact-then-
substring, and neither spelling contains the other. Every other name
comparison in the engine already folds inflections. This one did not.

`nucleus_region` on the SAME run is NOT fixed here and must not be claimed as
fixed: `organelle_city` was published with `regions: {}` — the vision
annotator returned nothing for any of the four names it was asked for. A
matcher cannot match against an empty vocabulary, and `test_an_asset_with_no_
regions_at_all_is_not_this_layers_problem` pins exactly that, so nobody later
mistakes a repair-pass failure for a matcher failure.

The non-biology fixtures are the point of the file as much as the biology
ones. Every fixture in `test_anchor_qualifiers.py` is a plant cell, so nothing
in the suite would have failed if this mechanism had been made subject-
specific. These assert the SAME properties over geography, chemistry, history
and maths.
"""

from __future__ import annotations

from spike.scene_engine.anchor_match import (
    anchor_layer_hits,
    by_token_subset,
    without_unknown_qualifiers,
)
from spike.scene_engine.vector_assets import match_layer_ids

# verbatim from visual_assets.vision.regions of the live plant-cell asset
WHITEBOARD = ["cell wall", "cell membrane", "cytoplasm", "nucleus", "nucleolus",
              "mitochondrion", "endoplasmic reticulum", "golgi apparatus",
              "ribosome", "chloroplast", "large central vacuole"]

# the same shapes, four subjects that are not biology
GEO = ["oceanic crust", "continental crust", "upper mantle", "outer core",
       "inner core"]
CHEM = ["anode", "cathode", "electrolyte", "salt bridge", "external circuit"]
HIST = ["forum", "aqueduct", "city walls", "amphitheatre"]
MATH = ["hypotenuse", "adjacent side", "opposite side", "right angle"]


class TestTheLiveInflectionFailure:
    def test_mitochondria_region_resolves_to_the_singular_the_artwork_wrote(self):
        """The exact anchor that failed in production, as a unit test."""
        assert without_unknown_qualifiers(WHITEBOARD, "mitochondria_region") \
            == ["mitochondrion"]

    def test_the_SHARED_matcher_still_refuses_the_QUALIFIED_name(self):
        """Proof the tolerance stayed on the anchor path. `match_layer_ids`
        also decides which strokes get DRAWN; loosening it there would change
        far more than where an arrow points.

        Note what the shared matcher can and cannot do, because it locates the
        bug precisely: given the BARE plural it resolves fine (it falls back
        to `resolve_part`), so inflection was never the whole story. It is the
        QUALIFIED name it cannot reach, and narrowing a qualifier is the
        anchor's own job. The old code failed because its vocabulary test
        decided "mitochondria" was a word the artwork did not know, so it
        never got as far as narrowing."""
        assert match_layer_ids(WHITEBOARD, ["mitochondria_region"]) == []
        assert match_layer_ids(WHITEBOARD, ["mitochondria"]) == ["mitochondrion"]


class TestTheSamePropertiesOutsideBiology:
    """If someone makes this mechanism subject-specific, these fail."""

    def test_a_qualifier_the_artwork_never_uses_is_dropped(self):
        assert without_unknown_qualifiers(GEO, "mantle_region") == ["upper mantle"]
        assert without_unknown_qualifiers(CHEM, "salt_bridges_area") == ["salt bridge"]
        assert without_unknown_qualifiers(MATH, "triangle_hypotenuse") == ["hypotenuse"]

    def test_word_order_and_function_words_do_not_defeat_it(self):
        assert anchor_layer_hits(HIST, "walls_of_the_city") == ["city walls"]

    def test_ambiguity_is_refused_outside_biology_too(self):
        """Two cores and two crusts: a narrowed name that names a family is
        not a part, in geography exactly as in a cell."""
        assert without_unknown_qualifiers(GEO, "core_region") == []
        assert without_unknown_qualifiers(GEO, "crusts_region") == []

    def test_a_word_the_picture_DOES_use_is_never_dropped(self):
        assert without_unknown_qualifiers(CHEM, "external_electrolyte") == []


class TestItStillRefusesToGuess:
    def test_the_three_scars_stay_unresolved(self):
        """Spelling similarity bound distinct structures to each other twice
        before; no rung here may reopen it."""
        assert anchor_layer_hits(["nucleus"], "nucleolus") == []
        assert anchor_layer_hits(["neuron"], "neutron") == []
        assert anchor_layer_hits(["mitosis"], "meiosis") == []

    def test_an_analogy_asset_is_answered_honestly(self):
        """A cell drawn as a city has no nucleus. The ladder must return
        nothing rather than invent a mapping from an organelle to a
        building — this is the live `organelle_city` case, and a leader line
        is the correct output."""
        city = ["city hall", "power plant", "factory", "road network",
                "perimeter wall"]
        assert anchor_layer_hits(city, "nucleus_region") == []
        assert anchor_layer_hits(city, "mitochondria_region") == []

    def test_an_asset_with_no_regions_at_all_is_not_this_layers_problem(self):
        """`organelle_city` shipped with `regions: {}`. No matcher can help;
        only a repair pass that re-asks vision can. Pinned so that a future
        reader does not mistake the two."""
        assert anchor_layer_hits([], "nucleus_region") == []
        assert without_unknown_qualifiers([], "nucleus_region") == []
        assert by_token_subset([], "nucleus_region") == []


class TestRungOrdering:
    def test_the_token_subset_rung_is_LAST_and_answers_none_of_the_above(self):
        """Run earlier it would answer names the qualifier rung answers
        better. If anyone reorders the ladder, this fails loudly."""
        assert by_token_subset(WHITEBOARD, "plant_central_vacuole") == []
        assert by_token_subset(WHITEBOARD, "golgi_sacs") == []
        # ...while the full ladder still returns PR #49's answers
        assert anchor_layer_hits(WHITEBOARD, "plant_central_vacuole") \
            == ["large central vacuole"]
        assert anchor_layer_hits(WHITEBOARD, "golgi_sacs") == ["golgi apparatus"]

    def test_a_subset_candidate_must_be_covered_ENTIRELY(self):
        """One direction only — candidate tokens ⊆ anchor tokens. Mere shared
        words are not enough, which is what keeps this from being the
        overlap tier partnames rejected."""
        assert by_token_subset(["cell wall", "cell membrane"], "wall") == []
        assert by_token_subset(["cell wall"], "the_cell_wall") == ["cell wall"]
