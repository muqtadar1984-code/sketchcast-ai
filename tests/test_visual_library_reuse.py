"""Library reuse has to deliver the RIGHT picture, to the right path.

Two bugs made the reuse layer worth less than nothing on 2026-09-04, and a
catalogue of ~500 pre-generated diagrams is being built on top of it:

1. TWO canonical_key functions. raster_assets folded away "cell"/"figure";
   visual_library did not. So hydrate() downloaded a hit into
   cache/cell_ciliated/ while the renderer only reads cache/ciliated/. Every
   cell-key library hit landed where nobody looks and the picture was paid for
   again — red_blood_cell was HIT at 17:52:10, GENERATED at 17:52:22 and
   rejected as a duplicate on publish, adding calls to the 429 burst that cost
   the lesson two blank boards.

2. NO key guard. Scoring is a bag of tokens over key + description, so
   "ciliated_cell" scored 1.23 against red_blood_cell, cleared the 0.58
   threshold and was served: the neurone board in that lesson shows a red
   blood cell, and the "boat" sketch is an ant. A wrong picture is not a
   smaller version of a missing one.

No Supabase, no model call: every test runs on a local index.
"""

from __future__ import annotations

import pytest

import shared.visual_library as vl
from shared.asset_keys import canonical_key as shared_key
from spike.scene_engine.raster_assets import canonical_key as renderer_key


TRICKY_KEYS = [
    "ciliated_cell", "red_blood_cell", "plant_cell", "animal_cell",
    "root_hair_cell", "palisade_cell", "neurone",
    "specialised_animal_cells_table", "sk_bacteria", "sk_boat",
    "Ciliated Cells Diagram", "human heart illustration",
    "plant_cell_outline", "volcano_cross_section", "avatar_female_teacher",
    "cell_diagram", "figure_3_2", "",
]


class TestOneCanonicalKey:
    @pytest.mark.parametrize("key", TRICKY_KEYS)
    def test_both_modules_agree(self, key):
        assert vl.canonical_key(key) == renderer_key(key), key
        assert vl.canonical_key(key) == shared_key(key), key

    def test_they_are_literally_the_same_function(self):
        """Two copies drifted once; a shared import cannot drift again."""
        assert vl.canonical_key is renderer_key is shared_key

    def test_the_cell_keys_that_broke_hydration_now_fold_the_same(self):
        for key in ("ciliated_cell", "red_blood_cell", "root_hair_cell",
                    "palisade_cell", "specialised_animal_cells_table"):
            assert "cell" not in vl.canonical_key(key).split("_"), key
            assert vl.canonical_key(key) == renderer_key(key), key

    def test_a_hydrated_file_lands_where_the_renderer_reads(self, tmp_path):
        """The whole point: one path, computed by one function."""
        from spike.scene_engine import raster_assets as ra
        requested = "Ciliated Cells Diagram"
        assert (tmp_path / vl.canonical_key(requested)) == \
               (tmp_path / ra.canonical_key(requested))

    def test_avatar_keys_fold_as_they_always_did(self):
        """Stored canonical_key values are read for the avatar exact match;
        the roster must keep resolving to the same string."""
        for key in ("avatar_teacher", "avatar_female_teacher",
                    "avatar_student_11_12_f"):
            assert vl.canonical_key(key) == "_".join(
                sorted(set(key.lower().split("_")))), key

    def test_presentation_noise_still_folds(self):
        assert vl.canonical_key("Human Heart Diagram") == \
               vl.canonical_key("heart human illustration")

    def test_a_key_made_only_of_noise_still_has_an_identity(self):
        assert vl.canonical_key("cell_diagram") == "cell_diagram"
        assert vl.canonical_key("") == "asset"


def _library(monkeypatch, tmp_path, *rows):
    monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "idx")
    monkeypatch.setattr(vl, "_sb", lambda: None)
    monkeypatch.delenv("VISUAL_LIBRARY_MIN_SCORE", raising=False)
    for row in rows:
        vl.register_local(row)
    return vl


_RED_BLOOD_CELL = {
    "asset_key": "red_blood_cell",
    "canonical_key": "blood_red",
    "description": ("A simple educational diagram of a red blood cell, a "
                    "biconcave disc shaped cell that carries oxygen in the "
                    "blood. Name the layer groups exactly: membrane, "
                    "cytoplasm, disc."),
    "subject": "biology", "grade": "k12", "curriculum": "generic",
    "topic": "red blood cell", "concepts": ["cell", "blood"],
    "status": "approved", "asset_type": "visual",
    "local_cache_path": "/tmp/rbc.png",
}

_SK_ANT = {
    "asset_key": "sk_ant", "canonical_key": "ant_sk",
    "description": "A simple whiteboard sketch of an ant",
    "subject": "biology", "grade": "k12", "curriculum": "generic",
    "topic": "ant", "concepts": [], "status": "approved",
    "asset_type": "visual", "local_cache_path": "/tmp/ant.png",
}


class TestAWrongPictureIsNeverServed:
    def test_ciliated_cell_can_never_match_red_blood_cell(
            self, tmp_path, monkeypatch):
        _library(monkeypatch, tmp_path, _RED_BLOOD_CELL)
        prompt = ("An educational diagram of a ciliated cell, a cell in the "
                  "airway lined with hair-like cilia. Name the layer groups "
                  "exactly: cilia, nucleus, membrane.")
        assert vl.find("ciliated_cell", prompt) is None
        # …and not merely because the threshold happened to save us
        assert vl.find("ciliated_cell", prompt, min_score=0.0) is None
        assert vl.key_guard_ok("ciliated_cell", _RED_BLOOD_CELL) is False

    def test_neurone_can_never_match_red_blood_cell_either(
            self, tmp_path, monkeypatch):
        """The board that shipped the wrong picture."""
        _library(monkeypatch, tmp_path, _RED_BLOOD_CELL)
        assert vl.find("neurone", "A neurone with its cell body, dendrites "
                                  "and a long axon", min_score=0.0) is None

    def test_a_boat_can_never_match_an_ant(self, tmp_path, monkeypatch):
        """Every sk_* key shared the 'sk' token, so the sketch namespace
        matched itself: sk_boat scored 0.90 against sk_ant."""
        _library(monkeypatch, tmp_path, _SK_ANT)
        assert vl.find("sk_boat", "A simple whiteboard sketch of a boat",
                       min_score=0.0) is None
        assert vl.key_guard_ok("sk_boat", _SK_ANT) is False

    def test_the_near_miss_is_still_recorded_as_evidence(
            self, tmp_path, monkeypatch):
        """find() must refuse it; best_match() must still see it, or the
        question 'is 0.58 the right threshold' becomes unanswerable."""
        _library(monkeypatch, tmp_path, _RED_BLOOD_CELL)
        prompt = "An educational diagram of a ciliated cell with cilia"
        row, score, source = vl.best_match("ciliated_cell", prompt)
        assert row is not None and row["asset_key"] == "red_blood_cell"
        assert score > 0 and source == "local"
        assert vl.find("ciliated_cell", prompt) is None

    def test_the_boilerplate_tail_is_not_scored(self, tmp_path, monkeypatch):
        """'Name the layer groups exactly: …' addresses the vision annotator
        and is the same shape on every prompt; scored, it lifts every
        comparison."""
        _library(monkeypatch, tmp_path, _RED_BLOOD_CELL)
        base = "An educational diagram of a ciliated cell with cilia"
        tail = " Name the layer groups exactly: cilia, nucleus, membrane."
        _, plain, _ = vl.best_match("ciliated_cell", base)
        _, with_tail, _ = vl.best_match("ciliated_cell", base + tail)
        assert abs(plain - with_tail) < 1e-9, "the tail changed the score"
        assert vl.strip_layer_tail("A cell." + tail).strip() == "A cell."

    def test_the_decision_log_says_whether_the_guard_passed(self):
        import inspect
        import shared.visual_library_integration as vli
        src = inspect.getsource(vli._patch)
        assert '"key_guard_passed"' in src


class TestGenuineReuseStillWorks:
    def test_a_synonym_of_the_same_concept_still_clears(
            self, tmp_path, monkeypatch):
        _library(monkeypatch, tmp_path, {
            "asset_key": "heart_anatomical", "canonical_key": "anatomical_heart",
            "description": ("An educational diagram of the human heart showing "
                            "its four chambers and the flow of blood"),
            "subject": "biology", "grade": "k12", "curriculum": "generic",
            "topic": "human heart", "concepts": ["heart"],
            "status": "approved", "asset_type": "visual",
            "local_cache_path": "/tmp/h.png"})
        hit = vl.find("human_heart_diagram",
                      "The human heart with its four chambers and the flow "
                      "of blood")
        assert hit is not None
        assert hit["asset_key"] == "heart_anatomical"
        assert hit["match_score"] >= vl.threshold_now()
        # and it would survive the 1.25 stopgap threshold too
        assert hit["match_score"] >= 1.25

    def test_a_reworded_request_still_finds_its_asset(
            self, tmp_path, monkeypatch):
        _library(monkeypatch, tmp_path, {
            "asset_key": "volcano_cross_section",
            "canonical_key": "cross_section_volcano",
            "description": ("A cross-section of a volcano showing the magma "
                            "chamber, the central vent and the cone"),
            "subject": "geography", "grade": "k12", "curriculum": "generic",
            "topic": "volcano cross section", "concepts": [],
            "status": "approved", "asset_type": "visual",
            "local_cache_path": "/tmp/v.png"})
        assert vl.find("erupting_volcano_diagram",
                       "A volcano cut in half showing the magma chamber, "
                       "central vent and cone") is not None

    def test_a_sketch_still_answers_a_request_for_that_object(
            self, tmp_path, monkeypatch):
        """Stripping the 'sk' namespace must not break real sketch reuse."""
        _library(monkeypatch, tmp_path, {
            "asset_key": "sk_bacteria", "canonical_key": "bacteria_sk",
            "description": "A simple whiteboard sketch of bacteria",
            "subject": "biology", "grade": "k12", "curriculum": "generic",
            "topic": "bacteria", "concepts": [], "status": "approved",
            "asset_type": "visual", "local_cache_path": "/tmp/b.png"})
        assert vl.key_guard_ok("sk_bacteria", {"asset_key": "sk_bacteria",
                                               "canonical_key": "bacteria_sk"})
        assert vl.find("sk_bacteria",
                       "A simple whiteboard sketch of bacteria") is not None

    def test_an_exact_key_is_always_a_candidate(self):
        for key in ("ciliated_cell", "sk_boat", "plant_cell", "neurone"):
            assert vl.key_guard_ok(key, {"asset_key": key,
                                         "canonical_key": vl.canonical_key(key)})

    def test_the_guard_needs_a_row_with_a_key(self):
        assert vl.key_guard_ok("plant_cell", None) is False
        assert vl.key_guard_ok("plant_cell", {"description": "a plant"}) is False


class TestAnAllNoiseKeyIsNotAutomaticallyRefused:
    """`core_tokens` falls back to keeping the noise words for a key that has
    nothing else ("cell_diagram", "cells"), so the query kept "cell"/"diagram"
    while EVERY candidate row had them stripped. The intersection was therefore
    empty by construction and the guard refused matches the library serves
    correctly today — turning a hit into a paid regeneration and fresh 429
    exposure, which is the opposite of what the guard is for."""

    _ANIMAL_CELL = {
        "asset_key": "animal_cell_diagram", "canonical_key": "animal",
        "description": ("An educational diagram of an animal cell showing the "
                        "nucleus, cytoplasm and cell membrane."),
        "subject": "biology", "grade": "k12", "curriculum": "generic",
        "topic": "animal cell", "concepts": ["cell"], "status": "approved",
        "asset_type": "visual", "local_cache_path": "/tmp/ac.png",
    }

    def test_the_query_that_could_never_be_satisfied(self):
        from shared.asset_keys import all_noise, core_tokens
        assert all_noise("cell_diagram"), "nothing here says WHICH picture"
        assert core_tokens("cell_diagram") == {"cell", "diagram"}
        assert vl.guard_tokens("animal_cell_diagram") == {"animal"}
        assert core_tokens("cell_diagram") & vl.guard_tokens(
            "animal_cell_diagram") == set(), \
            "empty by construction: the guard could NEVER pass"

    def test_the_guard_abstains_instead_of_refusing(self):
        assert vl.key_guard_ok("cell_diagram", self._ANIMAL_CELL) is True

    def test_and_the_library_serves_it_again(self, tmp_path, monkeypatch):
        _library(monkeypatch, tmp_path, self._ANIMAL_CELL)
        assert vl.find("cell_diagram",
                       "An educational diagram of an animal cell showing the "
                       "nucleus, cytoplasm and cell membrane") is not None

    def test_an_all_noise_ROW_is_abstained_on_too(self):
        """The fallback is symmetric: a row filed under a key that says
        nothing has nothing to assert about the request either — and it is the
        ROW that kept the noise words this time."""
        row = {"asset_key": "cell_diagram", "canonical_key": "cell_diagram"}
        assert vl.guard_tokens("plant_cell") & vl.guard_tokens(
            "cell_diagram") == set(), "the same empty-by-construction refusal"
        assert vl.key_guard_ok("plant_cell", row) is True

    def test_abstaining_is_not_matching_everything(self):
        """It hands the decision to the score; it does not answer yes."""
        assert vl.key_guard_ok("cell_diagram", _SK_ANT) is False
        assert vl.key_guard_ok("cell_diagram",
                               {"asset_key": "", "canonical_key": ""}) is False

    def test_a_distinguished_key_is_still_guarded_hard(self):
        """The abstention must not leak into the case the guard exists for."""
        assert vl.key_guard_ok("ciliated_cell", _RED_BLOOD_CELL) is False
        assert vl.key_guard_ok("neurone", _RED_BLOOD_CELL) is False
        assert vl.key_guard_ok("sk_boat", _SK_ANT) is False


class TestAConnectiveIsNotASubject:
    """`cells_to_tissue` and `tissues_to_organs_diagram` shared exactly one
    guard token — "to" — so the whole levels-of-organisation family could serve
    one another's diagrams, which is the wrong-picture class the guard exists
    to close."""

    _CELLS_TO_TISSUE = {
        "asset_key": "cells_to_tissue", "canonical_key": "tissue_to",
        "description": ("An educational diagram showing how many similar cells "
                        "group together to form a tissue."),
        "subject": "biology", "grade": "k12", "curriculum": "generic",
        "topic": "levels of organisation", "concepts": ["tissue"],
        "status": "approved", "asset_type": "visual",
        "local_cache_path": "/tmp/ctt.png",
    }

    def test_a_bare_connective_cannot_satisfy_the_guard(self):
        assert vl.key_guard_ok("tissues_to_organs_diagram",
                               self._CELLS_TO_TISSUE) is False

    def test_the_connective_is_gone_from_the_guard_tokens(self):
        assert vl.guard_tokens("cells_to_tissue") == {"tissue"}
        assert vl.guard_tokens("tissues_to_organs_diagram") == {"tissues",
                                                               "organs"}
        for word in ("to", "for", "in", "on", "with", "from", "into", "by",
                     "at"):
            assert vl.guard_tokens(f"alpha_{word}_beta") == {"alpha", "beta"}, \
                word

    def test_the_wrong_diagram_is_not_served(self, tmp_path, monkeypatch):
        _library(monkeypatch, tmp_path, self._CELLS_TO_TISSUE)
        assert vl.find("tissues_to_organs_diagram",
                       "An educational diagram showing how tissues group "
                       "together to form an organ", min_score=0.0) is None

    def test_the_right_one_still_is(self, tmp_path, monkeypatch):
        _library(monkeypatch, tmp_path, self._CELLS_TO_TISSUE)
        assert vl.find("cells_to_tissue",
                       "An educational diagram showing how many similar cells "
                       "group together to form a tissue") is not None

    def test_the_connective_still_separates_two_CACHE_entries(self):
        """Subtracted in the guard, NOT added to KEY_NOISE: folding "to" away
        would file `cells_to_tissue` and a plain `tissue` in one cache
        directory — two different pictures in one file."""
        assert vl.canonical_key("cells_to_tissue") != vl.canonical_key("tissue")
        assert "to" in vl.canonical_key("cells_to_tissue").split("_")
