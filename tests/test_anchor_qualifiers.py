"""A label anchored to a name the artwork spells differently.

Cells kit, 2026-09-08: 14 anchors resolved to nothing, and their labels fell
back to a stacked column with leader lines — the crossed arrows and floating
"Prokaryotic Cell / Eukaryotic Cell" the founder reported.

The cause is not the image prompt and not the model. The plan and the picture
are written by two different calls, and the plan qualifies a part the
annotator named plainly. Taken from the live `visual_assets` rows:

    plan asked            artwork's regions                    matched
    plant_central_vacuole  "large central vacuole"             no
    central_vacuole        "large central vacuole"             YES
    golgi_sacs             "golgi apparatus"                   no
    golgi                  "golgi apparatus"                   YES

One extra word loses a match the rest of the name makes perfectly.
"""

from __future__ import annotations

from spike.scene_engine.render import _without_unknown_qualifiers
from spike.scene_engine.vector_assets import match_layer_ids

# The two live plant-cell assets, verbatim from visual_assets.vision.regions.
WHITEBOARD = ["cell wall", "cell membrane", "cytoplasm", "nucleus", "nucleolus",
              "mitochondrion", "endoplasmic reticulum", "golgi apparatus",
              "ribosome", "chloroplast", "large central vacuole"]
LINE_DRAWING = ["cell wall", "plant row", "chloroplasts", "vacuole column",
                "cell wall layer", "cell wall column", "chloroplasts ovals",
                "large vacuole area", "chloroplasts column"]


class TestTheQualifierTheArtworkNeverHeardOf:
    def test_the_anchors_that_failed_on_the_live_kit_now_resolve(self):
        assert _without_unknown_qualifiers(WHITEBOARD, "plant_central_vacuole") == ["large central vacuole"]
        assert _without_unknown_qualifiers(WHITEBOARD, "golgi_sacs") == ["golgi apparatus"]

    def test_the_SHARED_matcher_is_left_alone(self):
        """This is the anchor's own last resort. `match_layer_ids` also decides
        which strokes get drawn and which of an asset's layers are subset, so a
        looser reading there would change more than where an arrow points —
        it broke a review-finding test the moment it was tried."""
        assert match_layer_ids(WHITEBOARD, ["plant_central_vacuole"]) == []
        assert match_layer_ids(WHITEBOARD, ["golgi_sacs"]) == []

    def test_a_word_the_picture_DOES_use_is_never_dropped(self):
        """The bleed this must not cause, and the reason the rule is about the
        vocabulary rather than about token counts: the artwork knows "nucleus"
        AND knows "cell membrane", so narrowing `nucleus_membrane` to
        "membrane" would put a nuclear-membrane label on the cell membrane."""
        assert _without_unknown_qualifiers(WHITEBOARD, "nucleus_membrane") == []

    def test_an_ambiguous_narrowing_is_refused(self):
        """A narrowed name matching more than one region has identified a
        family, not a part. The label keeps its honest leader line."""
        assert _without_unknown_qualifiers(LINE_DRAWING, "giant_vacuole") == []

    def test_a_name_with_nothing_in_common_stays_unresolved(self):
        assert _without_unknown_qualifiers(WHITEBOARD, "flux_capacitor") == []

    def test_it_is_the_LAST_tier_and_changes_nothing_that_matched_before(self):
        """Exact, substring and resolve_part all still win first, in that
        order, on every pair that already worked."""
        assert match_layer_ids(WHITEBOARD, ["nucleus"]) == ["nucleus"]          # exact
        assert match_layer_ids(WHITEBOARD, ["chloroplast"]) == ["chloroplast"]  # exact
        assert match_layer_ids(LINE_DRAWING, ["chloroplast"]) == [
            "chloroplasts", "chloroplasts ovals", "chloroplasts column"]        # substring
        assert match_layer_ids(WHITEBOARD, ["mitochondrion_outer"]) == ["mitochondrion"]

    def test_it_never_invents_a_region_that_is_not_there(self):
        for want in ("plant_central_vacuole", "golgi_sacs", "nucleus_membrane"):
            for vocab in (WHITEBOARD, LINE_DRAWING):
                assert set(_without_unknown_qualifiers(vocab, want)) <= set(vocab), want
