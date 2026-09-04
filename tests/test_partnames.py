"""Part-name resolution (spike/scene_engine/partnames.py).

The matcher was exact-then-substring, which is blind to separator style and to
plurals — including the Latin ones biology diagrams are full of. Measured on
the shipped matcher before this module existed:

    'mitochondria' vs 'mitochondrion' -> []
    'nuclei'       vs 'nucleus'       -> []
    'cell_wall'    vs 'cell wall'     -> []

Each of those left a label with no leader line, and (when the asset carried
regions at all) suppressed its arrow outright.
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
