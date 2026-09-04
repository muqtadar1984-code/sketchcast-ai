"""Part-name resolution (spike/scene_engine/partnames.py).

The matcher was exact-then-substring, which is blind to separator style and to
plurals — including the Latin ones biology diagrams are full of. Measured on
the shipped matcher before this module existed:

    'mitochondria' vs 'mitochondrion' -> []
    'nuclei'       vs 'nucleus'       -> []
    'cell_wall'    vs 'cell wall'     -> []

Each of those left a label with no leader line, and (when the asset carried
regions at all) suppressed its arrow outright.

What this module must NOT do is guess. Two "nearest by name" tiers were built
and both were measured binding distinct structures together — spelling
similarity bound nucleolus->nucleus and meiosis->mitosis; shared-token overlap
bound any two three-word names agreeing on two words. Both are gone, and the
tests below hold the door shut: an unresolvable name resolves to nothing, and
render.py draws its designed edge leader.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from spike.scene_engine.partnames import norm_part, resolve_part, same_part
from spike.scene_engine.vector_assets import match_layer_ids

CELL = ["cell wall", "cell membrane", "nucleus", "chloroplast", "cytoplasm",
        "sap vacuole", "mitochondrion"]


class TestSeparatorsAndCase:
    @pytest.mark.parametrize("want", ["cell_wall", "Cell Wall", "CELL-WALL",
                                      "cell   wall"])
    def test_separator_style_is_never_semantics(self, want):
        assert resolve_part(want, CELL) == ("cell wall", "exact")

    def test_norm_part_is_the_one_normalizer(self):
        assert norm_part("Cell_Wall ") == "cell wall"
        assert norm_part(None) == ""


class TestPlurals:
    @pytest.mark.parametrize("want,expect", [
        ("mitochondria", "mitochondrion"),
        ("nuclei", "nucleus"),
        ("chloroplasts", "chloroplast"),
        ("cell walls", "cell wall"),
    ])
    def test_english_and_latin_plurals_resolve(self, want, expect):
        key, how = resolve_part(want, CELL)
        assert (key, how) == (expect, "plural")

    def test_cilia_finds_cilium(self):
        assert resolve_part("cilia", ["cilium", "nucleus"]) == \
            ("cilium", "plural")

    def test_bacteria_finds_bacterium(self):
        assert resolve_part("bacteria", ["bacterium"]) == \
            ("bacterium", "plural")

    def test_same_part_is_the_symmetric_question(self):
        assert same_part("mitochondria", "mitochondrion")
        assert same_part("cell_wall", "Cell Wall")
        assert not same_part("ribosome", "nucleus")


class TestSubstringTierIsUnchanged:
    def test_a_qualified_name_still_contains_the_bare_one(self):
        assert resolve_part("vacuole", CELL) == ("sap vacuole", "substring")

    def test_a_literal_key_beats_its_qualified_sibling(self):
        # the rule match_layer_ids exists to protect: 'membrane' must not
        # bleed into 'nucleus membrane' when a literal 'membrane' exists
        assert resolve_part("membrane", ["membrane", "nucleus membrane"]) == \
            ("membrane", "exact")


class TestNoFalsePositives:
    def test_an_absent_part_stays_absent(self):
        assert resolve_part("ribosome", CELL) == (None, None)

    def test_a_different_word_with_the_same_initial_is_refused(self):
        # 'nucleus' vs 'nutrient': same initial, ratio well under 0.80
        assert resolve_part("nutrient", ["nucleus"]) == (None, None)

    # Every pair below was MEASURED binding under the removed character-
    # similarity tier: same initial letter, SequenceMatcher ratio >= 0.80, so
    # the tier returned a confident match and the renderer drew a full barbed
    # arrow into the wrong structure with no warning at all. They are real,
    # distinct structures a child is taught to tell apart.
    @pytest.mark.parametrize("want,other,ratio", [
        ("nucleolus", "nucleus", 0.875),
        ("chromoplast", "chloroplast", 0.818),
        ("neutron", "neuron", 0.923),
        ("meiosis", "mitosis", 0.857),
        ("proton", "photon", 0.833),
        ("stalactite", "stalagmite", 0.800),
        ("radius", "radium", 0.833),
        ("carpal", "carpel", 0.833),
    ])
    def test_a_near_spelling_is_never_the_same_structure(self, want, other,
                                                         ratio):
        import difflib
        # the pair really is that similar — this is not a straw man
        assert difflib.SequenceMatcher(None, want, other).ratio() >= ratio
        assert resolve_part(want, [other]) == (None, None)
        assert match_layer_ids([other], [want]) == []

    def test_an_unknown_region_from_another_subject_is_refused(self):
        assert resolve_part("golgi_body", CELL) == (None, None)

    def test_nothing_available_resolves_to_nothing(self):
        assert resolve_part("nucleus", []) == (None, None)
        assert resolve_part("", CELL) == (None, None)


class TestMatchLayerIdsStaysCompatible:
    """Every pair that matched before must match the same way: the new tier
    only runs once exact AND substring have both come back empty."""

    @pytest.mark.parametrize("available,want,expect", [
        (["nucleus", "wall"], ["nucleus"], ["nucleus"]),
        (["Nucleus"], ["nucleus"], ["Nucleus"]),
        (["membrane", "nucleus_membrane"], ["membrane"], ["membrane"]),
        (["sap_vacuole"], ["vacuole"], ["sap_vacuole"]),
        (["cell_wall", "nucleus"], ["cell_wall"], ["cell_wall"]),
    ])
    def test_previously_matching_pairs_are_byte_identical(self, available,
                                                          want, expect):
        assert match_layer_ids(available, want) == expect

    def test_the_new_tier_only_fires_when_the_old_ones_are_empty(self):
        assert match_layer_ids(["cell wall", "nucleus"], ["cell_wall"]) == \
            ["cell wall"]
        assert match_layer_ids(["mitochondrion"], ["mitochondria"]) == \
            ["mitochondrion"]

    def test_an_unmatchable_name_still_returns_nothing(self):
        assert match_layer_ids(["nucleus", "cell wall"], ["ribosome"]) == []


class TestThereIsNoGuessingTier:
    """The two tiers that were tried and removed, kept shut.

    A confidently wrong label is worse for a teacher than an unlabelled part:
    the unlabelled one gets a leader line to the edge and reads as a part
    nobody named, while the wrong one reads as a part somebody DID name.
    """

    def test_shared_words_in_another_order_are_not_a_match(self):
        # the shared-TOKEN tier's own showcase case. It is also the case that
        # let three-word names bind each other, so it goes with them.
        assert resolve_part("wall cell", ["cell wall", "nucleus"]) ==             (None, None)

    @pytest.mark.parametrize("want,other", [
        # two of three words shared — the class of collision the token tier
        # created, which NEITHER the shipped matcher nor its predecessor had
        ("left anterior descending", "left posterior descending"),
        ("upper left ventricle", "upper right ventricle"),
        ("outer cell membrane", "inner cell membrane"),
    ])
    def test_two_shared_words_out_of_three_are_not_a_match(self, want, other):
        wt, ot = set(want.split()), set(other.split())
        # the overlap really is 2/3 — the token tier called this 0.5 Jaccard
        # and returned it as confident
        assert len(wt & ot) == 2 and len(wt | ot) == 4
        assert resolve_part(want, [other]) == (None, None)

    def test_one_shared_word_is_not_enough_either(self):
        assert resolve_part("cell membrane", ["cell wall"]) == (None, None)
        assert resolve_part("left atrium", ["right atrium"]) == (None, None)

    def test_resolve_part_never_reports_a_fourth_tier(self):
        # nothing in the codebase may start trusting a 'nearest' verdict:
        # the only verdicts that exist are the three tiers above
        for want in ("cell_wall", "mitochondria", "vacuole", "wall cell",
                     "nucleolus", "ribosome"):
            _key, how = resolve_part(want, CELL)
            assert how in (None, "exact", "plural", "substring"), how
